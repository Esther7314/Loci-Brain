"""
========================================
core/names.py — the names table: normalising subjects
========================================

It lives in core/ because both layers read it: the tools normalise what a write
brings, and the engine pieces (the summariser's prompt, cue cards, look-back, the
profile line, the export package) read the same table.

Tags come in three kinds, and subjects are **the new third kind**:

| | what it is | guarantee | who writes it |
|---|---|---|---|
| scene anchors `tags` | what is in there (a bed, a city) | 🔴 the literal string is always in the body | deepseek |
| broadenings `aliases` | near-synonyms absent from the body; feeds BM25 only, never the vector | none | deepseek |
| 🆕 subjects `subjects` | who or what: people, and things (a game, a book, a group) | goes through the names table (two names for one person collapse into one) | deepseek extracts; we maintain the names table |

🔴 **It has to be its own third kind and must not be mixed in**:
  · mixed into `tags` -> breaks "the literal string is always in the body" (the
    body may carry only a pronoun while the subject is a name)
  · mixed into `aliases` -> it would enter BM25 scoring, and **who a memory is
    about must not affect relevance**

📌 Extraction is deepseek's job precisely so that writing a memory does not get
   heavier. All we maintain is the alias table.

📌 One thing gained for free: with subjects as tags, `SELF/WORLD` does not have
   to press "did I live it or hear about it" and "who is it about" onto a single
   axis — four rooms are enough, and a third party does not have to be forced into
   the WORLD branch.

The names table: `aliases.yaml` in the data volume (hand-maintained; the backfill
never writes it: what the side model says a name is waits as a guess in 待认 until the
owner confirms it — tools/grow/_backfill._record_kinds). Whether a name is a person or a game is
the table's call, not the field's: subjects holds names, and the table says what each
one is.

One entry per canonical name, every key optional:

    Connor:
      aliases: [RK800]           # the other spellings, collapsed into the key
      instance_of: 人            # what it is: 人 / 游戏 / 书 / 群 / ...
      present_in: [Detroit]      # where it appears: a character -> its work
      member_of: []              #                   a member -> its group

· A bare list is the older spelling of `{aliases: [...]}`, and a bare key is a name
  with nothing hung on it yet (a work or a group often has no other spelling). Tables
  written before kinds existed read unchanged.
· The special key `__不是人__` lists names that are "not a person". For a name with
  no instance_of of its own that is all the table knows about it; an explicit
  instance_of wins over it.
· A kind is a column, never an entry: 游戏 gets no card, Detroit does.
· Keys are unique, and two same-named things are two keys (`Leon`, `Leon (Detroit)`)
  that may share a spelling. A key always stands for itself; an alias two entries
  share is collapsed into neither (candidates() lists who claims it): which one is
  meant is decided from where it showed up, never from the name alone.

Exports: normalize_subjects(names) -> list[str] · canonical(name) -> str
         load_alias_table() -> dict[str, str] · load_not_person() -> frozenset
         load_names_table() -> dict[str, NameRecord] · record_of(name) · candidates(name)
         kind_of(name) -> str · is_person(name) -> bool | None · name_key(name) -> str
         add_alias · merge_names · mark_not_person · set_kind · link_name   (the panel's writers)
========================================
"""


import os
import threading
from dataclasses import dataclass

import yaml

from utils import replace_file

# Where the alias table lives: under config/, alongside src/.
# Keeping it out of config.yaml is deliberate — that file holds engine parameters
# (models, timeouts), this one holds **people's names**. The two change at
# different rates and by different hands, and mixing them means one overwrites
# the other sooner or later (that is exactly how an earlier pit got dug).
# This file sits at <_app>/src/core/names.py and the table at <_app>/config/,
# so it takes three levels up (core -> src -> _app), not two.
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

# What a person is, in the instance_of column. Every other kind is a thing.
KIND_PERSON = "人"
# Where a name appears: a character -> the work it is in, a member -> the group.
LINK_RELS = ("present_in", "member_of")


@dataclass(frozen=True)
class NameRecord:
    """One entry of the names table. `instance_of` is "" when the table does not say;
    `not_person` is the older `__不是人__` mark, set only on an entry with no
    instance_of of its own."""
    name: str
    aliases: tuple = ()
    instance_of: str = ""
    present_in: tuple = ()
    member_of: tuple = ()
    not_person: bool = False


_lock = threading.RLock()
_cache: dict | None = None
_cache_blocked: frozenset = frozenset()
_cache_records: dict = {}
_cache_mtime: float = -1.0


def _names_in(value) -> list[str]:
    """A YAML value read as a list of names: a list, one scalar, or nothing. Unquoted
    digits ("77") come back from YAML as numbers and are names all the same; nested
    mappings are not names and are skipped."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for x in items:
        if isinstance(x, (dict, list, tuple)) or x is None:
            continue
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _record(canon: str, value) -> NameRecord:
    """One entry in either spelling: a bare list (aliases only), a mapping, or nothing."""
    if isinstance(value, dict):
        kind = value.get("instance_of")
        return NameRecord(
            name=canon,
            aliases=tuple(_names_in(value.get("aliases"))),
            instance_of=("" if kind is None or isinstance(kind, (dict, list))
                         else str(kind).strip()),
            present_in=tuple(_names_in(value.get("present_in"))),
            member_of=tuple(_names_in(value.get("member_of"))))
    return NameRecord(name=canon, aliases=tuple(_names_in(value)))


def _parse(raw) -> tuple[dict, frozenset, dict]:
    """The parsed YAML -> (flat {spelling: canonical}, blocked spellings, records)."""
    records: dict[str, NameRecord] = {}
    listed_not_person: set[str] = set()
    if not isinstance(raw, dict):
        return {}, frozenset(), {}
    for canon, value in raw.items():
        canon = str(canon).strip()
        if not canon:
            continue
        # Special key: the names under it are "not people", not aliases of some
        # person. It must never fall into the table — doing so would normalise them
        # all into a single person named 「__不是人__」, which is worse than nothing.
        if canon == _NOT_PERSON_KEY:
            listed_not_person.update(n.lower() for n in _names_in(value))
            continue
        records[canon] = _record(canon, value)
    # An explicit kind wins over the older blocklist; the blocklist speaks only for a
    # name the table says nothing else about.
    kinded = {c.lower() for c, r in records.items() if r.instance_of}
    blocked = frozenset(n for n in listed_not_person if n not in kinded)
    for c, r in records.items():
        if c.lower() in blocked:
            records[c] = NameRecord(r.name, r.aliases, r.instance_of, r.present_in,
                                    r.member_of, not_person=True)
    # Who each spelling stands for. A key stands for itself (a canonical name maps to
    # itself too), whatever other entries list among their aliases; an alias that two
    # entries share stands for neither, and is left as written.
    keys: dict[str, set] = {}
    shared: dict[str, set] = {}
    for c, r in records.items():
        keys.setdefault(c.lower(), set()).add(c)
        for a in r.aliases:
            shared.setdefault(a.lower(), set()).add(c)
    flat: dict[str, str] = {}
    for sp in {*keys, *shared}:
        owners = keys.get(sp) or shared.get(sp)
        if len(owners) == 1:
            flat[sp] = next(iter(owners))
    return flat, blocked, records


def _load() -> tuple[dict, frozenset, dict]:
    """Read and parse the table once per mtime. A missing or broken file -> empty
    (never raises; subjects still land).

    ⚠️ The correct behaviour for a broken table is **not normalising**, not
    refusing to store: the extracted subject is still real, it just does not get
    collapsed with its other spelling this time. Losing normalisation is a
    smaller loss than losing the subject.
    Cached on mtime — this table changes every few months, but backfill reads it
    for every single entry.
    """
    global _cache, _cache_blocked, _cache_records, _cache_mtime
    with _lock:
        path = _alias_path()          # recomputed each time, so a table dropped into the volume later is still found
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            _cache, _cache_blocked, _cache_records, _cache_mtime = {}, frozenset(), {}, -1.0
            return _cache, _cache_blocked, _cache_records
        if _cache is not None and mtime == _cache_mtime:
            return _cache, _cache_blocked, _cache_records
        try:
            with open(path, "r", encoding="utf-8") as f:
                flat, blocked, records = _parse(yaml.safe_load(f) or {})
        except Exception:
            flat, blocked, records = {}, frozenset(), {}
        _cache, _cache_blocked, _cache_records, _cache_mtime = flat, blocked, records, mtime
        return flat, blocked, records


def load_alias_table() -> dict[str, str]:
    """The flat view every normaliser reads: {lowercased spelling: canonical name}.
    Kinds and links are not in it; load_names_table() has them."""
    return _load()[0]


def load_not_person() -> frozenset:
    """The blocklist: names marked by hand as "not a person" (lowercased), less any
    that the table has since given a kind of their own.

    Same file as the alias table, same read, same mtime cache — the two can never
    disagree with each other.
    """
    return _load()[1]


def load_names_table() -> dict[str, NameRecord]:
    """The whole table: {canonical name: NameRecord}, in file order."""
    return _load()[2]


def candidates(name) -> list[str]:
    """Every entry that claims this spelling (as its key or as an alias). More than one
    means the name alone cannot say which is meant."""
    n = str(name or "").strip().lower()
    if not n:
        return []
    return [c for c, r in load_names_table().items()
            if n == c.lower() or n in {a.lower() for a in r.aliases}]


def record_of(name) -> NameRecord | None:
    """The entry a spelling stands for, or None when the table does not know it or
    more than one entry claims it."""
    n = str(name or "").strip()
    if not n:
        return None
    flat, _, records = _load()
    key = flat.get(n.lower())
    return records.get(key) if key else None


def kind_of(name) -> str:
    """What the table says this name is (instance_of), or "" when it does not say."""
    rec = record_of(name)
    return rec.instance_of if rec else ""


def is_person(name) -> bool | None:
    """True for a person, False for a thing, None when the table does not know.
    An explicit instance_of decides; without one, the `__不是人__` list still says
    "not a person". Nothing else is read as a kind: a name with only aliases is
    unknown, not presumed a person."""
    rec = record_of(name)
    if rec and rec.instance_of:
        return rec.instance_of == KIND_PERSON
    n = str(name or "").strip().lower()
    if (rec and rec.not_person) or (n and n in load_not_person()):
        return False
    return None


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
    table, blocked, _ = _load()
    if n.lower() in blocked:
        return ""
    return table.get(n.lower(), n)


def name_key(name) -> str:
    """The name a card is filed under (`card_of`): the table's key for this spelling,
    or the spelling itself when the table does not know it. "" for a pronoun or
    nothing.

    Unlike canonical(), a name on the `__不是人__` list is kept: that list stops a
    word being *extracted* as a subject, while a card is written on purpose, and
    "not a person" is exactly what a thing's card is about.
    """
    n = str(name or "").strip()
    if not n or n in _PRONOUNS:
        return ""
    return load_alias_table().get(n.lower(), n)


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
# Writing into the table — the panel's clicks, and one narrow automatic writer
# ============================================================
# The same rule as muse/fold: the system's job is to lay things out; which one
# changes is decided by a human click.
# These write paths hang off the panel's "who is in here" screen. Nothing automatic
# calls them: the backfill keeps what the side model says a name is as a guess
# (core/name_guesses, tools/grow/_backfill._record_kinds), and the owner's 「是 X」 on
# the names page is what writes it here.
#
# 🔴 Why **text edits** rather than rewriting the whole file with
#    yaml.safe_dump:
#    this table is hand-written line by line, and the long comment at its top
#    explains why it is a gate rather than a convention.
#    safe_dump would flush every comment away — trading a single click for the
#    reasoning somebody wrote down.
#    Each edit touches the lines of the one entry it is about: an inserted line, a
#    replaced `instance_of:` line, or a bare list re-indented under `aliases:` the
#    first time that entry gains a kind or a link. Every other byte stays.

_NL = chr(10)
_CR = chr(13)
_TAB = chr(9)
_QUOTES = '"' + chr(39)

_NEW_TABLE_HEADER = _NL.join([
    "# " + "=" * 58,
    "# 别名表 —— 主体（subjects）归一用。手工维护；回填只会添新名字和补空着的种类。",
    "# " + "=" * 58,
    "# 规范名（key）= 落进 frontmatter 的那个词；别名（value）= 正文里的各种写法。",
    "# 一个名字底下还可以写 instance_of（它是什么：人 / 游戏 / 书 / 群）、",
    "# present_in（出现在哪部作品里）、member_of（是哪个群的成员）。",
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


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _key_line(line: str) -> str | None:
    """The top-level key a line opens, or None for anything else."""
    if _is_indented(line) or line.lstrip().startswith(("#", "-")) or ":" not in line:
        return None
    return line.split(":", 1)[0].strip().strip(_QUOTES)


def _find_key(lines: list, key: str) -> int | None:
    for idx, ln in enumerate(lines):
        if _key_line(ln) == key:
            return idx
    return None


def _block_end(lines: list, ki: int) -> int:
    """One past the last line of key ki's block: everything indented below it (and
    a list written flush under it, `- a`), up to the next unindented line — a key,
    or the comment above one. Trailing blank lines stay outside the block."""
    j = ki + 1
    while j < len(lines) and (not lines[j].strip() or _is_indented(lines[j])
                              or lines[j].startswith("-")):
        j += 1
    while j > ki + 1 and not lines[j - 1].strip():
        j -= 1
    return j


def _last_content(lines: list, ki: int) -> int:
    """The block's last line that is not a comment (ki itself when there is none).
    New lines go below it rather than at the very end of the block: a block may end
    in a comment, and a line inserted below that comment would look like what it is
    talking about."""
    last = ki
    for j in range(ki + 1, _block_end(lines, ki)):
        if _is_content(lines[j]):
            last = j
    return last


def _inline_value(line: str) -> str:
    """What follows the colon on a key or field line ("" when its value is a block
    below it, or only a comment)."""
    rest = line.split(":", 1)[1].strip()
    return "" if rest.startswith("#") else rest


def _load_inline(line: str):
    try:
        return yaml.safe_load(_inline_value(line))
    except yaml.YAMLError as e:
        raise ValueError(f"表里这一行读不懂，先手改好再点：{line.strip()}（{e}）")


def _item_value(body: str) -> str:
    """The name a `- name` line holds, read the way YAML reads it (quotes, numbers,
    a trailing comment)."""
    raw = body.lstrip()[1:].strip()
    try:
        v = yaml.safe_load(raw)
    except yaml.YAMLError:
        v = raw.strip(_QUOTES)
    return "" if v is None else str(v).strip()


def _list_items(lines: list, ki: int) -> list:
    """The line numbers of the `- name` items in key ki's block."""
    return [j for j in range(ki + 1, _block_end(lines, ki))
            if _is_content(lines[j]) and lines[j].lstrip().startswith("-")]


def _render(rec: NameRecord, as_mapping: bool) -> list:
    """An entry's block lines, for the one case where there are no lines to edit: an
    entry written flow-style on its key line."""
    if not as_mapping:
        return ["  - " + _yaml_scalar(a) for a in rec.aliases]
    out: list[str] = []
    if rec.aliases:
        out += ["  aliases:"] + ["    - " + _yaml_scalar(a) for a in rec.aliases]
    if rec.instance_of:
        out.append("  instance_of: " + _yaml_scalar(rec.instance_of))
    for rel in LINK_RELS:
        targets = getattr(rec, rel)
        if targets:
            out += [f"  {rel}:"] + ["    - " + _yaml_scalar(t) for t in targets]
    return out


def _expand_inline(lines: list, ki: int) -> None:
    """Rewrite one flow-style entry (`name: [a, b]`, `name: {instance_of: 书}`) as a
    block, so the line edits below have lines to work on. Only that entry's own
    line changes."""
    if not _inline_value(lines[ki]):
        return
    parsed = _load_inline(lines[ki])
    head = lines[ki].split(":", 1)[0].rstrip()
    lines[ki] = head + ":"
    lines[ki + 1:ki + 1] = _render(_record(head, parsed), isinstance(parsed, dict))


def _is_mapping(lines: list, ki: int) -> bool:
    """Is key ki's (block) entry written as a mapping, rather than a bare list or
    nothing at all?"""
    for j in range(ki + 1, _block_end(lines, ki)):
        if _is_content(lines[j]):
            return not lines[j].lstrip().startswith("-")
    return False


def _as_mapping(lines: list, ki: int) -> int:
    """Turn key ki's entry into the mapping spelling in place, and return the indent
    its fields sit at. A bare list becomes its `aliases:` — the same lines, comments
    inside it included, two spaces further in."""
    _expand_inline(lines, ki)
    end = _block_end(lines, ki)
    if _is_mapping(lines, ki):
        return next(_indent(lines[j]) for j in range(ki + 1, end) if _is_content(lines[j]))
    if _list_items(lines, ki):
        for j in range(ki + 1, end):
            if lines[j].strip():
                lines[j] = "  " + lines[j]
        lines.insert(ki + 1, "  aliases:")
    return 2


def _fields(lines: list, ki: int, ind: int) -> dict:
    """{field: (its line, [its item lines])} inside a mapping entry whose fields sit
    at indent ind. Items may sit deeper or flush with the field (both are YAML)."""
    out: dict = {}
    cur = None
    for j in range(ki + 1, _block_end(lines, ki)):
        ln = lines[j]
        if not _is_content(ln):
            continue
        body = ln.lstrip()
        if _indent(ln) == ind and not body.startswith("-") and ":" in body:
            cur = body.split(":", 1)[0].strip().strip(_QUOTES)
            out[cur] = (j, [])
        elif cur is not None and body.startswith("-"):
            out[cur][1].append(j)
    return out


def _set_field(lines: list, ki: int, field: str, value: str) -> None:
    """Set a one-value field (instance_of) on key ki's entry."""
    ind = _as_mapping(lines, ki)
    line = " " * ind + f"{field}: {_yaml_scalar(value)}"
    fields = _fields(lines, ki, ind)
    if field not in fields:
        lines.insert(_last_content(lines, ki) + 1, line)
        return
    j, items = fields[field]
    current = _load_inline(lines[j])
    if not items and current is not None and str(current).strip() == value:
        return
    for i in reversed(items):
        del lines[i]
    lines[j] = line


def _add_to_list(lines: list, ki: int, value: str) -> None:
    """Append value to key ki's bare list, unless it is already in it (any case)."""
    items = _list_items(lines, ki)
    if value.lower() in {_item_value(lines[i]).lower() for i in items}:
        return
    pad = " " * _indent(lines[items[-1]]) if items else "  "
    lines.insert(items[-1] + 1 if items else ki + 1, pad + "- " + _yaml_scalar(value))


def _add_to_field(lines: list, ki: int, field: str, value: str) -> None:
    """Append value to a list field (aliases, present_in, member_of) of key ki's
    entry, unless it is already there (any case)."""
    ind = _as_mapping(lines, ki)
    pad = " " * (ind + 2)
    fields = _fields(lines, ki, ind)
    if field not in fields:
        at = _last_content(lines, ki) + 1
        lines[at:at] = [" " * ind + f"{field}:", pad + "- " + _yaml_scalar(value)]
        return
    j, items = fields[field]
    if _inline_value(lines[j]):
        existing = _names_in(_load_inline(lines[j]))
        if value.lower() in {x.lower() for x in existing}:
            return
        lines[j] = " " * ind + f"{field}:"
        lines[j + 1:j + 1] = [pad + "- " + _yaml_scalar(x) for x in existing + [value]]
        return
    if value.lower() in {_item_value(lines[i]).lower() for i in items}:
        return
    if items:
        pad = " " * _indent(lines[items[-1]])
    lines.insert(items[-1] + 1 if items else j + 1, pad + "- " + _yaml_scalar(value))


def _remove_from_list(lines: list, ki: int, value: str) -> None:
    """Take value out of key ki's bare list (any case). Comments stay."""
    _expand_inline(lines, ki)
    for j in _list_items(lines, ki):
        if _item_value(lines[j]).lower() == value.lower():
            del lines[j]
            return


def _remove_from_field(lines: list, ki: int, field: str, value: str) -> None:
    """Take value out of a list field of key ki's mapping entry (any case)."""
    ind = _as_mapping(lines, ki)
    found = _fields(lines, ki, ind).get(field)
    if not found:
        return
    j, items = found
    if _inline_value(lines[j]):
        kept = [x for x in _names_in(_load_inline(lines[j])) if x.lower() != value.lower()]
        lines[j] = " " * ind + f"{field}:"
        lines[j + 1:j + 1] = [" " * (ind + 2) + "- " + _yaml_scalar(x) for x in kept]
        return
    for i in items:
        if _item_value(lines[i]).lower() == value.lower():
            del lines[i]
            return


def _add_alias_line(lines: list, ki: int, value: str) -> None:
    """File value among key ki's aliases, whichever spelling the entry uses."""
    _expand_inline(lines, ki)
    if _is_mapping(lines, ki):
        _add_to_field(lines, ki, "aliases", value)
    else:
        _add_to_list(lines, ki, value)


def _drop_entry(lines: list, ki: int) -> None:
    """Remove key ki's line and its block. Comments above it are someone's words and
    stay; a blank line left doubled by the removal is folded into one."""
    del lines[ki:_block_end(lines, ki)]
    if 0 < ki < len(lines) and not lines[ki].strip() and not lines[ki - 1].strip():
        del lines[ki]


def _ensure_key(lines: list, key: str) -> int:
    """Key's line number, appending it as a bare name (`name:`) when it is not there."""
    ki = _find_key(lines, key)
    if ki is not None:
        return ki
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ["", _yaml_scalar(key) + ":"]
    return len(lines) - 1


def _insert_under(text: str, key: str, value: str, comment: str = "") -> tuple:
    """File value under key: into its aliases, whichever spelling the entry uses.
    Returns (the new full text, whether anything changed). If the key does not
    exist, a new block is created.

    comment is written above the block **only when that block is created** — an
    existing block is left alone, so that adding a name does not accumulate
    another copy of the same sentence each time.
    """
    lines = text.splitlines()
    ki = _find_key(lines, key)
    if ki is None:
        block = []
        if comment:
            block += ["# " + ln for ln in comment.split(_NL)]
        block += [_yaml_scalar(key) + ":", "  - " + _yaml_scalar(value), ""]
        return text.rstrip(_NL) + _NL + _NL + _NL.join(block), True
    before = list(lines)
    _add_alias_line(lines, ki, value)
    if lines == before:
        return text, False           # already in there; do not write it twice
    return _NL.join(lines) + _NL, True


def _read_table_text() -> str:
    """The table's text, for a writer to edit. Refused (ValueError) when it does not parse
    as a mapping: the writers edit lines by their indentation, and on a file the YAML
    reader cannot read they would replace an existing kind or file aliases as names of
    their own. Readers just see an empty table (_load); a writer must not touch it until a
    person has fixed the file."""
    path = _alias_path()
    if not os.path.isfile(path):
        return _NEW_TABLE_HEADER
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"人名表 {path} 现在读不懂（YAML 写坏了），先手动修好它再改：{e}") from e
    if parsed is not None and not isinstance(parsed, dict):
        raise ValueError(f"人名表 {path} 不是「名字: …」的样子，先手动修好它再改")
    return text


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
    replace_file(tmp, path)
    with _lock:
        # Invalidate the cache actively: never bet on mtime's second-level
        # resolution (two edits within the same second would be invisible)
        _cache, _cache_mtime = None, -1.0


def _edit_table(edit) -> bool:
    """Read the table's text, let `edit` change its lines in place, and write it back
    when anything changed. Held under the module lock, so two clicks never interleave
    a read and a write."""
    with _lock:
        lines = _read_table_text().splitlines()
        before = list(lines)
        edit(lines)
        if lines == before:
            return False
        _write_table(_NL.join(lines) + _NL)
        return True


def _check_name(v, what: str) -> str:
    v = str(v or "").strip()
    if not v:
        raise ValueError(what + "不能是空的")
    if len(v) > 40:
        raise ValueError(what + "太长了（超过 40 个字，多半是把正文粘进来了）")
    if _NL in v or _CR in v:
        raise ValueError(what + "里不能有换行")
    return v


def _refuse_special(*names: str) -> None:
    if _NOT_PERSON_KEY in names:
        raise ValueError("__不是人__ 是特殊键，不能当名字")
    if any(is_pronoun(n) for n in names):
        raise ValueError("代词不进这张表——指代不是名字")


def _table_key(name: str) -> str:
    """The key a writer edits for this spelling: the entry it stands for, or the
    spelling itself as a new key. An alias two entries share is refused rather than
    guessed at."""
    rec = record_of(name)
    if rec:
        return rec.name
    owners = candidates(name)
    if len(owners) > 1:
        raise ValueError(f"「{name}」在表里挂在好几个名字底下（{'、'.join(owners)}），"
                         "写其中一个的规范名")
    return name


def mark_not_person(name) -> bool:
    """"This is not a person": record it in the blocklist so it stops being
    extracted. The historical entries are **left byte for byte untouched**.

    The rule: editing historical metadata means writing into someone's memories,
    while "this is how the model extracted it at the time" is itself a fact. The
    blocklist already achieves the goal (it stops surfacing and stops being
    extracted), and it is reversible at any moment — delete that line from the
    table and the name is back.

    An entry that already has a kind is answered by its instance_of, never by a
    second mark in the older list: a thing's kind already says "not a person"
    (nothing to write), and an entry the table calls 人 is refused, since that is
    the kind to correct.
    """
    name = _check_name(name, "名字")
    rec = record_of(name)
    if rec and rec.instance_of:
        if rec.instance_of != KIND_PERSON:
            return False
        raise ValueError(f"表里写着「{rec.name}」是人（instance_of: 人）——"
                         "是写错了就改它的种类，别再记一遍「不是人」")
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
    with _lock:
        new, changed = _insert_under(_read_table_text(), _NOT_PERSON_KEY, name,
                                     comment=why)
        if changed:
            _write_table(new)
    return changed


def add_alias(canon, alias) -> bool:
    """"These two are the same person" / "give them a formal name": alias is
    filed under canon (or under the entry canon is itself an alias of).

    ⚠️ It only governs **what comes next**: existing entries keep the old name on
       disk (there is no migration script for this table, which is what the note
       in its header about deciding who rewrites the stored data refers to). The
       panel screen collapses them for display using the table, so one row
       disappears the moment you click — but on disk they are still two separate
       words, and recall on the old name still finds them.

    A spelling that is already another entry's own key, or another entry's alias, is
    refused: filing it here as well would leave it standing for two entries, which
    the table then collapses into neither.
    """
    canon = _check_name(canon, "规范名")
    alias = _check_name(alias, "别名")
    _refuse_special(canon, alias)
    if canon.lower() == alias.lower():
        raise ValueError("这两个是同一个词")
    key = _table_key(canon)
    owner = record_of(alias)
    if owner and owner.name != key:
        where = ("它自己就是表里的一个名字" if owner.name.lower() == alias.lower()
                 else f"它已经挂在「{owner.name}」底下了")
        raise ValueError(f"「{alias}」{where}；要把两个并成一个，用「跟谁是一个人」")
    with _lock:
        new, changed = _insert_under(_read_table_text(), key, alias)
        if changed:
            _write_table(new)
    return changed


def merge_names(name, into) -> bool:
    """"These two are one" / "give it a proper name": name's entry is folded into
    into's. Its own key becomes an alias of into, its aliases move across (those into
    already has are skipped), its present_in / member_of links move too, and its entry
    is removed from the file. Links other entries hang on name are re-hung on into, so
    nothing points at a key that is gone. into is created when the table does not have
    it (a rename to a new spelling); a name the table does not have is simply filed as
    an alias.

    Kinds: one kind between the two is kept; two different kinds are refused, saying
    which is which, since that is the table being wrong about one of them.

    ⚠️ Like add_alias, it only governs **what comes next**: entries on disk keep the
       spelling they were written with.
    """
    name = _check_name(name, "名字")
    into = _check_name(into, "并到的名字")
    _refuse_special(name, into)
    src = _table_key(name)
    dst = _table_key(into)
    if name.lower() == into.lower():
        raise ValueError("这两个是同一个词")
    if src.lower() == dst.lower():
        if into.lower() == dst.lower():
            return False                 # name is already filed under into
        raise ValueError(f"「{into}」本来就是「{src}」的别名——并到它的规范名「{src}」上，"
                         "或者干脆不用并")
    names = load_names_table()
    src_rec = names.get(src) or NameRecord(name=src)
    dst_rec = names.get(dst) or NameRecord(name=dst)
    if src_rec.instance_of and dst_rec.instance_of \
            and src_rec.instance_of != dst_rec.instance_of:
        raise ValueError(f"「{src}」在表里是{src_rec.instance_of}，「{dst}」是"
                         f"{dst_rec.instance_of}——先把写错的那个种类改对，再并")
    hung_on_src = [(c, rel) for c, r in names.items() if c not in (src, dst)
                   for rel in LINK_RELS if src.lower() in {t.lower() for t in getattr(r, rel)}]

    def edit(lines: list) -> None:
        ki = _find_key(lines, src)
        if ki is not None:
            _drop_entry(lines, ki)
        ki = _ensure_key(lines, dst)
        if src_rec.instance_of and not dst_rec.instance_of:
            _set_field(lines, ki, "instance_of", src_rec.instance_of)
        for spelling in (src, *src_rec.aliases):
            if spelling.lower() != dst.lower():
                _add_alias_line(lines, _find_key(lines, dst), spelling)
        for rel in LINK_RELS:
            for target in getattr(src_rec, rel):
                if target.lower() != dst.lower():
                    _add_to_field(lines, _find_key(lines, dst), rel, target)
        for other, rel in hung_on_src:
            oi = _find_key(lines, other)
            if oi is None:
                continue
            _remove_from_field(lines, oi, rel, src)
            _add_to_field(lines, _find_key(lines, other), rel, dst)
    return _edit_table(edit)


def set_kind(name, kind) -> bool:
    """Say what a name is: its instance_of (人 / 游戏 / 书 / 群 / ...). An alias
    spelling sets the kind of the entry it stands for; a name the table does not
    have is added. An explicit kind replaces the name's line in `__不是人__`, if it
    had one. Returns whether the file changed."""
    name = _check_name(name, "名字")
    kind = _check_name(kind, "种类")
    _refuse_special(name, kind)
    key = _table_key(name)

    def edit(lines: list) -> None:
        _set_field(lines, _ensure_key(lines, key), "instance_of", kind)
        bi = _find_key(lines, _NOT_PERSON_KEY)
        if bi is not None:
            _remove_from_list(lines, bi, key)
    return _edit_table(edit)


def link_name(name, rel, target) -> bool:
    """Say where a name appears: rel is present_in (a character -> the work it is
    in) or member_of (a member -> the group). A name may hang in several places. A
    target the table does not have is added as a bare name — a work or a group can
    exist with no other spelling. Returns whether the file changed."""
    rel = str(rel or "").strip()
    if rel not in LINK_RELS:
        raise ValueError("关系只有两种：present_in（出现在哪部作品里）/ "
                         "member_of（是哪个群的成员）")
    name = _check_name(name, "名字")
    target = _check_name(target, "挂到的名字")
    _refuse_special(name, target)
    key = _table_key(name)
    tkey = _table_key(target)
    if key.lower() == tkey.lower():
        raise ValueError("一个名字挂不到它自己底下")

    def edit(lines: list) -> None:
        _ensure_key(lines, tkey)
        _add_to_field(lines, _ensure_key(lines, key), rel, tkey)
    return _edit_table(edit)
