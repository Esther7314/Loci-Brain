"""
========================================
_bucket_files.py — where a bucket's file is, and how it is read and committed
========================================

The storage layout: permanent / dynamic / feel / archive, each with a subdirectory per
primary domain. The id -> path index that finds a bucket's file, with its fallbacks;
loading one file into {"id", "metadata", "content", "path"}; and committing a changed
file either in place or into a new directory without leaving two copies.

BucketManager (core/bucket_manager.py) inherits FilesMixin. Every write path —
core/_bucket_write.py, core/_bucket_lifecycle.py, core/_bucket_clearing.py — finds and
commits its file through these.
========================================
"""

import json
import logging
import math
import os
from pathlib import Path
from typing import Optional

import frontmatter

from utils import atomic_write_text as _atomic_write_text
from utils import busy_retry, safe_path, sanitize_name
from ._bucket_fields import _DEFAULT_DOMAIN_NAME, _EDITABLE_BUCKET_TYPES, _clamp_importance

logger = logging.getLogger("loci_brain.bucket")


class FilesMixin:
    """Directories, the id -> path index, loading and committing bucket files."""

    # ---------------------------------------------------------
    # The directories, and the primary-domain subdirectory a bucket lives in
    # ---------------------------------------------------------
    @property
    def _active_dirs(self) -> list[str]:
        """The active bucket directories, archive excluded (used by list_all, _collect_all_tags and lookups). The order must not be shuffled: feel comes after dynamic to preserve the original scan order."""
        return [self.permanent_dir, self.dynamic_dir, self.feel_dir]

    def _iter_md_files(self, dirs: list[str]):
        """Recursively walk several directories for *.md, yielding (root, filename, full_path).

        Behaves exactly like the five copies of ``for root, _, files in os.walk(…): for f in files: if not f.endswith('.md'): continue`` it replaced.
        No filtering logic is added here; the caller decides what to skip.
        """
        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    yield root, fname, os.path.join(root, fname)

    @staticmethod
    def _primary_domain(domain: list[str] | str | None) -> str:
        """Take domain[0] as the primary-domain subdirectory name; empty or missing falls
        back to ``未分类``.

        Used in three places: create, _move_bucket and archive. It has to go through
        sanitize_name before it can be used as a path.
        """
        if isinstance(domain, str):
            primary = domain.strip()
        elif domain:
            primary = str(domain[0]).strip()
        else:
            primary = ""
        return sanitize_name(primary) if primary else _DEFAULT_DOMAIN_NAME

    def _sunk_orig_path(self, bucket_id: str) -> str:
        """Where a sunk bucket's original text lives: archive/原文/{id}.txt.

        ⚠️ It must be .txt and never .md: with the same id owning an md file in two places,
        _find_bucket_file would collide.
        """
        return os.path.join(self.archive_dir, "原文", f"{bucket_id}.txt")

    # ---------------------------------------------------------
    # Move bucket between directories, commit a changed file, and keep the
    # id -> path index in step with both
    # ---------------------------------------------------------
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: Optional[list[str]] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            os.rename(file_path, new_path)
            logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
        return str(new_path)

    def _bucket_target_path(
        self,
        file_path: str,
        bucket_type: str,
        domain: Optional[list[str]] = None,
        status: str = "",
    ) -> str:
        """Return the canonical storage path for an editable bucket type.

        The frontmatter ``type`` is the logical source of truth, but the vault
        layout is part of the public Obsidian contract too.  Keeping this
        mapping in one place prevents dashboard edits from changing metadata
        without moving the Markdown file.
        """
        normalized_type = str(bucket_type or "dynamic").strip().lower()
        if normalized_type not in _EDITABLE_BUCKET_TYPES:
            raise ValueError(f"unsupported editable bucket type: {normalized_type}")

        if normalized_type == "permanent":
            type_dir = self.permanent_dir
            subdir = self._primary_domain(domain)
        elif normalized_type == "feel":
            type_dir = self.feel_dir
            subdir = "沉淀物"
        else:
            # ``i`` / ``self`` are private logical channels, not separate
            # physical stores.  They intentionally live under dynamic/<domain>.
            type_dir = self.dynamic_dir
            subdir = self._primary_domain(domain)

        target_dir = os.path.join(type_dir, subdir)
        os.makedirs(target_dir, exist_ok=True)
        return str(safe_path(target_dir, os.path.basename(file_path)))

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )

    def _commit_bucket_update(
        self,
        file_path: str,
        target_path: str,
        serialized: str,
        bucket_id: str = "",
    ) -> str:
        """Commit an update without leaving type/path split-brain on failure.

        In-place edits use the normal atomic writer.  A directory migration is
        copy-on-commit: atomically write the complete new file at its canonical
        destination, then remove the untouched source.  If source removal
        fails, delete the new copy and keep the original as the sole truth.
        Existing destination files are never overwritten.

        A managed move has to keep the ID -> path index in step. _invalidate_bm25 does not
        clear that index wholesale, so a file moved here without updating the mapping would
        cause the next hit on the old path to mark the index not-ready -> another full
        re-parse of the store (5s+).
        The caller passes bucket_id where it can; without it this degrades to removing the
        old mapping, which is fail-safe.
        """
        if self._same_path(file_path, target_path):
            _atomic_write_text(file_path, serialized)
            return file_path

        if os.path.exists(target_path):
            raise FileExistsError(
                f"bucket migration target already exists: {target_path}"
            )

        _atomic_write_text(target_path, serialized)
        try:
            busy_retry(lambda: os.remove(file_path))
        except Exception:
            try:
                os.remove(target_path)
            except OSError as rollback_error:
                logger.critical(
                    "Failed to roll back bucket migration target %s: %s",
                    target_path,
                    rollback_error,
                )
            raise

        self._path_index_moved(file_path, target_path, bucket_id)

        logger.info(
            "Moved bucket / 移动记忆桶: %s → %s/",
            os.path.basename(file_path),
            os.path.dirname(target_path),
        )
        return target_path

    def _path_index_moved(self, old_path: str, new_path: str, bucket_id: str = "") -> None:
        """Maintain the ID -> path index after a managed move or archive (see the comments
        in _commit_bucket_update).

        With a bucket_id known, update the mapping in place; without one, look the old path
        up in reverse and remove that entry.
        It only maintains incrementally and never marks the index not-ready — wholesale
        invalidation for external edits goes through list_all's polling.
        """
        with self._bucket_path_index_guard:
            if bucket_id:
                self._bucket_path_index[bucket_id] = new_path
                return
            stale_keys = [
                k for k, v in self._bucket_path_index.items()
                if self._same_path(v, old_path)
            ]
            for k in stale_keys:
                if new_path:
                    self._bucket_path_index[k] = new_path
                else:
                    self._bucket_path_index.pop(k, None)

    def _path_index_removed(self, bucket_id: str, path: str = "") -> None:
        """Remove the entry after a managed delete. It does not mark the index not-ready:
        the file really is gone, so a miss IS the correct answer."""
        with self._bucket_path_index_guard:
            if bucket_id:
                self._bucket_path_index.pop(bucket_id, None)
            if path:
                for k in [k for k, v in self._bucket_path_index.items()
                          if self._same_path(v, path)]:
                    self._bucket_path_index.pop(k, None)

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # ---------------------------------------------------------
    def _ensure_bucket_path_index(self) -> None:
        """Build the complete ID → path index once for bulk conflict checks."""

        with self._bucket_path_index_guard:
            if self._bucket_path_index_ready:
                return
            # archive is included: a soft-deleted bucket still has to be findable by internal path lookup.
            dirs = [
                self.permanent_dir,
                self.dynamic_dir,
                self.archive_dir,
                self.feel_dir,
            ]
            index: dict[str, str] = {}
            for _root, fname, full_path in self._iter_md_files(dirs):
                stem = fname[:-3]
                index.setdefault(stem, full_path)
                # A managed filename looks like `<name>_<id>.md`. Indexing only the whole
                # stem would lose the bare id whenever the frontmatter fails to parse
                # (stored_id=""): the index would be ready but missing that entry, the bucket
                # an orphan, falling through to the filename-scan fallback in
                # `_find_bucket_file()` (which also logs a loud warning).
                # Same rule as `_scan_bucket_file_by_name()`'s `stem.endswith(f"_{bucket_id}")`
                # test: parse the `_<id>` suffix out and index that as a key too, so nothing
                # depends on whether the frontmatter can be read.
                if "_" in stem:
                    index.setdefault(stem.rsplit("_", 1)[-1], full_path)
                try:
                    post = frontmatter.load(full_path)
                    stored_id = str(post.get("id") or "")
                except Exception:
                    stored_id = ""
                if stored_id:
                    index.setdefault(stored_id, full_path)
            self._bucket_path_index = index
            self._bucket_path_index_ready = True

    def _scan_bucket_file_by_name(self, bucket_id: str) -> Optional[str]:
        """Scan once by **filename** for this id (a managed filename is either `<id>.md` or
        `<name>_<id>.md`).

        It uses `os.walk` alone and **parses not one frontmatter** — that is the crucial
        difference from `_ensure_bucket_path_index()`, which has to read the YAML of the
        entire store and takes seconds on a Windows bind mount. A hit is backfilled into the
        index on the way out.
        """
        dirs = [
            self.permanent_dir,
            self.dynamic_dir,
            self.archive_dir,
            self.feel_dir,
        ]
        for _root, fname, full_path in self._iter_md_files(dirs):
            stem = fname[:-3]
            if stem == bucket_id or stem.endswith(f"_{bucket_id}"):
                with self._bucket_path_index_guard:
                    self._bucket_path_index[bucket_id] = full_path
                return full_path
        return None

    def _find_bucket_file(
        self, bucket_id: str, *, scan_if_missing: bool = True
    ) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.

        ------------------------------------------------------------
        🔴 The path-index fallback
        ------------------------------------------------------------
        **When the index is ready but the lookup misses, it may not simply answer "no".**
        Fall back to one scan by filename, and on a hit put the entry back into the index
        before returning.

        Why: during acceptance testing this was walked into three times in a row — "index
        ready, entry missing -> `update()` silently returns False -> the write is lost
        without a word" (just after the container started, or while the store was being
        scanned concurrently). The likely root cause is that while building the table
        `_ensure_bucket_path_index()` **skips any file that reads empty or fails to parse**
        (the `stored_id=""` branch), while the other key it `setdefault`s is the **filename
        stem** (`<name>_<id>`) rather than the bare id — so that bucket effectively does not
        exist in the index, yet `_bucket_path_index_ready` is True, and "not found" got
        treated as "does not exist".
        ⚠️ **Only the fallback was added**; not one line of the index construction was
        rewritten.

        `scan_if_missing=False` exists for `create()`'s collision check: that caller
        **expects to find nothing** (the id was just generated), and scanning the whole store
        every time would add a walk to every single grow.
        That path has two other layers of protection: `os.path.exists(candidate_path)`, and a
        no-overwrite `_atomic_create_text` on write (a collision raises FileExistsError and
        another id is drawn).
        """
        if not bucket_id:
            return None

        with self._bucket_path_index_guard:
            path = self._bucket_path_index.get(bucket_id)
            if path and os.path.isfile(path):
                return path
            if path:
                # An unmanaged move/delete can invalidate a hit before the
                # periodic full-vault poll.  Drop it; the next poll rebuilds
                # the complete index and discovers any replacement location.
                self._bucket_path_index.pop(bucket_id, None)
                self._bucket_path_index_ready = False
            index_ready = self._bucket_path_index_ready
        if index_ready:
            # ▼▼▼ The fallback: **ready does not mean complete.** Scan once by filename
            #     before saying "no".
            #     The cost was measured (a copied store of 964 buckets / 2371 .md files, on a
            #     Windows bind mount):
            #       one filename scan      0.3s   <- this branch
            #       full index rebuild     5.1s   <- deliberately **not** taken here (the
            #                                        branch below takes it)
            #     "Genuinely absent" (no such bucket, a collision check) is the common case,
            #     and turning the common case into a seconds-long job would be a new disease.
            if not scan_if_missing:
                return None
            hit = self._scan_bucket_file_by_name(bucket_id)
            if hit:
                # 🔴 **It must make a sound**: this warning is the only evidence that the
                #    index dropped an entry. Recovering silently would bury the root cause —
                #    and the reason this bug took three collisions to catch is precisely that
                #    it was silent the whole way.
                logger.warning(
                    "path index was ready but missing %s; recovered by filename "
                    "scan and backfilled / 索引 ready 却缺条目，已按文件名找回并补回索引: %s",
                    bucket_id,
                    hit,
                )
            return hit
            # ▲▲▲ End of the fallback.

        # Preserve the cheap common path for ordinary single-bucket CRUD: most
        # managed filenames contain the ID, so there is no reason to parse the
        # full vault merely to serve one get/update.  Bulk migration explicitly
        # prewarms the complete index via _ensure_bucket_path_index().
        hit = self._scan_bucket_file_by_name(bucket_id)
        if hit:
            return hit

        # Imported/renamed files may not encode their ID in the filename.
        self._ensure_bucket_path_index()
        with self._bucket_path_index_guard:
            path = self._bucket_path_index.get(bucket_id)
            return path if path and os.path.isfile(path) else None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # ---------------------------------------------------------
    def _load_bucket(self, file_path: str, *, strict: bool = False) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.

        A file busy for a moment (another handle replacing it) is waited out. strict=True
        (get): a file that still cannot be opened raises OSError rather than reading as
        "no such entry" — a caller taking None for a blank entry would overwrite it. A file
        gone since it was found, or one that does not parse, is None either way.
        """
        try:
            post = busy_retry(lambda: frontmatter.load(file_path))
        except FileNotFoundError:
            return None
        except OSError:
            if strict:
                raise
            logger.warning(f"Failed to load bucket file / 加载桶文件失败: {file_path}: busy")
            return None
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None
        try:
            # Normalize the metadata object as one graph so aliases shared by
            # different top-level keys cannot reset the node/depth budgets.
            metadata = self._normalize_metadata_value(dict(post.metadata))
            domain_value = metadata.get("domain")
            if isinstance(domain_value, str):
                metadata["domain"] = [domain_value] if domain_value.strip() else []
            elif domain_value is None:
                metadata["domain"] = []
            elif not isinstance(domain_value, list):
                metadata["domain"] = list(domain_value) if isinstance(domain_value, tuple) else [str(domain_value)]
            # Older buckets may have stored string forms such as `'V0.9'` or `'[我的视角:V0.3]'`
            for field, default in (
                ("valence", 0.5),
                ("arousal", 0.3),
                ("model_valence", 0.5),
                ("weight", 0.5),
            ):
                if field in metadata:
                    metadata[field] = self._sanitize_float_field(metadata[field], default)
            # YAML is an external input boundary (manual files, migration ZIP).
            # Never let arbitrary scalar strings reach JSON
            # consumers that treat these fields as numbers.
            metadata["importance"] = _clamp_importance(
                metadata.get("importance", 5), f"load:{Path(file_path).name}"
            )
            try:
                activation_count = float(metadata.get("activation_count", 0) or 0)
                if not math.isfinite(activation_count) or activation_count < 0:
                    raise ValueError("invalid activation_count")
                metadata["activation_count"] = (
                    int(activation_count)
                    if activation_count.is_integer()
                    else round(activation_count, 3)
                )
            except (TypeError, ValueError, OverflowError):
                metadata["activation_count"] = 0
            # Defense in depth: future scalar branches must not accidentally
            # reintroduce NaN/bytes/set values to web JSON consumers.
            json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": metadata,
                "content": post.content,
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None
