"""
github_sync.py — syncing to a GitHub repository, for off-site backup of bucket data

The strategy:
- Only the .md files under buckets_dir are synced (plain text, small, and readable)
- embeddings.db is not uploaded (it is binary, and /api/embedding/migrate can rebuild it)
- Commits go through the GitHub Git Trees API in bulk (one sync = one commit)
- Both manual triggering and optional scheduled auto-sync are supported

Dependency: httpx (already in requirements.txt)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

import httpx

from utils import _win_long_path

from . import _sources as _src

logger = logging.getLogger("loci_brain.github_sync")

_API = "https://api.github.com"
_TIMEOUT = 60.0
_MAX_FILE_BYTES = 5 * 1024 * 1024  # GitHub caps a single blob near 100MB; 5MB is the conservative limit here
_TREE_CHUNK = 200                  # how many files one /git/trees request may inline, so no single request grows huge
_TREE_CHUNK_BYTES = 2 * 1024 * 1024  # Bound decoded bodies retained by one request.
_MAX_BACKUP_FILES = 10_000
_MAX_BACKUP_PATH_BYTES = 1024
_MAX_MANIFEST_PAYLOAD_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BASE64_BYTES = ((_MAX_MANIFEST_PAYLOAD_BYTES + 2) // 3) * 4 + 64 * 1024
_MAX_RESTORE_TOTAL_BYTES = 512 * 1024 * 1024
_MANIFEST_FILENAME = "_loci_backup_manifest.json"


class _LazyMarkdownFiles(Mapping[str, bytes]):
    """A path index whose file bodies are read only when requested.

    Retaining every Markdown file as ``bytes``, plus a second decoded ``str``
    copy for every tree entry, would make the scheduled backup itself an OOM
    trigger on a 512 MiB instance.  Keeping only paths here lets
    ``_batch_commit`` hold at most one bounded chunk of bodies behind a
    mapping-shaped private API.
    """

    def __init__(self, paths: Mapping[str, str]) -> None:
        self._paths = dict(paths)

    def __len__(self) -> int:
        return len(self._paths)

    def __iter__(self) -> Iterator[str]:
        return iter(self._paths)

    def __getitem__(self, relative_path: str) -> bytes:
        full_path = self._paths[relative_path]
        if os.path.islink(full_path):
            raise RuntimeError(
                f"GitHub backup refuses symbolic-link file: {relative_path}"
            )
        size = os.path.getsize(full_path)
        if size > _MAX_FILE_BYTES:
            raise RuntimeError(
                f"GitHub backup file grew beyond {_MAX_FILE_BYTES} bytes: "
                f"{relative_path} ({size} bytes)"
            )
        # Re-check through a bounded read: a file may grow between stat() and
        # open(), and an unbounded read here would defeat the memory guarantee.
        with open(full_path, "rb") as handle:
            content = handle.read(_MAX_FILE_BYTES + 1)
        if len(content) > _MAX_FILE_BYTES:
            raise RuntimeError(
                f"GitHub backup file grew beyond {_MAX_FILE_BYTES} bytes: "
                f"{relative_path}"
            )
        return content


def _iter_markdown_paths(root_dir: str) -> Iterator[str]:
    """Depth-first scandir traversal without materializing directory listings."""
    iterators: list[os.ScandirIterator] = []
    try:
        try:
            iterators.append(os.scandir(root_dir))
        except OSError as exc:
            logger.warning(f"[github_sync] cannot scan {root_dir}: {exc}")
            return

        while iterators:
            current = iterators[-1]
            try:
                entry = next(current)
            except StopIteration:
                current.close()
                iterators.pop()
                continue
            except OSError as exc:
                logger.warning(f"[github_sync] directory scan failed: {exc}")
                current.close()
                iterators.pop()
                continue

            try:
                # Match os.walk(..., followlinks=False): recurse into real
                # directories, but never follow a symlinked directory tree.
                if entry.is_dir(follow_symlinks=False):
                    try:
                        iterators.append(os.scandir(entry.path))
                    except OSError as exc:
                        logger.warning(f"[github_sync] cannot scan {entry.path}: {exc}")
                    continue
                if entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                    yield entry.path
            except OSError as exc:
                logger.warning(f"[github_sync] skip {entry.path}: {exc}")
    finally:
        for iterator in reversed(iterators):
            iterator.close()


class GitHubSync:
    """Uploads bucket .md files to a GitHub repository in bulk."""

    def __init__(
        self,
        token: str,
        repo: str,
        branch: str = "main",
        path_prefix: str = "loci",
    ):
        self.token = token
        self.repo = repo.strip()          # "owner/repo"
        self.branch = branch.strip() or "main"
        self.path_prefix = path_prefix.strip().strip("/")

        self._headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        self.last_sync: str | None = None
        self.last_status: str = "idle"   # idle | ok | error
        self.last_error: str = ""
        self.last_count: int = 0
        self.is_validated: bool = False   # set to True once validate() succeeds
        # A consecutive-failure counter. An automatic backup can fail several times running
        # without the user noticing a thing — they believe there is a backup when there is
        # not. Reset to zero on every success, incremented on every failure, so the
        # diagnostics panel can decide when to escalate to a conspicuous alarm.
        self.consecutive_failures: int = 0
        # Manual and scheduled backups share one instance.  Serializing them
        # prevents two bounded jobs from adding up to an unbounded peak.
        self._sync_lock = asyncio.Lock()

    # --------------------------------------------------------
    # Public interface
    # --------------------------------------------------------

    async def sync(self, buckets_dir: str) -> dict[str, Any]:
        """Sync every .md under buckets_dir to GitHub. Returns a result dict."""
        async with self._sync_lock:
            try:
                files = self._collect_files(buckets_dir)
                if not files:
                    self.last_status = "ok"
                    self.last_error = ""
                    self.last_sync = _src._now()
                    self.last_count = 0
                    return {"ok": True, "uploaded": 0, "message": "无可同步文件"}

                count = await self._batch_commit(files)
                self.last_sync = _src._now()
                self.last_status = "ok"
                self.last_error = ""
                self.last_count = count
                self.consecutive_failures = 0
                return {"ok": True, "uploaded": count}
            except Exception as e:
                self.last_status = "error"
                self.last_error = str(e)
                self.consecutive_failures += 1
                logger.error(f"[github_sync] sync failed (连续 {self.consecutive_failures} 次): {e}")
                return {"ok": False, "error": str(e)}

    async def import_from_github(self, buckets_dir: str) -> dict[str, Any]:
        """Pull every .md under path_prefix from the GitHub repository back into the local
        buckets_dir (restore / rollback).

        The inverse of sync(). Merge-and-overwrite semantics: a file with the same relative
        path is overwritten by the GitHub copy, while a file that exists only locally is
        left alone. embeddings.db is not in the repository, so the caller should run a
        backfill afterwards to rebuild the vectors. Path-traversal protection is included:
        repository content is untrusted, and `../` must not escape.
        """
        async with self._sync_lock:
            return await self._import_from_github_locked(buckets_dir)

    async def _import_from_github_locked(self, buckets_dir: str) -> dict[str, Any]:
        """Serialized implementation shared with the backup lock."""
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as c:
                # Fetch the branch HEAD -> the commit tree -> recursively list every blob
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/ref/heads/{self.branch}")
                if _is_empty_repo_response(r):
                    return {
                        "ok": True,
                        "imported": 0,
                        "skipped": 0,
                        "message": f"GitHub 仓库 {self.repo} 还是空仓库，暂无可导入的记忆文件",
                    }
                if r.status_code == 404:
                    return {"ok": False, "error": f"分支 {self.branch} 不存在"}
                r.raise_for_status()
                head_sha = r.json()["object"]["sha"]
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/commits/{head_sha}")
                r.raise_for_status()
                tree_sha = r.json()["tree"]["sha"]
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/trees/{tree_sha}?recursive=1")
                r.raise_for_status()
                tj = r.json()
                tree = tj.get("tree", [])
                truncated = bool(tj.get("truncated"))
                if truncated:
                    raise RuntimeError(
                        "GitHub returned a truncated tree; refusing an incomplete restore"
                    )
                if not isinstance(tree, list):
                    raise RuntimeError("GitHub tree response is not a list")

                prefix = (self.path_prefix + "/") if self.path_prefix else ""
                manifest_path = f"{prefix}{_MANIFEST_FILENAME}"
                manifest_item = next(
                    (
                        t for t in tree
                        if t.get("type") == "blob" and t.get("path") == manifest_path
                    ),
                    None,
                )
                backup_manifest = await self._read_backup_manifest_summary(c, manifest_item) if manifest_item else {"present": False}
                if manifest_item and not backup_manifest.get("present"):
                    raise RuntimeError(
                        "GitHub backup manifest is unreadable; refusing an unverified restore"
                    )
                manifest_entries = backup_manifest.pop("_entries", None)
                targets = [
                    t for t in tree
                    if t.get("type") == "blob" and t.get("path", "").startswith(prefix)
                    and t["path"].endswith(".md")
                ]
                if len(targets) > _MAX_BACKUP_FILES:
                    raise RuntimeError(
                        f"GitHub restore has more than {_MAX_BACKUP_FILES} Markdown files"
                    )
                target_by_rel: dict[str, dict[str, Any]] = {}
                declared_total = 0
                for target in targets:
                    rel = str(target.get("path", ""))[len(prefix):]
                    if (
                        not rel
                        or len(rel.encode("utf-8")) > _MAX_BACKUP_PATH_BYTES
                        or rel in target_by_rel
                    ):
                        raise RuntimeError(f"unsafe or duplicate GitHub restore path: {rel[:200]}")
                    try:
                        declared_size = int(target.get("size", 0) or 0)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise RuntimeError(f"invalid GitHub blob size: {rel}") from exc
                    if declared_size < 0 or declared_size > _MAX_FILE_BYTES:
                        raise RuntimeError(
                            f"GitHub restore file exceeds {_MAX_FILE_BYTES} bytes: {rel}"
                        )
                    declared_total += declared_size
                    if declared_total > _MAX_RESTORE_TOTAL_BYTES:
                        raise RuntimeError(
                            "GitHub restore exceeds the total decoded-byte limit"
                        )
                    target_by_rel[rel] = target

                expected_manifest: dict[str, dict[str, Any]] | None = None
                if backup_manifest.get("present"):
                    expected_manifest = self._validate_restore_manifest(
                        manifest_entries, target_by_rel
                    )
                    manifest_total = sum(
                        item["bytes"] for item in expected_manifest.values()
                    )
                    if (
                        backup_manifest.get("schema_version") != 1
                        or backup_manifest.get("file_count") != len(expected_manifest)
                        or backup_manifest.get("total_bytes") != manifest_total
                    ):
                        raise RuntimeError("backup manifest summary is inconsistent")
                    # Git Trees are cumulative; files deleted locally can remain
                    # in an older base tree.  A valid manifest is the canonical
                    # file set, so ignore stale remote Markdown outside it.
                    target_by_rel = {
                        rel: target_by_rel[rel] for rel in expected_manifest
                    }
                    targets = list(target_by_rel.values())
                if not targets:
                    return {"ok": True, "imported": 0, "skipped": 0,
                            "message": f"GitHub 仓库 {self.repo} 的 {prefix or '/'} 下没有 .md 记忆文件",
                            "backup_manifest": backup_manifest}

                base = os.path.abspath(buckets_dir)
                imported = 0
                skipped = 0
                restored_bytes = 0
                errors: list[str] = []
                for t in targets:
                    rel = t["path"][len(prefix):]
                    if not rel:
                        continue
                    # Path-traversal protection: once resolved it must still be inside buckets_dir
                    dest = os.path.abspath(os.path.join(base, rel))
                    if dest != base and not dest.startswith(base + os.sep):
                        skipped += 1
                        errors.append(f"{rel}: 越界路径，已跳过")
                        continue
                    try:
                        rb = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/blobs/{t['sha']}")
                        rb.raise_for_status()
                        bj = rb.json()
                        if bj.get("encoding") == "base64":
                            encoded = "".join(str(bj.get("content", "") or "").split())
                            data = base64.b64decode(encoded, validate=True)
                        else:
                            data = (bj.get("content", "") or "").encode("utf-8")
                        if len(data) > _MAX_FILE_BYTES:
                            raise RuntimeError(
                                f"decoded file exceeds {_MAX_FILE_BYTES} bytes"
                            )
                        restored_bytes += len(data)
                        if restored_bytes > _MAX_RESTORE_TOTAL_BYTES:
                            raise RuntimeError("restore exceeds total decoded-byte limit")
                        if expected_manifest is not None:
                            expected = expected_manifest[rel]
                            if (
                                len(data) != expected["bytes"]
                                or hashlib.sha256(data).hexdigest()
                                != expected["sha256"]
                            ):
                                raise RuntimeError("backup manifest integrity mismatch")
                        self._assert_safe_restore_destination(base, rel)
                        # The _win_long_path prefix sidesteps Windows' 260-character
                        # MAX_PATH. Restoring a backup is the number one reason that prefix
                        # exists: a deep sanitised domain path on top of an already long
                        # install directory really does exceed the limit (the same problem
                        # utils.atomic_write_text walked into and fixed).
                        dest_long = _win_long_path(dest)
                        os.makedirs(_win_long_path(os.path.dirname(dest)), exist_ok=True)
                        # Atomic write: importing overwrites local memories, and an
                        # interruption halfway must never leave a half-written file behind.
                        _tmp = f"{dest}.{uuid.uuid4().hex}.tmp"
                        _tmp_long = _win_long_path(_tmp)
                        try:
                            with open(_tmp_long, "wb") as f:
                                f.write(data)
                                f.flush()
                                os.fsync(f.fileno())
                            os.replace(_tmp_long, dest_long)
                        except Exception:
                            if os.path.exists(_tmp_long):
                                try:
                                    os.remove(_tmp_long)
                                except OSError:
                                    pass
                            raise
                        imported += 1
                    except Exception as e:
                        skipped += 1
                        errors.append(f"{rel}: {e}")

                self.last_sync = _src._now()
                restore_ok = skipped == 0
                self.last_status = "ok" if restore_ok else "error"
                return {
                    "ok": restore_ok,
                    "imported": imported,
                    "skipped": skipped,
                    "total": len(targets),
                    "truncated": False,
                    "errors": errors[:10],
                    "backup_manifest": backup_manifest,
                }
        except Exception as e:
            logger.error(f"[github_sync] import failed: {e}")
            return {"ok": False, "error": str(e)}

    async def validate(self) -> dict[str, Any]:
        """Verify that the token and repo are reachable and carry write permission
        (contents: write)."""
        try:
            async with httpx.AsyncClient(headers=self._headers, timeout=15.0) as c:
                r = await c.get(f"{_API}/repos/{self.repo}")
                if r.status_code == 404:
                    return {"ok": False, "error": f"仓库 {self.repo} 不存在或无权限访问"}
                if r.status_code == 401:
                    return {"ok": False, "error": "Token 无效或已过期"}
                r.raise_for_status()
                data = r.json()

                # Check write permission via `permissions.push` field
                # (GitHub returns this field when authenticated)
                perms = data.get("permissions", {})
                can_push = perms.get("push", False) or perms.get("admin", False)
                if perms and not can_push:
                    return {
                        "ok": False,
                        "error": "Token 只有读权限，无法上传文件。请在 GitHub → Settings → Developer settings → Fine-grained tokens 中将 Contents 权限设为 Read and write",
                    }

                self.is_validated = True
                return {
                    "ok": True,
                    "repo_full_name": data.get("full_name", self.repo),
                    "private": data.get("private", False),
                    "default_branch": data.get("default_branch", "main"),
                    "can_push": can_push,
                }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.token and self.repo),
            "repo": self.repo,
            "branch": self.branch,
            "path_prefix": self.path_prefix,
            "last_sync": self.last_sync,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_count": self.last_count,
            "is_validated": self.is_validated,
            "consecutive_failures": self.consecutive_failures,
        }

    # --------------------------------------------------------
    # Internals
    # --------------------------------------------------------

    def _collect_files(self, buckets_dir: str) -> Mapping[str, bytes]:
        """Index eligible Markdown paths without retaining their bodies."""
        paths: dict[str, str] = {}
        if not os.path.isdir(buckets_dir):
            return _LazyMarkdownFiles(paths)
        base_real = os.path.realpath(buckets_dir)
        for full in _iter_markdown_paths(buckets_dir):
            try:
                full_real = os.path.realpath(full)
                if os.path.commonpath((base_real, full_real)) != base_real:
                    logger.warning(
                        "[github_sync] skip path outside vault: %s", full
                    )
                    continue
                size = os.path.getsize(full)
                if size > _MAX_FILE_BYTES:
                    logger.warning(
                        f"[github_sync] skip {os.path.basename(full)}: "
                        f"too large ({size} bytes)"
                    )
                    continue
                relative = os.path.relpath(full, buckets_dir).replace("\\", "/")
                if len(relative.encode("utf-8")) > _MAX_BACKUP_PATH_BYTES:
                    raise RuntimeError(
                        f"GitHub backup path exceeds {_MAX_BACKUP_PATH_BYTES} bytes: "
                        f"{relative[:200]}"
                    )
                if relative not in paths and len(paths) >= _MAX_BACKUP_FILES:
                    raise RuntimeError(
                        f"GitHub backup has more than {_MAX_BACKUP_FILES} files; "
                        "split or archive the vault before retrying"
                    )
                paths[relative] = full
            except OSError as e:
                logger.warning(f"[github_sync] skip {os.path.basename(full)}: {e}")
        return _LazyMarkdownFiles(paths)

    @staticmethod
    def _assert_safe_restore_destination(base: str, relative_path: str) -> None:
        """Reject symlink/junction parents before a restore write."""
        base_real = os.path.realpath(base)
        current = base
        parts = relative_path.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise RuntimeError("unsafe restore path components")
        for part in parts:
            current = os.path.join(current, part)
            if os.path.lexists(current):
                if os.path.islink(current):
                    raise RuntimeError("restore path contains a symbolic link")
                current_real = os.path.realpath(current)
                if os.path.commonpath((base_real, current_real)) != base_real:
                    raise RuntimeError("restore path resolves outside the vault")

    @staticmethod
    def _validate_restore_manifest(
        raw_entries: object,
        targets: Mapping[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_entries, list) or len(raw_entries) > len(targets):
            raise RuntimeError("backup manifest file set does not match GitHub tree")
        expected: dict[str, dict[str, Any]] = {}
        total = 0
        for item in raw_entries:
            if not isinstance(item, dict):
                raise RuntimeError("backup manifest entry must be an object")
            path = item.get("path")
            digest = item.get("sha256")
            try:
                size = int(item.get("bytes"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("backup manifest contains an invalid size") from exc
            if (
                not isinstance(path, str)
                or path not in targets
                or path in expected
                or len(path.encode("utf-8")) > _MAX_BACKUP_PATH_BYTES
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in digest)
                or size < 0
                or size > _MAX_FILE_BYTES
            ):
                raise RuntimeError("backup manifest contains an invalid entry")
            total += size
            if total > _MAX_RESTORE_TOTAL_BYTES:
                raise RuntimeError("backup manifest exceeds total decoded-byte limit")
            expected[path] = {"bytes": size, "sha256": digest.lower()}
        return expected

    def _build_backup_manifest(self, files: Mapping[str, bytes]) -> dict[str, Any]:
        """Build a JSON-safe manifest for the markdown files in one sync."""
        if len(files) > _MAX_BACKUP_FILES:
            raise RuntimeError(
                f"GitHub backup has more than {_MAX_BACKUP_FILES} files; "
                "split or archive the vault before retrying"
            )
        entries = []
        total_bytes = 0
        # Sorting keys is cheap.  Sorting ``files.items()`` would force a lazy
        # mapping to materialize every body at once and reintroduce the OOM.
        for rel_path in sorted(files):
            if len(str(rel_path).encode("utf-8")) > _MAX_BACKUP_PATH_BYTES:
                raise RuntimeError(
                    f"GitHub backup path exceeds {_MAX_BACKUP_PATH_BYTES} bytes: "
                    f"{str(rel_path)[:200]}"
                )
            content = files[rel_path]
            size = len(content)
            total_bytes += size
            entries.append({
                "path": rel_path,
                "bytes": size,
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        return {
            "schema_version": 1,
            "source": "loci-brain",
            "generated_at": _src._now(),
            "repo": self.repo,
            "branch": self.branch,
            "path_prefix": self.path_prefix,
            "file_count": len(entries),
            "total_bytes": total_bytes,
            "files": entries,
        }

    def _iter_file_chunks(
        self,
        files: Mapping[str, bytes],
        manifest: Mapping[str, Any],
    ) -> Iterator[list[tuple[str, bytes]]]:
        """Yield verified bodies bounded by both count and decoded byte size."""
        expected_items = manifest.get("files", [])
        if not isinstance(expected_items, list) or len(expected_items) != len(files):
            raise RuntimeError("GitHub backup manifest does not match the file index")
        chunk: list[tuple[str, bytes]] = []
        chunk_bytes = 0
        # The manifest is already sorted.  Iterating its entries directly avoids
        # materializing a second O(N) path→metadata dictionary.
        for expected_item in expected_items:
            if not isinstance(expected_item, Mapping) or not expected_item.get("path"):
                raise RuntimeError("GitHub backup manifest contains an invalid file entry")
            relative_path = str(expected_item["path"])
            try:
                content = files[relative_path]
            except KeyError as exc:
                raise RuntimeError(
                    f"GitHub backup source changed while being indexed: {relative_path}; retry"
                ) from exc
            if not isinstance(content, bytes):
                content = bytes(content)
            expected_size = int(expected_item.get("bytes", -1))
            if (
                expected_size != len(content)
                or str(expected_item.get("sha256") or "")
                != hashlib.sha256(content).hexdigest()
            ):
                raise RuntimeError(
                    f"GitHub backup source changed while being read: {relative_path}; retry"
                )
            if chunk and (
                len(chunk) >= _TREE_CHUNK
                or chunk_bytes + len(content) > _TREE_CHUNK_BYTES
            ):
                yield chunk
                chunk = []
                chunk_bytes = 0
            chunk.append((relative_path, content))
            chunk_bytes += len(content)
        if chunk:
            yield chunk

    async def _batch_commit(self, files: Mapping[str, bytes]) -> int:
        """Commit every file at once through the Git Trees API; returns how many were
        uploaded.

        The key point: a tree entry inlines its `content` (UTF-8 text) directly and GitHub
        creates the blob while building the tree — so several hundred files take 1..N
        /git/trees requests rather than one /git/blobs request per file. The latter
        saturates GitHub's *secondary rate limit* almost immediately (a 403), which was the
        root cause of syncs mysteriously returning 403.

        Large batches are committed in chunks of _TREE_CHUNK, chained together through
        base_tree, with a single commit at the end. Every request carries exponential
        backoff retries to absorb the occasional secondary rate limit.
        """
        async with httpx.AsyncClient(headers=self._headers, timeout=_TIMEOUT) as c:
            # 1. Get the branch HEAD commit SHA. An empty GitHub repo has no refs at all and
            #    returns 409 here.
            r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/ref/heads/{self.branch}")
            bootstrap_branch = _is_empty_repo_response(r)
            head_sha: str | None = None
            base_tree_sha: str | None = None
            if r.status_code == 404:
                raise RuntimeError(f"分支 {self.branch} 不存在，请先在 GitHub 上创建该分支")
            if not bootstrap_branch:
                r.raise_for_status()
                head_sha = r.json()["object"]["sha"]

                # 2. Get the tree SHA belonging to that HEAD commit
                r = await self._request(c, "GET", f"{_API}/repos/{self.repo}/git/commits/{head_sha}")
                r.raise_for_status()
                base_tree_sha = r.json()["tree"]["sha"]

            # 3. Build a hard-bounded manifest first.  Lazy production mappings
            # read one body at a time during this pass and retain metadata only.
            manifest_path = f"{self.path_prefix}/{_MANIFEST_FILENAME}" if self.path_prefix else _MANIFEST_FILENAME
            manifest = self._build_backup_manifest(files)
            manifest_content = json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True
            )
            manifest_entry = {
                "path": manifest_path,
                "mode": "100644",
                "type": "blob",
                "content": manifest_content,
            }
            # Account for the outer Git Trees JSON escaping too.  Count/path
            # limits bound construction; this payload limit bounds the request.
            manifest_payload_bytes = len(json.dumps(
                {"tree": [manifest_entry]},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8"))
            if manifest_payload_bytes > _MAX_MANIFEST_PAYLOAD_BYTES:
                raise RuntimeError(
                    "GitHub backup manifest exceeds the bounded request limit "
                    f"({_MAX_MANIFEST_PAYLOAD_BYTES} bytes); split or archive the vault"
                )

            # 4. Read/decode/upload one bounded file chunk at a time.
            cur_base = base_tree_sha
            for file_chunk in self._iter_file_chunks(files, manifest):
                tree_entries: list[dict[str, Any]] = []
                for rel_path, content in file_chunk:
                    gh_path = f"{self.path_prefix}/{rel_path}" if self.path_prefix else rel_path
                    try:
                        text = content.decode("utf-8")
                        entry = {"path": gh_path, "mode": "100644", "type": "blob", "content": text}
                    except UnicodeDecodeError:
                        rb = await self._request(
                            c, "POST", f"{_API}/repos/{self.repo}/git/blobs",
                            json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
                        )
                        rb.raise_for_status()
                        blob_sha = rb.json()["sha"]
                        del rb
                        entry = {"path": gh_path, "mode": "100644", "type": "blob", "sha": blob_sha}
                    tree_entries.append(entry)
                tree_payload: dict[str, Any] = {"tree": tree_entries}
                if cur_base:
                    tree_payload["base_tree"] = cur_base
                r = await self._request(
                    c, "POST", f"{_API}/repos/{self.repo}/git/trees",
                    json=tree_payload,
                )
                r.raise_for_status()
                cur_base = r.json()["sha"]
                # httpx responses retain their originating request (including
                # its serialized JSON body).  Drop it before reading the next
                # chunk so two request bodies cannot overlap in memory.
                del r
                del tree_payload, tree_entries, file_chunk

            # Keep the manifest in its own bounded request.  Otherwise a large
            # manifest silently sits on top of the file-chunk byte budget.
            manifest_payload: dict[str, Any] = {"tree": [manifest_entry]}
            if cur_base:
                manifest_payload["base_tree"] = cur_base
            r = await self._request(
                c,
                "POST",
                f"{_API}/repos/{self.repo}/git/trees",
                json=manifest_payload,
            )
            r.raise_for_status()
            cur_base = r.json()["sha"]
            del r, manifest_payload, manifest_entry, manifest_content
            new_tree_sha = cur_base

            # 5. Create the commit
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            r = await self._request(
                c, "POST", f"{_API}/repos/{self.repo}/git/commits",
                json={
                    "message": f"Loci Brain sync — {now_str} ({len(files)} files)",
                    "tree": new_tree_sha,
                    "parents": [head_sha] if head_sha else [],
                },
            )
            r.raise_for_status()
            commit_sha: str = r.json()["sha"]

            # 6. Update the existing branch ref; on an empty repo's first commit, create it
            if bootstrap_branch:
                r = await self._request(
                    c, "POST", f"{_API}/repos/{self.repo}/git/refs",
                    json={"ref": f"refs/heads/{self.branch}", "sha": commit_sha},
                )
            else:
                r = await self._request(
                    c, "PATCH", f"{_API}/repos/{self.repo}/git/refs/heads/{self.branch}",
                    json={"sha": commit_sha, "force": False},
                )
            r.raise_for_status()

        return len(files)

    async def _read_backup_manifest_summary(
        self,
        client: httpx.AsyncClient,
        manifest_item: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            declared_size = int(manifest_item.get("size", -1))
            if declared_size < 0 or declared_size > _MAX_MANIFEST_PAYLOAD_BYTES:
                raise ValueError("backup manifest exceeds the decoded-byte limit")
            sha = manifest_item.get("sha", "")
            if not sha:
                return {"present": False}
            rb = await self._request(client, "GET", f"{_API}/repos/{self.repo}/git/blobs/{sha}")
            rb.raise_for_status()
            bj = rb.json()
            if bj.get("encoding") == "base64":
                encoded = bj.get("content", "")
                if not isinstance(encoded, str) or len(encoded) > _MAX_MANIFEST_BASE64_BYTES:
                    raise ValueError("backup manifest base64 payload is too large")
                compact = "".join(encoded.split())
                decoded = base64.b64decode(compact, validate=True)
                if len(decoded) > _MAX_MANIFEST_PAYLOAD_BYTES:
                    raise ValueError("backup manifest exceeds the decoded-byte limit")
                raw = decoded.decode("utf-8")
            else:
                raw = str(bj.get("content", "") or "")
                if (
                    len(raw) > _MAX_MANIFEST_PAYLOAD_BYTES
                    or len(raw.encode("utf-8")) > _MAX_MANIFEST_PAYLOAD_BYTES
                ):
                    raise ValueError("backup manifest exceeds the decoded-byte limit")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("backup manifest root must be an object")
            return {
                "present": True,
                "schema_version": data.get("schema_version"),
                "generated_at": data.get("generated_at", ""),
                "file_count": int(data.get("file_count") or 0),
                "total_bytes": int(data.get("total_bytes") or 0),
                "_entries": data.get("files"),
            }
        except Exception as e:
            return {"present": False, "error": str(e)[:200]}

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        _max_retries: int = 4,
    ) -> httpx.Response:
        """A request with backoff retries, aimed squarely at GitHub's secondary rate limit
        (403/429 plus Retry-After).

        An ordinary 4xx — a permission problem, a 404 — is returned straight away for the
        caller's raise_for_status to deal with, and is not retried.
        """
        for attempt in range(_max_retries + 1):
            resp = await client.request(method, url, json=json)
            if resp.status_code not in (403, 429):
                return resp
            # Decide whether this is the secondary rate limit rather than a genuine
            # permission 403
            body_l = resp.text.lower()
            is_rate = (
                "rate limit" in body_l
                or "retry-after" in {k.lower() for k in resp.headers}
                or resp.headers.get("x-ratelimit-remaining") == "0"
            )
            if not is_rate or attempt == _max_retries:
                return resp
            # Work out how long to wait: Retry-After if given, otherwise exponential backoff
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                wait = int(retry_after)
            else:
                wait = min(2 ** attempt, 30)
            logger.warning(f"[github_sync] secondary rate limit, retry in {wait}s (attempt {attempt + 1})")
            await asyncio.sleep(wait)
        return resp


def _is_empty_repo_response(resp: httpx.Response) -> bool:
    """GitHub returns 409 when refs are requested from a zero-commit repo."""
    if resp.status_code != 409:
        return False
    try:
        message = str(resp.json().get("message", ""))
    except Exception:
        message = resp.text
    return "empty" in message.lower()
