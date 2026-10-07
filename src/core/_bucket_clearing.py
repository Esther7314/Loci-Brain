"""
========================================
_bucket_clearing.py — what a withdrawn or deleted source leaves of a memory
========================================

clear_body() empties a memory whose source was withdrawn or deleted, and the two
record writers append and close the `invalidation` records a source change leaves on
the memories that stand on it. core/_source_change.py is the caller; the vector, the
sunk original and the rest are its own steps.

BucketManager (core/bucket_manager.py) inherits ClearingMixin.
========================================
"""

import os
from typing import Optional

import frontmatter

from utils import PROV_FIELD
from utils import atomic_write_text as _atomic_write_text
from . import _ledger


class ClearingMixin:
    """clear_body() and the invalidation records a source change writes."""

    # ---------------------------------------------------------
    # Clearing a body: what a withdrawn or deleted source leaves of a memory
    # ---------------------------------------------------------
    # The frontmatter a cleared memory keeps: ids, structure, numbers and its sources, so
    # what stood on it, what it was derived from and the 依据变了的 block still read. Every
    # value of text — name, summary, aliases, tags, subjects, `when`, meanings, cues, media
    # references — goes, and so does the name in the file name.
    _CLEARED_KEEPS = ("id", "type", "room", "created", "last_active", "activation_count",
                      "importance", "valence", "arousal", "domain", "status", "pinned",
                      "protected", "sources", PROV_FIELD, "invalidation", "supersedes",
                      "superseded_by", "cover", "covered_by", "deleted_at", "tombstone",
                      "tombstoned_at", "source_tool", "grow_batch_id", "provenance",
                      "direction_of_fit")
    CLEARED_BODY = "〔这条依据的来源被撤回或删除，正文已经清掉了。〕"
    # A cleared memory's file name: the name it had is text too.
    CLEARED_STEM = "已清"

    async def clear_body(self, bucket_id: str, gone: dict) -> Optional[dict]:
        """Clear a memory's text after a source behind it was withdrawn or deleted, archive
        or not: the body becomes `CLEARED_BODY`, the frontmatter keeps `_CLEARED_KEEPS`
        plus the open `source_gone` record `gone`, and the file is renamed to
        `已清_<id>.md`.
        Returns {"texts": what was there, "cleared": True} when it cleared it now,
        {"texts": [], "cleared": False} when it had been cleared already, None when there
        is no such memory. The vector, the sunk original and the rest are the caller's
        (core/_source_change.py), each a place of its own."""
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return None
            post = frontmatter.load(file_path)
            records = [r for r in (post.get("invalidation") or []) if isinstance(r, dict)]
            already = (post.content or "").strip() == self.CLEARED_BODY
            mine = [r for r in records if r.get("kind") == gone.get("kind")
                    and r.get("change") == gone.get("change")]
            target = os.path.join(os.path.dirname(file_path),
                                  f"{self.CLEARED_STEM}_{bucket_id}.md")
            if already and mine and self._same_path(file_path, target):
                return {"texts": [], "cleared": False}
            texts = [str(post.content or "")]
            texts += [str(post.get(k) or "") for k in ("name", "summary")]
            texts += [str(x) for k in ("aliases", "tags", "subjects")
                      for x in (post.get(k) or []) if isinstance(x, str)]
            kept = {k: post.metadata[k] for k in self._CLEARED_KEEPS if k in post.metadata}
            if not mine:
                records.append(dict(gone))
            kept["invalidation"] = self._normalize_invalidation(records)
            cleared = frontmatter.Post(self.CLEARED_BODY, **kept)
            serialized = frontmatter.dumps(cleared)
            if self._same_path(file_path, target):
                _atomic_write_text(file_path, serialized)
            else:
                _atomic_write_text(target, serialized)
                os.remove(file_path)
                self._path_index_moved(file_path, target, bucket_id)
            if self.embedding_outbox is not None:
                try:
                    self.embedding_outbox.discard(bucket_id)
                except Exception:
                    pass
            self._invalidate_bm25()
            self._record_ledger_event(
                _ledger.TRACE_CLEARED, bucket_id, str(kept.get("type") or "dynamic"),
                self.CLEARED_BODY, kept,
                {"cleared": True, "change_id": gone.get("change", ""),
                 "source": gone.get("of", "")})
            return {"texts": [t for t in texts if t.strip()], "cleared": True}

    def as_cleared(self, bucket: dict) -> dict:
        """A bucket as `clear_body` leaves it — the body `CLEARED_BODY`, the metadata only
        `_CLEARED_KEEPS`, no file path (its name is text too) — without writing anything:
        how a read shows a memory whose clearing is due but has not reached it yet."""
        meta = bucket.get("metadata") or {}
        kept = {k: meta[k] for k in self._CLEARED_KEEPS if k in meta}
        out = {k: v for k, v in bucket.items() if k not in ("path", "metadata", "content")}
        return {**out, "metadata": kept, "content": self.CLEARED_BODY}

    async def add_invalidation_record(self, bucket_id: str, record: dict) -> bool:
        """Append one invalidation record unless one with the same kind and change is
        already there, archive or not (a terminal memory still has to say a source behind
        it is gone). True when the memory carries it afterwards."""
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False
            post = frontmatter.load(file_path)
            records = [r for r in (post.get("invalidation") or []) if isinstance(r, dict)]
            if any(r.get("kind") == record.get("kind") and r.get("change") == record.get("change")
                   for r in records):
                return True
            records.append(dict(record))
            post["invalidation"] = self._normalize_invalidation(records)
            _atomic_write_text(file_path, frontmatter.dumps(post))
            self._invalidate_bm25()
            self._record_ledger_event(
                "TraceUpdated", bucket_id, str(post.get("type") or "dynamic"),
                post.content or "", dict(post.metadata), {"changed_fields": ["invalidation"]})
            return True

    async def close_invalidation_records(self, bucket_id: str, kind: str, of: str,
                                         stamp: str, by: str = "") -> bool:
        """Close the open records of `kind` about `of` that are not `cleared` (a source
        restored: what stood on it may be read again; a cleared body stays as it is).
        True when one was closed."""
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False
            post = frontmatter.load(file_path)
            records = [r for r in (post.get("invalidation") or []) if isinstance(r, dict)]
            closed = False
            for r in records:
                if (r.get("kind") == kind and str(r.get("of") or "") == of
                        and not r.get("cleared") and not str(r.get("confirmed_at") or "")):
                    r["confirmed_at"] = stamp
                    if by:
                        r["lifted_by"] = by
                    closed = True
            if not closed:
                return False
            post["invalidation"] = self._normalize_invalidation(records)
            _atomic_write_text(file_path, frontmatter.dumps(post))
            self._invalidate_bm25()
            self._record_ledger_event(
                "TraceUpdated", bucket_id, str(post.get("type") or "dynamic"),
                post.content or "", dict(post.metadata), {"changed_fields": ["invalidation"]})
            return True
