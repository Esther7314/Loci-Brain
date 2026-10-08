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
                 A slice whose source the registry reads as withdrawn, deleted or held
                 shows nothing it was cut from.
  slice_source   one slice's 原话, as the detail window's 来源 layer lists a source: its
                 source record with the registry's state and whether the original can be
                 asked for; the lines themselves come from core/detail.fetched over
                 `slice_record` (a host's asked of the host, an import's from Loci's own
                 copy), only when the panel asks.

A batch's label: an imported conversation's is 「来自导入 · <its title>」; a host's is
「<the host registering its lines> · N 段」 (core/scope.Hosts.registrar_for), or the
source's system when no host registers it.

Exports: SINCE_REPORT · SINCE_MIDNIGHT · SLICE_STATE_WORDS · day_cut · written_since ·
         slice_batches · slice_record · slice_source
========================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from . import _slicer as _sl
from . import _when as _w
from . import detail as _detail
from . import runtime as rt
from . import scope as _scope
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
    first: {id, short, at, text, tags_human}, under the request's read scope `scope`.
    None is the whole library, still read against the source registry
    (core/scope.whole_library_view): nothing standing on a source it says is withdrawn,
    deleted or held is listed, before the change's records reach the memories too."""
    buckets = list(buckets or [])
    if scope is None:
        scope = _scope.whole_library_view(getattr(rt.bucket_mgr, "sources", None), buckets)
    rows: list[tuple[datetime, dict]] = []
    for b in buckets:
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
        return _from_import(origin)
    return f"{_host_name(batch, hosts)} · {n} 段"


def _from_import(origin: dict) -> str:
    return "来自导入 · " + (str(origin.get("title") or "").strip() or "没有标题")


def _host_name(batch: dict, hosts) -> str:
    """The host registering the batch's lines, or the source's system."""
    source = batch.get("source") or {}
    host = None
    if hosts is not None:
        try:
            host = hosts.registrar_for(source)
        except Exception:          # a source the registry cannot read: say its system
            host = None
    return host.name if host is not None else str(source.get("system") or "宿主")


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


def _source_blocked(pending, registry, slice_id: str) -> str:
    """The registry's state of the slice's source when it is one under which nothing of
    it is shown (withdrawn, deleted, held); "" otherwise or without a registry."""
    if registry is None:
        return ""
    record = slice_record(pending, slice_id)
    if record is None:
        return ""
    state = registry.read_state(record)
    return state if state in _detail.BLOCKED_STATES else ""


def slice_batches(pending, buckets: Iterable[dict], *, hosts, threshold: float,
                  offset: int, limit: int, as_of: datetime, registry=None) -> dict:
    """Every batch the pending store holds, newest first, paged by batch:
    {batch_id, day, label, import, recorded_at, slices: [...]}. A slice whose source the
    registry reads as withdrawn, deleted or held shows no gist or draft, and says so in
    `source_words`."""
    lib = index(buckets)
    rows = []
    for b in pending.batches(include_closed=True):
        at = _w.parse_stamp(b.get("recorded_at"))
        if past(at, as_of):
            continue
        imported = bool(b.get("import"))
        slices = [_slice_line(s, lib, threshold, imported) for s in b["slices"]]
        for line in slices:
            gone = _source_blocked(pending, registry, line["slice_id"])
            if gone:
                line["gist"] = ""
                line.pop("draft", None)
                line["source_words"] = _detail.source_state_words(gone)
        current = sum(1 for s in slices if s["state"] != _sl.REPLACED)
        rows.append({"batch_id": b["batch_id"], "day": b.get("day"),
                     "label": _batch_label(b, hosts, current),
                     "import": b.get("import"), "recorded_at": stamp(at),
                     "slices": slices})
    return page(rows, offset, limit, as_of)


def _find_slice(pending, slice_id: str) -> tuple[dict, dict] | None:
    sid = str(slice_id or "").strip()
    for b in pending.batches(include_closed=True):
        for s in b["slices"]:
            if s["slice_id"] == sid:
                return b, s
    return None


def slice_record(pending, slice_id: str) -> dict | None:
    """The one source record the slice stands for now (PendingSlices.record_for), or None
    when it is unknown or its span no longer reads against its batch's lines (a slice a
    resend replaced)."""
    if _find_slice(pending, slice_id) is None:
        return None
    try:
        return pending.record_for(slice_id)
    except (KeyError, IndexError):
        return None


def slice_source(pending, slice_id: str, *, registry, hosts, label_hosts=None) -> dict | None:
    """The 原话 of one slice, as the detail window's 来源 layer lists a source: the slice
    (what it says, 第 a–b 行, its state) and `original` — its source record as
    core/detail.original_row gives it (host, span, registry state in words, `can_fetch`).
    None when the slice is unknown.

    Loci keeps no text of a host's lines (core/_slicer.py): their original is asked of the
    host serving them, line by line, only when the panel asks (`?fetch=1`, core/detail.
    fetched), and nothing of it is kept. An imported conversation's lines Loci holds
    itself. When the registry reads the source as withdrawn, deleted or held, nothing
    the slice was cut from is shown — its gist and draft included — and `original` says
    why; `original` is None for a slice whose lines are no longer there (replaced)."""
    found = _find_slice(pending, slice_id)
    if found is None:
        return None
    batch, s = found
    imported = bool(batch.get("import"))
    record = slice_record(pending, slice_id)
    original = None
    if record is not None:
        original = _detail.original_row(0, record, {"created": batch.get("recorded_at")},
                                        registry=registry, hosts=hosts)
        original["at"] = batch.get("day") or original["at"]
    blocked = original is not None and original["state"] in _detail.BLOCKED_STATES
    line = _slice_line(s, {}, 1.0, imported)
    out = {"slice_id": line["slice_id"], "batch_id": batch["batch_id"],
           "day": batch.get("day"),
           "label": _from_import(batch["import"]) if imported else _host_name(batch, label_hosts),
           "import": batch.get("import"),
           "gist": "" if blocked else line["gist"], "span": line["span"],
           "state": line["state"], "how": line["how"], "by": line["by"],
           "state_words": line["state_words"], "original": original}
    if line.get("draft") and not blocked:
        out["draft"] = line["draft"]
    if blocked:
        out["source_words"] = original["state_words"]
    return out
