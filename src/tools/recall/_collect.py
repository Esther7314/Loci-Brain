# -*- coding: utf-8 -*-
"""
tools/recall/_collect.py — fetching and filtering what one call looks at

_collect runs the four gates (when / room / tag / query) over the library or over the
search hits, keeps the top-k of a search after every gate, and reports what top-k cut.
A search's hits also get their roots (_find_roots): where their sources lead when
followed all the way back.

tools/recall/core.py re-exports entries_of and _collect.
"""

from datetime import datetime

from core import visibility as _V     # the one gate: what may be put in front of the model
from core import _when as _w          # "today" as the user lives it (local timezone) — never call datetime.now() directly
from core import runtime as rt
from core._rooms import check_gate, room_matches
from utils import read_from_ids
from .._common import read_scope

from ._when_words import _parse_when


# How many entries the search path accepts at most (top-k). **Tightened to 30**:
# more words in a query = an averaged vector = a poorer aim — and with a large k,
# a poor aim buries the real hit under a screenful of near-misses.
# ⚠️ The number of entries cut **must be reported** (`_collect`'s third return
#    value -> a final line at render time): quietly dropping 30 is exactly the
#    "the slots were wasted and number 61 disappeared forever" failure that
#    review complained about.
_SEARCH_TOPK = 30


# ------------------------------------------------------------
# Fetching and filtering
# ------------------------------------------------------------


def _entry(b: dict, ts: datetime, score=None, literal: bool = False, words: bool = False,
           meaning: bool = False) -> dict:
    """One store bucket in the shape the renderers read."""
    meta = b.get("metadata", {}) or {}
    return {"id": str(meta.get("id") or b.get("id") or ""), "meta": meta, "ts": ts,
            "content": str(b.get("content") or ""),
            # score exists only when the query gate ran; None = this entry came in via when/room/tag
            "score": score, "literal": literal, "words": words, "meaning": meaning}


def entries_of(rows: list) -> list[dict]:
    """Given store buckets as `_collect` hands them on (oldest first), without its gates:
    for rendering a set of entries already chosen, as the panel's breath page renders the
    近三天 entries a breath named (tools/breath/awaken.recent_card). A bucket without a
    time coordinate is left out, as `_collect` leaves it out."""
    out = []
    for b in rows:
        ts = _w.ts_of(b.get("metadata", {}) or {})
        if ts is not None:
            out.append(_entry(b, ts))
    out.sort(key=lambda x: x["ts"])
    return out


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
    # the top _SEARCH_TOPK by relevance. (Truncating before filtering would waste
    # the slots on hits that fail the gates, and the qualifying memory at rank k+1
    # would disappear forever.)
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
        # Not exact equality: tags are never complete (「床」 can appear in 16 bodies
        # and make it into the tags of only one), so equality matching would drop
        # half of an already sparse signal.
        if tag and not any(tag in str(t) for t in (meta.get("tags") or [])):
            continue
        ts = _w.ts_of(meta)
        if ts is None:
            continue
        if t0 and ts < t0:
            continue
        if t1 and ts >= t1:
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        words, meaning = how.get(bid, (False, False))
        out.append(_entry(b, ts, score=scores.get(bid), literal=bid in literals,
                          words=words, meaning=meaning))
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
