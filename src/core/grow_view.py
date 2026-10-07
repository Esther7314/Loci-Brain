"""
========================================
core/grow_view.py — grow's page on the panel: what was written today, and the slices
waiting to be written
========================================

  written_since  the memories written since a moment, newest first: when, the label, and
                 the human tags (core/detail.human_tags) — what grow's page lists as
                 「今天记住的」.
  day_cut        where that page's "today" starts: the moment the caller hands over (a
                 host knows its own daily report), else the latest daily report a host's
                 slices batch carried (`report_at`) — both `since_from: "report"` — else
                 local midnight (`since_from: "midnight"`).
  slice_batches  every batch of slices the pending store holds — the open ones and those
                 already handled or replaced — newest first, each slice with its state
                 and the guesses at or above the guess line that it was already recorded
                 (「好像已经记过」). Read only: the panel never handles a slice for the model.

A batch's label: an imported conversation's is 「来自导入 · <its title>」; a host's is
「<the host registering its lines> · N 段」 (core/scope.Hosts.registrar_for), or the
source's system when no host registers it.

Exports: SINCE_REPORT · SINCE_MIDNIGHT · SLICE_STATE_WORDS · day_cut · written_since ·
         slice_batches
========================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from . import _slicer as _sl
from . import _when as _w
from . import detail as _detail
from . import visibility as _V
from .activity import entry_ref, index
from .paging import page, past, stamp
from .profile import entry_label, short_id

SINCE_REPORT, SINCE_MIDNIGHT = "report", "midnight"

# (state, how) -> what a person reads in the slice's state column. An open slice of an
# imported conversation carries a draft the model is to check, hence its own words.
SLICE_STATE_WORDS = {
    (_sl.OPEN, ""): "等他看",
    (_sl.OPEN, "import"): "等他核",
    (_sl.CLOSED, "grow"): "写成记忆了",
    (_sl.CLOSED, "trace"): "补进已有的了",
    (_sl.CLOSED, "drop"): "他跳过了",
    (_sl.REPLACED, ""): "重切过",
}


# ── today ────────────────────────────────────────────────────────────────────

def day_cut(now: datetime, given=None, *, pending=None,
            host: str | None = None) -> tuple[datetime, str]:
    """(where today starts, what it was cut by), the one place grow's "today" is decided:

      1. the moment the caller hands over (`given`: a host's page knows its own daily
         report) -> SINCE_REPORT;
      2. else the latest daily report a slices batch carried (`report_at` on POST
         /api/v2/slices, `pending` the store's PendingSlices.last_report), not later
         than `now` -> SINCE_REPORT. Whose: `host`'s batches when a name is given; with
         None, any host's — the panel reads the whole library, so its "today" starts at
         the most recent report any host wrote, and `?host=` narrows it to one;
      3. else local midnight -> SINCE_MIDNIGHT."""
    cut = _w.parse_stamp(given) if given else None
    if cut is not None:
        return cut, SINCE_REPORT
    reported = pending.last_report(host, not_after=now) if pending is not None else None
    if reported is not None:
        return reported[0], SINCE_REPORT
    return _w.to_local(now).replace(hour=0, minute=0, second=0, microsecond=0), SINCE_MIDNIGHT


def _tags_human(meta: dict) -> list[dict]:
    """The row of human tags the detail window shows (core/detail.human_tags)."""
    return list(_detail.human_tags(meta))


def written_since(buckets: Iterable[dict], since: datetime, *, now: datetime, offset: int,
                  limit: int, as_of: datetime, scope=None) -> dict:
    """The memories on the timeline written at or after `since` (by `created`), newest
    first: {id, short, at, text, tags_human}."""
    rows: list[tuple[datetime, dict]] = []
    for b in buckets or []:
        meta = b.get("metadata") or {}
        if not _V.on_timeline(meta, scope):
            continue
        at = _w.parse_stamp(meta.get("created"))
        if at is None or at < since or past(at, as_of):
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        rows.append((at, {"id": bid, "short": short_id(bid), "at": stamp(at),
                          "text": entry_label(meta, str(b.get("content") or "")),
                          "tags_human": _tags_human(meta)}))
    rows.sort(key=lambda r: (r[0], r[1]["id"]), reverse=True)
    return page([r for _at, r in rows], offset, limit, as_of)


# ── the slices ───────────────────────────────────────────────────────────────

def _batch_label(batch: dict, hosts, n: int) -> str:
    origin = batch.get("import")
    if origin:
        return "来自导入 · " + (str(origin.get("title") or "").strip() or "没有标题")
    source = batch.get("source") or {}
    host = None
    if hosts is not None:
        try:
            host = hosts.registrar_for(source)
        except Exception:          # a source the registry cannot read: say its system
            host = None
    name = host.name if host is not None else str(source.get("system") or "宿主")
    return f"{name} · {n} 段"


def _slice_line(s: dict, lib: dict, threshold: float, imported: bool) -> dict:
    closed = s.get("closed") or {}
    how = str(closed.get("how") or "") if s["state"] == _sl.CLOSED else ""
    words_key = (s["state"], how if s["state"] == _sl.CLOSED
                 else ("import" if imported and s["state"] == _sl.OPEN else ""))
    guesses = []
    for g in s.get("guesses") or []:
        try:
            score = float(g.get("score"))
        except (TypeError, ValueError):
            continue
        if score < threshold:
            continue
        ref = entry_ref(lib, str(g.get("id") or ""))
        guesses.append({"id": ref["id"], "short": ref["short"], "text": ref["text"],
                        "score": round(score, 3)})
    out = {"slice_id": s["slice_id"], "gist": s.get("gist") or "", "span": s.get("span"),
           "guesses": guesses, "state": s["state"], "how": how or None,
           "by": list(closed.get("by") or []),
           "state_words": SLICE_STATE_WORDS.get(words_key, "")}
    if s.get("draft"):
        out["draft"] = s["draft"]
    if s.get("edited"):
        out["edited"] = True
    return out


def slice_batches(pending, buckets: Iterable[dict], *, hosts, threshold: float,
                  offset: int, limit: int, as_of: datetime) -> dict:
    """Every batch the pending store holds, newest first, paged by batch:
    {batch_id, day, label, import, recorded_at, slices: [...]}."""
    lib = index(buckets)
    rows = []
    for b in pending.batches(include_closed=True):
        at = _w.parse_stamp(b.get("recorded_at"))
        if past(at, as_of):
            continue
        imported = bool(b.get("import"))
        slices = [_slice_line(s, lib, threshold, imported) for s in b["slices"]]
        current = sum(1 for s in slices if s["state"] != _sl.REPLACED)
        rows.append({"batch_id": b["batch_id"], "day": b.get("day"),
                     "label": _batch_label(b, hosts, current),
                     "import": b.get("import"), "recorded_at": stamp(at),
                     "slices": slices})
    return page(rows, offset, limit, as_of)
