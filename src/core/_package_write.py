"""
========================================
_package_write.py — writing one package entry into the library
========================================

The files core/package_import.MigrateEngine writes while it applies a package: a new
entry is created without ever replacing a file; an overwrite stages the new text, archives
the old version under a fresh id and rolls both back if any step fails. Every method of
EntryWriter runs in a worker thread, under the entry's own mutation lock, which the engine
holds.
========================================
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import Any, Optional

import frontmatter

from ._package_read import (_ParsedBucket, _parse_md_meta, bucket_content_limit,
                            member_limit, normalize_import_metadata, read_member)
from .backup_archive import BackupArchiveError

from utils import _win_long_path, now_iso, safe_path, sanitize_name  # type: ignore

logger = logging.getLogger("loci_brain.migrate")

# bucket type -> storage subdirectory (kept in step with bucket_manager.py)
_TYPE_SUBDIR: dict[str, str] = {
    "permanent": "permanent",
    "dynamic": "dynamic",
    "archive": "archive",
    "archived": "archive",
    "feel": "feel",
}

# The default subdirectory, used for an unknown type
_DEFAULT_SUBDIR = "dynamic"

# The fields that name other entries by id (besides prov and the old `from`).
_LINK_FIELDS = ("supersedes", "superseded_by", "covered_by", "cover", "exception_of")


# ============================================================
# Helpers
# ============================================================

def _safe_unlink(path: str) -> None:
    """Best-effort deletion of a staging file. A failure is only logged, so that cleanup
    can never mask the real exception."""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        logger.warning(f"[migrate] failed to clean up staged file {path}: {e}")


def _same_file(path: str, data: bytes) -> bool:
    """Is the file at `path` exactly these bytes?"""
    try:
        if os.path.getsize(path) != len(data):
            return False
        with open(_win_long_path(path), "rb") as handle:
            return handle.read() == data
    except OSError:
        return False


def _new_entry_id(taken) -> str:
    """A fresh entry id in the library's own shape (12 hex characters) that `taken(id)`
    says is free."""
    while True:
        candidate = uuid.uuid4().hex[:12]
        if not taken(candidate):
            return candidate


def _remap_links(meta: dict, id_map: dict[str, str]) -> bool:
    """Point an entry's links at the ids their targets were written under: prov targets,
    the old `from`, the version chain, covers, the standing entry of a hold, and the
    attachment paths of an entry that moved (`_media/<id>/…`). A quoted line names a
    source, never an entry id, and is left as it is unless it equals one. Returns True when
    something changed."""
    if not id_map:
        return False
    changed = False
    prov = meta.get("prov")
    if isinstance(prov, list):
        lines = []
        for line in prov:
            target = str(line.get("target") or "") if isinstance(line, dict) else ""
            if target in id_map:
                line = {**line, "target": id_map[target]}
                changed = True
            lines.append(line)
        meta["prov"] = lines
    for name in ("from",) + _LINK_FIELDS:
        value = meta.get(name)
        if isinstance(value, list):
            mapped = [id_map.get(str(v).strip(), v) for v in value]
            if mapped != value:
                meta[name] = mapped
                changed = True
        elif isinstance(value, str) and value.strip():
            parts = [p.strip() for p in value.split(",")]
            mapped = [id_map.get(p, p) for p in parts]
            if mapped != parts:
                meta[name] = ",".join(mapped)
                changed = True
    media = meta.get("media")
    if isinstance(media, list):
        items = []
        for item in media:
            parts = str(item.get("path") or "").split("/") if isinstance(item, dict) else []
            if len(parts) >= 3 and parts[0] == "_media" and parts[1] in id_map:
                item = {**item, "path": "/".join(["_media", id_map[parts[1]], *parts[2:]])}
                changed = True
            items.append(item)
        meta["media"] = items
    return changed


def package_path(arc_path: str, buckets_dir: str) -> str:
    """The library path a package member came from (`buckets/<dir>/...`), when it lies
    in one of the memory folders; "" otherwise."""
    rel = arc_path[len("buckets/"):] if arc_path.startswith("buckets/") else ""
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] not in set(_TYPE_SUBDIR.values()):
        return ""
    try:
        return str(safe_path(buckets_dir, rel))
    except Exception:
        return ""


def atomic_write(path: str, rendered: str) -> None:
    # The _win_long_path prefix sidesteps Windows' 260-character MAX_PATH: a sanitised
    # nested domain path under a deep buckets_dir really does exceed it (the same
    # problem utils.atomic_write_text walked into and fixed — this stays consistent with
    # it rather than each place inventing its own).
    temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    temp_path_long = _win_long_path(temp_path)
    try:
        # newline="": the text is written as given, so a restored file is the same
        # bytes on every platform.
        with open(temp_path_long, "w", encoding="utf-8", newline="") as f:
            f.write(rendered)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path_long, _win_long_path(path))
    finally:
        try:
            if os.path.exists(temp_path_long):
                os.unlink(temp_path_long)
        except OSError:
            pass


def atomic_create(path: str, rendered: str) -> None:
    """Atomically create ``path`` while refusing to replace any file."""

    temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    temp_path_long = _win_long_path(temp_path)
    target_long = _win_long_path(path)
    try:
        with open(temp_path_long, "x", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-linking a complete same-filesystem staging inode gives us
        # O_EXCL semantics on both POSIX and Windows; os.replace would
        # silently overwrite an unrelated file with the same filename.
        os.link(temp_path_long, target_long)
    finally:
        _safe_unlink(temp_path_long)


# ============================================================
# EntryWriter
# ============================================================

class EntryWriter:
    """Writes the package's entries into one library: the engine's config (size limits)
    and its bucket manager (metadata normalisation, the id finder, the archive folder, the
    vector outbox)."""

    def __init__(self, config: dict, bucket_mgr: Any) -> None:
        self._config = config
        self._bucket_mgr = bucket_mgr

    def _id_taken(self, candidate: str) -> bool:
        finder = getattr(self._bucket_mgr, "_find_bucket_file", None)
        return bool(finder(candidate)) if callable(finder) else False

    def render_bucket(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str,
        link_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str, str]:
        """(Runs in a thread.) Pure computation: parse the frontmatter, work out the target
        path and the serialised markdown. Apart from os.makedirs creating directories, it
        performs no disk writes at all. `link_map`: package ids written under another id,
        which the entry's links are pointed at (`_remap_links`).

        Returns (content, target_path, rendered).
        """
        raw = read_member(
            pb.md_bytes if pb.md_bytes is not None else pb.md_path,
            limit=member_limit(self._config),
            label=pb.arc_path,
        )
        meta, content = _parse_md_meta(raw)
        written_id = str(meta.get("id") or "")
        meta = normalize_import_metadata(self._bucket_mgr, self._config, meta)
        content_size = len(content.encode("utf-8"))
        if content_size > bucket_content_limit(self._config):
            raise BackupArchiveError(
                f"{pb.arc_path} 正文过大（{content_size} bytes > "
                f"{bucket_content_limit(self._config)}）"
            )

        relinked = _remap_links(meta, link_map or {})

        # A file that already names itself by the id it is restored under, and links to
        # nothing that moved, goes back byte for byte, at its path in the library it came
        # from: the Markdown is the library, and re-serialising it would be a second writer
        # of every field.
        if written_id == target_id and not relinked:
            verbatim_path = package_path(pb.arc_path, buckets_dir)
            if verbatim_path:
                os.makedirs(os.path.dirname(verbatim_path), exist_ok=True)
                return content, verbatim_path, raw.decode("utf-8")

        # Always write an explicit ID; restoring never depends on guessing from a filename.
        meta["id"] = target_id
        target_path = self._target_path(meta, pb, target_id, buckets_dir)

        # Re-serialise the frontmatter plus body
        post = frontmatter.Post(content, **meta)
        rendered = frontmatter.dumps(post)
        return content, target_path, rendered

    @staticmethod
    def _target_path(meta: dict, pb: _ParsedBucket, target_id: str, buckets_dir: str) -> str:
        """Where a re-serialised entry goes: by type and primary domain, as bucket_manager
        files it. Creates the directory."""
        # Determine the target directory (by type and domain)
        btype = str(meta.get("type") or pb.bucket_type or "dynamic")
        subdir = _TYPE_SUBDIR.get(btype, _DEFAULT_SUBDIR)

        # Take the primary domain (kept in step with bucket_manager)
        domain = meta.get("domain") or pb.domain or []
        if btype == "feel":
            primary_domain = "沉淀物"
        elif isinstance(domain, list) and domain:
            primary_domain = str(domain[0])
        elif isinstance(domain, str) and domain:
            primary_domain = str(domain)
        else:
            primary_domain = "general"

        primary_domain = sanitize_name(primary_domain)
        target_dir = str(safe_path(buckets_dir, os.path.join(subdir, primary_domain)))
        os.makedirs(target_dir, exist_ok=True)

        safe_id = re.sub(r"[^\w.-]", "_", target_id, flags=re.UNICODE)[:200]
        if not safe_id:
            raise BackupArchiveError("恢复目标 ID 无法生成安全文件名")
        safe_name = sanitize_name(str(meta.get("name") or pb.name or target_id))[:40]
        return str(safe_path(target_dir, f"{safe_name}_{safe_id}.md"))

    def write_bucket_file(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str,
        link_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str]:
        """Write one new bucket without replacing an existing path."""
        _content, target_path, rendered = self.render_bucket(pb, target_id, buckets_dir,
                                                             link_map)
        atomic_create(target_path, rendered)
        logger.debug(f"[migrate] wrote {target_path} (id={target_id})")
        return target_id, target_path

    def write_bucket_file_staged(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str,
        link_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str, str]:
        """(Runs in a thread.) Write the new content to a staging file in the same directory
        as target_path, without touching target_path itself.

        Exclusively for the overwrite conflict path: only once the write has succeeded does
        the caller decide whether to touch the old bucket, and a failed write leaves the old
        bucket entirely untouched. Returns (content, target_path, staged_path); the caller
        does its own os.replace(staged_path, target_path) once it has confirmed the old
        bucket has been dealt with safely.
        """
        content, target_path, rendered = self.render_bucket(pb, target_id, buckets_dir,
                                                            link_map)
        staged_path = f"{target_path}.staging-{uuid.uuid4().hex}"
        atomic_write(staged_path, rendered)
        logger.debug(f"[migrate] staged {staged_path} (id={target_id}, target={target_path})")
        return content, target_path, staged_path

    def write_historical_copy(
        self,
        existing_path: str,
        bucket_id: str,
        buckets_dir: str,
    ) -> str:
        """Create the pre-overwrite version under a fresh archived ID in the library's own
        shape (12 hex characters), so it reads by id like any other entry."""

        post = frontmatter.load(existing_path)
        new_id = _new_entry_id(self._id_taken)
        post["id"] = new_id
        post["type"] = "archived"
        post["superseded_by"] = bucket_id
        post["archived_at"] = now_iso()
        archive_dir = str(
            getattr(self._bucket_mgr, "archive_dir", "")
            or safe_path(buckets_dir, "archive")
        )
        os.makedirs(archive_dir, exist_ok=True)
        safe_name = sanitize_name(str(post.get("name") or "memory"))[:40]
        target_path = str(safe_path(archive_dir, f"{safe_name}_{new_id}.md"))
        atomic_create(target_path, frontmatter.dumps(post))
        return target_path

    def overwrite_bucket_transaction(
        self,
        pb: _ParsedBucket,
        target_id: str,
        buckets_dir: str,
        existing_path: str,
        link_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str]:
        """Preserve old data and commit an overwrite with rollback on failure: stage the
        new text, archive the old version, publish, then forget the old vectors."""

        _content, target_path, staged_path = self.write_bucket_file_staged(
            pb, target_id, buckets_dir, link_map
        )
        historical_path = ""
        target_created = False
        same_target = (
            os.path.normcase(os.path.abspath(existing_path))
            == os.path.normcase(os.path.abspath(target_path))
        )
        try:
            if not same_target and os.path.exists(target_path):
                raise FileExistsError(f"恢复目标已存在: {target_path}")
            historical_path = self.write_historical_copy(
                existing_path,
                target_id,
                buckets_dir,
            )

            if same_target:
                os.replace(_win_long_path(staged_path), _win_long_path(target_path))
            else:
                # Publish without replacement, then remove the still-untouched
                # source.  If source removal fails, delete the publication and
                # historical copy; the original remains the sole truth.
                os.link(_win_long_path(staged_path), _win_long_path(target_path))
                target_created = True
                try:
                    os.unlink(_win_long_path(existing_path))
                except Exception:
                    _safe_unlink(target_path)
                    target_created = False
                    _safe_unlink(historical_path)
                    historical_path = ""
                    raise
                _safe_unlink(staged_path)

            self._forget_old_vectors(target_id)
            return target_id, target_path
        except Exception:
            if target_created:
                _safe_unlink(target_path)
            if historical_path:
                _safe_unlink(historical_path)
            raise
        finally:
            _safe_unlink(staged_path)

    def _forget_old_vectors(self, target_id: str) -> None:
        """The overwritten text's queued and stored vectors no longer describe the entry;
        failing to drop them is only logged."""
        outbox = getattr(self._bucket_mgr, "embedding_outbox", None)
        if outbox is not None and callable(getattr(outbox, "discard", None)):
            try:
                outbox.discard(target_id)
            except Exception as exc:
                logger.warning("[migrate] failed to discard old outbox item: %s", exc)
        embedding = getattr(self._bucket_mgr, "embedding_engine", None)
        if embedding is not None and callable(getattr(embedding, "delete_embedding", None)):
            try:
                embedding.delete_embedding(target_id)
            except Exception as exc:
                logger.warning("[migrate] failed to discard old embedding: %s", exc)
