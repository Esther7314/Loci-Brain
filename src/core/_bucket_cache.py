"""
========================================
_bucket_cache.py — the parsed listing of the active store
========================================

list_all() keeps the parsed active buckets in memory. A managed write updates that
listing in place (`_cache_upsert`, `_cache_bump`) or drops it (`_invalidate_bm25`); a
generation counter forms a compare-and-set with every write, so a listing parsed before
a write is never published after it. An edit made outside Loci (Obsidian, git, a hand
edit) is noticed by the file-state poll in list_all() and reconciled into the derived
indexes (`_reconcile_external_changes`).

BucketManager (core/bucket_manager.py) inherits CacheMixin.
========================================
"""

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger("loci_brain.bucket")


class CacheMixin:
    """list_all() and the cache it keeps; external-change detection and reconcile."""

    # ---------------------------------------------------------
    # A managed write: drop the listing, or update it in place
    # ---------------------------------------------------------
    def _invalidate_bm25(self) -> None:
        """Called after a write: mark BM25 for rebuild and clear the active-bucket cache,
        since the set has changed and the cache is void.

        The name is historical (every write path already calls it); what it really is, is
        the single invalidation hook for "the set changed".
        """
        with self._active_cache_state_guard:
            self._active_cache_generation += 1
            self._bm25_dirty = True
            self._active_cache = None
            self._active_file_state = {}
            self._last_file_state_check = 0.0
        # _bucket_path_index is NOT cleared here: clearing it after every managed write made
        # the next create()'s collision check re-parse every frontmatter in the store (886
        # files on a Windows bind mount ≈5.4s a time — enough to make hold slow and grow
        # time out). The path index
        # does not need wholesale invalidation for a **managed** write:
        #   ① create() inserts its own new entry after writing (under the same guard);
        #   ② every hit is verified with os.path.isfile, so a moved or deleted file drops
        #      its entry on the spot and marks the index not-ready, falling through to the
        #      filename scan and then a full rebuild;
        #   ③ a managed filename carries the id, so passing verification while pointing at a
        #      different bucket is impossible.
        # Invalidation for external edits (Obsidian, a hand-edit through git) goes through
        # list_all's file-state polling instead (the other _bucket_path_index_ready=False),
        # and is left exactly as it was — that is the case that genuinely needs a full
        # rebuild.

    def _cache_bump(
        self,
        bucket_id: str,
        *,
        last_active=None,
        activation_count=None,
        file_path: str = "",
    ) -> None:
        """touch only changes one bucket's activation fields and leaves the set unchanged,
        so the cache is updated in place rather than cleared wholesale."""
        with self._active_cache_state_guard:
            # Even with no published cache, a concurrent builder may have
            # parsed the pre-touch file.  Bumping the generation makes its
            # eventual publish fail the CAS and forces a rescan.
            self._active_cache_generation += 1
            if self._active_cache is None:
                return
            for b in self._active_cache:
                if b.get("id") == bucket_id:
                    m = b.get("metadata")
                    if isinstance(m, dict):
                        if last_active is not None:
                            m["last_active"] = last_active
                        if activation_count is not None:
                            m["activation_count"] = activation_count
                    break
            if file_path:
                self._refresh_cached_file_state(file_path)

    def _cache_upsert(self, bucket_id: str, file_path: str, old_path: str = "") -> None:
        """A managed create or update changed one bucket: its new parsed form goes into the
        active cache in place, as _cache_bump does for touch, instead of dropping the whole
        listing. A full re-parse is ~0.8 s for 1700 entries locally and 2–3 s on a bind
        mount, and whatever reads next (breath, the cue poke right after a write) would
        pay it. Costs one file parse, done here while the caller still holds the bucket's
        lock. The generation still moves (a builder that parsed the old file must not
        publish) and BM25 is marked for its incremental sync. A file outside the active
        directories, or one that does not read back, falls back to dropping the cache."""
        path = os.path.normcase(os.path.abspath(file_path))
        archive = os.path.normcase(os.path.abspath(self.archive_dir))
        try:
            in_archive = os.path.commonpath((path, archive)) == archive
        except ValueError:
            in_archive = False
        fresh = None if in_archive else self._load_bucket(file_path)
        if fresh is None:
            self._invalidate_bm25()
            return
        with self._active_cache_state_guard:
            self._active_cache_generation += 1
            self._bm25_dirty = True
            if self._active_cache is None:
                return
            for i, cached in enumerate(self._active_cache):
                if str(cached.get("id") or "") == bucket_id:
                    self._active_cache[i] = fresh
                    break
            else:
                self._active_cache.append(fresh)
            if old_path and not self._same_path(old_path, file_path):
                self._active_file_state.pop(os.path.normcase(os.path.abspath(old_path)), None)
            self._refresh_cached_file_state(file_path)

    def _refresh_cached_file_state(self, file_path: str) -> None:
        """Acknowledge an internal in-place write without invalidating the cache."""
        with self._active_cache_state_guard:
            if self._active_cache is None:
                return
            normalized = os.path.normcase(os.path.abspath(file_path))
            try:
                stat = os.stat(file_path)
                self._active_file_state[normalized] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                self._active_file_state.pop(normalized, None)
            self._last_file_state_check = time.monotonic()

    # ---------------------------------------------------------
    # Edits made outside Loci: the file-state fingerprint list_all() polls, and the
    # reconcile that carries a change it finds into the derived indexes
    # ---------------------------------------------------------
    def _scan_active_file_state(self) -> dict[str, tuple[int, int]]:
        """Return a cheap metadata fingerprint for every active Markdown file.

        🔴 It uses os.scandir, not walk + os.stat.
           Listing a directory already carries each entry's mtime and size (DirEntry caches
           them itself), so calling os.stat afterwards **asks the same question twice** — and
           on a Windows Docker bind mount one stat is one round trip across the boundary.
           Measured over 1000 files: 2.45 seconds, versus 1.05 seconds with scandir.
           Not one field of the fingerprint changed (mtime_ns + size); only the way it is
           fetched did.
        """
        state: dict[str, tuple[int, int]] = {}
        stack = [str(d) for d in self._active_dirs]
        while stack:
            current = stack.pop()
            try:
                scanner = os.scandir(current)
            except OSError:
                continue
            with scanner:
                for entry in scanner:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.name.endswith(".md"):
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    state[os.path.normcase(os.path.abspath(entry.path))] = (
                        stat.st_mtime_ns,
                        stat.st_size,
                    )
        return state

    def external_change_status(self) -> dict[str, Any]:
        with self._active_cache_state_guard:
            return {
                "poll_seconds": self.external_change_poll_seconds,
                "detected": self._external_changes_detected,
                "last_detected": self._last_external_change,
                "cached_files": len(self._active_file_state),
            }

    def _reconcile_external_changes(
        self,
        previous: list[dict],
        current: list[dict],
    ) -> None:
        """Propagate externally-created/edited/deleted Markdown to derived state."""
        old_by_id = {str(bucket.get("id") or ""): bucket for bucket in previous}
        new_by_id = {str(bucket.get("id") or ""): bucket for bucket in current}
        old_by_id.pop("", None)
        new_by_id.pop("", None)

        added_ids = set(new_by_id) - set(old_by_id)
        removed_ids = set(old_by_id) - set(new_by_id)
        content_changed_ids = {
            bucket_id
            for bucket_id in set(old_by_id) & set(new_by_id)
            if str(old_by_id[bucket_id].get("content") or "")
            != str(new_by_id[bucket_id].get("content") or "")
        }
        updated_ids = {
            bucket_id
            for bucket_id in set(old_by_id) & set(new_by_id)
            if bucket_id in content_changed_ids
            or (old_by_id[bucket_id].get("metadata") or {})
            != (new_by_id[bucket_id].get("metadata") or {})
        }

        outbox = self.embedding_outbox
        if outbox is not None:
            for bucket_id in sorted(added_ids | content_changed_ids):
                try:
                    outbox.enqueue(
                        bucket_id,
                        str(new_by_id[bucket_id].get("content") or ""),
                    )
                except Exception as exc:
                    logger.warning(
                        "external edit embedding enqueue failed for %s: %s",
                        bucket_id,
                        exc,
                    )
        for bucket_id in sorted(removed_ids):
            # Moving a file to archive is not physical deletion; keep its
            # derived vector. Only remove the index when the ID vanished from
            # every managed directory.
            if self._find_bucket_file(bucket_id) is not None:
                continue
            if outbox is not None:
                try:
                    outbox.discard(bucket_id)
                except Exception:
                    pass
            if self.embedding_engine is not None:
                try:
                    self.embedding_engine.delete_embedding(bucket_id)
                except Exception as exc:
                    logger.warning(
                        "external delete embedding cleanup failed for %s: %s",
                        bucket_id,
                        exc,
                    )

        logger.info(
            "External vault change reconciled / 外部记忆文件变更已对账: "
            "added=%s changed=%s removed=%s",
            len(added_ids),
            len(updated_ids),
            len(removed_ids),
        )

    # ---------------------------------------------------------
    # List all buckets
    # ---------------------------------------------------------
    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        """
        if include_archive:
            buckets = []
            dirs = list(self._active_dirs) + [self.archive_dir]
            for _root, _fname, file_path in self._iter_md_files(dirs):
                bucket = self._load_bucket(file_path)
                if bucket:
                    buckets.append(bucket)
            return buckets

        # Active buckets use a parsed cache, but Obsidian/Git/manual edits may
        # bypass BucketManager.  The build mutex is cross-loop; the short state
        # guard and generation form a CAS with every managed write.  File state
        # is scanned both before and after parsing so an external editor cannot
        # make us publish old content paired with a new fingerprint.
        async with self._active_cache_lock:
            previous_cache: list[dict] | None = None
            while True:
                now = time.monotonic()
                with self._active_cache_state_guard:
                    generation = self._active_cache_generation
                    cached = self._active_cache
                    if cached is not None:
                        poll_due = (
                            self.external_change_poll_seconds == 0
                            or now - self._last_file_state_check
                            >= self.external_change_poll_seconds
                        )
                        if not poll_due:
                            return [dict(bucket) for bucket in cached]
                        cached_state = dict(self._active_file_state)
                    else:
                        poll_due = False
                        cached_state = {}

                if cached is not None and poll_due:
                    current_state = self._scan_active_file_state()
                    with self._active_cache_state_guard:
                        if generation != self._active_cache_generation:
                            previous_cache = None
                            continue
                        # 🔴 What is recorded is **the moment the scan finished**, not the
                        #    `now` from before it started.
                        #    Scanning 1000 files on a Windows bind mount takes 1~2.6 seconds
                        #    while the interval is 1 second — so with the pre-scan timestamp,
                        #    every single call necessarily decides "time to rescan" and
                        #    **the cache never hits**.
                        #    Measured: one recall calls list_all four times internally, all
                        #    four rescanned, and 10.4 of its 10.5 seconds went here.
                        #    "Check at most once per second" is supposed to be measured on the
                        #    wall clock; how long the check itself takes must not turn the
                        #    check into something that happens every time.
                        self._last_file_state_check = time.monotonic()
                        if current_state == cached_state:
                            current_cache = self._active_cache
                            if current_cache is not None:
                                return [dict(bucket) for bucket in current_cache]
                            continue

                        previous_cache = [dict(bucket) for bucket in cached]
                        self._active_cache_generation += 1
                        generation = self._active_cache_generation
                        self._active_cache = None
                        self._active_file_state = {}
                        self._bm25_dirty = True
                        self._external_changes_detected += 1
                        self._last_external_change = self._now_iso()
                        with self._bucket_path_index_guard:
                            self._bucket_path_index_ready = False
                            self._bucket_path_index = {}

                with self._active_cache_state_guard:
                    generation = self._active_cache_generation

                state_before = self._scan_active_file_state()
                buckets = []
                for _root, _fname, file_path in self._iter_md_files(
                    self._active_dirs
                ):
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)
                state_after = self._scan_active_file_state()

                if state_before != state_after:
                    await asyncio.sleep(0)
                    continue

                with self._active_cache_state_guard:
                    if generation != self._active_cache_generation:
                        previous_cache = None
                        continue
                    self._active_cache = [dict(bucket) for bucket in buckets]
                    self._active_file_state = state_after
                    self._last_file_state_check = time.monotonic()

                if previous_cache is not None:
                    self._reconcile_external_changes(previous_cache, buckets)
                return buckets
