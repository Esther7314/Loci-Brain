"""
========================================
_bucket_lifecycle.py — a memory's life after it is written
========================================

touch (it was used: last_active moves), sink (the body moves to archive/原文/ and only
the summary stays), archive (the decay engine's forgetting), soft delete (moved to
archive/ with deleted_at), restore (back from any of those), and the one physical
erase there is: a bucket born as test data.

Every one of them runs under the bucket's lease (`_bucket_turn`) and commits through
core/_bucket_files.py, like the write path does.

BucketManager (core/bucket_manager.py) inherits LifecycleMixin.
========================================
"""

import logging
import os
from datetime import datetime

import frontmatter

from utils import atomic_write_text as _atomic_write_text
from utils import parse_bool, parse_iso_datetime, safe_path
from . import _usage
from ._sources import note_written
from ._bucket_fields import _DEFAULT_DOMAIN_NAME, _EDITABLE_BUCKET_TYPES

logger = logging.getLogger("loci_brain.bucket")


class LifecycleMixin:
    """touch, sink / unsink, archive, soft delete, restore, test-data erase."""

    # ---------------------------------------------------------
    # touch: set the "last recalled" timestamp to now
    # ---------------------------------------------------------
    async def touch(self, bucket_id: str, ripple: bool = False) -> None:
        """Set this memory's `last_active` to now. **The forgetting engine reads this one
        field and nothing else.**

        🔴 **What counts as "being recalled" — the facts first, the rule below**:
           There are exactly three places that call this, and all three are "**I actually
           did something with this memory**":
             · wrote an insight that cites it (`grow`'s from)
             · changed a version, with it as the source (`regrow`)
             · folded it into a summarising sentence (`fold`)
           **`recall` finding it -> no call.** Scrolling past it while browsing -> no call.

        📌 The rule — a search hit does not extend a memory's life:
           "things recalled repeatedly are remembered firmly — **but that case is the
           minority**", together with **"a faded entry still has its summary; it is not
           forgotten outright"**.
           The second half is the key: **fading is not deletion.** It means "it stops
           surfacing on its own; go looking and it is still there, merely discounted."
           So the question "should this get its life extended" carries far less weight than
           it sounds like it does — not enough to justify importing the whole "look something
           up often and it lives longer" model, which is a practice curve, not memory.

        `ripple` is accepted only so the call sites do not have to change; **it does
        nothing whatever you pass** (why there is no temporal ripple: the note below
        touch_many).
        """
        async with self._bucket_turn(bucket_id):
            await self._touch_locked(bucket_id)

    async def _touch_locked(self, bucket_id: str) -> datetime | None:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None

        try:
            post = frontmatter.load(file_path)
            post["last_active"] = self._now_iso()
            post["activation_count"] = int(post.get("activation_count") or 0) + 1  # type: ignore[call-overload]

            _atomic_write_text(file_path, frontmatter.dumps(post))
            self._cache_bump(
                bucket_id,
                last_active=post["last_active"],
                activation_count=post["activation_count"],
                file_path=file_path,
            )

            current_time = parse_iso_datetime(
                post.get("created", post.get("last_active", ""))
            )
            self._record_ledger_event(
                "TraceTouched",
                bucket_id,
                str(post.get("type") or "dynamic"),
                post.content or "",
                dict(post.metadata),
            )
            return current_time
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")
            return None

    async def touch_many(self, bucket_ids: list, ripple: bool = False, road: str = "write") -> None:
        """touch in bulk. One failure does not affect the others.

        `ripple` does nothing whatever you pass (see the note below); it is accepted only
        so the call sites do not have to change.

        Every caller is a write standing on these memories, so this is also where the
        usage log records them as used as a source (core/_usage.py).
        """
        touched = []
        for bid in bucket_ids:
            try:
                await self.touch(bid)
                touched.append(bid)
            except Exception as e:
                logger.warning(f"touch_many: 触碰 {bid} 失败: {e}")
        self.usage.record(_usage.SOURCE, touched, road)

    # 🔴 **There is no temporal ripple** — touching a bucket does not wake the buckets
    #    **adjacent to it in time** ("recall one thing and you incidentally wake the things
    #    around it").
    #    · Nothing reads `activation_count`: the forgetting formula has no count factor
    #      ("things recalled often do not sink" is last_active's job), so bumping it would
    #      be **visible nowhere at all**.
    #    · It would not be cheap: every ripple means a `list_all()` pass over the whole
    #      store (hundreds of files), to change a number nobody reads.
    #
    #    📌 The deeper reason: "things recalled repeatedly are remembered firmly — **but
    #      that case is the minority**". That is the practice-makes-permanent model
    #      (Ebbinghaus), and it is **not the memory we are after**. What we are after is
    #      "what mattered, what moved something, lasts", not "what got looked up most lasts".
    #
    #    ⚠️ The `activation_count` field still sits in old frontmatter: it costs nothing
    #       there, and removing it would mean touching thousands of files. **But nothing
    #       reads it** — anyone who sees it and thinks of building a rule on it should come
    #       back and read this first.

    # ---------------------------------------------------------
    # Sinking and restoring: forgetting is not "the whole entry disappears", it is
    # "resolution drops"
    # ---------------------------------------------------------
    async def sink_bucket(self, bucket_id: str) -> bool:
        """Sink a bucket: the original text moves to archive/原文/{id}.txt and the body in
        the main store is replaced by its summary.

        🔴 The vector is left exactly as it is and never recomputed — the semantic
        fingerprint is still the one computed from the original text.
        **This is what a person is actually like: remembering what something felt like while
        being unable to reproduce the words.**
        BM25 reads the original back out of the txt (_bm25_source), so
        "matching against the original text" is not lost.
        Reversible: trace(restore=True) goes through _unsink_locked and fetches it back.
        """
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                logger.warning(f"sink_bucket 读取失败 {bucket_id}: {exc}")
                return False
            if (str(post.get("type") or "") == "archived" or post.get("deleted_at")
                    or parse_bool(post.get("tombstone"), default=False)):
                return False
            if str(post.get("decay_stage") or "") == "sunk":
                return True  # idempotent: it has already sunk
            summary = str(post.get("summary") or "").strip()
            orig = post.content or ""
            if not summary or not orig.strip():
                # Without a summary it cannot sink, since sinking IS "only the summary is
                # left". Wait for the backfill to supply one and sink on the next round.
                return False
            txt = self._sunk_orig_path(bucket_id)
            try:
                os.makedirs(os.path.dirname(txt), exist_ok=True)
                with open(txt, "w", encoding="utf-8", newline="") as f:
                    f.write(orig)
            except OSError as exc:
                logger.warning(f"sink_bucket 原文落盘失败 {bucket_id}: {exc}")
                return False
            post.content = summary
            post["decay_stage"] = "sunk"
            try:
                self._commit_bucket_update(
                    file_path, file_path, frontmatter.dumps(post), bucket_id=bucket_id,
                )
            except (OSError, ValueError) as exc:
                logger.error(f"sink_bucket 提交失败 {bucket_id}: {exc}")
                return False
            # ⚠️ _index_after_write is deliberately NOT called: leaving the vector alone is
            #    the design, not an oversight
            self._invalidate_bm25()
            self._record_ledger_event(
                "MemorySunk", bucket_id, str(post.get("type") or "dynamic"),
                post.content or "", dict(post.metadata), {"orig_path": txt},
            )
            logger.info(f"Memory sunk / 沉底: {bucket_id}（原文 → {txt}）")
            return True

    async def _unsink_locked(self, bucket_id: str, file_path: str, post) -> dict:
        """Restore a sunk bucket (called by restore_archived while it holds the lock).

        The original is read back out of the txt into the body and decay_stage is cleared.
        Restoring means it was recalled, so last_active is refreshed — otherwise the next
        decay cycle would immediately sink it again. The vector was computed from the
        original text in the first place and needs no recomputation.
        """
        txt = self._sunk_orig_path(bucket_id)
        try:
            with open(txt, encoding="utf-8") as f:
                orig = f.read()
        except OSError:
            return {"ok": False, "error": f"orig_missing: {txt}（沉底原文找不到了）"}
        post.content = orig
        post.metadata.pop("decay_stage", None)
        post["last_active"] = self._now_iso()
        post["activation_count"] = int(post.get("activation_count") or 0) + 1
        try:
            self._commit_bucket_update(
                file_path, file_path, frontmatter.dumps(post), bucket_id=bucket_id,
            )
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"restore_failed: {exc}"}
        self._invalidate_bm25()
        self._record_ledger_event(
            "MemoryUnsunk", bucket_id, str(post.get("type") or "dynamic"),
            post.content or "", dict(post.metadata),
        )
        logger.info(f"Memory unsunk / 沉底还原: {bucket_id}")
        return {"ok": True, "restored": bucket_id, "type": str(post.get("type") or "dynamic"),
                "unsunk": True}

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # Called by decay engine to simulate "forgetting"
    # ---------------------------------------------------------
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        """
        async with self._bucket_turn(bucket_id):
            return await self._archive_locked(bucket_id)

    async def _archive_locked(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            # Read once, get domain info and update type
            post = frontmatter.load(file_path)
            domain: list[str] = post.get("domain") or [_DEFAULT_DOMAIN_NAME]  # type: ignore[assignment]
            primary_domain = self._primary_domain(domain)
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)

            dest = safe_path(archive_subdir, os.path.basename(file_path))
            # Collision guard: when archive/ already holds a file of the same name, append
            # the bucket_id as a suffix, so an earlier archived memory cannot be quietly
            # overwritten (matching delete()'s soft-delete protection).
            if os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(file_path):
                stem = os.path.splitext(os.path.basename(file_path))[0]
                dest = safe_path(archive_subdir, f"{stem}_{bucket_id}.md")

            # Commit the archived metadata at the destination before removing
            # the untouched source.  A failed move must not leave an
            # ``type=archived`` file stranded in an active directory.
            post["type"] = "archived"
            self._commit_bucket_update(
                file_path,
                str(dest),
                frontmatter.dumps(post),
                bucket_id=bucket_id,
            )
        except Exception as e:
            logger.error(
                f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
            )
            return False

        self._invalidate_bm25()
        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        self._record_ledger_event(
            "TraceArchived",
            bucket_id,
            str(post.get("type") or "archived"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # Delete bucket
    # ---------------------------------------------------------
    async def delete(self, bucket_id: str) -> bool:
        """
        Soft-delete a memory bucket: move to archive/ and stamp `deleted_at`.
        F-10: a memory does not disappear, it fades. Nothing is physically deleted; the file
        moves into archive/ and a deleted_at timestamp is written into its frontmatter. The
        embedding is still cleaned up, to save space.
        """
        async with self._bucket_turn(bucket_id):
            deleted = await self._delete_locked(bucket_id)
        if deleted:
            note_written(bucket_id)
        return deleted

    async def _delete_locked(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # --- Read the file, write deleted_at, move it into archive/ ---
        try:
            post = frontmatter.load(file_path)
            tombstone_at = self._now_iso()
            post["deleted_at"] = tombstone_at
            post["tombstone"] = True
            post["tombstoned_at"] = tombstone_at
            post["erasure_mode"] = "tombstone_only"
            os.makedirs(self.archive_dir, exist_ok=True)
            dest = os.path.join(self.archive_dir, os.path.basename(file_path))
            # If archive/ already holds a file of the same name (very rare), append the
            # bucket_id as a suffix so nothing is overwritten
            if os.path.exists(dest) and dest != file_path:
                dest = os.path.join(
                    self.archive_dir,
                    f"{os.path.splitext(os.path.basename(file_path))[0]}_{bucket_id}.md",
                )
            self._commit_bucket_update(
                file_path,
                str(dest),
                frontmatter.dumps(post),
                bucket_id=bucket_id,
            )
        except OSError as e:
            logger.error(f"Failed to soft-delete bucket / 软删除桶文件失败: {file_path}: {e}")
            return False

        # The embedding is still cleaned up, so orphaned vectors do not take up space
        if self.embedding_outbox is not None:
            try:
                self.embedding_outbox.discard(bucket_id)
            except Exception as e:
                logger.warning(f"discard embedding outbox failed for {bucket_id}: {e}")
        if self.embedding_engine is not None:
            try:
                self.embedding_engine.delete_embedding(bucket_id)
            except Exception as e:
                logger.warning(f"delete embedding failed for {bucket_id}: {e}")

        self._invalidate_bm25()
        logger.info(f"Soft-deleted bucket (moved to archive) / 软删除记忆桶: {bucket_id}")
        self._record_ledger_event(
            "TraceDeletedToArchive",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # Restore: back from archive, from a soft delete, or from sinking
    # ---------------------------------------------------------
    async def restore_archived(self, bucket_id: str) -> dict:
        """Restore an archived/tombstoned Markdown bucket to its original channel.

        Discovery never calls this method.  It is deliberately exposed only
        through an explicit ``trace(..., restore=True)`` decision.
        """
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return {"ok": False, "error": "not_found"}
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                return {"ok": False, "error": f"read_failed: {exc}"}

            normalized_path = os.path.normcase(os.path.abspath(file_path))
            normalized_archive = os.path.normcase(os.path.abspath(self.archive_dir))
            try:
                stored_in_archive = (
                    os.path.commonpath((normalized_path, normalized_archive))
                    == normalized_archive
                )
            except ValueError:
                stored_in_archive = False
            archived_state = (
                stored_in_archive
                or str(post.get("type") or "").strip().lower() == "archived"
                or bool(post.get("deleted_at"))
                or parse_bool(post.get("tombstone"), default=False)
            )
            if not archived_state:
                # A sunk bucket (decay_stage=sunk) keeps its shell in the main store with
                # only the summary as its body, and restore=True is its one and only door
                # back — this stage is reversible by design.
                if str(post.get("decay_stage") or "") == "sunk":
                    return await self._unsink_locked(bucket_id, file_path, post)
                return {"ok": False, "error": "not_archived"}

            original_kind = self.footprint_snapshot().original_kind(
                bucket_id, dict(post.metadata)
            )
            if original_kind not in _EDITABLE_BUCKET_TYPES:
                original_kind = "dynamic"
            if parse_bool(post.get("pinned"), default=False) or parse_bool(
                post.get("protected"), default=False
            ):
                original_kind = "permanent"

            post["type"] = original_kind
            for field in (
                "deleted_at", "tombstone", "tombstoned_at", "erasure_mode"
            ):
                post.metadata.pop(field, None)
            try:
                target_path = self._bucket_target_path(
                    file_path,
                    original_kind,
                    post.get("domain") or [_DEFAULT_DOMAIN_NAME],
                    str(post.get("status") or "active"),
                )
                committed_path = self._commit_bucket_update(
                    file_path, target_path, frontmatter.dumps(post),
                    bucket_id=bucket_id,
                )
            except (OSError, ValueError) as exc:
                logger.error("Failed to restore archived bucket %s: %s", bucket_id, exc)
                return {"ok": False, "error": f"restore_failed: {exc}"}

            self._invalidate_bm25()
            await self._index_after_write(bucket_id, post.content or "")
            await self._sync_meaning_embedding(bucket_id, post.get("meaning") or [])
            self._record_ledger_event(
                "TraceRestored",
                bucket_id,
                original_kind,
                post.content or "",
                dict(post.metadata),
            )
            logger.info("Restored archived bucket: %s -> %s", bucket_id, committed_path)
            return {"ok": True, "restored": bucket_id, "type": original_kind}

    # ---------------------------------------------------------
    # The one physical erase: a bucket born as test data
    # ---------------------------------------------------------
    async def hard_delete_test_bucket(self, bucket_id: str, *, reason: str = "") -> dict:
        """Erase only a bucket born as test data, with an explicit audit reason."""
        async with self._bucket_turn(bucket_id):
            return await self._hard_delete_test_bucket_locked(bucket_id, reason=reason)

    async def _hard_delete_test_bucket_locked(
        self,
        bucket_id: str,
        *,
        reason: str = "",
    ) -> dict:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return {"ok": False, "error": "not_found"}
        try:
            post = frontmatter.load(file_path)
        except Exception as exc:
            return {"ok": False, "error": f"read_failed: {exc}"}
        provenance = post.get("provenance")
        if not (isinstance(provenance, dict)
                and provenance.get("kind") == "test"
                and provenance.get("erasable") is True):
            return {"ok": False, "error": "not_erasable_test_data"}
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            return {"ok": False, "error": "missing_delete_reason"}
        if len(normalized_reason) > 500:
            return {"ok": False, "error": "delete_reason_too_long"}
        bucket_type = str(post.get("type") or "dynamic")
        try:
            os.remove(file_path)
        except OSError as exc:
            return {"ok": False, "error": f"delete_failed: {exc}"}
        self._path_index_removed(bucket_id, file_path)
        if self.embedding_outbox is not None:
            try:
                self.embedding_outbox.discard(bucket_id)
            except Exception:
                pass
        if self.embedding_engine is not None:
            try:
                self.embedding_engine.delete_embedding(bucket_id)
            except Exception as exc:
                logger.warning("hard delete embedding cleanup failed for %s: %s", bucket_id, exc)
        self._invalidate_bm25()
        self._record_ledger_event(
            "TraceHardDeleted", bucket_id, bucket_type, "",
            {"provenance": {"kind": "test", "erasable": True}},
            {"reason": normalized_reason, "content_erased": True},
        )
        logger.warning("Physically erased test bucket: %s", bucket_id)
        return {"ok": True, "deleted": bucket_id}
