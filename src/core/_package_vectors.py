"""
========================================
_package_vectors.py — merging a package's embeddings.db into the live index
========================================

When the package was made with the embedding model the library uses now,
core/package_import.MigrateEngine keeps its vectors instead of computing them again. The
snapshot is untrusted: it is read in bounded batches, every cell is size-checked in SQL
before Python sees it, every vector is re-encoded as finite floats of the expected
dimension, and only the rows of entries that were written are copied, under the ids they
were written under.
========================================
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from typing import Any, Optional

from .backup_archive import BackupArchiveError

from utils import now_iso  # type: ignore

logger = logging.getLogger("loci_brain.migrate")

_EMBEDDING_FETCH_BATCH = 32
_MAX_EMBEDDING_CELL_BYTES = 1024 * 1024
_MAX_EMBEDDING_DIMENSIONS = 65_536
# One vector row per entry at most: the package's member cap bounds it.
_MAX_EMBEDDING_ROWS = 100_000
_MAX_EMBEDDING_TIMESTAMP_BYTES = 256
_MAX_EMBEDDING_HASH_BYTES = 256


@dataclass(frozen=True)
class _SnapshotColumns:
    """The snapshot's column names, from its fixed schema (None: the column is absent)."""
    id: str
    vector: str
    updated: Optional[str]
    hash: Optional[str]
    meaning: Optional[str]


def merge_embeddings(db_bytes: bytes, id_map: dict[str, str], *, embedding_engine: Any,
                     import_model_dim: int) -> set[str]:
    """(Runs in a thread.) Merge the vectors from the zip's embeddings.db into the
    current db.

    It understands both the current bucket_id/embedding schema and the earlier
    id/vector one.
    Returns the set of target bucket IDs whose vectors were restored successfully.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tf.write(db_bytes)
        tmp_path = tf.name

    try:
        return merge_embeddings_path(tmp_path, id_map, embedding_engine=embedding_engine,
                                     import_model_dim=import_model_dim)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def merge_embeddings_path(
    source_db: str,
    id_map: dict[str, str],
    *,
    embedding_engine: Any,
    import_model_dim: int,
) -> set[str]:
    """Merge a validated snapshot in bounded, validated batches.

    The SQLite file is untrusted.  Never ``fetchall`` vector cells and never
    copy arbitrary SQLite values into the live index.  SQL ``CASE`` keeps an
    oversized cell out of the Python result entirely; valid JSON vectors are
    normalized before batches are committed.
    """

    current_db = getattr(embedding_engine, "db_path", "")
    if not current_db or not os.path.isfile(current_db):
        logger.warning("[migrate] 当前 embeddings.db 路径无效，跳过向量合并")
        return set()

    safe_id_map = _safe_id_map(id_map)
    if not safe_id_map:
        return set()

    src = sqlite3.connect(source_db, timeout=30)
    dst = sqlite3.connect(current_db, timeout=30)
    try:
        columns = _snapshot_columns(src, dst)
        if columns is None:
            logger.warning("[migrate] 导入包 embeddings.db 缺少 embeddings 表，跳过")
            return set()
        _stage_wanted_ids(src, list(safe_id_map))
        cursor = src.execute(_snapshot_query(columns))
        expected_dim = expected_embedding_dimension(import_model_dim, embedding_engine)
        merged, skipped = _copy_rows(cursor, dst, safe_id_map, expected_dim,
                                     fallback_time=now_iso(),
                                     with_meaning=columns.meaning is not None)
        logger.info(
            "[migrate] 合并了 %d 条 embedding 向量，跳过 %d 条",
            len(merged),
            skipped,
        )
        return merged
    finally:
        src.close()
        dst.close()


def _safe_id_map(id_map: dict[str, str]) -> dict[str, str]:
    """source id -> target id, for ids that are strings of a sane length."""
    return {
        source_id: target_id
        for source_id, target_id in id_map.items()
        if (
            isinstance(source_id, str)
            and isinstance(target_id, str)
            and 0 < len(source_id) <= 200
            and 0 < len(target_id) <= 200
        )
    }


def _snapshot_columns(src: sqlite3.Connection,
                      dst: sqlite3.Connection) -> Optional[_SnapshotColumns]:
    """The snapshot's embeddings columns, or None when it has no embeddings table. An
    unknown table structure is refused."""
    table = src.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'embeddings'"
    ).fetchone()
    if table is None:
        return None

    columns = {
        str(row[1])
        for row in src.execute("PRAGMA table_info(embeddings)")
    }
    if {"bucket_id", "embedding"}.issubset(columns):
        id_column = "bucket_id"
        vector_column = "embedding"
        updated_column = "updated_at" if "updated_at" in columns else None
        hash_column = "content_hash" if "content_hash" in columns else None
        meaning_column = ("meaning_embedding" if "meaning_embedding" in columns
                          else None)
    elif {"id", "vector"}.issubset(columns):
        id_column = "id"
        vector_column = "vector"
        updated_column = None
        hash_column = None
        meaning_column = None
    else:
        raise BackupArchiveError("embeddings 表结构无法识别")

    # The meaning vector (its own column) travels with the content vector when
    # both libraries have the column.
    if meaning_column is not None and "meaning_embedding" not in {
            str(row[1]) for row in dst.execute("PRAGMA table_info(embeddings)")}:
        meaning_column = None
    return _SnapshotColumns(id_column, vector_column, updated_column, hash_column,
                            meaning_column)


def _stage_wanted_ids(src: sqlite3.Connection, source_ids: list[str]) -> None:
    """The ids whose rows are wanted, in a temp table the snapshot query joins on: user
    data stays parameterised."""
    src.execute(
        "CREATE TEMP TABLE loci_migrate_wanted_ids "
        "(source_id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    for offset in range(0, len(source_ids), _EMBEDDING_FETCH_BATCH):
        batch = source_ids[offset:offset + _EMBEDDING_FETCH_BATCH]
        src.executemany(
            "INSERT INTO loci_migrate_wanted_ids (source_id) VALUES (?)",
            ((source_id,) for source_id in batch),
        )


def _bounded_column(column: str | None, limit: int) -> str:
    """Three result columns for one cell: its type, its size, and the value only when it
    is text or blob within `limit` bytes."""
    if column is None:
        return "'text', 0, ''"
    # Identifiers come only from the fixed schema names above.
    qualified = f"e.{column}"
    return (
        f"typeof({qualified}), "
        f"length(CAST({qualified} AS BLOB)), "
        f"CASE WHEN typeof({qualified}) IN ('text', 'blob') "
        f"AND length(CAST({qualified} AS BLOB)) <= {limit} "
        f"THEN {qualified} ELSE NULL END"
    )


def _snapshot_query(columns: _SnapshotColumns) -> str:
    # All interpolated identifiers are selected from the fixed schema
    # names above; user data remains parameterized in the temp table.
    return (
        f"SELECT e.{columns.id}, "  # nosec B608
        f"{_bounded_column(columns.vector, _MAX_EMBEDDING_CELL_BYTES)}, "
        f"{_bounded_column(columns.updated, _MAX_EMBEDDING_TIMESTAMP_BYTES)}, "
        f"{_bounded_column(columns.hash, _MAX_EMBEDDING_HASH_BYTES)}, "
        f"{_bounded_column(columns.meaning, _MAX_EMBEDDING_CELL_BYTES)} "
        "FROM embeddings AS e "
        f"JOIN loci_migrate_wanted_ids AS wanted "
        f"ON e.{columns.id} = wanted.source_id"
    )


def _copy_rows(cursor: sqlite3.Cursor, dst: sqlite3.Connection,
               safe_id_map: dict[str, str], expected_dim: int, *,
               fallback_time: str, with_meaning: bool) -> tuple[set[str], int]:
    """Copy the query's rows into the live index batch by batch, each batch committed on
    its own. Returns (the target ids merged, how many rows were skipped)."""
    merged: set[str] = set()
    processed = 0
    skipped = 0

    while rows := cursor.fetchmany(_EMBEDDING_FETCH_BATCH):
        normalized_rows: list[tuple[str, str, str, str, Optional[str]]] = []
        normalized_ids: list[str] = []
        for row in rows:
            processed += 1
            if processed > _MAX_EMBEDDING_ROWS:
                raise BackupArchiveError("embeddings 行数超过迁移上限")
            normalized = _normalize_row(row, safe_id_map, expected_dim, fallback_time)
            if normalized is None:
                skipped += 1
                if skipped <= 5:
                    logger.warning(
                        "[migrate] 跳过非法或过大的 embedding 行: %r",
                        row[0],
                    )
                continue
            normalized_rows.append(normalized)
            normalized_ids.append(normalized[0])

        if normalized_rows:
            _write_rows(dst, normalized_rows, with_meaning)
            merged.update(normalized_ids)
        # Drop the current SQLite payloads before fetchmany builds the
        # next batch; otherwise Python briefly retains two batches.
        rows.clear()
    return merged, skipped


def _normalize_row(
    row: tuple, safe_id_map: dict[str, str], expected_dim: int, fallback_time: str,
) -> Optional[tuple[str, str, str, str, Optional[str]]]:
    """One snapshot row as (target_id, vector, updated_at, content_hash, meaning_vector),
    or None when it is not one to copy."""
    (
        source_id,
        vector_type,
        vector_size,
        vector_value,
        updated_type,
        updated_size,
        updated_value,
        hash_type,
        hash_size,
        hash_value,
        meaning_type,
        meaning_size,
        meaning_value,
    ) = row
    target_id = safe_id_map.get(source_id)
    normalized_vector = normalize_embedding_vector(
        vector_value,
        vector_type,
        vector_size,
        expected_dim,
    )
    updated_at = normalize_embedding_text(
        updated_value,
        updated_type,
        updated_size,
        _MAX_EMBEDDING_TIMESTAMP_BYTES,
    )
    content_hash = normalize_embedding_text(
        hash_value,
        hash_type,
        hash_size,
        _MAX_EMBEDDING_HASH_BYTES,
    )
    # No meaning vector is no meaning vector; a malformed one is dropped
    # alone and the entry's next meaning write computes it again.
    meaning_vector = (
        normalize_embedding_vector(
            meaning_value, meaning_type, meaning_size, expected_dim)
        if meaning_value is not None else None
    )
    if (
        target_id is None
        or normalized_vector is None
        or updated_at is None
        or content_hash is None
    ):
        return None
    return (
        target_id,
        normalized_vector,
        updated_at or fallback_time,
        content_hash,
        meaning_vector,
    )


def _write_rows(dst: sqlite3.Connection,
                rows: list[tuple[str, str, str, str, Optional[str]]],
                with_meaning: bool) -> None:
    """One batch into the live index, committed."""
    if with_meaning:
        dst.executemany(
            """INSERT OR REPLACE INTO embeddings
                               (bucket_id, embedding, updated_at, content_hash,
                                meaning_embedding)
                               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )
    else:
        dst.executemany(
            """INSERT OR REPLACE INTO embeddings
                               (bucket_id, embedding, updated_at, content_hash)
                               VALUES (?, ?, ?, ?)""",
            [r[:4] for r in rows],
        )
    dst.commit()


def expected_embedding_dimension(import_model_dim: int, embedding_engine: Any) -> int:
    """The dimension every vector must have: the package's, else the live backend's;
    0 when neither is known."""
    if 0 < import_model_dim <= _MAX_EMBEDDING_DIMENSIONS:
        return import_model_dim
    backend = getattr(embedding_engine, "_backend", None)
    try:
        dimension = int(backend.vector_dim()) if backend else 0
    except (TypeError, ValueError, OverflowError):
        dimension = 0
    return dimension if 0 < dimension <= _MAX_EMBEDDING_DIMENSIONS else 0


def normalize_embedding_text(
    value: Any,
    value_type: Any,
    declared_size: Any,
    limit: int,
) -> str | None:
    if value_type not in {"text", "blob"}:
        return None
    if not isinstance(declared_size, int) or not 0 <= declared_size <= limit:
        return None
    if isinstance(value, bytes):
        try:
            result = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(value, str):
        result = value
    else:
        return None
    try:
        encoded_size = len(result.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if encoded_size > limit or "\x00" in result:
        return None
    return result


def normalize_embedding_vector(
    value: Any,
    value_type: Any,
    declared_size: Any,
    expected_dimension: int,
) -> str | None:
    payload = normalize_embedding_text(
        value,
        value_type,
        declared_size,
        _MAX_EMBEDDING_CELL_BYTES,
    )
    if payload is None:
        return None
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    if (
        not isinstance(parsed, list)
        or not parsed
        or len(parsed) > _MAX_EMBEDDING_DIMENSIONS
        or (expected_dimension and len(parsed) != expected_dimension)
    ):
        return None
    normalized: list[float] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        normalized.append(number)
    try:
        encoded = json.dumps(
            normalized,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if len(encoded.encode("utf-8")) > _MAX_EMBEDDING_CELL_BYTES:
        return None
    return encoded
