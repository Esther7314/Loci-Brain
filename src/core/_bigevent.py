# -*- coding: utf-8 -*-
"""
tools/_bigevent.py — the big event: one sentence laid over a stretch of time.

**What it is**: saying "what were we doing back then".

Nine rules, each with a reason behind it. Do not quietly change them.
⚠️ Rules 5/6/7/9 were revised when fold shipped — the revision is about *how* they are
implemented; not one of the nine changed meaning:

1. **It is an event, not a mind.** Summarise a pile of events and the result is still an
   event, just at a coarser grain.
   ⚠️ There was a slip here once: wanting it to be regrow-able (regrow was MIND-only),
   the reasoning ran "then it must be an insight" — wrong. **A big event does not move
   to MIND.** If it needs regrow, open regrow up to it; do not relocate it to another
   room to get a feature.
2. **Past tense.** A want faces forward (has not happened yet); a big event faces back
   (you can only write it while looking back).
3. **Start and end are filled in together, once.** With "record `when` at the start,
   `trace` it closed at the end", the second step gets forgotten just the same; and by
   the time you are looking back you already know both ends anyway. So:
   `when="2026-07-31..2026-08-05"`, with the end left empty while it is still going
   (`"2026-07-31.."`).
   ⚠️ **No new field**: the span goes into the `when` that already exists. Reach for an
   existing mechanism before inventing a new one.
4. **Never mandatory.** Use it when there is one, fall back to the plain view when there
   is not. That is what stops it from ever becoming an upkeep chore — and that is the
   whole reason it holds up.
5. **Cover, do not delete.** (fold revision) Counts lose nothing, search still hits,
   drilling down always reaches it; what is covered simply stops taking its own line —
   **a table of ingredients replaced by a sentence**. Information may only increase,
   never decrease.
6. **Periods run in parallel and may cross.** They are not a relay: "working on that
   project", "reshaping the memory system" and "moving house" are overlapping periods.
   One small memory may be covered by two big events at once — that is not a conflict,
   it is the truth. (fold revision: `covered_by` is a **list**. Explicit covering
   stacks; nobody evicts anybody. The first implementation made it a single value and
   that was caught and reverted.)
7. **Loose coupling: cover by time range, so a small memory never needs to know which
   period it belongs to.** An intermediate design had periods resolve their members into
   a frozen list of ids; that was reverted back to the original: draw a circle and write
   a name on it. A period stores only its name and its `when` range — no cover /
   covered_by, no suppression of anything — and who falls inside it is computed from
   dates at read time. Backfilled entries join their period automatically, crossing and
   nesting work by construction, and moving a boundary is just a regrow of `when`. The
   frozen-id-list version was borrowed from consolidation/ACP, where compression
   *replaces* content and therefore has to keep books; **here nothing is ever deleted,
   things are only given names**, so that borrowed half was sent back. Snapshot
   bookkeeping belongs to mind merges and nowhere else.
8. **Change versions with regrow**, old versions stay on file -> the evolution of a
   through-line comes for free, and the moment of a version change *is* the milestone,
   so no separate "milestone" concept has to be designed. (Since fold, regrow is just
   fold with n=1; the behaviour did not change.)
9. **The trigger hangs off recalling a stretch of time**: at that moment you are already
   looking back with the material spread out in front of you, so "there seems to have
   been one thing going on here" surfaces by itself and does not have to be remembered
   as a chore. (fold revision: there is now a second trigger — muse reports "this
   stretch of days has N entries and no name yet". It only points at the gap; **the
   naming sentence is still written by hand**.)

**Why expiry is needed** (a disease found in the earlier version): the old line was
hand-written with no expiry mechanism at all, so the awakening screen kept showing "for
the last few days it has all been about X" **two days after that stopped being true —
and it was read as fact every single time**. (The same flaw as a profile fact box: the
more something looks like objective information, the less it gets questioned.) Now the
span lives in `when`, `covering()` works from real time, and anything expired simply
stops appearing.

**It does not appear in breath**: the awakening is for things that surface on their own,
whereas "what have I been doing lately" is something you **look up**; popping it into
the subconscious feels wrong. It only lays itself over a recall of a stretch of time.

Exports: `BIGEVENT_TAG` · `parse_span()` · `fmt_span()` · `covering()` · `first_line()`
"""

import re
from datetime import datetime, timedelta

from tools import _runtime as rt
from . import _when as _w

BIGEVENT_TAG = "__大event__"

# How many may cover a single recall of a stretch of time. Three rather than one:
# "moving house" and "reshaping how memory works" were two through-lines running at
# once, and forcing them into a single sentence loses something (rule 6: parallel,
# and allowed to cross).
COVER_MAX = 3

# The span lives inside `when`: `start..end`, with an empty end meaning still ongoing
SPAN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})?$")


def parse_span(meta: dict) -> tuple[datetime | None, datetime | None]:
    """(start, end). The end is **exclusive** (a day has already been added);
    None means still ongoing.

    An old bucket — the hand-written one predating this format — has a single date or
    nothing in `when`, so the start falls back to when/created and the end is left
    empty, i.e. treated as ongoing.
    """
    w = str(meta.get("when") or "").strip()
    m = SPAN_RE.match(w)
    if m:
        # `SPAN_RE` validates the **shape** only, not that the day actually exists.
        # Called bare, one period bucket holding `when="2026-13-45.."` would **take down
        # period coverage for an entire recall** — not "one cover missing", the whole pass
        # (parse_span runs inside `covering()`'s loop, outside any try).
        # If it slips through here, treat it as not written correctly and drop to the
        # old-bucket fallback below: the same treatment as an empty `when`.
        start = _w.parse_date_or_none(m.group(1))
        end_raw = _w.parse_date_or_none(m.group(2)) if m.group(2) else None
        if start is not None and (m.group(2) is None or end_raw is not None):
            return start, (end_raw + timedelta(days=1)) if end_raw else None
    return _w.parse_stamp(w) or _w.parse_stamp(meta.get("created")), None


def fmt_span(meta: dict) -> str:
    """The span, for human eyes: `7-31 起` / `7-31~8-05`."""
    w = str(meta.get("when") or "").strip()
    m = SPAN_RE.match(w)

    def _short(d: str) -> str:
        # A bare `int()` would raise on `when="2026-7-31..2026-8-5"` — single-digit
        # months, **very easy to produce by hand** — while `parse_span` does not raise
        # on the very same input: the compute layer would survive and the display layer
        # blow up. The rule: this cell would rather be empty. An empty cell is visible; an
        # exception takes the whole page with it.
        try:
            return f"{int(d[5:7])}-{int(d[8:10])}"
        except (ValueError, TypeError, IndexError):
            return ""

    # 🔴 When `_short` gives up it returns an empty string, and this line has to drop the
    #    whole span with it. Otherwise it prints a lone ` 起` — **an `起` with no date in
    #    front of it is harder to notice than an exception**, because it just looks like
    #    the way the page is supposed to be. (Walked straight into this while adding the
    #    try above.)
    start = _short(m.group(1)) if m else _short(w[:10]) if len(w) >= 10 else ""
    if not start:
        return ""
    if m and m.group(2):
        end = _short(m.group(2))
        return f"{start}~{end}" if end else f"{start} 起"
    return f"{start} 起"


def is_big(meta: dict) -> bool:
    return BIGEVENT_TAG in [str(t) for t in (meta.get("tags") or [])]


def _usable(meta: dict) -> bool:
    """Superseded versions, **anything covered by a higher layer**, closed ones and
    archived ones all fail to count.

    `covered_by` was added later: a big event can itself be covered (recursively, with
    no fixed number of layers), and a covered layer should not surface on its own over a
    recalled stretch of time — show the topmost layer, and keep the ones underneath
    reachable by drilling down. Archived or deleted is the gate's `state_of`.
    """
    from ._fold import is_covered   # _fold imports this module
    from .visibility import LIVE, state_of   # the gate imports _fold, which imports this module
    if is_covered(meta) or state_of(meta) != LIVE:
        return False
    return str(meta.get("status") or "") not in ("resolved", "abandoned")


def first_line(content: str) -> str:
    """The sentence is the first line of the body. The lines after it are left for
    footnotes such as the range or how the boundary was drawn."""
    body = content.strip()
    return body.splitlines()[0] if body else ""


def covering(buckets: list, t0: datetime | None, t1: datetime | None,
             limit: int = COVER_MAX) -> list[tuple[dict, str, str]]:
    """Big events that **overlap** [t0, t1), newest first. **The buckets are passed in
    by the caller.**

    Overlap, not containment — rule 6: these are overlapping periods, not a relay race.
    Both ends empty (no time filter) means the question is "right now", which is the set
    whose end has not arrived yet.

    🔴 The caller hands the list in; this function never fetches it. Two reasons:

       ① **Repeat scans stay visible.** The browse view calls this once per cell, per
          day — a single recall can call it seven or eight times. Fetching here would
          rescan the whole store on every call, and **the caller could not see itself
          doing it**. With the list handed down from above, the caller can see at a
          glance how many times it fetched it.
       ② **It is unit-testable.** No global runtime carrying the bucket manager, the
          decay engine, vectors, logging and config is needed; you pass in a list.

    ⚠️ It is not async: it touches neither disk nor network, it is pure computation.
       (An `await` on it yields a coroutine where a list is expected, which fails loudly
       on the spot rather than silently.)
    """
    now = _w.now()
    out = []
    for b in buckets:
        meta = b.get("metadata", {}) or {}
        if not (is_big(meta) and _usable(meta)):
            continue
        s, e = parse_span(meta)
        if s is None:
            continue
        if t0 is None and t1 is None:
            if e is not None and e <= now:
                continue          # a through-line already over: it should not surface when the question is "now"
        else:
            if t1 is not None and s >= t1:
                continue
            if t0 is not None and e is not None and e <= t0:
                continue
        out.append((meta, str(b.get("content") or ""),
                    str(meta.get("id") or b.get("id") or "")))
    out.sort(key=lambda t: str(t[0].get("when") or ""), reverse=True)
    return out[:limit]
