"""
========================================
core/_holds.py — a hold: a short exception hung on a standing agreement
========================================

A hold is a second, shorter telic entry that points at the standing one by
`exception_of: <id>` and says how far the exception reaches with `hold`:

  defer  "don't push me on it for now" — the thing stands, it just stops nudging.
         Putting something aside myself is the same hold.
  avoid  "don't touch it at all" — rarer and heavier: it also leaves my dreams.

The original entry is never edited. While a hold is live the two belong together (the
read side shows them side by side); closing the hold brings the original back, and
closing the original leaves the hold alone (it just stops mattering).

How a hold ends:
  by date   its `when` — one day ("until then") or a closed span ("from .. to"); past
            the last day it no longer holds. Read off the calendar on every read
            (`hold_is_live`), and the decay sweep closes it on disk with
            `closed_by: "expired"` so it can age like anything else.
  by event  it carries a `cue`; nothing lifts on an event here — that needs the model
            to confirm on a card.
  a review  a `defer` with no date gets `review_after` (today + the host's
            `surfacing.hold_review_days`) at creation: a day to ask "still aside?",
            not an end. It is its own field so a dated hold (ends) and a defaulted one
            (asks) are told apart on disk.
  by hand   `avoid` has no review and no end unless a date was given: only the owner
            lifts it, by closing it with trace.

Which roads a live hold closes is decided by the callers (breath's three blocks in
core/profile.py; dream material in core/_dream.py for avoid); recall never asks.

Exports: HOLD_LEVELS · CLOSED_BY_EXPIRY · DEFAULT_REVIEW_DAYS · is_hold · hold_span ·
         hold_end · hold_is_live · hold_expired · check_hold_when · review_days ·
         review_after_for · hold_index · holds_on · is_held
========================================
"""

from datetime import date, datetime, timedelta

from utils import is_closed

from ._bigevent import SPAN_RE
from ._when import parse_date_or_none

# Weakest first: is_held reports the strongest live level on an entry.
HOLD_LEVELS = ("defer", "avoid")
_STRENGTH = {level: rank for rank, level in enumerate(HOLD_LEVELS, start=1)}

# What a hold closed by its own date says in closed_by, next to "user" (closed by a
# person on the panel).
CLOSED_BY_EXPIRY = "expired"

# The host's default length of "for now", in days, when the config does not say. It is
# the host's setting, not the agreement's: one owner's "for now" is a week, another's a
# fortnight. 0 = never ask.
DEFAULT_REVIEW_DAYS = 7

# How far back a version chain is followed to find the agreement a hold was hung on.
_CHAIN_MAX = 64


def _meta_of(row) -> dict:
    """Accept what the callers hold: a store bucket {"metadata": …}, a (meta, text) pair
    from the muse/dream loader, or a bare meta."""
    if isinstance(row, tuple):
        row = row[0] if row else {}
    if isinstance(row, dict) and isinstance(row.get("metadata"), dict):
        return row["metadata"]
    return row if isinstance(row, dict) else {}


def is_hold(meta) -> bool:
    """Is this entry itself a hold? (both halves present: the target and the level)"""
    m = _meta_of(meta)
    return bool(str(m.get("exception_of") or "").strip()) and m.get("hold") in HOLD_LEVELS


def _day(text: str) -> date | None:
    d = parse_date_or_none(text)
    return d.date() if d else None


def hold_span(meta) -> tuple[date | None, date | None]:
    """(first day, last day) a hold's `when` covers. One day means "until then", so it
    has no first day; an open or unreadable `when` has no last day either, and such a
    hold lasts until it is closed — a hold never lifts on a date it cannot read."""
    w = str(_meta_of(meta).get("when") or "").strip()
    if not w:
        return None, None
    m = SPAN_RE.match(w)
    if m:
        return _day(m.group(1)), (_day(m.group(2)) if m.group(2) else None)
    return None, _day(w[:10]) if len(w) >= 10 else None


def hold_end(meta) -> date | None:
    """The last day a hold holds, or None when nothing on the calendar ends it."""
    return hold_span(meta)[1]


def _standing(m: dict) -> bool:
    """Open and current: not closed, not replaced by a newer version, not archived."""
    if is_closed(m) or m.get("superseded_by") or m.get("deleted_at"):
        return False
    return str(m.get("type") or "") != "archived"


def hold_is_live(meta, now: datetime) -> bool:
    """Does this hold hold today? Open status, and today inside its days when it has a
    `when`. Before the first day of a span it does not hold yet."""
    m = _meta_of(meta)
    if not is_hold(m) or not _standing(m):
        return False
    first, last = hold_span(m)
    today = now.date()
    if first and today < first:
        return False
    return not (last and today > last)


def hold_expired(meta, now: datetime) -> bool:
    """A hold still open on disk whose last day has passed: the sweep closes it."""
    m = _meta_of(meta)
    if not is_hold(m) or not _standing(m):
        return False
    last = hold_end(m)
    return bool(last and now.date() > last)


def check_hold_when(when: str) -> str:
    """The refusal for a hold's `when`, or "" when it can be stored: one real day, or a
    closed span of real days that does not run backwards."""
    w = str(when or "").strip()
    m = SPAN_RE.match(w)
    if m:
        if not m.group(2):
            return ('条子的 when 要有止：「到哪天」写 "2026-10-11"，'
                    '「从哪天到哪天」写 "2026-10-06..2026-10-11"。没日子就不填，到时候会问你。')
        first, last = _day(m.group(1)), _day(m.group(2))
        if first is None or last is None:
            return f"日历上没有这一天：{w}。"
        if last < first:
            return f"条子的止早于起：{w}。"
        return ""
    if len(w) == 10 and _day(w):
        return ""
    return ('条子的 when 是到哪天（"2026-10-11"）或从哪天到哪天（"2026-10-06..2026-10-11"）；'
            '"3w" 这种时长不收——日子写死，到点就自己放下。')


def review_days(config) -> int:
    """`surfacing.hold_review_days` from the host's config; DEFAULT_REVIEW_DAYS when it
    is absent or unreadable, never below 0."""
    raw = ((config or {}).get("surfacing") or {}).get("hold_review_days", DEFAULT_REVIEW_DAYS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_REVIEW_DAYS


def review_after_for(level: str, when: str, now: datetime, config) -> str:
    """The `review_after` a new hold is written with: only a `defer` without a date gets
    one (a date already ends it; `avoid` is never asked about). "" = none."""
    if level != "defer" or str(when or "").strip():
        return ""
    days = review_days(config)
    if days <= 0:
        return ""
    return (now.date() + timedelta(days=days)).isoformat()


class HoldIndex:
    """One pass over the store: every open hold by the id it is hung on, and each
    entry's predecessor (`supersedes`), so a hold hung on one wording of an agreement
    keeps holding after the agreement is reworded."""

    __slots__ = ("by_target", "prior")

    def __init__(self) -> None:
        self.by_target: dict[str, list[dict]] = {}
        self.prior: dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(self.by_target)


def hold_index(rows) -> HoldIndex:
    """Build the index from buckets, (meta, text) pairs or metas. Closed and replaced
    holds are left out; whether a dated one holds today is asked on read."""
    idx = HoldIndex()
    for row in rows or []:
        m = _meta_of(row)
        bid = str(m.get("id") or "")
        prev = str(m.get("supersedes") or "").strip()
        if bid and prev:
            idx.prior[bid] = prev
        if is_hold(m) and _standing(m):
            idx.by_target.setdefault(str(m["exception_of"]).strip(), []).append(m)
    return idx


def _chain(target, idx: HoldIndex) -> list[str]:
    """The target's id and the ids of its earlier versions, newest first."""
    if isinstance(target, str):
        bid = target.strip()
    else:
        bid = str(_meta_of(target).get("id") or "")
    out: list[str] = []
    while bid and bid not in out and len(out) < _CHAIN_MAX:
        out.append(bid)
        bid = idx.prior.get(bid, "")
    return out


def holds_on(target, now: datetime, idx: HoldIndex) -> list[dict]:
    """The holds live today on an entry (id or meta), any version of it."""
    if not idx:
        return []
    return [h for bid in _chain(target, idx) for h in idx.by_target.get(bid, ())
            if hold_is_live(h, now)]


def is_held(target, now: datetime, idx: HoldIndex) -> str | None:
    """The strongest hold live today on an entry: None, "defer" or "avoid"."""
    live = holds_on(target, now, idx)
    if not live:
        return None
    return max((h["hold"] for h in live), key=_STRENGTH.__getitem__)
