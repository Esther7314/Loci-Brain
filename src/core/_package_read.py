"""
========================================
_package_read.py — reading and validating an export package
========================================

The importer's first step (core/package_import.MigrateEngine.parse_zip_file): the members
of an extracted package become parsed buckets, the embedding model the package was made
with, and what an export package says about itself. Every bucket is held to the library's
size limits here, so a package that cannot be restored whole is refused before anything
is written.
========================================
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import frontmatter

from .backup_archive import (
    PACKAGE_MEDIA_PREFIX as _PACKAGE_MEDIA,
    PACKAGE_ORIGINALS_PREFIX as _PACKAGE_ORIGINALS,
    PACKAGE_STATE_PREFIX as _PACKAGE_STATE,
    BackupArchiveError,
    validate_sqlite_bytes,
    validate_sqlite_file,
)

_PACKAGE_PREFIXES = (_PACKAGE_STATE, _PACKAGE_ORIGINALS, _PACKAGE_MEDIA)

logger = logging.getLogger("loci_brain.migrate")

_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_DEFAULT_MAX_METADATA_BYTES = 16 * 1024
_MAX_UNLIMITED_MIGRATE_BUCKET_BYTES = 8 * 1024 * 1024
_MAX_UNLIMITED_MIGRATE_METADATA_BYTES = 1024 * 1024
_FRONTMATTER_OVERHEAD_BYTES = 64 * 1024


@dataclass
class _ParsedBucket:
    """One bucket file parsed out of the zip."""
    bucket_id: str
    arc_path: str        # its path inside the zip, e.g. "buckets/dynamic/foo/name_id.md"
    md_bytes: bytes | None  # compatibility path; production imports use md_path
    name: str
    bucket_type: str
    domain: list[str]
    created: str
    md_path: str = ""     # disk-backed extracted member owned by MigrateEngine
    # The normalised frontmatter: what the receiving library's registry is asked about,
    # and the links that are pointed at new ids.
    meta: dict = field(default_factory=dict)


def _parse_md_meta(raw: bytes) -> tuple[dict, str]:
    """Parse frontmatter metadata plus body out of md bytes. On failure, an empty dict and
    an empty string."""
    try:
        post = frontmatter.loads(raw.decode("utf-8", errors="replace"))
        return dict(post.metadata), post.content
    except Exception:
        return {}, ""


def _safe_str(val: Any, max_len: int = 512) -> str:
    """Safely turn a value into a string, truncated."""
    return str(val)[:max_len] if val is not None else ""


# ============================================================
# Size limits (config["limits"]; 0 or less means the importer's own cap)
# ============================================================

def _configured_limit(config: dict, name: str, default: int, unlimited_cap: int) -> int:
    limits = config.get("limits") or {}
    try:
        value = int(limits.get(name, default))
    except (TypeError, ValueError, OverflowError):
        value = default
    return unlimited_cap if value <= 0 else value


def bucket_content_limit(config: dict) -> int:
    limits = config.get("limits") or {}
    if "max_migrate_bucket_bytes" in limits:
        return _configured_limit(
            config,
            "max_migrate_bucket_bytes",
            _DEFAULT_MAX_BUCKET_BYTES,
            _MAX_UNLIMITED_MIGRATE_BUCKET_BYTES,
        )
    return _configured_limit(
        config,
        "max_bucket_bytes",
        _DEFAULT_MAX_BUCKET_BYTES,
        _MAX_UNLIMITED_MIGRATE_BUCKET_BYTES,
    )


def metadata_limit(config: dict) -> int:
    return _configured_limit(
        config,
        "max_metadata_bytes",
        _DEFAULT_MAX_METADATA_BYTES,
        _MAX_UNLIMITED_MIGRATE_METADATA_BYTES,
    )


def member_limit(config: dict) -> int:
    """The most bytes one bucket file may have: body, frontmatter and its overhead."""
    return bucket_content_limit(config) + metadata_limit(config) + _FRONTMATTER_OVERHEAD_BYTES


def normalize_import_metadata(bucket_mgr: Any, config: dict,
                              metadata: dict[str, Any]) -> dict[str, Any]:
    """The bucket manager's own normalisation, then the JSON-safe and size checks."""
    normalizer = getattr(bucket_mgr, "_normalize_metadata_value", None)
    normalized = normalizer(metadata) if callable(normalizer) else metadata
    if not isinstance(normalized, dict):
        raise BackupArchiveError("bucket metadata 必须是对象")
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BackupArchiveError(f"bucket metadata 不是 JSON-safe: {exc}") from exc
    if len(encoded) > metadata_limit(config):
        raise BackupArchiveError(
            f"bucket metadata 过大（{len(encoded)} bytes > {metadata_limit(config)}）"
        )
    return normalized


def read_member(source: bytes | str, *, limit: int, label: str) -> bytes:
    """A member's bytes, from memory or from its extracted file, refused past `limit`."""
    if isinstance(source, bytes):
        if len(source) > limit:
            raise BackupArchiveError(f"{label} 过大（{len(source)} bytes > {limit}）")
        return source
    try:
        size = os.path.getsize(source)
    except OSError as exc:
        raise BackupArchiveError(f"无法读取 {label}: {exc}") from exc
    if size > limit:
        raise BackupArchiveError(f"{label} 过大（{size} bytes > {limit}）")
    with open(source, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit or len(data) != size:
        raise BackupArchiveError(f"{label} 读取长度异常")
    return data


def read_bucket_member(config: dict, pb: _ParsedBucket) -> bytes:
    """A parsed bucket's file, read again under the same limit it was parsed with."""
    return read_member(
        pb.md_bytes if pb.md_bytes is not None else pb.md_path,
        limit=member_limit(config),
        label=pb.arc_path)


# ============================================================
# The package
# ============================================================

def parse_package(package: dict[str, Any], *, disk_backed: bool, config: dict,
                  bucket_mgr: Any) -> dict:
    """What an extracted package (backup_archive.extract_backup_archive_file) holds:
    its buckets, the embedding model and vectors it carries, and for an export package
    its library state members. Any bucket that cannot be restored fails the whole
    package."""
    files: dict[str, bytes | str] = package["files"]
    names = set(files)

    # 1) export_meta.json: the embedding model, and what an export package says of itself
    (import_model, import_model_dim, import_backend,
     package_info, package_members) = _read_export_meta(files, names, config)

    # 2) embeddings.db. A corrupt snapshot must not be allowed to masquerade as a
    #    restorable index.
    has_embeddings, db_bytes, db_path = _read_embeddings_db(files, names, disk_backed)

    # 3) The bucket markdown files. Any corrupt entry fails the whole restore pre-flight,
    #    so that the interface can never report "success" while memories were silently
    #    dropped.
    buckets: list[_ParsedBucket] = []
    seen_ids: set[str] = set()
    for arc_path in sorted(names):
        if not arc_path.startswith("buckets/") or not arc_path.endswith(".md"):
            continue
        # Only the memory directories are restored; a leftover buckets/letters/
        # folder in an old backup is not one of them.
        if arc_path.startswith("buckets/letters/"):
            continue
        try:
            buckets.append(_parse_bucket_member(
                arc_path, files[arc_path], seen_ids,
                disk_backed=disk_backed, config=config, bucket_mgr=bucket_mgr))
        except BackupArchiveError:
            raise
        except Exception as e:
            raise BackupArchiveError(f"bucket markdown 无法解析: {arc_path}: {e}") from e

    return {
        "buckets": buckets,
        "import_model": import_model,
        "import_model_dim": import_model_dim,
        "import_backend": import_backend,
        "has_embeddings": has_embeddings,
        "db_bytes": db_bytes,
        "db_path": db_path,
        "integrity_verified": package["integrity_verified"],
        "integrity_warning": package["integrity_warning"],
        "manifest": package["manifest"],
        "package": package_info,
        "package_members": package_members,
    }


def _read_export_meta(
    files: dict[str, bytes | str], names: set[str], config: dict,
) -> tuple[str, int, str, Optional[dict[str, Any]], dict[str, bytes | str]]:
    """(import_model, import_model_dim, import_backend, package_info, package_members).
    An unreadable export_meta.json only costs the vectors: whatever was read before the
    failure is kept, and the import carries on without restoring them."""
    import_model = ""
    import_model_dim = 0
    import_backend = ""
    package_info: Optional[dict[str, Any]] = None
    package_members: dict[str, bytes | str] = {}
    if "export_meta.json" not in names:
        return import_model, import_model_dim, import_backend, package_info, package_members
    meta: dict = {}
    try:
        meta_raw = read_member(
            files["export_meta.json"],
            limit=metadata_limit(config),
            label="export_meta.json",
        )
        meta = json.loads(meta_raw.decode("utf-8"))
        emb_info = meta.get("embedding", {})
        import_model = str(emb_info.get("model", "") or "")
        import_model_dim = int(emb_info.get("dim") or 0)
        import_backend = str(emb_info.get("backend", "") or "")
    except Exception as e:
        logger.warning(f"[migrate] export_meta.json 解析失败，将跳过向量恢复: {e}")
    package_info = package_of(meta if isinstance(meta, dict) else {}, names)
    if package_info is not None:
        package_members = {
            name: files[name] for name in names
            if name.startswith(_PACKAGE_PREFIXES)
        }
    return import_model, import_model_dim, import_backend, package_info, package_members


def _read_embeddings_db(
    files: dict[str, bytes | str], names: set[str], disk_backed: bool,
) -> tuple[bool, Optional[bytes], str]:
    """(has_embeddings, db_bytes, db_path) of a validated embeddings.db, if there is one."""
    if "embeddings.db" not in names:
        return False, None, ""
    source = files["embeddings.db"]
    if disk_backed:
        db_path = str(source)
        validate_sqlite_file(db_path)
        return os.path.getsize(db_path) > 0, None, db_path
    db_bytes = bytes(source)
    validate_sqlite_bytes(db_bytes)
    return bool(db_bytes), db_bytes, ""


def _parse_bucket_member(
    arc_path: str, source: bytes | str, seen_ids: set[str], *,
    disk_backed: bool, config: dict, bucket_mgr: Any,
) -> _ParsedBucket:
    """One bucket file of the package, held to the library's limits; its id must be safe
    and not one the package already used (`seen_ids`, which it is added to)."""
    content_limit = bucket_content_limit(config)
    raw = read_member(
        source,
        limit=content_limit + metadata_limit(config) + _FRONTMATTER_OVERHEAD_BYTES,
        label=arc_path,
    )
    post = frontmatter.loads(raw.decode("utf-8"))
    meta = normalize_import_metadata(bucket_mgr, config, dict(post.metadata))
    content_size = len((post.content or "").encode("utf-8"))
    if content_size > content_limit:
        raise BackupArchiveError(
            f"{arc_path} 正文过大（{content_size} bytes > {content_limit}）"
        )

    bucket_id = _bucket_id_of(meta, arc_path)
    if bucket_id in seen_ids:
        raise BackupArchiveError(f"备份中存在重复 bucket_id: {bucket_id}")
    seen_ids.add(bucket_id)

    domain = meta.get("domain") or []
    if isinstance(domain, str):
        domain = [domain]
    elif not isinstance(domain, list):
        domain = []
    return _ParsedBucket(
        bucket_id=bucket_id,
        arc_path=arc_path,
        md_bytes=None if disk_backed else raw,
        name=_safe_str(meta.get("name", bucket_id), 200),
        bucket_type=_safe_str(meta.get("type", "dynamic"), 32),
        domain=[_safe_str(item, 100) for item in domain],
        created=_safe_str(meta.get("created", ""), 32),
        md_path=str(source) if disk_backed else "",
        meta=meta,
    )


def _bucket_id_of(meta: dict, arc_path: str) -> str:
    """The id a bucket file names itself by, else the one its filename ends in; refused
    when it is empty or could not be a filename."""
    bucket_id = str(meta.get("id") or meta.get("bucket_id") or "")
    if not bucket_id:
        stem = os.path.splitext(os.path.basename(arc_path))[0]
        parts = stem.rsplit("_", 1)
        bucket_id = parts[-1] if len(parts) > 1 else stem

    if (
        not bucket_id
        or len(bucket_id) > 200
        or any(ord(char) < 32 for char in bucket_id)
        or "/" in bucket_id
        or "\\" in bucket_id
    ):
        raise BackupArchiveError(f"{arc_path} 的 bucket_id 不安全或为空")
    return bucket_id


def package_of(meta: dict, names: set[str]) -> Optional[dict[str, Any]]:
    """What an export package (core/export_package.py) says about itself, or None for
    an older backup that carries memories and vectors only. A package of another
    library version is refused whole: its fields mean what that version meant."""
    try:
        fmt = int(meta.get("package_format") or 0)
    except (TypeError, ValueError):
        fmt = 0
    if fmt < 2:
        return None
    from . import schema as _schema
    try:
        version = int(meta.get("library_schema_version"))
    except (TypeError, ValueError):
        raise BackupArchiveError("导出包没说它是第几版的库，没法判断字段的意思")
    if version != _schema.CURRENT_VERSION:
        raise BackupArchiveError(
            f"这个导出包是第 {version} 版的库，这里是第 {_schema.CURRENT_VERSION} 版。"
            "版本不同，字段的意思也不同：先把两边升到同一版再导。")
    return {
        "format": fmt,
        "library_schema_version": version,
        "state_files": sorted(n for n in names if n.startswith(_PACKAGE_STATE)),
        "originals": sum(1 for n in names if n.startswith(_PACKAGE_ORIGINALS)),
        "media": sum(1 for n in names if n.startswith(_PACKAGE_MEDIA)),
    }
