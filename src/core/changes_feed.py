"""
========================================
core/changes_feed.py — the panel's regrow/fold page: how the library reshaped itself
========================================

One list, newest first, read from the ledger (`_ledger/events.jsonl`) and the entries as
they are now. A ledger line says which entry changed and which field names a write
touched, never what they say; the entry's current metadata says what kind of change it
was:

    revised       改了一版             a new entry whose prov has wasRevisionOf (regrow)
    merged        N 条合成一条          a new gist covering N entries (fold)
    period        圈了一段日子          a new period (core/_bigevent.is_big)
    pinned        钉上门口              a write that touched `pinned` and left it true
    invalidation  依据变了，模型怎么处理的 — an entry whose basis moved (an open
                  invalidation record, or a panel correction not yet confirmed), and
                  `how` it was dealt with:
                    rewrite  依据变了，重写了一版   a new version of it was written
                    archive  依据变了，收起来了     it was put away (TraceDeletedToArchive)
                    kept     依据变了，看过照留     its records gained `confirmed_at` on the
                             day of a write that touched `invalidation`

A new version of an entry whose basis had moved is told as `invalidation` / `rewrite`, not
also as `revised`. "Was merely touched" lines, source lines, plain creations and other
updates are not on this page.

`kept` is read off the day: the ledger line does not say whether a write touching
`invalidation` added a record or confirmed one, so a line counts when the entry carries a
record confirmed on that line's local day, and each entry is told once per day (the latest
such line).

Each row carries the ledger's `seq` as its id — the panel's alone: a host with a ceiling
never sees seq numbers (core/_ledger.py).

The page shows the last `WINDOW_DAYS` days (the owner's rule for regrow/fold): `since` cuts
the older lines off; they stay in the ledger, only not on this page.

Exports: WINDOW_DAYS · recent(events, all_buckets, *, scope=None, as_of=None, since=None)
-> list[dict]
========================================
"""

from __future__ import annotations

from datetime import datetime

from utils import WAS_REVISION_OF, read_prov

from . import _bigevent as _B
from . import _fold as _F
from . import _invalidation as _I
from . import _when as _w
from . import visibility as _V
from .paging import past

REVISED = "revised"
MERGED = "merged"
PERIOD = "period"
PINNED = "pinned"
INVALIDATION = "invalidation"

REWRITE = "rewrite"
ARCHIVE = "archive"
KEPT = "kept"

# How far back the page reaches: two weeks.
WINDOW_DAYS = 14

_INVALIDATION_WORDS = {REWRITE: "依据变了，重写了一版", ARCHIVE: "依据变了，收起来了",
                       KEPT: "依据变了，看过照留"}


def _basis_moved(meta: dict) -> bool:
    """An open invalidation record, or a panel correction not yet confirmed."""
    from .profile import _EDITED_BY_USER_TAG     # lazy: profile imports _invalidation
    if _I.open_records(meta):
        return True
    tags = [str(t) for t in (meta.get("tags") or [])]
    return _EDITED_BY_USER_TAG in tags and not _I.edit_confirmed(meta)


def _entry(bid: str, meta: dict, content: str, scope) -> dict:
    """{id, short, text}; text None when the read gate withholds it (an entry standing on a
    withdrawn or deleted source)."""
    from .profile import entry_label, short_id
    shown = _V.visible_for(meta, scope, road=_V.READ)
    return {"id": bid, "short": short_id(bid),
            "text": entry_label(meta, content) if shown else None}


def _revision_of(meta: dict) -> str:
    for line in read_prov(meta):
        if line["rel"] == WAS_REVISION_OF:
            return line["target"]
    return ""


def _payload(event: dict) -> dict:
    p = event.get("payload")
    return p if isinstance(p, dict) else {}


def _pinned_now(payload: dict) -> bool:
    """The `pinned` flag the line recorded (an old line carries the whole metadata)."""
    flags = payload.get("flags") if isinstance(payload.get("flags"), dict) else {}
    value = flags.get("pinned", payload.get("pinned"))
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def recent(events, all_buckets: list, *, scope=None, as_of: datetime | None = None,
           since: datetime | None = None) -> list[dict]:
    """The page's rows, newest first:

        {id: seq, at, kind, words, entry: {id, short, text}, how? (invalidation),
         of? (revised / rewrite: the version it replaced), count? (merged), span? (period)}

    `events` are the ledger's lines in order; `all_buckets` every entry, archive included
    (an entry put away is still named). `scope` (a core.scope.ScopeView) leaves out entries
    the request may not read; `as_of` leaves out lines recorded after it
    (core/paging.past); `since` leaves out lines recorded before it."""
    by_id = {str((b.get("metadata") or {}).get("id") or b.get("id") or ""): b
             for b in all_buckets}
    rows: list[dict] = []
    kept_seen: set[tuple[str, str]] = set()
    for event in reversed(list(events)):
        etype = str(event.get("event_type") or "")
        bid = str(event.get("trace_id") or "")
        row = by_id.get(bid)
        if row is None:
            continue
        meta = row.get("metadata") or {}
        if scope is not None and not scope.permits(meta):
            continue
        stamp = _w.parse_stamp(event.get("recorded_at"))
        if stamp is None:
            continue
        stamp = stamp.replace(microsecond=0)        # the row's `at` is to the second
        if past(stamp, as_of) or (since is not None and stamp < since):
            continue
        payload = _payload(event)
        changed = {str(f) for f in payload.get("changed_fields") or []}
        out: dict = {}
        if etype == "TraceCreated":
            prior = _revision_of(meta)
            old = ((by_id.get(prior) or {}).get("metadata") or {}) if prior else {}
            if old and _basis_moved(old):
                out = {"kind": INVALIDATION, "how": REWRITE, "of": prior}
            elif prior:
                out = {"kind": REVISED, "words": "改了一版", "of": prior}
            elif _B.is_big(meta):
                out = {"kind": PERIOD, "words": "圈了一段日子", "span": _B.fmt_span(meta) or None}
            elif _F.is_gist(meta) and _F.cover_ids(meta):
                n = len(_F.cover_ids(meta))
                out = {"kind": MERGED, "words": f"{n} 条合成一条", "count": n}
        elif etype == "TraceUpdated":
            if "pinned" in changed and _pinned_now(payload):
                out = {"kind": PINNED, "words": "钉上门口"}
            elif "invalidation" in changed:
                day = stamp.date().isoformat()
                confirmed = any(str(r.get(_I.CONFIRMED_AT) or "") == day
                                for r in _I.records(meta))
                if confirmed and (bid, day) not in kept_seen:
                    kept_seen.add((bid, day))
                    out = {"kind": INVALIDATION, "how": KEPT}
        elif etype == "TraceDeletedToArchive" and _basis_moved(meta):
            out = {"kind": INVALIDATION, "how": ARCHIVE}
        if not out:
            continue
        if out["kind"] == INVALIDATION:
            out["words"] = _INVALIDATION_WORDS[out["how"]]
        rows.append({"id": int(event.get("seq") or 0), "at": stamp.isoformat(timespec="seconds"),
                     **out, "entry": _entry(bid, meta, str(row.get("content") or ""), scope)})
    return rows
