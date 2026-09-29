"""
========================================
tools/_subjects.py — normalising subjects
========================================

Tags come in three kinds, and subjects are **the new third kind**:

| | what it is | guarantee | who writes it |
|---|---|---|---|
| scene anchors `tags` | what is in there (a bed, a city) | 🔴 the literal string is always in the body | deepseek |
| broadenings `aliases` | near-synonyms absent from the body; feeds BM25 only, never the vector | none | deepseek |
| 🆕 subjects `subjects` | who | goes through the alias table (two names for one person collapse into one) | deepseek extracts; we maintain the alias table |

🔴 **It has to be its own third kind and must not be mixed in**:
  · mixed into `tags` -> breaks "the literal string is always in the body" (the
    body may carry only a pronoun while the subject is a name)
  · mixed into `aliases` -> it would enter BM25 scoring, and **who a memory is
    about must not affect relevance**

📌 Extraction is deepseek's job precisely so that writing a memory does not get
   heavier. All we maintain is the alias table.

📌 One thing gained for free: once subjects became tags, the real gap found
   earlier (`SELF/WORLD` pressing "did I live it or hear about it" and "who is it
   about" onto a single axis) **disappeared by itself** — the rooms could safely
   be cut down to four, and a third party no longer has to be forced into the
   WORLD branch.

Alias table: `buckets/_app/config/别名表.yaml` (hand-maintained; never written
by a model).

Exports: normalize_subjects(names) -> list[str] · canonical(name) -> str
         load_alias_table() -> dict[str, str]
========================================
"""


import os
import threading

import yaml

# Where the alias table lives: under config/, alongside src/.
# Keeping it out of config.yaml is deliberate — that file holds engine parameters
# (models, timeouts), this one holds **people's names**. The two change at
# different rates and by different hands, and mixing them means one overwrites
# the other sooner or later (that is exactly how an earlier pit got dug).
# This file sits at <_app>/src/tools/_subjects.py and the table at <_app>/config/,
# so it takes three levels up (tools -> src -> _app), not two.
# ⚠️ Getting that off by one level fails **silently**: the table cannot be read ->
#    an empty table is returned -> subjects still land on disk, just un-normalised,
#    and nothing on screen looks wrong. That is why the self-check below prints
#    the table itself.
_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config")
_ALIAS_FILENAME = "aliases.yaml"
# The old (Chinese) filename is still recognised, so that an existing install
# does not suddenly lose its table on upgrade — losing it is **silent** (an empty
# table is returned, subjects still land, just un-normalised), which is why this
# compatibility line has to stay.
_ALIAS_FILENAME_OLD = "别名表.yaml"


def _alias_path() -> str:
    """Where the alias table is: **data volume first, old layout as fallback**.

    🔴 Changed on the grounds that this table is data, not code:
      it holds **the names of living people**. Once the code is baked into an
      image, leaving the table at `<repo_root>/config/` causes two things:
      ① it is not in the image -> the table cannot be read -> **silent
      non-normalisation** (exactly the silence warned about at the top of this
      file); ② worse, actually baking it into the image means **publishing
      people's names**.
      So the order is: environment variable -> data volume -> old layout
      (`<repo_root>/config/`, for existing deployments).
      With none of them present it returns the data-volume path, so that even the
      error message points at where the file ought to go.
    """
    env = os.environ.get("LOCI_ALIAS_TABLE", "").strip()
    if env:
        return env
    buckets = (os.environ.get("LOCI_BUCKETS_DIR", "").strip()
               or os.path.join(os.path.dirname(_CONFIG_DIR), "buckets"))
    candidates = [
        os.path.join(buckets, _ALIAS_FILENAME),           # new: data volume + English name
        os.path.join(buckets, _ALIAS_FILENAME_OLD),       # compat: data volume + old Chinese name
        os.path.join(_CONFIG_DIR, _ALIAS_FILENAME),       # compat: old layout
        os.path.join(_CONFIG_DIR, _ALIAS_FILENAME_OLD),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0]        # none found: point the error message at where it belongs


_ALIAS_PATH = _alias_path()

# The "this is not a person" blocklist lives under a special key in the same table.
# Why not a separate file: everything about names stays in one file, so editing it
# never requires remembering where the other one is.
# Why it has to exist — live evidence: a memory was stored saying "<name> is noise
# the model extracted wrongly", the tagging model read those characters in the
# body and **extracted them as a person's name again**, taking that name from one
# entry to two.
# The act of calling something noise was itself producing noise. Deleting alone
# therefore does not work; there has to be a gate that stops it being extracted
# again.
_NOT_PERSON_KEY = "__不是人__"

_lock = threading.RLock()
_cache: dict | None = None
_cache_blocked: frozenset = frozenset()
_cache_mtime: float = -1.0


def load_alias_table() -> dict[str, str]:
    """Read the alias table, returning {lowercased alias: canonical name}. A
    missing or broken file -> an empty table (never raises; subjects still land).

    ⚠️ The correct behaviour for a broken table is **not normalising**, not
    refusing to store: the extracted subject is still real, it just does not get
    collapsed with its other spelling this time. Losing normalisation is a
    smaller loss than losing the subject.
    Cached on mtime — this table changes every few months, but backfill reads it
    for every single entry.
    """
    global _cache, _cache_blocked, _cache_mtime
    with _lock:
        path = _alias_path()          # recomputed each time, so a table dropped into the volume later is still found
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            _cache, _cache_blocked, _cache_mtime = {}, frozenset(), -1.0
            return {}
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        table: dict[str, str] = {}
        blocked: set[str] = set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if isinstance(raw, dict):
                for canon, aliases in raw.items():
                    canon = str(canon).strip()
                    if not canon:
                        continue
                    # Special key: the names under it are "not people", not
                    # aliases of some person.
                    # It must never fall into table — doing so would normalise
                    # them all into a single person named 「__不是人__」, which is
                    # worse than doing nothing.
                    if canon == _NOT_PERSON_KEY:
                        for a in (aliases or []):
                            a = str(a).strip()
                            if a:
                                blocked.add(a.lower())
                        continue
                    table[canon.lower()] = canon      # a canonical name maps to itself too
                    for a in (aliases or []):
                        a = str(a).strip()
                        if a:
                            table[a.lower()] = canon
        except Exception:
            table, blocked = {}, set()
        _cache, _cache_blocked, _cache_mtime = table, frozenset(blocked), mtime
        return table


def load_not_person() -> frozenset:
    """The blocklist: names marked by hand as "not a person" (lowercased).

    Same file as the alias table, same read, same mtime cache — the two can never
    disagree with each other.
    """
    load_alias_table()                # also fills _cache_blocked (or uses the cache)
    return _cache_blocked


# A pronoun is never a subject: names only, pronouns dropped entirely.
# Why not use the alias table to map a pronoun onto a person: in this library a
# given pronoun **almost** always refers to the same person — and "almost" is
# precisely the kind of word the alias table exists to guard against. A pronoun
# refers, it does not name, and resolving reference is either certain (which is
# not achievable here) or not attempted at all.
# The consequence, stated plainly: an entry whose body carries only a pronoun and
# no name gets empty subjects. Existing data is unaffected (the migration seeded
# from the old rooms), and coverage can be re-examined once retrieval is wired
# up — this is a decision at the prompt-and-filter level, reversible at any time.
# ⚠️ This layer is a **deterministic gate**; it does not rely on the prompt
#    behaving (the prompt was changed too, and the two stop different things).
_PRONOUNS = frozenset(
    "我 你 他 她 它 咱 咱们 我们 你们 他们 她们 它们 自己 大家 别人 人家 对方 谁".split()
)


def is_pronoun(name) -> bool:
    return str(name or "").strip() in _PRONOUNS


def canonical(name) -> str:
    """Normalise one subject name to its canonical form. An empty string means
    this name is dropped.

    Three cases return empty: empty input · a pronoun · a name marked by hand as
    "not a person" (the blocklist).
    A name absent from the table is returned as-is (whitespace stripped) — someone
    appearing for the first time still lands, merely un-normalised.
    """
    n = str(name or "").strip()
    if not n or n in _PRONOUNS:
        return ""
    table = load_alias_table()        # read first, or _cache_blocked is stale
    if n.lower() in _cache_blocked:
        return ""
    return table.get(n.lower(), n)


def normalize_subjects(names) -> list[str]:
    """Normalise a set of subject names, deduplicating while preserving order.
    Not a list, or empty -> an empty list.

    Order is preserved rather than sorted: the order deepseek extracts roughly
    matches the order of appearance in the body, and "who appears first" carries
    information (the first one is usually this memory's protagonist).
    """
    if not names:
        return []
    if isinstance(names, str):
        names = [s for s in names.split(",")]
    if not isinstance(names, (list, tuple, set)):
        return []
    out: list[str] = []
    for n in names:
        c = canonical(n)
        if c and c not in out:
            out.append(c)
    return out

_SELF_WORDS = frozenset({"我", "自己", "I", "me"})
_OTHER_WORDS = frozenset({"你", "对方", "you"})


def normalize_bound(names) -> tuple[list[str], str]:
    """Who is bound by something wanted (a telic entry). Returns (names, error).

    Written like `subjects` and through the same alias table, with one difference:
    the pronouns that name a side cannot simply be dropped, because "[我]" (I owe it)
    and "[]" (just a wish) mean different things. "我" / "自己" become the AI's name,
    "你" / "对方" the owner's; any other pronoun is refused, since a third-person
    pronoun could point at anyone.
    """
    from utils import get_ai_name, get_owner_name
    if not names:
        return [], ""
    if isinstance(names, str):
        names = names.split(",")
    if not isinstance(names, (list, tuple, set)):
        return [], "bound 要是名字列表，例如 [\"我\"]、[\"我\", \"对方的名字\"]。"
    out: list[str] = []
    for raw in names:
        n = str(raw or "").strip()
        if not n:
            continue
        if n in _SELF_WORDS:
            n = get_ai_name()
        elif n in _OTHER_WORDS:
            owner = get_owner_name()
            if not owner:
                return [], (f"bound 里的「{n}」认不出是谁（没设 LOCI_OWNER_NAME），"
                            "写对方的名字。")
            n = owner
        elif n in _PRONOUNS:
            return [], f"bound 里写名字，不写「{n}」——它可能指任何人。"
        c = canonical(n) or n
        if c not in out:
            out.append(c)
    return out, ""


# ============================================================
# Writing into the table — **only ever reached because a person clicked**
# ============================================================
# The same rule as muse/fold: the system's job is to lay things out; which one
# changes is decided by a human click.
# So neither of these two write paths has any automatic trigger; both hang off
# buttons on the panel's "who is in here" screen.
#
# 🔴 Why **text insertion** rather than rewriting the whole file with
#    yaml.safe_dump:
#    this table is hand-written line by line, and the long comment at its top
#    explains why it is a gate rather than a convention.
#    safe_dump would flush every comment away — trading a single click for the
#    reasoning somebody wrote down.
#    Insertion only adds lines and touches not one other byte.

_NL = chr(10)
_CR = chr(13)
_TAB = chr(9)
_QUOTES = '"' + chr(39)

_NEW_TABLE_HEADER = _NL.join([
    "# " + "=" * 58,
    "# 别名表 —— 主体（subjects）归一用。手工维护，不给模型写。",
    "# " + "=" * 58,
    "# 规范名（key）= 落进 frontmatter 的那个词；别名（value）= 正文里的各种写法。",
    "# 特殊键 __不是人__ 底下那些不是别名，是「这几个词根本不是人」的黑名单。",
    "# 形状见 config/aliases.example.yaml。",
    "",
])


def _yaml_scalar(v: str) -> str:
    """What a name should look like written into YAML. Whether to quote is left
    to yaml itself — with all-digit names like "77", or names containing a colon
    or a hash, hand-written quoting eventually misses one."""
    out = yaml.safe_dump(str(v), allow_unicode=True,
                         default_flow_style=True).strip()
    if out.endswith("..."):          # safe_dump appends a document-end marker to a scalar
        out = out[:-3].strip()
    return out


def _is_indented(line: str) -> bool:
    return line.startswith(" ") or line.startswith(_TAB)


def _list_items_under(lines: list, i: int) -> tuple:
    """Walk down from line i, where the key sits, and return (the line number of
    the last list item, the raw text of those items).

    Insertion goes after **the last list item** rather than at the end of the
    block: a block may contain comments, and inserting below one would make that
    comment look like it was talking about the new name.
    """
    last, items = i, []
    j = i + 1
    while j < len(lines):
        ln = lines[j]
        if ln.strip() == "":
            j += 1
            continue
        if not _is_indented(ln):
            break                    # the next unindented key: this block ends here
        body = ln.lstrip()
        if body.startswith("- "):
            last = j
            items.append(body[2:].strip().strip(_QUOTES))
        j += 1
    return last, items


def _insert_under(text: str, key: str, value: str, comment: str = "") -> tuple:
    """Insert value at the end of key's list. Returns (the new full text, whether
    anything changed). If the key does not exist, a new block is created.

    comment is written above the block **only when that block is created** — an
    existing block is left alone, so that adding a name does not accumulate
    another copy of the same sentence each time.
    """
    lines = text.splitlines()
    ki = None
    for idx, ln in enumerate(lines):
        if _is_indented(ln) or ln.lstrip().startswith("#") or ":" not in ln:
            continue
        if ln.split(":", 1)[0].strip().strip(_QUOTES) == key:
            ki = idx
            break
    if ki is None:
        block = []
        if comment:
            block += ["# " + ln for ln in comment.split(_NL)]
        block += [_yaml_scalar(key) + ":", "  - " + _yaml_scalar(value), ""]
        return text.rstrip(_NL) + _NL + _NL + _NL.join(block), True
    last, items = _list_items_under(lines, ki)
    low = [x.lower() for x in items]
    if value in items or value.lower() in low:
        return text, False           # already in there; do not write it twice
    lines.insert(last + 1, "  - " + _yaml_scalar(value))
    return _NL.join(lines) + _NL, True


def _read_table_text() -> str:
    path = _alias_path()
    if not os.path.isfile(path):
        return _NEW_TABLE_HEADER
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_table(text: str) -> None:
    """Writing to disk: encode to bytes first -> write a .tmp -> os.replace for an
    atomic swap.

    🔴 Paid for in blood: open(p, "w") **truncates before it writes**, so if the
       encoding step throws, the file is zeroed in place with the new content
       never having arrived (that is how web/loci.py was once lost — it had to be
       recovered out of a container image).
       Encoding first means any failure happens in memory and the original file
       is never touched at all.
    """
    global _cache, _cache_mtime
    path = _alias_path()
    data = text.encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    with _lock:
        # Invalidate the cache actively: never bet on mtime's second-level
        # resolution (two edits within the same second would be invisible)
        _cache, _cache_mtime = None, -1.0


def _check_name(v, what: str) -> str:
    v = str(v or "").strip()
    if not v:
        raise ValueError(what + "不能是空的")
    if len(v) > 40:
        raise ValueError(what + "太长了（超过 40 个字，多半是把正文粘进来了）")
    if _NL in v or _CR in v:
        raise ValueError(what + "里不能有换行")
    return v


def mark_not_person(name) -> bool:
    """"This is not a person": record it in the blocklist so it stops being
    extracted. The historical entries are **left byte for byte untouched**.

    The rule: editing historical metadata means writing into someone's memories,
    while "this is how the model extracted it at the time" is itself a fact. The
    blocklist already achieves the goal (it stops surfacing and stops being
    extracted), and it is reversible at any moment — delete that line from the
    table and the name is back.
    """
    name = _check_name(name, "名字")
    # This comment is written **only the first time** the block is created. The
    # panel screen says nothing at all about a marked-out name (hidden means
    # hidden; listing it again would mean it was never hidden), so "how to undo
    # this" has to be written here — anyone undoing it has to open this file
    # anyway.
    why = _NL.join([
        "下面这些不是别名，是「这几个词根本不是人」——",
        "面板「人名表」那一屏上点「这不是人」写进来的。",
        "它们不再摆出来、以后也不再抽；历史那些条一个字节都没动。",
        "想反悔：把对应那一行删掉就回来了。",
    ])
    new, changed = _insert_under(_read_table_text(), _NOT_PERSON_KEY, name,
                                 comment=why)
    if changed:
        _write_table(new)
    return changed


def add_alias(canon, alias) -> bool:
    """"These two are the same person" / "give them a formal name": alias is
    filed under canon.

    ⚠️ It only governs **what comes next**: existing entries keep the old name on
       disk (there is no migration script for this table, which is what the note
       in its header about deciding who rewrites the stored data refers to). The
       panel screen collapses them for display using the table, so one row
       disappears the moment you click — but on disk they are still two separate
       words, and recall on the old name still finds them.
    """
    canon = _check_name(canon, "规范名")
    alias = _check_name(alias, "别名")
    if _NOT_PERSON_KEY in (canon, alias):
        raise ValueError("__不是人__ 是特殊键，不能当名字")
    if is_pronoun(canon) or is_pronoun(alias):
        raise ValueError("代词不进这张表——指代不是名字")
    if canon.lower() == alias.lower():
        raise ValueError("这两个是同一个词")
    new, changed = _insert_under(_read_table_text(), canon, alias)
    if changed:
        _write_table(new)
    return changed
