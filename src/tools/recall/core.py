# -*- coding: utf-8 -*-
"""
tools/recall/core.py — the main remembering logic: filter -> zoom -> render

All of it is statistics (grouping, proportions, averages, maxima); zero LLM
calls, nothing precomputed.
"the more there is, the easier it is to forget" is not a bug but the correct
behaviour of zooming: where there are many entries they collapse into the
dominant colour, and the odd one out keeps its name.
"""

import re
from collections import Counter
from datetime import datetime, timedelta

from core import _bigevent as _big    # a big event: one sentence laid over a stretch of time
from core import _fold as _F          # fold / gist: what is covered no longer surfaces on its own
from core import _sources as _src     # outside material: source records and their registry state
from core import _usage               # the usage log: what a lookup handed back
from core import visibility as _V     # the one gate: what may be put in front of the model
from .. import _runtime as rt
from .._common import read_scope, resolve_bucket_id
from core import _when as _w          # "today" as the user lives it (local timezone) — never call datetime.now() directly
from core._rooms import (ALL_ROOMS, check_gate, is_mind_room, normalize_room,
                      room_matches)
from core import profile as _P        # what counts as an open promise; days since written
from utils import (HAD_PRIMARY_SOURCE, WAS_DERIVED_FROM, WAS_QUOTED_FROM, WAS_REVISION_OF,
                   read_from_ids, read_prov)

# What each provenance line is, in the read by id's 「来源:」 block.
_PROV_WORD = {WAS_DERIVED_FROM: "派生", WAS_REVISION_OF: "新版本",
              WAS_QUOTED_FROM: "引原话", HAD_PRIMARY_SOURCE: "一手来源"}

# The registry's state of a source, as the 「来源:」 block says it; active says nothing.
_SOURCE_STATE_WORD = {_src.WITHDRAWN: "⚠️撤回", _src.DELETED: "⚠️删除",
                      _src.UNREADABLE: "⚠️宿主那边读不到"}


def _source_strings(meta: dict) -> list[str]:
    """The string forms of an entry's source records, each once; a hand-edited record
    that no longer reads as one is skipped."""
    out: list[str] = []
    for rec in meta.get(_src.SOURCES_FIELD) or []:
        try:
            text = _src.record_string(rec)
        except (KeyError, TypeError):
            continue
        if text not in out:
            out.append(text)
    return out


def _source_label(target: str) -> str:
    """A quoted source as the 「来源:」 block names it: its string form, and for a run of
    lines (`…#m_0012..m_0031`) how many lines it spans when the slice store still holds
    the batch it was cut from."""
    slices = getattr(rt.bucket_mgr, "slices", None)
    if slices is None or "#" not in target:
        return target
    try:
        sid, _revision = _src.SourceId.parse(target)
    except _src.SourceRecordError:
        return target
    count = slices.run_length(sid) if sid.through else None
    return f"{target}（{count} 行）" if isinstance(count, int) else target


def _source_mark(target: str) -> str:
    """The registry's word for a quoted source, and a note when the host has revised it
    since (the entry keeps pointing at the revision it was formed on). A bare host id
    has no identity to look up and gets nothing."""
    registry = getattr(rt.bucket_mgr, "sources", None)
    if registry is None or "#" not in target:
        return ""
    try:
        sid, revision = _src.SourceId.parse(target)
    except _src.SourceRecordError:
        return ""
    known = registry.describe(sid)
    state = registry.state_of(sid)          # a run: the worst of its lines
    if not known and state == _src.ACTIVE:
        return ""
    known = known or {"revisions": []}
    marks = [_SOURCE_STATE_WORD[state]] if state in _SOURCE_STATE_WORD else []
    latest = (known["revisions"] or [{}])[-1].get("revision")
    if latest and latest != revision:
        marks.append(f"（宿主那边已改到 @{latest}，这条是按 @{revision or '原版'} 记的）")
    return ("  " + " ".join(marks)) if marks else ""

# The zoom target: how many cells each call returns (12-20; at or below _LIST_MAX
# entries it does not zoom at all and simply lists them)
_CELL_MAX = 20
_LIST_MAX = 20
_HIGHLIGHT_MAX = 3

# ── Two paths: browsing and searching ────────────────────────
# There is exactly one criterion: **is there a query**. when/room/tag are a
# **range**; query is a **target**.
#   browsing (no query): I am looking, I cannot recall what is there -> **cut the
#     far end hard**, order by time, no scores
#   searching (with a query): I am looking for something, I know what -> **give
#     more at the far end** (cutting here means missing), order by relevance, and
#     the score is the point
# The fork has always existed in the code (no query gate means no score); what was
# missing is that the two paths produced identically shaped output.
_BROWSE_NEAR_DAYS = 3     # "within three days it still comes out the way recall does"
_BROWSE_REP_DAYS = 21     # "give 2-3 entries from 2-3 weeks back" — picking out anything older is pointless
_BROWSE_REP_MAX = 3

# How many entries the search path accepts at most (top-k). **Tightened to 30**:
# more words in a query = an averaged vector = a poorer aim — and with a large k,
# a poor aim buries the real hit under a screenful of near-misses.
# ⚠️ The number of entries cut **must be reported** (`_collect`'s third return
#    value -> a final line at render time): quietly dropping 30 is exactly the
#    "the slots were wasted and number 61 disappeared forever" failure that
#    review complained about.
_SEARCH_TOPK = 30

# The relevance floor: below it, most results are merely adjacent.
# It came out of a search for a term meaning "taking care of one's health" (in the
# sense of massage and herbal medicine) that dredged up a pile of entries about
# the body and intimacy: if there is no direct connection, better not to show
# anything and say there is no relevant memory.
# After scoring was cut to two dimensions (semantic 2.5 + bm25 1.5) the absolute
# values all shrank, so the line was measured again (six real queries):
#   queries with keyword/literal support: genuinely relevant 50-80, clean.
#   purely semantic short queries: genuinely relevant around 36, adjacent 31-33,
#   and the noise from the health query peaked at 34.2
#   —— a line at 35 separates the two exactly, but with a margin of only 1-2
#   points (cosine has a narrow dynamic range to begin with).
#   Better too few than too many: what falls below the line is reported in one
#   final line (how many, the highest score, the earliest entry) — visible, and
#   drillable.
# ⚠️ This is the **combined score** (0-100), not a cosine; the underlying
# _VECTOR_RECALL_THRESHOLD=0.65 is the **cosine line for the 意思 mark**, a
# different thing. Entries with a literal hit are floored at max(score, line) and
# are never blocked by this.
# The LOCI_RELEVANCE_FLOOR environment variable overrides it (the slider on the
# dashboard's memory page goes through a URL parameter).
try:
    RELEVANCE_FLOOR = float(__import__("os").environ.get("LOCI_RELEVANCE_FLOOR", "") or 35.0)
except (TypeError, ValueError):
    RELEVANCE_FLOOR = 35.0
# System tag prefixes: never allowed onto any tag line a human reads (「疑似同件:xxx」
# used to leak through)
_SYS_TAG_PREFIXES = ("__", "aspect:", "疑似同件:", "相似认知:")
# Machine-voiced tags are **filtered out and never shown**, a lesson caught on
# muse's first screen:
# a prefix table can only stop the kinds already seen; the **shape** `xx:yy` is
# the real criterion.
# What faces a reader deserves plain-language scene words — structured tags like
# `aspect:patterns` are residue left behind when the old system was retired.
# The criterion is the same one as `is_scene_word()` in tools/_muse.py; do not
# write a second version of it here.
_MACHINE_TAG_RE = re.compile(r"^[^:：]{1,12}[:：]")
# A machine tag whose value is another entry's handle.
_HANDLE_TAG_RE = re.compile(r"^[^:：]{1,12}[:：]([0-9a-f]{6,12})$")


def is_human_tag(tag: str) -> bool:
    """Whether this tag deserves to be shown. System prefixes and the machine-voiced
    `xx:yy` shape do not."""
    t = str(tag)
    return bool(t) and not t.startswith(_SYS_TAG_PREFIXES) and not _MACHINE_TAG_RE.match(t)

_SEED_RE = re.compile(r"\[\[([a-z_]+)\]\]")
# The ground tone recognises emotion seeds only (the English keys of the seven
# emotions and six desires); any other [[wikilink]] (a project name, a person's
# name, ...) is not a seed
_SEED_NAMES = frozenset({
    "joy", "anger", "sorrow", "fear", "love", "aversion", "desire",
    "lust", "sound", "scent", "taste", "touch", "dharma", "greed",
})

# The granularity ladder (in seconds). Density decides granularity: try from fine
# to coarse and take the first one whose non-empty cell count is <= _CELL_MAX
_LADDER = [
    ("小时", 3600),
    ("半天", 12 * 3600),
    ("天", 24 * 3600),
    ("周", 7 * 24 * 3600),
    ("半月", 15 * 24 * 3600),
    ("月", 30 * 24 * 3600),
    ("季", 91 * 24 * 3600),
    ("年", 365 * 24 * 3600),
]


# ============================================================
# Plain language is **the shell, not the core**
# ============================================================
# 🔴 **The body of a memory is never altered and never goes through a model.**
#    This section only handles **the structural phrasing** — machine readouts like
#    room proportions and V/A become a sentence a person can read, while tags are
#    **used as the exact words they were stored as**.
#    Every character is either **what was written down at storage time** (the tag
#    words) or **fixed text from these tables**.
#    A model rephrasing it differently every time makes it read less like "my
#    memory", not more.
#
# ⚠️ Only **how it is read out** changes, **not what is read**: the exact
#    percentages and V/A remain, to the number, in the `recall_data()` skin (the
#    dashboard needs them to draw distributions).
#    Numbers where numbers are wanted, plain language where an instant read is
#    wanted — two skins over one set of statistics.
#
# 📌 The reference table (these four lines are the acceptance sample):
#      I/EVENT/SELF/WHAT 37%   → 「多半是我自己在做事」
#      MIND/TRAITS 19%         → 「想得也不少」
#      青岛6 交接单5 火车4      → 「围着青岛、交接单转」(tag words used verbatim)
#      V0.62 / A0.50           → 「心里还行，不算绷着」
#    The wording is tunable: anything that reads wrong is fixed by editing these
#    four tables, without touching a line of logic.

_ROOM_PHRASES = {
    "EVENT/SELF":  ("多半是我自己在做事",   "几乎都是我自己在做事"),
    "EVENT/WORLD": ("多半是我听说看到的",   "几乎都是我听说看到的"),
    "MIND/TRAITS": ("多半在想我是个什么样的人", "几乎都在想我是个什么样的人"),
    "MIND/VIEWS":  ("多半在想我怎么看一件事",  "几乎都在想我怎么看一件事"),
}
# The subclause: added when the main clause is on the event side and thinking
# still takes up a sizeable minority (the second example above, 「想得也不少」)
_SUBCLAUSE_GATE = 0.15


def rooms_in_words(rooms: Counter, n: int) -> str:
    """Room proportions -> one plain sentence. **Only the four rooms count** (old
    names are normalised first).

    A proportion is a **result, not a quota**: this sentence says what I have been
    doing lately, and whatever comes out on top is reported as it is.
    """
    if not rooms or not n:
        return ""
    event_n = sum(c for r, c in rooms.items() if r.startswith("EVENT"))
    mind_n = sum(c for r, c in rooms.items() if r.startswith("MIND"))
    top_room, top_n = max(rooms.items(), key=lambda kv: kv[1])
    mostly, nearly_all = _ROOM_PHRASES.get(top_room, (f"多半在 {top_room}", f"几乎都在 {top_room}"))
    dominance = max(event_n, mind_n) / n
    if dominance >= 0.85:
        sentence = nearly_all
    elif dominance >= 0.6:
        sentence = mostly
    else:
        sentence = "做的和想的一半一半"
        return sentence
    # If the other half clears the subclause gate, add a clause — MIND/TRAITS at
    # 19% in the sample above is exactly this case
    if top_room.startswith("EVENT") and _SUBCLAUSE_GATE <= mind_n / n < 0.5:
        sentence += "，想得也不少"
    elif top_room.startswith("MIND") and _SUBCLAUSE_GATE <= event_n / n < 0.5:
        sentence += "，也记了些发生的事"
    return sentence


def mood_in_words(v, a) -> str:
    """V/A -> 「心里还行，不算绷着」. One clause per axis, joined by a comma."""
    if v is None:
        return ""
    if v >= 0.7:
        v_words = "心里挺好"
    elif v >= 0.55:
        v_words = "心里还行"
    elif v >= 0.45:
        v_words = "心里平平"
    elif v >= 0.3:
        v_words = "心里有点沉"
    else:
        v_words = "心里不好受"
    if a is None:
        return v_words
    if a >= 0.7:
        a_words = "绷得紧"
    elif a >= 0.55:
        a_words = "有点绷着"
    elif a >= 0.35:
        a_words = "不算绷着"
    else:
        a_words = "松着"
    return f"{v_words}，{a_words}"


def tags_in_words(tags: list, k: int = 2, with_counts: bool = False, framed: bool = True) -> str:
    """Tags -> 「围着青岛、交接单转」. **Not one character of a tag is changed**;
    only the frame around them is a template.

    Use with_counts=True where the numbers matter: `床 3` and `床 30` describe two
    completely different stretches of life, and without counts those two lines
    look identical. Use framed=False where **the row label already says it in
    plain language** (the card line `围着什么   代码 3 · 交接单 3`) — saying the
    same thing twice only makes it harder to read.
    """
    items = [(t, n) for t, n in (tags or []) if is_human_tag(t)][:max(1, k)]
    if not items:
        return ""
    parts = [f"{t} {n}" if with_counts else str(t) for t, n in items]
    if not framed:
        return " · ".join(parts)
    return "围着" + "、".join(parts) + "转"


def kind_badge(meta: dict) -> str:
    """The badge in front of an item line: **a mind wears 🧠, an event wears
    nothing.**

    🔴 **The room code is gone**: a code like `EVENT/SELF` is written for a
    machine, and three of them in one line blur the single fact that has to stay
    clear — "what happened" versus "what I thought".
    The real ailment was mixed listing with no marker of identity, leaving the
    reader to guess from the content — in the data, room has always been perfectly
    distinct; **it was the display layer that failed to separate them**.
    ⚠️ Do not solve this by splitting into two blocks (an earlier draft put events
       as the main body with thinking in a separate little section; it was
       dropped): keep them interleaved, since telling them apart at a glance is
       enough.
    """
    return "🧠" if is_mind_room((meta or {}).get("room")) else ""


# ------------------------------------------------------------
# Parsing when: plain-language time scales (calendar scales are computed
# automatically; life-scale expressions wait on anchors)
# ------------------------------------------------------------

def _parse_when(when: str) -> tuple[datetime | None, datetime | None, str]:
    """Returns (start, end, error). An empty string = no time filter.

    ⚠️ Everything here goes through `tools/_when` (local timezone). It used to use
    the container's `datetime.now()`, which is UTC — ask for 「今天」 at 2 a.m. and
    the container answers with the previous afternoon.
    """
    w = when.strip()
    if not w:
        return None, None, ""
    now = _w.now()
    today = _w.today()

    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", w)
    if m:
        n = float(m.group(1))
        delta = timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
        return now - delta, now, ""

    # Each stretch is defined once and then given every spelling that means it.
    #
    # 🔴 English spellings were added because the tool description, the README and every
    #    comment around them are in English while this table was not — so the very first
    #    thing a new reader reaches for, `when="today"`, failed. The tool said one thing
    #    and did another, which is the fault this whole file is careful about elsewhere.
    #    Nothing was taken away: every Chinese spelling still resolves exactly as before.
    _ranges = {
        "today": (today, now),
        "yesterday": (today - timedelta(days=1), today),
        "day before yesterday": (today - timedelta(days=2), today - timedelta(days=1)),
        "this week": (today - timedelta(days=today.weekday()), now),
        "last week": (today - timedelta(days=today.weekday() + 7),
                      today - timedelta(days=today.weekday())),
        "this month": (today.replace(day=1), now),
        "last month": ((today.replace(day=1) - timedelta(days=1)).replace(day=1),
                       today.replace(day=1)),
        "this year": (today.replace(month=1, day=1), now),
    }
    _spellings = {
        "今天": "today", "昨天": "yesterday", "前天": "day before yesterday",
        "本周": "this week", "上周": "last week",
        "本月": "this month", "上月": "last month", "今年": "this year",
        # Written without the space as well, because that is how it gets typed.
        "dayBeforeYesterday": "day before yesterday",
        "thisweek": "this week", "lastweek": "last week",
        "thismonth": "this month", "lastmonth": "last month", "thisyear": "this year",
    }
    words = {**_ranges, **{k: _ranges[v] for k, v in _spellings.items()}}
    if w in words:
        a, b = words[w]
        return a, b, ""

    # Everything below is "which day / which month" — calendar scales, computed
    # against the local calendar rather than a UTC instant
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", w)
    if m:
        try:
            a = _w.parse_date(m.group(1))
            b = _w.parse_date(m.group(2)) + timedelta(days=1)
            return a, b, ""
        except ValueError:
            pass
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}", w)
    if m:
        try:
            a = _w.parse_date(w)
            return a, a + timedelta(days=1), ""
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{4})-(\d{2})", w)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            a = datetime(y, mo, 1, tzinfo=_w.LOCAL_TZ)
            b = (datetime(y + 1, 1, 1, tzinfo=_w.LOCAL_TZ) if mo == 12
                 else datetime(y, mo + 1, 1, tzinfo=_w.LOCAL_TZ))
            return a, b, ""
        except ValueError:
            pass  # an impossible month like 2026-99 falls through to the "not understood" branch below

    return None, None, (
        f"when 看不懂：{w}。认识的写法：48h / 7d / "
        "今天 / 昨天 / 前天 / 本周 / 上周 / 本月 / 上月 / 今年（"
        "today / yesterday / this week / last week / this month / last month / this year 同义）/ "
        "2026-07 / 2026-07-15 / 2026-07-01..2026-07-15。"
        "「刚搬去那阵子」这类生活刻度要等锚点——先用 query 门扔关键词。"
    )


# ------------------------------------------------------------
# Fetching and filtering
# ------------------------------------------------------------

def _ts_of(meta: dict) -> datetime | None:
    """A memory's time coordinate: **when first, created as fallback**. This is
    the only definition; there is no second one.

    ⚠️ There used to be a `by="touched"` branch reading `last_active` ("order by
    most recently touched, for digesting"). **`by` was cut entirely** — there is
    no such act as "digesting" here; every event and every thought grows anew.
    The rule: **every parameter must map onto a sentence that actually surfaces in
    the mind**, and "order by most recently touched" is not such a sentence.

    Returns **a timezone-aware local time**. It used to be
    `datetime.fromisoformat(s[:19])` — that slice cuts off `Z` / `+08:00` along
    with everything else, forcing a timestamp that stated its timezone into "no
    idea which timezone", and then comparing it against a UTC now().
    """
    for k in ("when", "created"):
        ts = _w.parse_stamp(meta.get(k))
        if ts is not None:
            return ts
    return None


# What recall lists and counts lives in the gate (core/visibility.on_timeline): the
# timeline's kind filter and the lookup listing's rules together. The panel's counters
# import it under this name, so the number on a page and the number recall shows are one.
_visible = _V.on_timeline


async def _collect(when, room, tag, query, all_buckets=None) -> tuple[list[dict], str, dict]:
    """Filter down to what is being looked at this time. Returns
    `(entries, error, ledger)`.

    The ledger currently records exactly one thing: how many entries top-k cut
    (`topk砍掉`) — **whatever was blocked has to stay visible**.
    """
    ledger: dict = {"topk砍掉": 0, "topk": _SEARCH_TOPK}
    t0, t1, err = _parse_when(when)
    if err:
        return [], err, ledger
    room = room.strip()
    gate_err = check_gate(room)
    if gate_err:
        return [], gate_err, ledger
    tag = tag.strip()

    # The query gate: fetch generously (300) -> pass every gate -> only then keep
    # the top _SEARCH_TOPK by relevance. (It used to truncate before filtering, so
    # the slots were wasted on hits that failed the gates and the qualifying
    # memory at rank k+1 disappeared forever.)
    scores: dict[str, float] = {}
    literals: set[str] = set()   # buckets with a literal hit: the relevance floor treats them as max(score, floor)
    how: dict[str, tuple[bool, bool]] = {}   # id -> (some query words appear, close in meaning)
    if query.strip():
        try:
            hits = await rt.bucket_mgr.search(query.strip(), limit=300)
        except Exception as e:
            return [], f"搜索失败：{e}", ledger
        pool = []
        for h in hits:
            hid = str(h.get("id") or "")
            full = await rt.bucket_mgr.get(hid)
            if full:
                try:
                    scores[hid] = float(h.get("score") or 0.0)
                except (TypeError, ValueError):
                    scores[hid] = 0.0
                if h.get("literal_hit"):
                    literals.add(hid)
                how[hid] = (bool(h.get("bm25_hit")), bool(h.get("vector_match")))
                pool.append(full)
    else:
        # 🔴 Browsing needs the whole library, and the caller hands it in rather than this
        #    function reaching for it. Same reasoning as `covering()`: a call that fetches
        #    the library for itself is invisible to whoever called it, so several of them
        #    in one request each look reasonable and together scan the library N times.
        #    That is exactly what was happening here — a single browse fetched it once in
        #    this function and again in `_render_browse`, and neither could see the other.
        #    With it passed in, a caller that fetches twice is looking at both lines.
        if all_buckets is None:
            all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        pool = all_buckets

    out = []
    browsing = not query.strip()
    scope_view = await read_scope()
    for b in pool:
        meta = b.get("metadata", {}) or {}
        if not _V.on_timeline(meta, scope_view):
            continue
        # Faded or sunk entries **do not turn up while browsing** ("it surfaces on
        # its own even when I am not looking for it" belongs to what is still
        # alive).
        # A search (with a query) still reaches them, only at a discounted score —
        # forgetting happens quietly, so nothing is marked or counted here.
        if browsing and str(meta.get("decay_stage") or "") in ("faded", "sunk"):
            continue
        # The room gate compares **after normalisation**: the old ten-room names
        # are still on disk (the migration shipped a script but was never run
        # against the real library), and room="MIND" has to match a legacy
        # I/MIND/TRAITS, or this gate matches nothing at all in practice.
        if room and not room_matches(meta.get("room"), room):
            continue
        # The tag gate is a **containment match**: searching 「床」 has to find
        # 「床上」 and 「床头」 too.
        # It used to be exact equality — and tags are never complete (「床」 appears
        # in 16 bodies but made it into the tags of only one), so equality matching
        # dropped half of an already sparse signal.
        if tag and not any(tag in str(t) for t in (meta.get("tags") or [])):
            continue
        ts = _ts_of(meta)
        if ts is None:
            continue
        if t0 and ts < t0:
            continue
        if t1 and ts >= t1:
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        words, meaning = how.get(bid, (False, False))
        out.append({"id": bid, "meta": meta, "ts": ts,
                    "content": str(b.get("content") or ""),
                    # score exists only when the query gate ran; None = this entry came in via when/room/tag
                    "score": scores.get(bid),
                    "literal": bid in literals,
                    "words": words, "meaning": meaning})
    if scores and len(out) > _SEARCH_TOPK:
        # Only after every gate does relevance close it down: keep the k
        # highest-scoring entries, then return to the timeline.
        # How many were cut goes into the ledger — **whatever was blocked stays
        # visible** (reported in a final line at render time).
        out.sort(key=lambda x: scores.get(x["id"], 0.0), reverse=True)
        ledger["topk砍掉"] = len(out) - _SEARCH_TOPK
        out = out[:_SEARCH_TOPK]
    out.sort(key=lambda x: x["ts"])
    if scores:
        ledger["roots"] = await _find_roots(out)
    return out, "", ledger


# ------------------------------------------------------------
# Roots: what a search hit grew out of
# ------------------------------------------------------------
# A hit's roots are where its sources (wasDerivedFrom / hadPrimarySource, `read_from_ids`)
# lead when followed all the way back: the entries with no source in the library. An entry
# with no source is its own root. A newer version of a root stands for it (a revision is
# the same entry, not a source). A source the `read` road of the gate would not show is not
# followed — the walk does not see what the reader may not, so nothing hidden is counted,
# named or used to group. Hits with the same roots are one line in the search view: the same
# origin found several times is still one origin.
_ROOT_DEPTH = 32


async def _find_roots(entries: list[dict]) -> dict[str, dict | None]:
    """Set `e["roots"]` (a frozenset of ids) on each entry and return the store's bucket
    for every root reached (id -> bucket), for the line to say what state it is in."""
    cache: dict[str, dict | None] = {
        e["id"]: {"id": e["id"], "metadata": e["meta"], "content": e["content"]} for e in entries}

    get = getattr(rt.bucket_mgr, "get_including_archive", None) or rt.bucket_mgr.get
    scope_view = await read_scope()

    async def fetch(bid: str) -> dict | None:
        if bid not in cache:
            b = await get(bid)
            cache[bid] = (b if b and _V.visible_for(b.get("metadata") or {}, scope_view,
                                                    road=_V.READ) else None)
        return cache[bid]

    async def newest(bid: str) -> str:
        seen = {bid}
        while True:
            nxt = str(((await fetch(bid)) or {}).get("metadata", {}).get("superseded_by") or "").strip()
            if not nxt or nxt in seen or not await fetch(nxt):
                return bid
            seen.add(nxt)
            bid = nxt

    memo: dict[str, frozenset] = {}

    async def roots_of(bid: str, path: frozenset) -> frozenset:
        if bid in memo:
            return memo[bid]
        meta = ((await fetch(bid)) or {}).get("metadata") or {}
        sources = [s for s in read_from_ids(meta) if s not in path and await fetch(s)]
        if not sources or len(path) >= _ROOT_DEPTH:
            found = frozenset({await newest(bid)})
        else:
            acc: set[str] = set()
            for s in sources:
                acc |= await roots_of(s, path | {bid})
            found = frozenset(acc)
        memo[bid] = found
        return found

    reached: dict[str, dict | None] = {}
    for e in entries:
        e["roots"] = await roots_of(e["id"], frozenset())
        for r in e["roots"]:
            reached[r] = cache.get(r)
    return reached


# ------------------------------------------------------------
# Statistics for one cell
# ------------------------------------------------------------

def _short_id(bucket_id: str) -> str:
    """A 12-hex id is cut to 6 as a handle; a readable id like feel_... is given
    in full (cutting it would make it useless)."""
    return bucket_id[:6] if re.fullmatch(r"[0-9a-f]{12}", bucket_id) else bucket_id


def _score_tag(e: dict) -> str:
    """Show relevance next to the handle. Only entries that went through the query
    gate have a score.

    Why show it: a search for a term meaning "taking care of one's health" (in the
    sense of massage and herbal medicine) came back mostly with entries about the
    body and intimacy — far too much of a stretch. The root cause turned out to be
    that the score had been sitting inside _collect() all along, used only to
    truncate when there were more than 60 results, **never as a threshold** — and
    the number was invisible from outside, so there was no way to judge on what
    grounds the system had dredged an entry up.

    ⚠️ This step deliberately does nothing but make it visible; it adds no
    threshold. The underlying _VECTOR_RECALL_THRESHOLD=0.65 may be loose for
    Chinese (two unrelated Chinese passages hitting a cosine of 0.7 is common),
    but how loose has to be judged against the real distribution — any threshold
    chosen before seeing that distribution is a guess. Live with it for a few days
    first, then decide.
    """
    s = e.get("score")
    if not isinstance(s, (int, float)) or not s:
        return ""
    return f" {float(s):.2f}"


def _label_of(e: dict) -> str:
    """The display text for a memory: gist > name (timestamp stripped) > the start
    of the body. Every one of them is text written down at storage time."""
    meta = e["meta"]
    s = str(meta.get("summary") or "").strip()
    if s:
        return s
    name = re.sub(r"^[\d\- :]+", "", str(meta.get("name") or "")).strip()
    if name:
        return name
    return re.sub(r"\s+", " ", e["content"])[:40]


def _cell_stats(entries: list[dict]) -> dict:
    rooms = Counter()
    tags = Counter()
    seeds = Counter()
    v_sum = a_sum = v_n = 0.0
    for e in entries:
        meta = e["meta"]
        # Statistics are grouped under the **four new rooms**: old data is shown
        # under the new names, so the screen has only one vocabulary
        r = normalize_room(meta.get("room"))
        if r:
            rooms[r] += 1
        for t in (meta.get("tags") or []):
            t = str(t)
            if is_human_tag(t):     # machine-voiced tags are never shown
                tags[t] += 1
        for s in _SEED_RE.findall(e["content"]):
            if s in _SEED_NAMES:
                seeds[s] += 1
        try:
            v_sum += float(meta.get("valence", 0.5))
            a_sum += float(meta.get("arousal", 0.3))
            v_n += 1
        except (TypeError, ValueError):
            pass

    # What stands out = the odd one (its tags share nothing with the dominant
    # tags, highest importance) + the heavy one (arousal x importance)
    top_tags = {t for t, _ in tags.most_common(3)}
    def _imp(e):
        try:
            return float(e["meta"].get("importance", 5))
        except (TypeError, ValueError):
            return 5.0
    def _weigh(e):
        try:
            return float(e["meta"].get("arousal", 0.3)) * _imp(e)
        except (TypeError, ValueError):
            return 0.0
    # 🔴 **Covered entries never appear in "what stands out"** — that is the
    #    per-entry area, and a covered entry no longer surfaces on its own.
    #    But they **still count in the statistics above** (count, room, tags, V/A
    #    all intact): the acceptance rule is fixed at "the amount of information
    #    any recall can show may only grow, never shrink", and a gist merely
    #    **replaces those entries with one sentence** in the breakdown; it does not
    #    erase them from the books.
    surfacing = [e for e in entries if not _F.is_covered(e["meta"])]
    odd = [e for e in surfacing
           if top_tags and not (set(map(str, e["meta"].get("tags") or [])) & top_tags)]
    odd.sort(key=_imp, reverse=True)
    heavy = sorted(surfacing, key=_weigh, reverse=True)
    highlights: list[tuple[str, dict]] = []
    seen = set()
    for e in odd[:1]:
        highlights.append(("◇", e))
        seen.add(e["id"])
    for e in heavy:
        if len(highlights) >= _HIGHLIGHT_MAX:
            break
        if e["id"] not in seen:
            highlights.append(("★", e))
            seen.add(e["id"])
    # Heavy first, odd last — it reads better that way
    highlights.sort(key=lambda p: p[0] == "◇")

    v_avg = (v_sum / v_n) if v_n else None
    a_avg = (a_sum / v_n) if v_n else None
    return {
        "n": len(entries),
        "rooms": rooms.most_common(2),
        # The two plain-language sentences are computed **from the whole
        # Counter**, not from most_common(2) — 「多半是我自己在做事」 is a claim
        # about the whole cell, and looking at only the top two gets the
        # denominator wrong.
        "房间话": rooms_in_words(rooms, len(entries)),
        "情绪话": mood_in_words(v_avg, a_avg),
        # Six rather than four: **a tag is not an attribute of one memory, it is
        # the distribution across a pile of them** — a single entry's tags carry
        # almost no information (the body is right there anyway), and their value
        # is entirely in the moment of collapse. So the harder it collapses, the
        # more of them are needed, and with counts attached.
        "tags": tags.most_common(6),
        "v": v_avg,
        "a": a_avg,
        "seeds": [s for s, _ in seeds.most_common(2)],
        "highlights": highlights,
    }


# ------------------------------------------------------------
# Zooming and rendering
# ------------------------------------------------------------

def _split_cells(entries: list[dict], max_cells: int = _CELL_MAX) -> tuple[str, list[tuple[str, list[dict]]]]:
    """**Granularity is set by span, not by density.**

    What was rejected over and over is splitting by count: 5 entries spanning a
    month and 500 entries spanning a month **should both be split by week** —
    granularity answers "what scale should this stretch of time be viewed at",
    which has nothing to do with how many entries are inside it.
    Try the ladder from finest to coarsest and take the first scale that cuts the
    whole span into <= max_cells cells; empty cells are still discarded (never
    rendered), but **density plays no part in choosing the scale**.
    (An earlier version did choose by density, to cure "two entries six months
    apart cut into two half-month cells" — choosing by span cures that just as
    well: a six-month span naturally lands on the month or quarter scale.)
    """
    base = entries[0]["ts"].timestamp()
    span = max(1.0, entries[-1]["ts"].timestamp() - base)
    for gname, gsec in _LADDER:
        if span / gsec > max_cells:
            continue  # this scale cuts the span into too many cells — too fine, go one coarser
        slices: dict[int, list[dict]] = {}
        for e in entries:
            slices.setdefault(int((e["ts"].timestamp() - base) // gsec), []).append(e)
        this_year = _w.now().year
        labeled = []
        for k in sorted(slices):
            cell = slices[k]
            a, b = cell[0]["ts"], cell[-1]["ts"]
            day_a = a.strftime("%m-%d") if a.year == this_year else a.strftime("%Y-%m-%d")
            if gsec < 24 * 3600:
                # Hour / half-day cells: one day produces several, so the label
                # must carry the time (otherwise they all read as the same date)
                label = f"{day_a} {a.strftime('%H:%M')}"
            elif a.date() == b.date():
                label = day_a
            else:
                label = f"{day_a}~{b.strftime('%m-%d')}"
            labeled.append((label, cell))
        return gname, labeled
    return "全部", [("全部", entries)]


def _fmt_header(label: str, st: dict) -> str:
    """One line per cell (the overview path of slices=N). **Machine readouts
    become plain language; tag words are used exactly as stored.**"""
    bits = [f"{label} · {st['n']}条"]
    for x in (st["房间话"], tags_in_words(st["tags"], 2), st["情绪话"]):
        if x:
            bits.append(x)
    seeds = "".join(f"[[{s}]]" for s in st["seeds"])
    return " ▏".join(bits) + (" " + seeds if seeds else "")


def _fmt_highlights(st: dict) -> str:
    """What stands out, **one per line**, with the gist given in full.

    The question that ended the old layout, asked three times over: "the gist is
    still incomplete, so what exactly are you looking at?" — three of them used to
    be crammed onto one line, each cut to 26 characters, and reading it told you
    only that something had happened, never what.
    The truncation was an oversight, not a design: collapsing collapses **how many
    entries there are**, never **what each one says**.
    """
    return "\n".join(f"   {mark}{kind_badge(e['meta'])}{_label_of(e)}"
                     f"({_short_id(e['id'])}{_score_tag(e)})"
                     for mark, e in st["highlights"])


def _split_calendar(entries: list[dict], unit: str) -> list[tuple[str, list[dict]]]:
    """Split by **calendar week / calendar month** (used by the week and month
    bands of the time-gradient view).

    It used to be `_split_cells_fixed(gsec)`: 7-day / 30-day blocks counted from
    the oldest entry. The result was that 31 January and 1 February could land in
    the same "month" while 1 January and 31 January were split into two.
    When a person says "by week" or "by month" they mean weeks and months on the
    calendar, not "168 hours counted from some particular memory".
    """
    keyf = _w.year_week if unit == "week" else _w.year_month
    slices: dict[tuple, list[dict]] = {}
    for e in entries:
        slices.setdefault(keyf(e["ts"]), []).append(e)
    this_year = _w.now().year
    labeled = []
    for k in sorted(slices):
        cell = slices[k]
        a, b = cell[0]["ts"], cell[-1]["ts"]
        if unit == "month":
            label = f"{a.year}-{a.month:02d}" if a.year != this_year else f"{a.month} 月"
        else:
            day_a = a.strftime("%m-%d") if a.year == this_year else a.strftime("%Y-%m-%d")
            label = day_a if a.date() == b.date() else f"{day_a}~{b.strftime('%m-%d')}"
        labeled.append((label, cell))
    return labeled


# ⚰️ Two people's names used to be hard-coded here (_ME_NAMES / _HER_NAMES).
#    Once the rooms were cut down to four, room_implied_tags() returns an empty
#    set unconditionally, so **nothing used those two sets any more** — and they
#    were deleted along with the names. The path that still filters names is
#    dehydrator._person_tags(), which reads them from configuration.


def room_implied_tags(room: str) -> set[str]:
    """Once a room has been filtered on, **the people the room's definition
    already implies** carry zero information if they show up again in the tags.

    What made it obvious: filtering to the room for "things between us" means
    every entry is about the same two people — **the room already says it, and the
    tags then say it again**. Which is why three rows of 75/46/31 entries all had
    the same tags. It was not that those tags were frequent; it was that they were
    **repeating what the room had already said**.

    This is the same rule as "never repeat a dimension you have already filtered
    on", which earlier removed the `I/EVENT/SELF/WHO 100%` repeated dozens of
    times at the end of every row; this extends it from the room name to **the
    people the room implies**.

    ⚠️ With no room filter, none of them is removed — in that case a person tag
    genuinely carries information (it distinguishes who the entry is about).

    ------------------------------------------------------------
    🔴 This function currently **degenerates to an empty set on purpose**; it is
    not broken:
    once the rooms were cut down to four, a room name **no longer implies any
    person** (`EVENT/SELF` says only "I was there", not who with). "Who it is
    about" moved wholesale into the `subjects` field.
    So the right home for this deduplication moves with it: once subjects is wired
    into retrieval, this becomes "if subjects was filtered on, that same name in
    the tags carries zero information" — **the same rule, a different field**.
    Until then, returning an empty set is correct: removing names now would
    **delete real information** (the room no longer guarantees that person was
    present).
    """
    return set()


def common_tags(entries: list[dict], ratio: float = 0.55) -> set[str]:
    """Tags that **almost every entry in this batch has** — they define the batch
    rather than characterise any one cell.

    What made it obvious: after filtering to the room meaning "things between us",
    every cell carried the same handful of tags — the definition of that room,
    carrying zero information. A row collapsing 75 entries told the reader nothing
    at all.

    It is the same ailment as before: that time it was the room (every row
    followed by `I/EVENT/SELF/WHO 100%`, repeated dozens of times) — the room got
    deduplicated and **the tags were missed**.
    ⚠️ This branch is only a backstop (its threshold is conservative). What
    actually works is room_implied_tags() — frequency can never catch it: a tag
    present in only 50% of memories can still rank first in nearly every cell,
    because the problem was never "frequent".
    Pure statistics; nothing goes through a model.
    """
    cnt = Counter()
    for e in entries:
        for t in {str(x) for x in (e["meta"].get("tags") or [])}:
            if not t.startswith(_SYS_TAG_PREFIXES):
                cnt[t] += 1
    return {t for t, n in cnt.items() if n >= max(2, len(entries) * ratio)}


def _pick_tags(st: dict, drop: set[str], k: int = 2) -> list[str]:
    """Pick k tags that actually distinguish; if all of them were dropped, fall
    back to the originals (better repetitive than empty)."""
    kept = [t for t, _ in st["tags"] if t not in drop]
    return (kept or [t for t, _ in st["tags"]])[:k]


def _pick_tags_n(st: dict, drop: set[str], k: int = 2) -> list[tuple[str, int]]:
    """Same as _pick_tags but with counts (`床 3` and `床 30` describe two
    different stretches of life, and without the number you cannot tell)."""
    kept = [(t, n) for t, n in st["tags"] if t not in drop]
    return (kept or list(st["tags"]))[:k]


def _far_line(label: str, st: dict, fixed_room: bool = False,
              drop: set[str] | None = None) -> str:
    """A stretch collapsed into one sentence. Neither the dimension that was
    filtered on nor the tags common to the whole batch are repeated."""
    drop = drop or set()
    bits = [f"{label} · {st['n']}条"]
    if not fixed_room and st["房间话"]:
        bits.append(st["房间话"])
    tags = _pick_tags_n(st, drop)
    if tags:
        bits.append(tags_in_words(tags, 2))
    if st["情绪话"]:
        bits.append(st["情绪话"])
    line = " ▏".join(bits)
    if st["highlights"]:
        mark, e = st["highlights"][0]
        # 22 -> 40: this line really does have to fit stats + tags + mood + one
        # representative, so it cannot go entirely uncut; but 22 characters is the
        # same as giving no content at all
        line += (f" {mark}{kind_badge(e['meta'])}{_label_of(e)[:40]}"
                 f"({_short_id(e['id'])}{_score_tag(e)})")
    return line


def _fmt_card(label: str, st: dict) -> str:
    """One card (the 1-3 cell tier, and also breath's middle-term block).
    **All three lines speak plain language.**

    The row labels changed with it: `房间/标签/底色` are database column names,
    while 「在做什么 / 围着什么转 / 心里」 is how a person says it.
    """
    lines = [f"{label} · {st['n']}条 " + "─" * 24]
    lines.append("在做什么   " + (st["房间话"] or "-"))
    # This tag line **carries counts** (`床 3` and `床 30` are two different
    # stretches of life); the row label already says 「围着什么」, so the value does
    # not wrap itself in 「围着…转」 a second time
    lines.append("围着什么   " + (tags_in_words(st["tags"], 6, with_counts=True, framed=False) or "-"))
    lines.append("心里       " + (st["情绪话"] or "-")
                 + ("  " + " ".join(f"[[{s}]]" for s in st["seeds"]) if st["seeds"] else ""))
    if st["highlights"]:
        first = True
        for mark, e in st["highlights"]:
            prefix = "扎眼的     " if first else "           "
            # The gist is **never cut**: this card is breath's middle-term block
            # (it goes through slices=1), and it used to truncate at 46
            # characters — two lines out of three broke off mid-sentence.
            # 📌 The rule: **collapsing collapses how many entries there are, never
            #    what each one says.** A gist is only about 60 characters to begin
            #    with.
            lines.append(f"{prefix}{mark} {kind_badge(e['meta'])}{_label_of(e)} "
                         f"({_short_id(e['id'])}{_score_tag(e)})")
            first = False
    return "\n".join(lines)


# The Chinese display names of the four rooms. Always normalize_room() before
# looking one up, so a room that is not one of the four shows as itself rather than
# crashing a lookup (use _room_cn(), never a bare .get()).
# TRAITS is about a person, any person: myself, the other, anyone else.
ROOM_CN: dict[str, str] = {
    "EVENT/SELF":  "我亲历的",
    "EVENT/WORLD": "我听说看到的",
    "MIND/TRAITS": "人是什么样",
    "MIND/VIEWS":  "我怎么看",
}


def _room_cn(room) -> str:
    """A room's Chinese display name, accepting both old and new names; anything
    unrecognised is echoed back as-is (never turned into a blank)."""
    r = normalize_room(room)
    if r:
        return ROOM_CN.get(r, r)
    return str(room or "") or "没房间"


def entry_json(e: dict) -> dict:
    """A memory's shape for the front end. Every character was written down at
    storage time; this only moves it, never edits it."""
    meta = e["meta"]
    def _f(key, default):
        try:
            return float(meta.get(key, default))
        except (TypeError, ValueError):
            return default
    tags = [str(t) for t in (meta.get("tags") or [])]
    # The front end always receives the **four new** room names (old data is
    # normalised here), or the panel would show two sets of room names at once
    room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
    return {
        "id": e["id"],
        "short": _short_id(e["id"]),
        "label": _label_of(e),
        "room": room,
        "room_cn": _room_cn(meta.get("room")),
        "ts": e["ts"].isoformat(timespec="seconds"),
        "date": e["ts"].strftime("%Y-%m-%d"),
        "importance": _f("importance", 5.0),
        "v": _f("valence", 0.5),
        "a": _f("arousal", 0.3),
        "pinned": bool(meta.get("pinned")),
        # The three decay stages are for the dashboard skin only (someone has to
        # be able to judge whether the engine is doing its job right); the text
        # skin (breath/recall output) never mentions them — forgetting happens
        # quietly
        "decay": str(meta.get("decay_stage") or "") or "alive",
        "kind": "mind" if is_mind_room(meta.get("room")) else "event",
        "status": str(meta.get("status") or ""),
        "when": str(meta.get("when") or ""),
        "tags": [t for t in tags if not t.startswith(_SYS_TAG_PREFIXES)],
        # Relevance: present only when the query gate ran; None = filtered in via when/room/tag (see _score_tag)
        "score": e.get("score"),
        "dup_of": [t.split(":", 1)[1] for t in tags if t.startswith("疑似同件:")],
        "seeds": sorted({s for s in _SEED_RE.findall(e["content"]) if s in _SEED_NAMES}),
    }


def _stats_json(st: dict) -> dict:
    """The JSON shape of _cell_stats (each entry inside highlights becomes an id
    plus its text)."""
    return {
        "n": st["n"],
        # The two plain-language sentences go to the front end as well: the text
        # skin and the panel say **the same sentence**, while the panel still gets
        # the exact percentages and V/A — wherever numbers are wanted, every one
        # of them is there.
        "房间话": st["房间话"],
        "情绪话": st["情绪话"],
        "rooms": [{"room": r, "room_cn": _room_cn(r), "n": n,
                   "pct": round(100 * n / st["n"]) if st["n"] else 0}
                  for r, n in st["rooms"]],
        "tags": [{"tag": t, "n": n} for t, n in st["tags"]],
        "v": st["v"], "a": st["a"], "seeds": st["seeds"],
        "highlights": [{"mark": mark, "id": e["id"], "short": _short_id(e["id"]),
                        "label": _label_of(e), "score": e.get("score")}
                       for mark, e in st["highlights"]],
    }


# ============================================================
# Two paths: browsing (no query) · searching (with a query)
# ============================================================

def _pick_reps(far: list[dict], k: int = _BROWSE_REP_MAX) -> list[tuple[str, dict]]:
    """Pick k representatives from the whole distant stretch.

    The rule: anything further back is labelled "some time ago" and given 2-3
    entries from 2-3 weeks back.
    So the last three weeks are preferred; only when there is nothing at all in
    those three weeks (the query is about something long ago) does it fall back to
    the whole stretch.
    The picking reuses the existing "the odd one + the heavy one" (the highlights
    from _cell_stats) rather than inventing a second method.
    """
    cutoff = _w.today() - timedelta(days=_BROWSE_REP_DAYS)
    pool = [e for e in far if e["ts"] >= cutoff] or far
    return _cell_stats(pool)["highlights"][:k]


def _rep_line(mark: str, e: dict) -> str:
    # 60 rather than 30: the gists recall returned were coming back incomplete —
    # and a representative entry is the only place that stretch of time gives any
    # content at all, so cutting it in half is the same as giving nothing. The
    # date stays as a handle (for drilling in), not as a classification.
    return (f"  {mark}{kind_badge(e['meta'])}{_label_of(e)[:60]}({_short_id(e['id'])}) "
            f"{e['ts'].strftime('%m-%d')}")


def _big_line(meta: dict, content: str, bid: str) -> str:
    """One line for a period.

    🔴 The `◈` symbol was replaced by the word 「时期」.
       A symbol has to be learned before it can be read, and this line was
       supposed to be understood at a glance.
    """
    span = _big.fmt_span(meta)
    return (f"  时期 {_big.first_line(content)[:38]}({_short_id(bid)})"
            + (f" {span}" if span else ""))


def _cell_span(cell: list[dict]) -> tuple[datetime, datetime]:
    """One cell's time range, as a **half-open interval**: the right edge is pushed
    to the day after the last entry.

    🔴 Both of these are pits, and both were fallen into:
    ① `entries` is ordered **newest to oldest**, so `cell[0]` is the newest entry
       and `cell[-1]` the oldest — passing them straight to `covering()` as
       `(t0, t1)` hands over the start and end **reversed**, and the only periods
       that then surface are the ones fully containing the whole stretch (a silent
       display failure that had been there for a long time before it was found).
    ② For a memory with `when=2026-12-25`, `ts` is **midnight** on that day. Taking
       `max(ts)` as the right edge collapses the interval to a single point, and
       since `covering()` tests for overlap (skipping when `s >= t1`), a period
       starting exactly at that midnight would be excluded from the very day it
       covers.
    """
    ts = [e["ts"] for e in cell]
    return min(ts), max(ts) + timedelta(days=1)


def _big_lines(all_buckets: list, t0, t1, seen: set[str]) -> list[str]:
    """The **periods** overlapping this cell, one title line each
    (`时期 <name> (id) 8-13~8-16`).

    🔴 A period is a **pure naming layer**: it never writes `covered_by`, so it
    cannot go down `_gist_lines`' "who got covered" path — its members are
    **computed live by date**, and the display layer should therefore compute them
    live too: `_bigevent.covering()` (the original mechanism, unchanged).
    ⚠️ A period surfaces only once per render (`seen`): covering three days does
       not mean saying it three times.
    ⚠️ This **only ever adds a line**: the per-entry area and the statistics below
       lose nothing (a period collapses no rows) — which is where the rule
       "information may only grow, never shrink" lands.
    🔴 **Only one per cell** (it used to be up to 3). Periods accumulate over time,
       and browsing wants a gradient, not a list. `covering()` returns newest
       first, so taking the first one gives the period closest to this cell.
    """
    out: list[str] = []
    for meta, content, bid in _big.covering(all_buckets, t0, t1):
        if bid in seen:
            continue
        seen.add(bid)
        out.append(_big_line(meta, content, bid))
        break          # one per cell
    return out


async def _gist_lines(entries: list[dict], skip: set[str] | None = None, scope_view=None) -> list[str]:
    """Which gists cover the covered entries in this cell -> one title line per
    gist.

    **This is the "one extra line"**:
        08-13~08-16  「那几天在青岛做讲义」   <- the gist (newly added)
          56条 · 房间… · 标签… · 突出…        <- everything that was there before,
                                                to the character

    🔴 The rule: a covered entry does not appear on its own in the per-entry area,
    but **where it went has to stay visible** — so this line carries the gist's id
    (the handle for drilling in) and how many entries in this cell it covers.
    Information may only grow, never shrink: N single lines are gone, and a title
    line plus a drillable id has appeared.
    """
    covered: dict[str, int] = {}
    for e in entries:
        # Crossing: one entry can be covered by two threads at once -> both gist
        # titles count it
        for gid in _F.covers_of(e["meta"]):
            if gid and gid not in (skip or set()) and (scope_view is None or scope_view.permits_id(gid)):
                covered[gid] = covered.get(gid, 0) + 1
    out: list[str] = []
    for gid, n in sorted(covered.items(), key=lambda kv: -kv[1]):
        b = await rt.bucket_mgr.get_including_archive(gid)
        if not b:
            out.append(f"  ▣（盖着这里 {n} 条的 gist {_short_id(gid)} 查无此桶——链断了，报给AI）")
            continue
        meta = b.get("metadata", {}) or {}
        head = _big.first_line(str(b.get("content") or ""))[:38]
        mark = "◈" if _big.is_big(meta) else "▣"
        span = _big.fmt_span(meta) if _big.is_big(meta) else ""
        out.append(f"  {mark}{head}({_short_id(gid)})"
                   + (f" {span}" if span else "") + f" ▏盖着这里 {n} 条")
    return out


async def _render_browse(entries, gates, room, tag, all_buckets=None) -> str:
    """Browsing: I am looking, and cannot recall what is there. **The far end is
    cut hard** — the last three days stay as they were, everything older collapses
    into one stretch labelled "some time ago".

    Precise date bands like `07-13~07-19` read wrong: **that is a machine's way of
    dividing time; a person only thinks "some time ago"**. So the far end
    **stops being divided into cells at all** and 2-3 representatives are picked
    from the whole stretch.
    That also cured another ailment: before the change, **1 entry and 75 entries
    took up exactly the same amount of space**.
    """
    now = _w.now()
    today = _w.today()
    # 🔴 **The whole browse fetches the library exactly once here.** The period
    #    lines below used to fetch it themselves (once per cell, once per day,
    #    once for the far end — seven or eight times in a single browse), and
    #    those fetches were **hidden inside `covering()`, invisible to every
    #    caller**.
    #    Now the list lives here and whoever needs it reaches for it, so one extra
    #    fetch would be right there in plain sight.
    #    ⚠️ Periods need **the whole library**, not the entries this call filtered
    #       down to — whether a period covers this cell has nothing to do with
    #       whether the period itself passed the filters.
    if all_buckets is not None:
        span_buckets = all_buckets
    else:
        try:
            span_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            rt.logger.warning(f"时期那半的库没捞到，这次浏览不盖时期: {e}")
            span_buckets = []
    # A period the request may not read is not named (a read scope, core/scope.py).
    scope_view = await read_scope()
    if scope_view is not None:
        span_buckets = [b for b in span_buckets if scope_view.permits(b)]
    dn = today - timedelta(days=_BROWSE_NEAR_DAYS - 1)   # 今天 / 昨天 / 前天
    tomorrow = today + timedelta(days=1)

    future = [e for e in entries if e["ts"] >= tomorrow]
    near = [e for e in entries if dn <= e["ts"] < tomorrow]
    far = [e for e in entries if e["ts"] < dn]

    # A dimension that was filtered on is a constant — do not say it again; the
    # same goes for the tags common to the whole batch.
    fixed_room = bool(room.strip())
    drop = (room_implied_tags(room)
            | common_tags(entries)
            | ({tag.strip()} if tag.strip() else set()))

    def _head(label: str, st: dict) -> str:
        bits = [f"── {label} · {st['n']}条"]
        if not fixed_room and st["房间话"]:
            bits.append(st["房间话"])
        # Near-end tags carry counts too (`床 3` and `床 30` are two different
        # stretches of life).
        # With counts attached the 「围着…转」 frame is **dropped** — wrapping
        # numbers in it reads badly, the words and numbers are already given
        # exactly as stored, and the frame only helps where there are no counts.
        tags = _pick_tags_n(st, drop)
        if tags:
            bits.append(tags_in_words(tags, 2, with_counts=True, framed=False))
        if st["情绪话"]:
            bits.append(st["情绪话"])
        seeds = "".join(f"[[{x}]]" for x in st["seeds"])
        return " ▏".join(bits) + (" " + seeds if seeds else "")

    lines = [f"〔{gates}〕{len(entries)} 条 · 新→旧"]
    # Each period surfaces only once per render (once it has appeared at the near
    # end, the far stretch does not repeat it)
    spans_shown: set[str] = set()

    # Days that have not arrived yet: collapsed by calendar month, so however far
    # ahead they are they take only a few lines
    if future:
        lines.append("— 还没到的 —")
        for label, cell in reversed(_split_calendar(future, "month")):
            lines.append(_far_line(label, _cell_stats(cell), fixed_room, drop))
            lines.extend(_big_lines(span_buckets, *_cell_span(cell), spans_shown))

    # Within three days: unchanged (one line per day, with what stands out on its
    # own line)
    if near:
        days: dict[str, list] = {}
        for e in near:
            days.setdefault(e["ts"].strftime("%m-%d"), []).append(e)
        for label in sorted(days, reverse=True):
            st = _cell_stats(days[label])
            lines.append(_head(label, st))
            # The period/gist title goes **above** what stands out: say what these
            # days are called first, then which entry inside them jumps out
            lines.extend(_big_lines(span_buckets, *_cell_span(days[label]), spans_shown))
            hl = _fmt_highlights(st)
            if hl:
                lines.append(hl)

    # Anything older: **no cells at all**, one sentence for the whole stretch
    # ("some time ago") plus 2-3 representatives (replaced by a big event where
    # there is one)
    if far:
        st = _cell_stats(far)
        a = far[0]["ts"]
        # The title **no longer reports a precise date band**: saying "some time
        # ago" and then hanging `08-01~08-02` off it contradicts itself — that is
        # still a machine's way of dividing time. The date stays only after each
        # representative, where it is a handle rather than a classification.
        # ⚠️ The exception: if room/tag was filtered on, the user is **following
        # one thing** — "from when to when did this run" is exactly the question
        # being asked, so the span has to be given.
        if fixed_room or tag.strip():
            span_txt = f"{a.strftime('%m-%d')} ~ {far[-1]['ts'].strftime('%m-%d')}"
            bits = [f"— 前段时间（{span_txt}）· {len(far)}条"]
        else:
            bits = [f"— 前段时间 · {len(far)}条"]
        # 🔴 **The tag distribution carries counts** — this line is the far end's
        # only answer to "what were those days like": `亲密关系 30 · 接纳 12` and
        # `亲密关系 3 · 接纳 2` mean opposite things, and without the counts the
        # two lines look identical. breath has always had the counts; it was this
        # path that threw them away.
        #
        # ⚠️ Here **only the people a room implies are dropped**, never
        # common_tags (the frequent ones) — a frequent tag is noise elsewhere
        # (every cell showing the same three words), but on this line **it is the
        # answer**.
        # Frequency can never catch it on its own: once the counts are attached,
        # frequency stops being the handle and becomes the content.
        drop_lite = room_implied_tags(room) | ({tag.strip()} if tag.strip() else set())
        dist = tags_in_words([(t, n) for t, n in st["tags"] if t not in drop_lite],
                             5, with_counts=True, framed=False)
        if dist:
            bits.append(dist)
        if st["情绪话"]:
            bits.append(st["情绪话"])
        lines.append(" ▏".join(bits) + " —")
        # The rule: give 2-3 entries from 2-3 weeks back, **and where there is a
        # big event, use that instead**.
        # Big events take the slots first, and representatives fill whatever is
        # left — fewer than 3 never leaves a gap.
        # (It covers, it does not replace: the statistics line above and what
        # stands out lose nothing; there is simply one more sentence.)
        covering_spans = _big.covering(span_buckets, *_cell_span(far))
        covers = [x for x in covering_spans if x[2] not in spans_shown]
        for meta, content, bid in covers[:1]:      # one period per cell
            spans_shown.add(bid)
            lines.append(_big_line(meta, content, bid))
        # ⚰️ **The gist's mind lines were pulled out of the browse view.**
        #    This used to list, one per line, every gist covering some of the
        #    entries in this stretch.
        #    🔴 The rule: **what recall is there to show is events.** Thinking can
        #       appear under "what stands out", but it should not be crowded into
        #       the same position as events and periods —
        #       and once there are a few gists, the cell becomes nothing but title
        #       lines (periods are capped at 3; gists had no cap at all).
        #    ⚠️ Entries folded away **still do not appear individually and still
        #       count in the statistics**; to see which gist covers one, use
        #       `recall(query=<full id>)` or the search path, both unchanged.
        for mark, e in _pick_reps(far, _BROWSE_REP_MAX - len(covers[:1])):
            lines.append(_rep_line(mark, e))
        # The trigger point hangs off "recall a stretch of time" — at that moment
        # I am looking back anyway, with the material spread out in front of me,
        # and "it feels like I was doing one particular thing back then" surfaces
        # naturally; I do not have to remember to go looking for it. So the prompt
        # appears once, and only when **nothing genuinely covers this stretch**.
        # ⚠️ The criterion is whether any period covers this stretch at all, not
        #    `covers` (the ones that have not yet surfaced in this cell) — a period
        #    already mentioned at the near end does not mean the stretch is
        #    uncovered.
        if not covering_spans and (now - a).days >= 7:
            # ⚠️ This suggests `fold`, not `grow(kind="big")`. That entry point was
            #    withdrawn — passing it now returns "use fold instead", so the older
            #    wording sent the reader down a path that answers with a correction.
            #    A system telling you to do something it will refuse costs a round trip
            #    and, worse, reads as the system not knowing its own shape.
            lines.append("  （这段时间上没有时期盖着。真觉得是在做一件什么事就写下来："
                         'fold(when="起..止", room=…, text=…, v=…, a=…)）')

    lines.append("（钻：缩小 when / 加 room·tag / slices=N 控格数；看原文：拿 id 搜）")
    return chr(10).join(lines)


def _topk_line(ledger: dict | None) -> str:
    """How many entries top-k cut — **whatever was blocked stays visible**. This
    line is the counterpart to tightening top-k.

    The rule behind it: more words in a query = an averaged vector = a poorer aim.
    So this line does not just report a number, it spells out the way forward:
    **use one or two core words, in the wording actually used at the time**.
    """
    n = int((ledger or {}).get("topk砍掉") or 0)
    if n <= 0:
        return ""
    k = int((ledger or {}).get("topk") or _SEARCH_TOPK)
    return (f"── 还有 {n} 条命中被 top-{k} 挡在外面（按相关度截的）——"
            "词多了向量就取平均，换一两个核心词、用当时的原话再搜一次")


def _eff_score(e: dict, floor: float) -> float:
    """The effective score: a literal hit becomes max(score, floor). This is a
    floor, not a bonus — it lifts the low ones and leaves the high ones alone.

    Its whole purpose is **not missing things**, not putting them first.
    """
    s = e.get("score") or 0.0
    return max(s, floor) if e.get("literal") else s


def _how_mark(e: dict) -> str:
    """How a search hit matched, said right after its score — the two sides the score is
    made of, never a third number:
      字面   the whole query appears in it as written
      意思   its vector is close to the query's (cosine at or above the vector line)
    Both when both hold. A hit with neither got over the line on some of the query's words
    (部分字面) or on a weaker closeness in meaning alone (意思)."""
    marks = [word for word, on in (("字面", e.get("literal")), ("意思", e.get("meaning"))) if on]
    if marks:
        return "+".join(marks)
    return "部分字面" if e.get("words") else "意思"


def _written(e: dict) -> str:
    """「N天前写的」 from `created` on the local calendar: an old line was true the day it
    was written, not necessarily now."""
    days = _P.written_days_ago(e["meta"], _w.now().date())
    if days is None:
        return ""
    return "今天写的" if days <= 0 else f"{days}天前写的"


_PROMISE_MARK = "⏳答应了还没关 · "


def _lead_of(key: frozenset, members: list[dict], floor: float) -> dict:
    """Which hit of one root's group the line shows: an open promise (the best-scoring, if
    several), else the root itself when it matched, else the best-scoring hit."""
    def best(es: list[dict]) -> dict:
        return max(es, key=lambda m: (_eff_score(m, floor), m["ts"]))
    promised = [m for m in members if _P.is_open_promise(m["meta"])]
    if promised:
        return best(promised)
    root = next(iter(key)) if len(key) == 1 else None
    return next((m for m in members if m["id"] == root), None) or best(members)


def _root_tail(lead: dict, key: frozenset, members: list[dict], listed: set[str],
               below: set[str], roots: dict) -> str:
    """What the line says about the rest of its root's group. Every other hit is named by
    its id (collapsing is for reading, not hiding); a root the search did not list says why
    (under the line, not in this listing, or in the archive)."""
    others = sorted((m for m in members if m is not lead and m["id"] not in key),
                    key=lambda m: m["ts"], reverse=True)
    ids = " · ".join(("⏳" if _P.is_open_promise(m["meta"]) else "") + _short_id(m["id"])
                     for m in others)
    if key == {lead["id"]}:
        return f"＋{len(others)} 条派生：{ids}" if others else ""
    refs = []
    for r in sorted(key):
        if r in listed:
            note = "（也搜中了）"
        elif r in below:
            note = "（在线下）"
        else:
            mark = _V.visible_for((roots.get(r) or {}).get("metadata") or {}, road=_V.READ).mark
            note = f" {mark}" if mark else "（这次没列）"
        refs.append(_short_id(r) + note)
    head = f"派生自 {refs[0]}" if len(refs) == 1 else f"派生自 {len(refs)} 个根：{'、'.join(refs)}"
    return head + (f"，同根另有 {len(others)} 条：{ids}" if others else "")


def _search_rows(hit: list[dict], floor: float) -> list[tuple[dict, frozenset, list[dict]]]:
    """The search view's lines: one per root (`_find_roots`), as (lead, roots, hits).

    Order: lines holding an open promise first, then the rest; newest first within each
    (by the date the line shows). Only what this search matched is ordered — a promise that
    did not match is not brought in."""
    groups: dict[frozenset, list[dict]] = {}
    for e in hit:
        groups.setdefault(e.get("roots") or frozenset({e["id"]}), []).append(e)
    rows = [(_lead_of(key, members, floor), key, members) for key, members in groups.items()]
    rows.sort(key=lambda r: (any(_P.is_open_promise(m["meta"]) for m in r[2]), r[0]["ts"]),
              reverse=True)
    return rows


def _render_search(entries, gates, floor: float = None, ledger: dict | None = None) -> str:
    """Searching: I am looking for something and I know what. **This is the
    default view whenever there is a query.**

    Whatever clears the line is ordered by time (newest first) with its score
    attached, how it matched (`_how_mark`) and how long ago it was written. Hits that
    grew from the same root are one line (`_search_rows`), and lines holding an open
    promise go first.
    🔴 **The default view was turned back to this one**: a bare query used to
    switch to scene clusters, so "find that one thing" had to take a detour. The
    rule: **order by time plus score by default, and ask for scene clusters
    explicitly** (`view="scene"`). That also killed off "supplying `when` changes
    the shape of the view", another case of one parameter doing two jobs — `when`
    now governs range only, and shape is decided by `view` alone.

    A later change made the score govern filtering only, never ordering: the
    results of "find one thing" only show how it got here when they are laid out
    along the timeline; sorting by relevance stirs July and August together.
    Time is a discount, not a gate: older entries stay out of the way by default
    through the decay discount, and when you really are looking for one and hit it
    accurately, it survives the discount and comes up anyway.

    Anything below the line is not listed — if there is no direct connection,
    better not to show it at all.
    But those entries **have not disappeared**: a final line carries how many
    there are, the highest score, and what the earliest one says — which
    incidentally answers "when did this start".
    """
    floor = RELEVANCE_FLOOR if floor is None else float(floor)
    hit = [e for e in entries if _eff_score(e, floor) >= floor]
    below = [e for e in entries if _eff_score(e, floor) < floor]
    top_below = max(((e.get("score") or 0.0) for e in below), default=0.0)

    if not hit:
        return (f"〔{gates}〕**没有相关的记忆。**\n"
                f"够到 {len(below)} 条，但最高才 {top_below:.1f} 分（线在 {floor:.0f}）——"
                "都只是沾边，不弹出来。\n"
                "真觉得该有：换当时说过的原话当 query（别造词），或者用 when/room 直接翻。")

    rows = _search_rows(hit, floor)
    listed = {e["id"] for e in hit}
    below_ids = {e["id"] for e in below}
    roots = (ledger or {}).get("roots") or {}
    all_roots = set().union(*(key for _lead, key, _m in rows))
    head = f"〔{gates}〕{len(hit)} 条"
    if all_roots != listed:
        # The same origin found several times is still one origin: say how many there are.
        head += f"，出自 {len(all_roots)} 个根（同一个根收成一行）"
    promised_first = any(_P.is_open_promise(m["meta"]) for _l, _k, ms in rows for m in ms)
    head += (" · " + ("答应了还没关的在最前，其余" if promised_first else "")
             + f"按时间 新→旧（线 {floor:.0f}，分数只管过滤）")
    lines = [head]
    for lead, key, members in rows:
        # The search path **never cuts the gist**: cut it and you cannot tell
        # whether this is the entry you were after, and telling is the entire
        # point of searching. The browse path still cuts — there the goal is an
        # impression, not content.
        # 🧠 = thinking; wearing no badge means it is something that happened (the
        # room code is gone).
        written = _written(lead)
        line = (f"{_eff_score(lead, floor):5.1f} {_how_mark(lead)}  "
                f"{_PROMISE_MARK if _P.is_open_promise(lead['meta']) else ''}"
                f"{kind_badge(lead['meta'])}{_label_of(lead)}"
                f"  ({_short_id(lead['id'])})  {lead['ts'].strftime('%m-%d')}"
                + (f" · {written}" if written else ""))
        tail = _root_tail(lead, key, members, listed, below_ids, roots)
        lines.append(line + (f" ▏{tail}" if tail else ""))
    if below:
        earliest = min(below, key=lambda x: x["ts"])
        lines.append(f"── 另有 {len(below)} 条在线下（最高 {top_below:.1f}，"
                     f"最早 {earliest['ts'].strftime('%m-%d')}：「{_label_of(earliest)[:40]}」）——"
                     "多半只是沾边，没列")
    lines.append(_topk_line(ledger))
    lines.append("（看原文、看收起来的派生：拿 id 搜；换个说法再搜：用当时的原话，别造词）")
    return chr(10).join(x for x in lines if x)


def _render_scene_clusters(entries, gates, floor: float = None, ledger: dict | None = None) -> str:
    """Recall as scenes: how this thing got from there to here.

    🔴 **It has to be asked for explicitly**: `recall(query=…, view="scene")`.
    It used to be the default view for a bare query, which meant the same query
    changed shape depending on whether `when` was supplied — **one parameter doing
    two jobs**, and the source of the confusion.
    The default went back to "time plus score" (find that one thing), and scene
    clusters are reserved for "how this thing got here".

    The structure a mind actually holds is not a flat list but **clusters with a
    main scene and subordinate ones** — a representative can carry sub-scenes
    hanging off it (for instance, learning to code -> ① the day of making a
    spreadsheet, talking about code ② talking with a chatbot at the airport about
    how to learn it systematically (sub-scene: asking it for a document from an
    aeroplane seat) ③ reading the notes on a laptop at a desk).
    What clusters grip is the scene anchors (which is all tags now hold): one
    cluster = the hits that share scene words.
    Clusters are ordered by their earliest entry ("how it got here" is told from
    the beginning); the representative inside a cluster is the highest-scoring one.
    """
    floor = RELEVANCE_FLOOR if floor is None else float(floor)
    hit = [e for e in entries if _eff_score(e, floor) >= floor]
    below = [e for e in entries if _eff_score(e, floor) < floor]
    top_below = max(((e.get("score") or 0.0) for e in below), default=0.0)
    if not hit:
        return (f"〔{gates}〕**没有相关的记忆。**\n"
                f"够到 {len(below)} 条，但最高才 {top_below:.1f} 分（线在 {floor:.0f}）——"
                "都只是沾边，不弹出来。\n"
                "真觉得该有：换当时说过的原话当 query（别造词），或者用 when/room 直接翻。")

    def _vis_tags(e) -> set[str]:
        # Clustering also grips plain-language scene words only: machine-voiced
        # tags (the `aspect:patterns` kind) will string completely unrelated
        # memories into a single false "scene"
        return {str(t) for t in (e["meta"].get("tags") or []) if is_human_tag(t)}

    # Greedy clustering: take the main scene in descending score order, and gather
    # whatever shares its scene words as subordinate scenes
    ranked = sorted(hit, key=lambda e: _eff_score(e, floor), reverse=True)
    unassigned = list(ranked)
    clusters: list[list[dict]] = []
    while unassigned:
        head = unassigned.pop(0)
        ht = _vis_tags(head)
        members = [head]
        if ht:
            rest = []
            for e in unassigned:
                if ht & _vis_tags(e):
                    members.append(e)
                else:
                    rest.append(e)
            unassigned = rest
        clusters.append(members)

    clusters.sort(key=lambda c: min(e["ts"] for e in c))  # how it got here: tell it from the beginning
    lines = [f"〔{gates}〕{len(hit)} 条 · {len(clusters)} 个画面 · 一路过来（线 {floor:.0f}）"]
    for c in clusters[:8]:
        rep = max(c, key=lambda e: _eff_score(e, floor))
        kids = sorted((e for e in c if e is not rep), key=lambda e: e["ts"])
        shared = set.intersection(*(_vis_tags(e) for e in c)) if len(c) > 1 else set()
        label = ("·".join(sorted(shared)[:2]) + " ") if shared else ""
        written = _written(rep)
        lines.append(f"■ {rep['ts'].strftime('%m-%d')} {label}"
                     f"{_eff_score(rep, floor):5.1f} {_how_mark(rep)}  "
                     f"{kind_badge(rep['meta'])}{_label_of(rep)}"
                     f"  ({_short_id(rep['id'])})" + (f" · {written}" if written else ""))
        for e in kids[:3]:
            written = _written(e)
            lines.append(f"   └ {e['ts'].strftime('%m-%d')}  {kind_badge(e['meta'])}"
                         f"{_label_of(e)[:56]}  ({_short_id(e['id'])})"
                         + (f" · {written}" if written else ""))
        if len(kids) > 3:
            lines.append(f"   └ …还有 {len(kids) - 3} 条同画面的")
    if len(clusters) > 8:
        n_rest = sum(len(c) for c in clusters[8:])
        lines.append(f"…还有 {len(clusters) - 8} 个画面（{n_rest} 条）——加 when 缩小段落再看")
    if below:
        earliest = min(below, key=lambda x: x["ts"])
        lines.append(f"── 另有 {len(below)} 条在线下（最高 {top_below:.1f}，"
                     f"最早 {earliest['ts'].strftime('%m-%d')}：「{_label_of(earliest)[:40]}」）")
    lines.append(_topk_line(ledger))
    lines.append("（要平铺的时间轴：去掉 view；看原文：拿 id 搜）")
    return chr(10).join(x for x in lines if x)


# What a read by id says first about an entry that is not live. The body still comes out
# whole — a lookup hides nothing — but never as if it were a current memory.
_STATE_LOUD = {
    _V.ARCHIVED: "⚠️在归档区：这条已经沉下去了，不是现在的记忆——下面是它沉下去之前的原文，别当成眼下的事。",
    _V.DELETED: "⚠️在归档区（已删除）：这条被删掉了，不是现在的记忆——下面是它删掉之前的原文，别当成眼下的事。",
}


# What a read by id says of an entry carrying the internally-generated mark (core/_dream
# is_marked), on its own line and on every line linking to it: it happened inside, not out
# there.
DREAMT_MARK = "💭梦或想象"


async def _linked(bucket_id: str, scope_view=None) -> tuple[dict | None, str, str] | None:
    """One entry a read by id links to — a source, what it covers, a period's member, who
    cites it — as its line shows it: (bucket or None, hint, mark).

    The gate's `read` road decides: a linked entry is always a line (its gist), never
    expanded into its body, and one that is not live carries its state out loud. One the
    request's read scope (`scope_view`) does not reach is None: no line at all, not even its id."""
    if scope_view is not None and not scope_view.permits_id(bucket_id):
        return None
    b = await rt.bucket_mgr.get_including_archive(bucket_id)
    if not b:
        return None, "", ""
    meta = b.get("metadata", {}) or {}
    verdict = _V.visible_for(meta, scope_view, road=_V.READ)
    if _V.SOURCE_GONE in verdict.reasons:
        return b, "", "  （依据的来源被撤回或删除了，正文不给）"
    if not verdict:
        return b, "", "  （不在这次能看的范围里）"
    hint = re.sub(r"^[\d\- :]+", "",
                  str(meta.get("summary") or meta.get("name") or "").strip())[:60]
    from core._dream import is_marked
    dreamt = f"  {DREAMT_MARK}" if is_marked(meta, b.get("content") or "") else ""
    return b, hint, (f"  {verdict.mark}" if verdict.mark else "") + dreamt


async def recall_text_and_data(when: str, room: str, tag: str, query: str,
                               floor=None, view: str = "", max_cells: int = 0,
                               road: str = "") -> dict:
    """Collect once, serve both skins. For the panel, and for breath's 近三天, whose text
    and JSON skins are these two. `road`: what was collected also has to pass that road
    of the gate (breath's `recent`), so both skins are made from what it lets through.

    🔴 The panel's endpoint used to call `recall_data()` and `recall_core()`
       separately, and each of those runs its own `_collect` — meaning **the same
       search was computed twice**. Measured with a query: 3 seconds through the
       tool face, 8.6 through the panel, and that extra pass was the difference.
       ⚠️ The irony: `recall_data`'s docstring had always said the two skins share
          one collection underneath and **never compute their own**. That sentence
          described the intent; the code had never honoured it.
       Now it really does collect once.

    ⚠️ Each skin gets its own **shallow copy** of entries: rendering sorts and
       slices, and sharing one list would mean whoever ran first decided the
       outcome.
    """
    collection = await _collect(when, room, tag, query)
    entries, err, ledger = collection
    if road:
        scope_view = await read_scope()
        entries = [e for e in entries if _V.visible_for(e["meta"], scope_view, road=road)]
    data = await recall_data(when, room, tag, query, floor=floor, view=view,
                             collected=(list(entries), err, dict(ledger)))
    if data.get("ok") and data.get("total"):
        kwargs = {"max_cells": max_cells} if 1 <= max_cells <= 20 else {}
        data["card"] = await recall_core(when, room, tag, query, floor=floor,
                                         view=view,
                                         collected=(list(entries), err, dict(ledger)),
                                         **kwargs)
    else:
        data["card"] = ""
    return data


async def recall_data(when: str, room: str, tag: str, query: str,
                      floor=None, view: str = "", collected=None) -> dict:
    """recall's **other skin**: the same four gates, the same _collect/_cell_stats,
    returning a dict for the front end.

    The text skin is recall_core(). Both skins share one collection and one set of
    statistics underneath and must never compute their own — the dominant colour
    shown on the page and the one the AI sees on waking have to be the same
    number, or there are two systems.
    ⚠️ `by` was cut; `view` affects only the text skin's shape, and this skin still
    returns everything.
    """
    entries, err, ledger = collected if collected is not None else await _collect(
        when, room, tag, query)
    if err:
        return {"ok": False, "error": err, "entries": [], "total": 0}
    if not entries:
        return {"ok": True, "entries": [], "total": 0, "stats": None,
                "gates": {"when": when, "room": room, "tag": tag, "query": query,
                          "view": view}}
    st = _cell_stats(entries)
    # The relevance floor only means anything while searching (no query gate, no
    # score).
    # It is meant to be dragged around on the dashboard, so the floor is **passed
    # in with each request** — never hard-coded and never stored. Any threshold
    # chosen before seeing the real distribution is a guess.
    fl = RELEVANCE_FLOOR if floor is None else float(floor)
    payload = []
    today = _w.now().date()
    for e in reversed(entries):  # newest first, the same direction as the text skin
        j = entry_json(e)
        j["written_days"] = _P.written_days_ago(e["meta"], today)
        if e.get("score") is not None:
            # The literal-hit floor: max(score, floor), so what the front end sees
            # is the score that actually took effect
            j["score"] = round(_eff_score(e, fl), 2)
            j["literal"] = bool(e.get("literal"))
            # The text skin's line facts, unrendered: how it matched, its roots (hits
            # sharing them are one line there), and whether it is a promise still open.
            j["meaning"] = bool(e.get("meaning"))
            j["words"] = bool(e.get("words"))
            j["how"] = _how_mark(e)
            j["roots"] = sorted(e.get("roots") or ())
            j["open_promise"] = _P.is_open_promise(e["meta"])
        payload.append(j)
    return {
        "ok": True,
        "total": len(entries),
        "stats": _stats_json(st),
        "entries": payload,
        "gates": {"when": when, "room": room, "tag": tag, "query": query, "view": view},
        "floor": fl,
        "floor_default": RELEVANCE_FLOOR,
        # How many top-k cut: the panel has to see it too (the same number as the
        # text skin's final line)
        "topk": ledger.get("topk"),
        "topk_dropped": ledger.get("topk砍掉", 0),
        "below": sum(1 for e in entries
                     if e.get("score") is not None and _eff_score(e, fl) < fl),
    }


async def recall_core(when: str, room: str, tag: str, query: str,
                      max_cells: int = _CELL_MAX, floor=None, view: str = "",
                      collected=None) -> str:
    """The text skin. **The full parameter list**: when / room / tag / query /
    slices / view — those six and no more.

    🔪 **`by` was cut entirely** (from the code and the tool description alike):
       `by="touched"` — there is no such act as "digesting" here;
       `by="回看"`   — superseded by `slices` (slices=N is "how coarse or fine I
       want that stretch").
       The rule: **every parameter must map onto a sentence that actually surfaces
       in the mind.**
    🆕 `view="scene"`: scene clusters went from the default to something you
       **ask for explicitly**.
    """
    view = str(view or "").strip()
    # view="slices": the host's raw lines sliced and waiting to be handled
    # (tools/_slices.py). It reads the pending store, not the library, so the four
    # filters mean nothing to it and are refused rather than ignored.
    if view == "slices":
        if any(str(x or "").strip() for x in (when, room, tag, query)):
            return ('view="slices" 单独用：它列的是宿主交来、还没认领的切片，不在库里，'
                    "when / room / tag / query 管不到它。")
        from .. import _slices
        return await _slices.render_pending()
    # view="original" with query=<id>: ask the hosts for the original of that memory's
    # sources (tools/recall/original.py). Only on this explicit ask: it goes over the
    # network, so no other read does it.
    if view == "original":
        if any(str(x or "").strip() for x in (when, room, tag)) or not query.strip():
            return ('view="original" 只配 query="记忆的 id" 用：它向宿主取那条记忆依据的原话，'
                    "when / room / tag 管不到它。")
        from .original import render_original
        return await render_original(query)
    if view and view != "scene":
        return (f'view 无效：{view}。只有三种："scene"'
                "（按共享场景词聚成簇，看这件事怎么一路过来的）；"
                '"slices"（宿主交来、还没认领的切片）；'
                '"original"（配 query="记忆的 id"，向宿主取那条依据的原话）。'
                "不给 view = 默认按时间＋分数排，找那件事。")
    if view and not query.strip():
        return ('view="scene" 要跟 query 一起用——簇是按**命中的记忆**共享的场景词聚的，'
                "没有 query 就没有命中，也就没有画面。"
                "只想翻一段时间：recall(when=…)（要更粗/更细加 slices=N）。")

    # --- Direct id lookup: the query is itself a full bucket_id -> return that
    # entry's verbatim text plus all of its metadata ---
    # This is the "click through to the original" door (the tier C list gives
    # gists; you come in here with an id).
    # A partial id is matched as a unique prefix (the same resolver the write
    # tools use); a collision lists the candidates and no match says so plainly
    # — never fall through to semantic search.
    q, id_err = await resolve_bucket_id(query)
    if id_err:
        return id_err
    if re.fullmatch(r"[0-9a-f]{12}", q) or re.fullmatch(r"feel_\d{12}_V\d{3}(_\d+)?", q):
        # Under a read scope (`scope_view`) everything this read names is asked of it: an entry
        # out of scope reads like one that does not exist (resolve_bucket_id already said
        # so for the entry itself), and a linked one is left out — no line, no id, not
        # counted.
        scope_view = await read_scope()
        b = await rt.bucket_mgr.get_including_archive(q)
        _usage.offer([q], "recall.read")
        if not b:
            return f"查无此桶：{q}（id 形状但不存在——可能已物理删除或打错，不做语义联想）。"
        if b:
            meta = b.get("metadata", {}) or {}
            # A read by id is a lookup: dont_surface hides nothing, and an entry that is
            # not live still comes out whole — with its state said before anything else.
            verdict = _V.visible_for(meta, scope_view, road=_V.READ)
            if verdict.out_of_scope:
                return f"查无此桶：{q}（id 形状但不存在——可能已物理删除或打错，不做语义联想）。"
            if _V.SOURCE_GONE in verdict.reasons:
                return (f"{q} 依据的来源被撤回或删除了，正文不给。它在开口前「依据变了的」里："
                        "只凭还剩的来源重写（regrow），或者收起来（trace delete=True）。")
            if not verdict:
                return f"{q} 不在这次能看的范围里。"

            def seen(bid) -> bool:
                return scope_view is None or scope_view.permits_id(str(bid or ""))
            lines = [f"═ {q} · {str(meta.get('name') or '')}"]
            if verdict.mark:
                lines.append(_STATE_LOUD[verdict.state])
            info = []
            for k, label in (("room", "房间"), ("when", "when"), ("valence", "V"),
                             ("arousal", "A"), ("importance", "重"), ("status", "状态")):
                v = meta.get(k)
                if v not in (None, ""):
                    info.append(f"{label}:{v}")
            # A tag that points at another entry by its handle (「疑似同件:abc123」) names
            # it: under a read scope only when the request may read it.
            tags_ = [str(t) for t in (meta.get("tags") or []) if not str(t).startswith("__")
                     and (scope_view is None or not (m := _HANDLE_TAG_RE.match(str(t)))
                          or scope_view.permits_handle(m.group(1)))]
            if tags_:
                info.append("标签:" + ",".join(tags_[:6]))
            if str(meta.get("card_of") or "").strip():
                info.append(f"名字卡:{str(meta['card_of']).strip()}")
            # From a dream: the entry's own mark, or an entry it stands on that carries one
            # (a gist or a thought grown from a dream note carries no field of its own).
            from core import _dream
            if _dream.is_marked(meta, b.get("content") or ""):
                info.append(f"{DREAMT_MARK}（内部生成，不是外面发生的事）")
            else:
                dreamt_roots = [r for r in await _dream.dream_roots(meta) if seen(r)]
                if dreamt_roots:
                    info.append(f"{DREAMT_MARK}：依据里有梦或想象（{'、'.join(dreamt_roots[:3])}"
                                + (f" 等 {len(dreamt_roots)} 条" if len(dreamt_roots) > 3 else "")
                                + "）")
            # Sources must never be reduced to bare ids: a mind holds only the
            # product of thinking, the events live in its provenance, and reading it
            # has to bring the sources' gists along or the thinking has nothing to
            # stand on. Each line says what kind of source it is; a quoted line is the
            # host's conversation, not a memory, so nothing is looked up for it.
            # A source record of the host's material is shown by its string form, with
            # the registry's word for it when it is no longer simply there.
            src_lines: list[str] = []
            quoted: set[str] = set()
            for line in read_prov(meta):
                fid, word = line["target"], _PROV_WORD[line["rel"]]
                if line["rel"] == WAS_QUOTED_FROM:
                    quoted.add(fid)
                    src_lines.append(f"  ← {_source_label(fid)}  [{word}] "
                                     "（宿主对话里的原话，不在库里）" + _source_mark(fid))
                    continue
                # A source that sank or was deleted still explains the thought, but
                # reading it as current would be wrong: it carries its mark.
                got = await _linked(fid, scope_view)
                if got is None:
                    continue
                src, hint, mark = got
                if src:
                    src_lines.append(f"  ← {fid}  [{word}] {hint}{mark}")
                else:
                    src_lines.append(f"  ← {fid}  [{word}] （查无此桶——源可能被硬删过）")
            for text in _source_strings(meta):
                if text not in quoted:
                    src_lines.append(f"  ← {_source_label(text)}  [来源记录] "
                                     "（宿主的材料，不在库里）" + _source_mark(text))
            if meta.get("supersedes") and seen(meta["supersedes"]):
                info.append(f"换掉了:{meta['supersedes']}")
            if meta.get("superseded_by") and seen(meta["superseded_by"]):
                info.append(f"⚠️已被换版:{meta['superseded_by']}（这是旧版）")
            # The other end of drilling down: **who covers this** (possibly
            # several — entries can cross).
            # The re-versioning case (n=1) is already stated by the superseded_by
            # line above and is not repeated here.
            _sup = str(meta.get("superseded_by") or "")
            _cbs = [c for c in _F.covers_of(meta) if c != _sup and seen(c)]
            if _cbs:
                info.append(f"⚠️被 {'、'.join(_cbs)} 盖着（不再独立冒头；搜索和这儿照样看得见）")
            lines.append(" · ".join(info))
            # A basis this memory grew out of was overturned (regrow's overturn marks
            # every descendant). The entry still counts; whoever reads it has to know
            # which ground moved, and to what. One looked at and kept as it is
            # (core/_invalidation.py, `confirmed_at`) is no longer a warning, only a note.
            for rec in (meta.get("invalidation") or []):
                if not isinstance(rec, dict) or rec.get("kind") != "overturn":
                    continue
                if not (seen(rec.get("of")) and seen(rec.get("by"))):
                    continue
                confirmed = str(rec.get("confirmed_at") or "")
                if confirmed:
                    lines.append(f"依据变过，{confirmed[5:10]} 确认照留"
                                 f"（{rec.get('of')} 被 {rec.get('by')} 推翻过）。")
                else:
                    lines.append(f"⚠️依据变了：{rec.get('of')} 被 {rec.get('by')} 推翻了"
                                 f"（{str(rec.get('at') or '')[:10]}）——这条站在它上面，看的时候记着。")
            if src_lines:
                lines.append("来源:")
                lines.extend(src_lines)
                # The host's own words are not in the library; this says where to ask.
                if quoted or _source_strings(meta):
                    lines.append(f'  （宿主那边的原话：recall(query="{q}", view="original")）')
            # **This is unfold, and it is not a separate tool**: what a gist
            # covers is laid out here, one line each (id + gist).
            # Why not a separate tool: the drilling-down action already exists
            # (search by id), and adding an unfold would give one thing two entry
            # points — of which I would only ever remember one.
            # A period (the time-circling form) has no member list to consult — it
            # stores only a name and a span, and its members are **computed live**.
            # Drilling down still works: what currently falls inside the span is
            # laid out here.
            if _big.is_big(meta) and str(meta.get("when") or ""):
                _t0, _t1, _serr = _F.check_span(str(meta.get("when")))
                if not _serr:
                    mem = [m for m in await _F.span_members(_t0, _t1) if seen(m)]
                    lines.append(f"范围内现在有 {len(mem)} 条（**现场算的**，没记账；"
                                 f"下钻：拿下面的 id 再搜）:")
                    for mid in mem[:30]:
                        _mb, mhint, mmark = await _linked(mid, scope_view) or (None, "", "")
                        lines.append(f"  ◈ {mid}  {mhint}{mmark}")
                    if len(mem) > 30:
                        lines.append(f"  …… 还有 {len(mem) - 30} 条")
            cov = [c for c in _F.cover_ids(meta) if seen(c)]
            if cov:
                lines.append(f"盖着 {len(cov)} 条（下钻：拿下面的 id 再搜）:")
                for cid in cov[:30]:
                    cb, chint, cmark = await _linked(cid, scope_view) or (None, "", "")
                    cmeta = (cb or {}).get("metadata", {}) or {}
                    if not cb:
                        chint = "（查无此桶——可能被硬删过）"
                    # With crossing, covered_by is a list: if this gist is still on
                    # it, nothing needs marking; only when it is absent (someone
                    # edited it by hand) is the current owner marked
                    now_by = _F.covers_of(cmeta)
                    mark = ("" if (not cb or q in now_by)
                            else f"  ↑现在归 {'、'.join(now_by) or '（没人盖）'}")
                    lines.append(f"  ▣ {cid}  {chint}{cmark}{mark}")
                if len(cov) > 30:
                    lines.append(f"  …… 还有 {len(cov) - 30} 条")
            # The reverse chain: what thinking has grown out of this entry —
            # being one of another entry's sources is direct evidence of how far
            # something has been digested
            try:
                refs = [r for r in await rt.bucket_mgr.referenced_by(q) if seen(r)]
            except Exception:
                refs = []
            if refs:
                lines.append("被引用（有东西从这条长出来过）:")
                for rid in refs[:6]:
                    _rb, rhint, rmark = await _linked(rid, scope_view) or (None, "", "")
                    lines.append(f"  → {rid}  {rhint}{rmark}")
            # The body, verbatim and never cut; one that is not live says where it
            # comes from on the rule above it as well.
            lines.append("─" * 30 if not verdict.mark
                         else "─" * 12 + " 归档区里的原文 " + "─" * 12)
            lines.append(str(b.get("content") or ""))
            return "\n".join(lines)
        # id-shaped but no such bucket -> fall back to an ordinary search (it may
        # be a partial id, or the bucket may have been physically deleted)
    if not (when.strip() or room.strip() or tag.strip() or query.strip()):
        return ("recall 至少给一个门：when（时间）/ room（房间）/ tag（标签）/ query（扔词搜）。"
                "例：recall(when=\"上周\") · recall(room=\"MIND\") · recall(when=\"本月\", tag=\"Home\")")

    # 🔴 ONE fetch of the library for this whole call, and only on the path that needs it.
    #    Browsing needs it twice — once to filter down to what is being looked at, and
    #    once more for the periods, which have to be checked against the WHOLE library
    #    rather than the filtered result. Both used to fetch it themselves, so a single
    #    browse scanned everything twice and neither half could see the other doing it.
    #    A query does not need it at all: search returns its own hits.
    all_buckets = None
    if not query.strip():
        try:
            all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            rt.logger.warning(f"浏览要的全库没捞到: {e}")

    entries, err, ledger = collected if collected is not None else await _collect(
        when, room, tag, query, all_buckets)
    if err:
        return err
    _usage.offer([e["id"] for e in entries], "recall.search" if query.strip() else "recall.browse")
    if not entries:
        gates = "，".join(x for x in [when and f"when={when}", room and f"room={room}",
                                     tag and f"tag={tag}", query and f"query={query}"] if x)
        return f"这儿没有东西（{gates}）。门再开大一点试试。"

    gates = " ".join(x for x in [when and f"when={when}", room and f"room={room}",
                                 tag and f"tag={tag}", query and f"query={query}",
                                 view and f"view={view}"] if x)

    # ── 「今天」 listed entry by entry: today's events still have me inside them,
    #    so they do not collapse.
    #    ⚠️ The `by="回看"` that was cut is a different thing: that read a stretch
    #    of history oldest-to-newest (and slices=N covers it), while this views
    #    what just happened newest-to-oldest.
    async def _render_today(es: list[dict], g: str) -> str:
        # 🔴 The time of day comes from `created` (the moment it actually hit
        #    disk), never from `ts`: whenever a `when` exists, ts is **midnight on
        #    that day**, and most of today's entries carry when=今天, so listing
        #    them all as 00:00 shows nothing at all.
        #    created has no suffix, meaning UTC; parse_stamp converts it to local.
        # 🔴 **Ordering must use the same definition as the display**: sort by ts
        #    and then display created and today's events all pile up at 00:00, so
        #    the listed times come out scrambled.
        def _hm(e: dict):
            return _w.parse_stamp(e["meta"].get("created")) or e["ts"]

        lines = [f"〔{g}〕{len(es)} 条 · 今天（全列，不塌缩）· 新→旧"]
        # Covered entries are not listed individually today either; they are
        # replaced by the gist title line above.
        # **The count is still len(es)** (nothing was lost); only lines were
        # saved — which is where the "information may only grow" rule lands.
        scope_view = await read_scope()
        lines.extend(await _gist_lines(es, scope_view=scope_view))
        for e in sorted(es, key=_hm, reverse=True):
            # Covered by a gist the request may read: that gist's line stands for it.
            if _F.is_covered(e["meta"]) and (scope_view is None or any(
                    scope_view.permits_id(g) for g in _F.covers_of(e["meta"]))):
                continue
            lines.append(f"{_hm(e).strftime('%H:%M')}  {kind_badge(e['meta'])}{_label_of(e)}  "
                         f"({_short_id(e['id'])})")
        lines.append("（看原文：拿 id 搜；要昨天/上周那种概览就换 when）")
        return chr(10).join(lines)

    # ── The fork: **there is exactly one criterion, whether there is a query.**
    #    when/room/tag are a **range** (I am looking); query is a **target** (I am
    #    looking for).
    #    The score has always been available (no query gate means no score); what
    #    was missing is that the two paths produced identically shaped output.
    #    An explicit slices=N means "I want to see it by cells", takes neither
    #    path, and falls through to the older zoom below.
    if max_cells == _CELL_MAX:
        if query.strip():
            # 🔴 **The default is time plus score** (find that one thing).
            #    Scenes have to be asked for with `view="scene"` — and `when` now
            #    governs range only, so supplying it does not change the shape of
            #    the view at all (that "one parameter doing two jobs" is fixed).
            if view == "scene":
                return _render_scene_clusters(entries, gates, floor, ledger)
            return _render_search(entries, gates, floor, ledger)
        # 「今天」 never collapses: today's events still have me inside them, and
        # collapsing them into "some time ago + 2-3 representatives" pushes what
        # just happened far away.
        # 🔴 **Only 「今天」 counts** — 昨天 and 前天 collapse as before; those are
        # already history.
        if when.strip() == "今天":
            return await _render_today(entries, gates)
        return await _render_browse(entries, gates, room, tag, all_buckets)

    gname, slices = _split_cells(entries, max_cells)

    # B · 1-3 cells: the full card
    if len(slices) <= 3:
        blocks = [_fmt_card(label, _cell_stats(cell)) for label, cell in reversed(slices)]
        return f"〔{gates}〕{len(entries)} 条 · 粒度:{gname}\n\n" + "\n\n".join(blocks) + \
            "\n\n（钻：缩小 when / 加 room·tag；看原文：拿 id 搜）"

    # A · overview: two lines per cell
    lines = [f"〔{gates}〕{len(entries)} 条 · {len(slices)} 格 · 粒度:{gname} · 新→旧"]
    for label, cell in reversed(slices):
        st = _cell_stats(cell)
        lines.append(_fmt_header(label, st))
        hl = _fmt_highlights(st)
        if hl:
            lines.append(hl)
    lines.append("（钻：缩小 when / 加 room·tag；看原文：拿 id 搜）")
    return "\n".join(lines)
