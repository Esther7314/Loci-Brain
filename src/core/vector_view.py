"""
========================================
core/vector_view.py — the settings page's vector block: which memories have no vector,
why, and 「现在补」
========================================

A memory without a vector sits out semantic search, muse's semantic layer and the slice
guesses. Three reasons, each said in words (`why_words`):

  retrying    in the embedding outbox and failed at least once: how many tries, the last
              error, when it is tried next
  queued      in the outbox, not tried yet
  not_queued  the index holds no vector for it and the outbox has no item either (the
              outbox's reconcile would queue it: 「现在补」 runs that)

`missing` reads the outbox's items (EmbeddingOutbox.items_view: ids and retry state, never
content) and, when the index can be listed, the ids holding a vector. `backfill` is
「现在补」: queue what has no vector (`reconcile`), then make every pending item due now
and close the circuit (`retry_now`). Neither writes the ledger.

Exports: WHY_WORDS · missing · backfill
========================================
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from . import _when as _w
from .activity import entry_ref, index
from .paging import page, past, stamp

RETRYING, QUEUED, NOT_QUEUED = "retrying", "queued", "not_queued"
WHY_WORDS = {QUEUED: "排着队，还没轮到",
             NOT_QUEUED: "没有向量，也没排上队（按「现在补」排进去）"}
_ERROR_MAX = 80
_ORDER = {RETRYING: 0, QUEUED: 1, NOT_QUEUED: 2}


def _retrying_words(attempts: int, error: str) -> str:
    error = " ".join(str(error or "").split())[:_ERROR_MAX]
    return f"一直失败（试了 {attempts} 次：{error}）" if error else f"一直失败（试了 {attempts} 次）"


def missing(outbox_rows: Iterable[dict], buckets: Iterable[dict],
            index_ids: Optional[Iterable[str]], *, circuit: str, provider_ready: bool,
            now: datetime, offset: int, limit: int, as_of: datetime) -> dict:
    """{missing, items: [{id, short, text, why, why_words, attempts, next_try}], circuit,
    provider_ready, note, paging}. `index_ids` None = the index could not be listed, so
    only what the outbox holds is known."""
    buckets = list(buckets or [])
    lib = index(buckets)
    rows: list[tuple[int, float, dict]] = []
    queued_ids = set()
    for item in outbox_rows or []:
        bid = str(item.get("id") or "")
        if not bid:
            continue
        queued_ids.add(bid)
        at = _w.parse_stamp(item.get("queued_at"))
        if past(at, as_of):
            continue
        attempts = int(item.get("attempts") or 0)
        why = RETRYING if attempts > 0 else QUEUED
        due = float(item.get("next_attempt_at") or 0.0)
        next_try = (stamp(datetime.fromtimestamp(due, tz=_w.LOCAL_TZ))
                    if due > now.timestamp() else None)
        ref = entry_ref(lib, bid)
        rows.append((_ORDER[why], -(at.timestamp() if at else 0.0), {
            "id": bid, "short": ref["short"], "text": ref["text"], "why": why,
            "why_words": (_retrying_words(attempts, item.get("last_error"))
                          if why == RETRYING else WHY_WORDS[QUEUED]),
            "attempts": attempts, "next_try": next_try}))
    if index_ids is not None:
        have = set(str(i) for i in index_ids)
        for b in buckets:
            meta = b.get("metadata") or {}
            bid = str(b.get("id") or meta.get("id") or "")
            if (not bid or bid in have or bid in queued_ids or meta.get("deleted_at")
                    or not str(b.get("content") or "").strip()):
                continue
            ref = entry_ref(lib, bid)
            rows.append((_ORDER[NOT_QUEUED], 0.0, {
                "id": bid, "short": ref["short"], "text": ref["text"], "why": NOT_QUEUED,
                "why_words": WHY_WORDS[NOT_QUEUED], "attempts": 0, "next_try": None}))
    rows.sort(key=lambda r: (r[0], r[1], r[2]["id"]))
    items = [r[2] for r in rows]
    note = "" if provider_ready else "向量模型没开：排着的不会动，补也补不上"
    return {"missing": len(items), "circuit": circuit, "provider_ready": provider_ready,
            "note": note, **page(items, offset, limit, as_of)}


async def backfill(outbox) -> dict:
    """「现在补」: {queued: how many were newly queued, made_due: how many waiting items
    were made due now}."""
    queued = await outbox.reconcile(include_archive=True)
    made_due = outbox.retry_now()
    return {"queued": int(queued or 0), "made_due": int(made_due or 0)}
