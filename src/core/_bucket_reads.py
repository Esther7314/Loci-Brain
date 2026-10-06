"""
========================================
_bucket_reads.py — reading entries
========================================

By id (get / get_including_archive / is_live), by exact body, the reverse `prov` chain
(who stands on this memory), the per-directory counts, and the tag set create() asks
for its first_of_kind mark.

BucketManager (core/bucket_manager.py) inherits ReadsMixin. The listing these read
from is list_all() in core/_bucket_cache.py; search is core/_bucket_search.py.
========================================
"""

import os
from typing import Any, Optional

import frontmatter

from utils import read_from_ids
from ._rooms import is_mind_room


class ReadsMixin:
    """Lookups by id and by body, the reverse chain, statistics."""

    # ---------------------------------------------------------
    # Read bucket content
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        F-10: a soft-deleted bucket (one carrying deleted_at) is invisible to ordinary
        callers and comes back as None.
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        data = self._load_bucket(file_path, strict=True)
        # F-10: a soft-deleted bucket must not be visible through get()
        if data and data.get("metadata", {}).get("deleted_at"):
            return None
        return data

    def is_live(self, bucket_id: str) -> bool:
        """Is this id a bucket in the active store? Archived, soft-deleted (both live in
        archive/) and missing ids are not. Synchronous and file-level: a cover check runs
        once per covered entry inside read loops, and must not parse YAML."""
        if not bucket_id or not isinstance(bucket_id, str):
            return False
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        path = os.path.normcase(os.path.abspath(file_path))
        archive = os.path.normcase(os.path.abspath(self.archive_dir))
        try:
            return os.path.commonpath((path, archive)) != archive
        except ValueError:
            return True

    async def get_including_archive(self, bucket_id: str) -> Optional[dict]:
        """Read one bucket by ID without hiding its archived/tombstoned state."""
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        return self._load_bucket(file_path, strict=True) if file_path else None

    def find_exact_content(
        self,
        content: str,
        domain_filter: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """Read Markdown directly for an exact match, bypassing derived caches."""
        expected = self._sanitize_text(content)
        filter_set = {
            str(domain).strip().lower()
            for domain in (domain_filter or [])
            if str(domain).strip()
        }
        for _root, _fname, file_path in self._iter_md_files(self._active_dirs):
            bucket = self._load_bucket(file_path)
            if not bucket or bucket.get("content") != expected:
                continue
            metadata = bucket.get("metadata", {})
            if metadata.get("deleted_at"):
                continue
            domains = metadata.get("domain") or []
            if isinstance(domains, str):
                domains = [domains]
            if filter_set and not {
                str(domain).strip().lower() for domain in domains
            } & filter_set:
                continue
            return bucket
        return None

    # ---------------------------------------------------------
    # The reverse chain: whose sources include this memory.
    # The forward chain is `prov` (its sources read through utils.read_from_ids); the
    # reverse one is computed on the spot by scanning the parse cache — the store holds a
    # few hundred entries, so with a warm cache it is one in-memory pass, and no separate
    # index is built (building one would be a second source of truth).
    # ---------------------------------------------------------
    async def referenced_by(self, bucket_id: str) -> list[str]:
        """Who stands on this memory: whose sources (utils.read_from_ids) include it.
        This is what "cited by" in a direct id lookup and regrow's overturn walk use."""
        out: list[str] = []
        for b in await self.list_all(include_archive=False):
            meta = b.get("metadata", {}) or {}
            if bucket_id in read_from_ids(meta):
                out.append(str(meta.get("id") or b.get("id") or ""))
        return out

    async def mind_from_ids(self) -> set[str]:
        """The set of ids among the sources of any insight-type bucket (the MIND branch, or
        the older type=feel/i).

        This is the "not digested" test: once an insight has been distilled out of
        something, that thing is digested; only what refuses to distil keeps turning over in
        dreams.
        """
        out: set[str] = set()
        for b in await self.list_all(include_archive=False):
            meta = b.get("metadata", {}) or {}
            # Never write `"/MIND/" in room`: the new room name MIND/TRAITS has no leading slash
            if (not is_mind_room(meta.get("room"))
                    and str(meta.get("type") or "") not in ("feel", "i")):
                continue
            out.update(read_from_ids(meta))
        return out

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        """
        stats: dict[str, Any] = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Collect all tags currently in the vault (excluding archive), for the first_of_kind check
    # Returns set[str]; an empty vault gives an empty set; on an exception it returns None,
    # telling the caller to give up
    # ---------------------------------------------------------
    def _collect_all_tags(self) -> Optional[set]:
        tags = set()
        # archive_dir is excluded — archived buckets are "the past", they
        # shouldn't block a tag from being marked first_of_kind today.
        for _root, _fname, full_path in self._iter_md_files(self._active_dirs):
            try:
                post = frontmatter.load(full_path)
                for t in list(post.get("tags") or []):  # type: ignore[call-overload]
                    if t:
                        tags.add(str(t))
            except Exception:
                # One bucket failing to parse does not affect the whole; first_of_kind is a soft feature
                continue
        return tags
