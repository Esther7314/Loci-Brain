"""
========================================
package_import.py — the engine that imports a full memory package
========================================

Takes the zip that GET /api/loci/export produces (core/export_package.py: buckets/*.md +
embeddings.db + export_meta.json, and in a package of format 2 the library's state, sunk
originals and attachments) and merges it incrementally into the current system; POST
/api/loci/import-package is its doorway. An older backup with memories and vectors only
is still read the same way.

Key behaviours:
- Parse the zip, identify the bucket files, and read the embedding model information out
  of export_meta.json
- Compare the package's embedding model with the current system's, and decide whether the
  vector data can be kept
- Detect bucket ID conflicts and return the list of them, waiting for the user to decide
- Conflict decisions: skip | overwrite | keep_both (keep both, reassigning the ID)
- Matching embedding model -> merge the vector data; mismatched -> import the md files
  only, and re-vectorise automatically afterwards
- keep_both copies get a fresh id in the library's shape (12 hex characters), and every
  link of the package that named the original (prov, the version chain, covers, a hold's
  standing entry, attachment paths) names the copy
- An entry whose file is already the package's, byte for byte, counts as imported: nothing
  is archived or written again
- A package's library state goes back after the entries
  (export_package.restore_library_state): whole into a library that had no entries,
  merged into one that had; a package of another library version is refused
- What the receiving library has withdrawn stays withdrawn: entries standing on a source
  its registry withdrew, deleted or holds — and what is derived from them — are not
  written (`refused`), and an entry of the library blocked by such a change is neither
  overwritten nor copied. Refusing, not writing and then clearing: an import is no host
  and has no change authority, and a write-then-clear would put the text on disk first
- The job is kept on disk (JOB_FILE): a restart shows an interrupted import, and a second
  run of the same package keeps the first run's mode (an empty library is still restored
  whole); a library that had entries is backed up (core/schema.backup) before anything is
  written

State machine: idle -> parsing -> parsed -> applying -> reindexing -> done | error

What it does not do:
- It never calls an LLM (no content parsing, summarising or tagging — this moves files)
- It does not modify config
- It does not parse conversation history (that is import_memory.py's job)
- It does not switch embedding backend. Recomputing the whole store when the embedding
  model changes belongs to reembed.py.

Exports: the MigrateEngine class (instantiated by server.py and injected into the routes)
========================================
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

import frontmatter

from locibrain.storage.backup_archive import (
    PACKAGE_MEDIA_PREFIX as _PACKAGE_MEDIA,
    PACKAGE_ORIGINALS_PREFIX as _PACKAGE_ORIGINALS,
    PACKAGE_STATE_PREFIX as _PACKAGE_STATE,
    BackupArchiveError,
    extract_backup_archive_file,
    validate_sqlite_bytes,
    validate_sqlite_file,
)

_PACKAGE_PREFIXES = (_PACKAGE_STATE, _PACKAGE_ORIGINALS, _PACKAGE_MEDIA)

from utils import (_win_long_path, atomic_write_text, now_iso, read_from_ids,  # type: ignore
                   safe_path, sanitize_name)

logger = logging.getLogger("loci_brain.migrate")

# ============================================================
# Phase constants
# ============================================================
PHASE_IDLE = "idle"
PHASE_PARSING = "parsing"
PHASE_PARSED = "parsed"
PHASE_APPLYING = "applying"
PHASE_REINDEXING = "reindexing"
PHASE_DONE = "done"
PHASE_ERROR = "error"

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
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_DEFAULT_MAX_METADATA_BYTES = 16 * 1024
_MAX_UNLIMITED_MIGRATE_BUCKET_BYTES = 8 * 1024 * 1024
_MAX_UNLIMITED_MIGRATE_METADATA_BYTES = 1024 * 1024
_FRONTMATTER_OVERHEAD_BYTES = 64 * 1024
_EMBEDDING_FETCH_BATCH = 32
_MAX_EMBEDDING_CELL_BYTES = 1024 * 1024
_MAX_EMBEDDING_DIMENSIONS = 65_536
# One vector row per entry at most: the package's member cap bounds it.
_MAX_EMBEDDING_ROWS = 100_000
_MAX_EMBEDDING_TIMESTAMP_BYTES = 256
_MAX_EMBEDDING_HASH_BYTES = 256
_PARSED_WORKSPACE_TTL_SECONDS = 3600.0
_PARSED_WORKSPACE_SWEEP_SECONDS = 60.0

# The import job, kept on disk (`<buckets>/_state/import_package.json`) so that a restart
# shows it and a second run of the same package carries the first one's mode: a library
# that was empty when the package started coming in is still restored whole, although half
# the entries are already there.
JOB_FILE = os.path.join("_state", "import_package.json")
_JOB_SAVE_EVERY = 50
_REFUSED_SHOWN = 200
# The fields that name other entries by id (besides prov and the old `from`).
_LINK_FIELDS = ("supersedes", "superseded_by", "covered_by", "cover", "exception_of")
# Why an entry of the package was not written; the panel shows these.
REFUSAL_WORDS = {
    "withdrawn": "它依据的来源在这个库里已经撤回或删除了",
    "held": "它依据的来源在这个库里被宿主按住了，等宿主的正式变更再说",
    "derived": "它是从一条撤回或按住的记忆推出来的",
    "blocked_here": "库里同号的那条因为来源撤回被挡着，不拿包里的覆盖、也不另存一份",
}

_MIGRATE_ENGINES: weakref.WeakSet[Any] = weakref.WeakSet()
_MIGRATE_ENGINES_GUARD = threading.Lock()
_MIGRATE_SWEEPER_STARTED = False


def _register_migrate_engine(engine: Any) -> None:
    """Register an engine with one process-wide, weak-reference TTL sweeper."""

    global _MIGRATE_SWEEPER_STARTED
    with _MIGRATE_ENGINES_GUARD:
        _MIGRATE_ENGINES.add(engine)
        if _MIGRATE_SWEEPER_STARTED:
            return
        _MIGRATE_SWEEPER_STARTED = True

    def sweep() -> None:
        while True:
            time.sleep(_PARSED_WORKSPACE_SWEEP_SECONDS)
            with _MIGRATE_ENGINES_GUARD:
                engines = list(_MIGRATE_ENGINES)
            now = time.monotonic()
            for candidate in engines:
                try:
                    candidate._expire_parsed_workspace(now)
                except Exception as exc:
                    logger.warning("[migrate] parsed workspace TTL cleanup failed: %s", exc)

    try:
        threading.Thread(
            target=sweep,
            name="loci-migrate-workspace-sweeper",
            daemon=True,
        ).start()
    except RuntimeError as exc:
        # Status/reservation calls also run the expiry check, so a constrained
        # runtime that cannot start this daemon still has a safe lazy fallback.
        logger.warning("[migrate] could not start workspace TTL sweeper: %s", exc)
        with _MIGRATE_ENGINES_GUARD:
            _MIGRATE_SWEEPER_STARTED = False


# ============================================================
# Data classes
# ============================================================

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


@dataclass
class ConflictInfo:
    """A description of one bucket_id in the package colliding with the current system."""
    bucket_id: str
    import_name: str
    import_created: str
    current_name: str
    current_created: str


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


@asynccontextmanager
async def _noop_bucket_turn():
    yield


async def _to_thread_reaped(function: Any, *args: Any) -> Any:
    """Run sync work without letting cancellation orphan the worker thread.

    ``asyncio.to_thread`` only cancels its awaiter.  The underlying thread keeps
    running, so cleaning the migration workspace immediately would race that
    worker.  Shield it and absorb repeated task cancellations until it exits;
    only then propagate cancellation to the caller.
    """

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        try:
            worker.result()
        except BaseException:
            pass
        raise asyncio.CancelledError
    return worker.result()


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


def _same_file(path: str, data: bytes) -> bool:
    """Is the file at `path` exactly these bytes?"""
    try:
        if os.path.getsize(path) != len(data):
            return False
        with open(_win_long_path(path), "rb") as handle:
            return handle.read() == data
    except OSError:
        return False


# ============================================================
# MigrateEngine
# ============================================================

class MigrateEngine:
    """The engine that imports a full memory package (a zip). One instance per server
    process; only one job may run at a time."""

    def __init__(self, config: dict, bucket_mgr: Any, embedding_engine: Any) -> None:
        self._config = config
        self._bucket_mgr = bucket_mgr
        self._embedding_engine = embedding_engine
        self._state_guard = threading.RLock()

        # ---- State ----
        self._phase: str = PHASE_IDLE
        self._job_id: str = ""
        self._apply_reservation: str = ""

        # ---- Products of the parsing phase ----
        self._parsed_buckets: list[_ParsedBucket] = []
        self._conflicts: list[ConflictInfo] = []
        self._conflict_ids_at_parse: frozenset[str] = frozenset()
        self._import_model: str = ""
        self._import_model_dim: int = 0
        self._import_backend: str = ""
        self._has_embeddings: bool = False
        self._zip_db_bytes: Optional[bytes] = None
        self._zip_db_path: str = ""
        self._parse_temp_dir: str = ""
        self._parsed_at_monotonic: float = 0.0
        self._total_buckets: int = 0
        self._integrity_verified: bool = False
        self._integrity_warning: str = ""
        self._backup_manifest: Optional[dict[str, Any]] = None
        # An export package (core/export_package.py) also carries the library's state,
        # sunk originals and attachments: member -> extracted file (or bytes on the
        # compatibility path), restored after the entries are written.
        self._package_info: Optional[dict[str, Any]] = None
        self._package_members: dict[str, Any] = {}
        self._state_report: Optional[dict[str, Any]] = None
        # The uploaded file's sha256: a second run of the same package is known by it.
        self._package_sha: str = ""
        # package id -> why it was not written (REFUSAL_WORDS); the library backup taken
        # before writing into a library that had entries; whether this run carried on an
        # interrupted one.
        self._refused: dict[str, str] = {}
        self._backup_path: str = ""
        self._resumed: bool = False

        # ---- Counters for the apply phase ----
        self._apply_total: int = 0
        self._apply_done: int = 0
        self._apply_imported: int = 0
        self._apply_skipped: int = 0
        self._apply_errors: list[str] = []

        # ---- The re-vectorising phase ----
        self._reindex_total: int = 0
        self._reindex_done: int = 0
        self._reindex_errors: int = 0
        self._buckets_to_reindex: list[tuple[str, str]] = []  # (bucket_id, markdown path)

        # ---- Error information ----
        self._error_message: str = ""

        _register_migrate_engine(self)

    # ----------------------------------------------------------
    # Properties
    # ----------------------------------------------------------

    @property
    def phase(self) -> str:
        with self._state_guard:
            return self._phase

    @property
    def job_id(self) -> str:
        with self._state_guard:
            return self._job_id

    @property
    def is_busy(self) -> bool:
        with self._state_guard:
            return self._phase in (PHASE_PARSING, PHASE_APPLYING, PHASE_REINDEXING)

    def _begin_parse(self) -> str | None:
        """Atomically reserve the singleton parser before its first await."""

        with self._state_guard:
            if self._phase in (PHASE_PARSING, PHASE_APPLYING, PHASE_REINDEXING):
                return None
            self._job_id = uuid.uuid4().hex
            self._apply_reservation = ""
            self._phase = PHASE_PARSING
            return self._job_id

    def reserve_parse(self) -> str | None:
        """Reserve upload+parse before reading an untrusted request body."""

        return self._begin_parse()

    def _cleanup_parse_artifacts(self) -> None:
        """Release disk/memory payloads once they are no longer needed."""

        self._parsed_at_monotonic = 0.0
        temp_dir, self._parse_temp_dir = self._parse_temp_dir, ""
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        self._zip_db_bytes = None
        self._zip_db_path = ""
        self._package_members = {}
        for bucket in self._parsed_buckets:
            bucket.md_bytes = None
            bucket.md_path = ""

    def _reset_parse_state(self) -> None:
        self._cleanup_parse_artifacts()
        self._parsed_buckets = []
        self._conflicts = []
        self._conflict_ids_at_parse = frozenset()
        self._import_model = ""
        self._import_model_dim = 0
        self._import_backend = ""
        self._has_embeddings = False
        self._integrity_verified = False
        self._integrity_warning = ""
        self._backup_manifest = None
        self._package_info = None
        self._state_report = None
        self._package_sha = ""
        self._refused = {}
        self._backup_path = ""
        self._resumed = False
        self._total_buckets = 0
        self._apply_errors = []
        self._apply_imported = 0
        self._apply_skipped = 0
        self._apply_total = 0
        self._apply_done = 0
        self._buckets_to_reindex = []
        self._reindex_total = 0
        self._reindex_done = 0
        self._reindex_errors = 0
        self._error_message = ""

    def reserve_apply(self, expected_job_id: str) -> str | None:
        """Reserve one parsed generation for exactly one background apply."""

        self._expire_parsed_workspace()
        with self._state_guard:
            if (
                self._phase != PHASE_PARSED
                or not expected_job_id
                or expected_job_id != self._job_id
            ):
                return None
            reservation = uuid.uuid4().hex
            self._apply_reservation = reservation
            self._parsed_at_monotonic = 0.0
            self._phase = PHASE_APPLYING
            return reservation

    def _discard_parsed_locked(self, message: str) -> None:
        """Discard the current parsed generation while ``_state_guard`` is held."""

        self._cleanup_parse_artifacts()
        self._parsed_buckets = []
        self._conflicts = []
        self._conflict_ids_at_parse = frozenset()
        self._buckets_to_reindex = []
        self._apply_reservation = ""
        self._phase = PHASE_ERROR
        self._error_message = str(message)[:500]

    def _expire_parsed_workspace(self, now: float | None = None) -> bool:
        """Generation-bound expiry for an unapplied disk-backed parse."""

        current = time.monotonic() if now is None else float(now)
        with self._state_guard:
            if (
                self._phase != PHASE_PARSED
                or self._parsed_at_monotonic <= 0
                or current - self._parsed_at_monotonic
                < _PARSED_WORKSPACE_TTL_SECONDS
            ):
                return False
            self._discard_parsed_locked(
                "迁移解析结果已超过 1 小时有效期，请重新上传"
            )
            return True

    def abandon_parsed(self, expected_job_id: str, message: str = "迁移已取消") -> bool:
        """Explicitly discard one still-unapplied parsed generation."""

        with self._state_guard:
            if (
                not expected_job_id
                or self._phase != PHASE_PARSED
                or expected_job_id != self._job_id
            ):
                return False
            self._discard_parsed_locked(message)
            return True

    def abandon_apply(self, reservation_id: str, message: str) -> bool:
        """Release an apply reservation when its background task was not scheduled."""

        with self._state_guard:
            if (
                not reservation_id
                or self._phase != PHASE_APPLYING
                or reservation_id != self._apply_reservation
            ):
                return False
            self._apply_reservation = ""
            self._phase = PHASE_ERROR
            self._error_message = str(message)[:500]
        self._cleanup_parse_artifacts()
        self._parsed_buckets = []
        self._buckets_to_reindex = []
        return True

    def _embedding_match(self) -> bool:
        """Does the current embedding model match the one in the package?"""
        if not self._import_model:
            return False
        current_model = str(getattr(self._embedding_engine, "model", "") or "")
        same_model = (
            self._import_model.strip().lower().removeprefix("models/")
            == current_model.strip().lower().removeprefix("models/")
        )
        if not same_model:
            return False
        backend = getattr(self._embedding_engine, "_backend", None)
        try:
            current_dim = int(backend.vector_dim()) if backend else 0
        except Exception:
            current_dim = 0
        return not self._import_model_dim or not current_dim or self._import_model_dim == current_dim

    # ----------------------------------------------------------
    # Status queries
    # ----------------------------------------------------------

    def get_status(self) -> dict:
        self._expire_parsed_workspace()
        return {
            "phase": self._phase,
            "job_id": self._job_id,
            "total_buckets": self._total_buckets,
            "conflicts_count": len(self._conflicts),
            "conflicts": [
                {
                    "bucket_id": c.bucket_id,
                    "import_name": c.import_name,
                    "import_created": c.import_created,
                    "current_name": c.current_name,
                    "current_created": c.current_created,
                }
                for c in self._conflicts
            ],
            "import_model": self._import_model,
            "import_backend": self._import_backend,
            "current_model": getattr(self._embedding_engine, "model", ""),
            "embedding_match": self._embedding_match(),
            "has_embeddings": self._has_embeddings,
            "integrity_verified": self._integrity_verified,
            "integrity_warning": self._integrity_warning,
            "backup_manifest": {
                "schema_version": self._backup_manifest.get("schema_version"),
                "created_at": self._backup_manifest.get("created_at", ""),
                "version": self._backup_manifest.get("version", ""),
                "file_count": self._backup_manifest.get("file_count", 0),
                "total_bytes": self._backup_manifest.get("total_bytes", 0),
            } if self._backup_manifest else None,
            "apply_progress": {
                "done": self._apply_done,
                "total": self._apply_total,
            },
            "reindex_progress": {
                "done": self._reindex_done,
                "total": self._reindex_total,
                "errors": self._reindex_errors,
            },
            "apply_errors": self._apply_errors[-20:],
            "result": {
                "imported": self._apply_imported,
                "skipped": self._apply_skipped,
                "refused": len(self._refused),
            },
            # What was not written because the receiving library had withdrawn or held
            # what it stands on (REFUSAL_WORDS says why, in the panel's words).
            "refused": [{"bucket_id": bid, "why": why, "say": REFUSAL_WORDS.get(why, why)}
                        for bid, why in list(self._refused.items())[:_REFUSED_SHOWN]],
            "backup": self._backup_path,
            "resumed": self._resumed,
            # An export package: its format, library version and what else it carries;
            # after apply, what of the library's state was restored or merged.
            "package": self._package_info,
            "library_state": self._state_report,
            "error": self._error_message,
            # The job kept on disk: after a restart, the last import — an interrupted one
            # says so, and that uploading the same package again carries it on.
            "job": self._job_on_disk(),
        }

    # ----------------------------------------------------------
    # The job on disk
    # ----------------------------------------------------------

    def _job_path(self) -> str:
        return os.path.join(str(self._config.get("buckets_dir") or "buckets"), JOB_FILE)

    def _read_job(self) -> Optional[dict[str, Any]]:
        try:
            with open(self._job_path(), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _save_job(self, job: dict[str, Any]) -> None:
        job["updated_at"] = now_iso()
        try:
            os.makedirs(os.path.dirname(self._job_path()), exist_ok=True)
            atomic_write_text(self._job_path(), json.dumps(job, ensure_ascii=False, indent=2))
        except OSError as exc:
            logger.warning("[migrate] could not save the import job: %s", exc)

    def _job_on_disk(self) -> Optional[dict[str, Any]]:
        job = self._read_job()
        if job is None:
            return None
        out = {k: job.get(k) for k in ("phase", "mode", "started_at", "updated_at",
                                       "finished_at", "done", "total", "imported",
                                       "refused", "backup", "error")}
        with self._state_guard:
            running = self._phase == PHASE_APPLYING
        if job.get("phase") == PHASE_APPLYING and not running:
            out["phase"] = "interrupted"
            out["say"] = (f"上一次导回在第 {job.get('done') or 0} / {job.get('total') or 0} 条"
                          "停下了。重新上传同一个包就接着导：已经写进去的会认出来，"
                          "空库那次的来源登记、待核切片这些照样原样放回。")
        return out

    # ----------------------------------------------------------
    # Step one: parse the zip
    # ----------------------------------------------------------

    async def parse_zip(self, zip_bytes: bytes) -> dict:
        """Compatibility byte API; HTTP uploads use disk-backed parse_zip_file."""
        job_id = self._begin_parse()
        if job_id is None:
            return {
                "ok": False,
                "busy": True,
                "error": f"当前状态为 {self._phase}，请等待任务完成后再上传",
            }
        self._reset_parse_state()
        worker = asyncio.create_task(asyncio.to_thread(self._parse_zip_sync, zip_bytes))
        try:
            parsed = await asyncio.shield(worker)
            await self._accept_parsed(parsed)
        except asyncio.CancelledError:
            # A to_thread worker cannot be killed.  Reap it before releasing
            # the singleton reservation so cancellation cannot stack parsers.
            orphan: dict[str, Any] | None = None
            try:
                orphan = await worker
            except Exception:
                pass
            if orphan:
                shutil.rmtree(str(orphan.get("temp_dir") or ""), ignore_errors=True)
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = "zip 预检已取消"
            raise
        except Exception as e:
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = f"zip 预检失败: {e}"
            logger.error(f"[migrate] parse_zip error: {e}", exc_info=True)
            return {"ok": False, "error": self._error_message}

        with self._state_guard:
            self._phase = PHASE_PARSED
            self._parsed_at_monotonic = time.monotonic()
        return {"ok": True, **self.get_status()}

    async def parse_zip_file(
        self,
        archive_path: str,
        *,
        reservation_id: str | None = None,
    ) -> dict:
        """Parse an uploaded ZIP from disk while retaining members on disk."""

        job_id = reservation_id
        if job_id is None:
            job_id = self._begin_parse()
        with self._state_guard:
            reservation_valid = bool(
                job_id
                and self._phase == PHASE_PARSING
                and job_id == self._job_id
            )
            current_phase = self._phase
        if not reservation_valid:
            return {
                "ok": False,
                "busy": True,
                "error": f"当前状态为 {current_phase}，请等待任务完成后再上传",
            }

        self._reset_parse_state()
        workspace = tempfile.mkdtemp(prefix="loci-migrate-")
        worker = asyncio.create_task(
            asyncio.to_thread(self._parse_zip_path_sync, archive_path, workspace)
        )
        try:
            parsed = await asyncio.shield(worker)
            parsed["temp_dir"] = workspace
            await self._accept_parsed(parsed)
        except asyncio.CancelledError:
            try:
                await worker
            except Exception:
                pass
            shutil.rmtree(workspace, ignore_errors=True)
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = "zip 预检已取消"
            raise
        except Exception as e:
            shutil.rmtree(workspace, ignore_errors=True)
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = f"zip 预检失败: {e}"
            logger.error(f"[migrate] parse_zip_file error: {e}", exc_info=True)
            return {"ok": False, "error": self._error_message}

        with self._state_guard:
            self._phase = PHASE_PARSED
            self._parsed_at_monotonic = time.monotonic()
        return {"ok": True, **self.get_status()}

    def abandon_parse(self, reservation_id: str, message: str) -> bool:
        """Release a reserved upload that failed before the parser started."""

        with self._state_guard:
            if (
                not reservation_id
                or self._phase != PHASE_PARSING
                or reservation_id != self._job_id
            ):
                return False
            self._cleanup_parse_artifacts()
            self._phase = PHASE_ERROR
            self._error_message = str(message)[:500]
            return True

    async def _accept_parsed(self, parsed: dict[str, Any]) -> None:
        self._parsed_buckets = parsed["buckets"]
        self._total_buckets = len(self._parsed_buckets)
        self._import_model = parsed["import_model"]
        self._import_model_dim = parsed["import_model_dim"]
        self._import_backend = parsed["import_backend"]
        self._has_embeddings = parsed["has_embeddings"]
        self._zip_db_bytes = parsed.get("db_bytes")
        self._zip_db_path = str(parsed.get("db_path") or "")
        self._parse_temp_dir = str(parsed.get("temp_dir") or "")
        self._integrity_verified = bool(parsed.get("integrity_verified"))
        self._integrity_warning = str(parsed.get("integrity_warning") or "")
        self._package_info = parsed.get("package")
        self._package_members = dict(parsed.get("package_members") or {})
        self._package_sha = str(parsed.get("package_sha256") or "")
        manifest = parsed.get("manifest")
        self._backup_manifest = (
            {
                "schema_version": manifest.get("schema_version"),
                "created_at": manifest.get("created_at", ""),
                "version": manifest.get("version", ""),
                "file_count": manifest.get("file_count", 0),
                "total_bytes": manifest.get("total_bytes", 0),
            }
            if isinstance(manifest, dict)
            else None
        )
        if not self._parsed_buckets:
            raise BackupArchiveError(
                "zip 内未找到任何 bucket markdown 文件（期望路径前缀：buckets/）"
            )
        await self._identify_conflicts()

    def _configured_limit(self, name: str, default: int, unlimited_cap: int) -> int:
        limits = self._config.get("limits") or {}
        try:
            value = int(limits.get(name, default))
        except (TypeError, ValueError, OverflowError):
            value = default
        return unlimited_cap if value <= 0 else value

    def _bucket_content_limit(self) -> int:
        limits = self._config.get("limits") or {}
        if "max_migrate_bucket_bytes" in limits:
            return self._configured_limit(
                "max_migrate_bucket_bytes",
                _DEFAULT_MAX_BUCKET_BYTES,
                _MAX_UNLIMITED_MIGRATE_BUCKET_BYTES,
            )
        return self._configured_limit(
            "max_bucket_bytes",
            _DEFAULT_MAX_BUCKET_BYTES,
            _MAX_UNLIMITED_MIGRATE_BUCKET_BYTES,
        )

    def _metadata_limit(self) -> int:
        return self._configured_limit(
            "max_metadata_bytes",
            _DEFAULT_MAX_METADATA_BYTES,
            _MAX_UNLIMITED_MIGRATE_METADATA_BYTES,
        )

    def _normalize_import_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        normalizer = getattr(self._bucket_mgr, "_normalize_metadata_value", None)
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
        if len(encoded) > self._metadata_limit():
            raise BackupArchiveError(
                f"bucket metadata 过大（{len(encoded)} bytes > {self._metadata_limit()}）"
            )
        return normalized

    @staticmethod
    def _read_member(source: bytes | str, *, limit: int, label: str) -> bytes:
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

    def _parse_zip_sync(self, zip_bytes: bytes) -> dict:
        """Compatibility byte API that still streams decompressed data to disk."""

        workspace = tempfile.mkdtemp(prefix="loci-migrate-")
        fd, archive_path = tempfile.mkstemp(prefix="loci-upload-", suffix=".zip")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(zip_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            parsed = self._parse_zip_path_sync(archive_path, workspace)
            parsed["temp_dir"] = workspace
            return parsed
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        finally:
            _safe_unlink(archive_path)

    def _parse_zip_path_sync(self, archive_path: str, workspace: str) -> dict:
        package = extract_backup_archive_file(archive_path, workspace)
        parsed = self._parse_package(package, disk_backed=True)
        digest = hashlib.sha256()
        with open(archive_path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        parsed["package_sha256"] = digest.hexdigest()
        return parsed

    def _parse_package(self, package: dict[str, Any], *, disk_backed: bool) -> dict:
        buckets: list[_ParsedBucket] = []
        import_model = ""
        import_model_dim = 0
        import_backend = ""
        has_embeddings = False
        db_bytes: Optional[bytes] = None
        db_path = ""
        files: dict[str, bytes | str] = package["files"]
        names = set(files)
        package_info: Optional[dict[str, Any]] = None
        package_members: dict[str, bytes | str] = {}

        # 1) Read export_meta.json -> get the embedding model information
        if "export_meta.json" in names:
            meta: dict = {}
            try:
                meta_raw = self._read_member(
                    files["export_meta.json"],
                    limit=self._metadata_limit(),
                    label="export_meta.json",
                )
                meta = json.loads(meta_raw.decode("utf-8"))
                emb_info = meta.get("embedding", {})
                import_model = str(emb_info.get("model", "") or "")
                import_model_dim = int(emb_info.get("dim") or 0)
                import_backend = str(emb_info.get("backend", "") or "")
            except Exception as e:
                logger.warning(f"[migrate] export_meta.json 解析失败，将跳过向量恢复: {e}")
            package_info = self._package_of(meta if isinstance(meta, dict) else {}, names)
            if package_info is not None:
                package_members = {
                    name: files[name] for name in names
                    if name.startswith(_PACKAGE_PREFIXES)
                }

        # 2) Check whether embeddings.db is present. A corrupt snapshot must not be allowed
        #    to masquerade as a restorable index.
        if "embeddings.db" in names:
            source = files["embeddings.db"]
            if disk_backed:
                db_path = str(source)
                validate_sqlite_file(db_path)
                has_embeddings = os.path.getsize(db_path) > 0
            else:
                db_bytes = bytes(source)
                validate_sqlite_bytes(db_bytes)
                has_embeddings = bool(db_bytes)

        # 3) Walk the bucket markdown files. Any corrupt entry fails the whole restore
        # pre-flight, so that the interface can never report "success" while memories were
        # silently dropped.
        seen_ids: set[str] = set()
        for arc_path in sorted(names):
            if not arc_path.startswith("buckets/") or not arc_path.endswith(".md"):
                continue
            # Only the memory directories are restored; a leftover buckets/letters/
            # folder in an old backup is not one of them.
            if arc_path.startswith("buckets/letters/"):
                continue
            try:
                content_limit = self._bucket_content_limit()
                raw = self._read_member(
                    files[arc_path],
                    limit=content_limit + self._metadata_limit() + _FRONTMATTER_OVERHEAD_BYTES,
                    label=arc_path,
                )
                post = frontmatter.loads(raw.decode("utf-8"))
                meta = self._normalize_import_metadata(dict(post.metadata))
                content_size = len((post.content or "").encode("utf-8"))
                if content_size > content_limit:
                    raise BackupArchiveError(
                        f"{arc_path} 正文过大（{content_size} bytes > {content_limit}）"
                    )

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
                if bucket_id in seen_ids:
                    raise BackupArchiveError(f"备份中存在重复 bucket_id: {bucket_id}")
                seen_ids.add(bucket_id)

                domain = meta.get("domain") or []
                if isinstance(domain, str):
                    domain = [domain]
                elif not isinstance(domain, list):
                    domain = []
                buckets.append(_ParsedBucket(
                    bucket_id=bucket_id,
                    arc_path=arc_path,
                    md_bytes=None if disk_backed else raw,
                    name=_safe_str(meta.get("name", bucket_id), 200),
                    bucket_type=_safe_str(meta.get("type", "dynamic"), 32),
                    domain=[_safe_str(item, 100) for item in domain],
                    created=_safe_str(meta.get("created", ""), 32),
                    md_path=str(files[arc_path]) if disk_backed else "",
                    meta=meta,
                ))
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

    @staticmethod
    def _package_of(meta: dict, names: set[str]) -> Optional[dict[str, Any]]:
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

    async def _library_is_empty(self) -> bool:
        """No entry on disk, archive included: the library a package can be restored into
        whole (core/export_package.restore_library_state)."""
        list_all = getattr(self._bucket_mgr, "list_all", None)
        if not callable(list_all):
            return False
        try:
            existing = await list_all(include_archive=True)
        except TypeError:
            existing = await list_all()
        return not existing

    async def _identify_conflicts(self) -> None:
        """Find parse-time conflicts from one vault snapshot.

        Calling ``get`` once per imported ID turns a legitimate large import
        into an O(imported * existing) filesystem/frontmatter scan.  Production
        managers expose ``list_all``; build one ID map from that single scan.
        The fallback only supports minimal legacy/test managers.
        """
        conflicts: list[ConflictInfo] = []
        existing_by_id: dict[str, dict[str, Any]] = {}
        list_all = getattr(self._bucket_mgr, "list_all", None)
        if callable(list_all):
            try:
                existing_buckets = await list_all(include_archive=True)
            except TypeError:
                existing_buckets = await list_all()
            for existing in existing_buckets or []:
                if not isinstance(existing, dict):
                    continue
                bucket_id = existing.get("id")
                if isinstance(bucket_id, str) and bucket_id:
                    existing_by_id.setdefault(bucket_id, existing)

        for pb in self._parsed_buckets:
            if callable(list_all):
                existing = existing_by_id.get(pb.bucket_id)
            else:
                existing = await self._bucket_mgr.get(pb.bucket_id)
            if existing is not None:
                emeta = existing.get("metadata", {})
                conflicts.append(ConflictInfo(
                    bucket_id=pb.bucket_id,
                    import_name=pb.name,
                    import_created=pb.created,
                    current_name=_safe_str(emeta.get("name", pb.bucket_id), 200),
                    current_created=_safe_str(emeta.get("created", ""), 32),
                ))
        self._conflicts = conflicts
        self._conflict_ids_at_parse = frozenset(
            conflict.bucket_id for conflict in conflicts
        )

    # ----------------------------------------------------------
    # Step two: apply the import, honouring the conflict decisions
    # ----------------------------------------------------------

    async def apply(
        self,
        decisions: dict[str, str],
        *,
        reservation_id: str | None = None,
    ) -> None:
        """Apply the import.

        decisions: {bucket_id: "skip" | "overwrite" | "keep_both"}
        A bucket that conflicts but is absent from `decisions` defaults to skip — safety
        first. A bucket with no conflict is imported directly and needs no decision. A
        bucket whose file in the library is already byte for byte the package's counts as
        imported whatever the decision (a second run of an interrupted import finds its
        first half that way).

        What the receiving library has withdrawn stays withdrawn: an entry standing on a
        source its registry records as withdrawn, deleted or held, or derived from such an
        entry, is not written (`refused`, REFUSAL_WORDS), and an entry of the library
        blocked by such a change is neither overwritten nor copied. Nothing is cleared
        afterwards, because nothing that should not be there is written — the package
        still holds those entries, and the same package can be brought back once the host
        restores the source.
        """
        if reservation_id is None:
            reservation_id = self.reserve_apply(self._job_id)
        with self._state_guard:
            apply_valid = bool(
                reservation_id
                and self._phase == PHASE_APPLYING
                and reservation_id == self._apply_reservation
            )
            current_phase = self._phase
        if not apply_valid:
            raise RuntimeError(f"当前状态为 {current_phase}，apply 需要先完成并占用 parse_zip")

        self._apply_total = len(self._parsed_buckets)
        self._apply_done = 0
        self._apply_imported = 0
        self._apply_skipped = 0
        self._apply_errors = []
        self._buckets_to_reindex = []
        self._refused = {}
        self._backup_path = ""
        self._resumed = False

        embedding_matches = self._embedding_match()
        buckets_dir = self._config.get("buckets_dir", "buckets")
        imported_id_map: dict[str, str] = {}
        imported_files: dict[str, str] = {}
        self._state_report = None
        job: dict[str, Any] = {}

        try:
            existing = await self._existing_entries()
            survey = await _to_thread_reaped(self._survey, existing)
            earlier = self._read_job()
            resuming = bool(
                earlier and earlier.get("phase") == PHASE_APPLYING
                and self._package_sha and earlier.get("package_sha256") == self._package_sha
            )
            self._resumed = resuming
            # Judged before anything is written: a library with no entries takes a
            # package's state whole, one with entries has it merged. A second run of an
            # interrupted import keeps the first run's judgement.
            if resuming:
                fresh = earlier.get("mode") == "fresh"
                self._backup_path = str(earlier.get("backup") or "")
            else:
                fresh = bool(self._package_members) and not existing
            if existing and not fresh and not self._backup_path:
                from . import schema as _schema
                self._backup_path = str(await _to_thread_reaped(
                    _schema.backup, buckets_dir, "before-import-package"))
            job = {"phase": PHASE_APPLYING, "mode": "fresh" if fresh else "merge",
                   "package_sha256": self._package_sha, "job_id": self._job_id,
                   "started_at": (earlier or {}).get("started_at") if resuming else now_iso(),
                   "backup": self._backup_path, "done": 0, "total": self._apply_total,
                   "imported": 0, "refused": 0}
            self._save_job(job)

            self._refused = await _to_thread_reaped(
                self._refusals, decisions, survey)
            planned = self._plan_ids(decisions, survey)
            link_map = {old: new for old, new in planned.items() if old != new}

            ensure_path_index = getattr(
                self._bucket_mgr,
                "_ensure_bucket_path_index",
                None,
            )
            if callable(ensure_path_index):
                await _to_thread_reaped(ensure_path_index)
            for pb in self._parsed_buckets:
                try:
                    if pb.bucket_id in self._refused:
                        self._apply_skipped += 1
                        continue
                    result = await self._apply_one_bucket(
                        pb,
                        decisions.get(pb.bucket_id, "skip"),
                        buckets_dir,
                        conflicted_at_parse=(
                            pb.bucket_id in self._conflict_ids_at_parse
                        ),
                        target_id=planned.get(pb.bucket_id, pb.bucket_id),
                        link_map=link_map,
                    )
                    if result is None:
                        self._apply_skipped += 1
                        continue
                    target_id, target_path = result
                    self._apply_imported += 1
                    imported_id_map[pb.bucket_id] = target_id
                    imported_files[target_id] = target_path

                except Exception as e:
                    err_msg = f"[{pb.bucket_id}] {pb.name[:60]}: {e}"
                    logger.error(f"[migrate] apply error: {err_msg}", exc_info=True)
                    self._apply_errors.append(err_msg)
                    self._apply_skipped += 1
                finally:
                    self._apply_done += 1
                    if self._apply_done % _JOB_SAVE_EVERY == 0:
                        job.update(done=self._apply_done, imported=self._apply_imported,
                                   refused=len(self._refused))
                        self._save_job(job)

            # ---- Handling the vector data ----
            merged_ids: set[str] = set()
            if embedding_matches and self._has_embeddings and (
                self._zip_db_bytes or self._zip_db_path
            ):
                # When both model and dimension match, reuse the snapshot's vectors.
                # keep_both maps the source ID onto the new one.
                try:
                    if self._zip_db_path:
                        merged_ids = await _to_thread_reaped(
                            self._merge_embeddings_path,
                            self._zip_db_path,
                            imported_id_map,
                        )
                    else:
                        merged_ids = await _to_thread_reaped(
                            self._merge_embeddings,
                            self._zip_db_bytes or b"",
                            imported_id_map,
                        )
                except Exception as e:
                    message = f"向量快照合并失败，已转入后台重建: {e}"
                    logger.warning("[migrate] %s", message)
                    self._apply_errors.append(message)

            if self._package_members:
                from . import export_package
                self._state_report = await _to_thread_reaped(
                    functools.partial(
                        export_package.restore_library_state,
                        self._bucket_mgr,
                        self._package_members,
                        fresh=fresh,
                        id_map=dict(imported_id_map),
                        left_out=frozenset(self._refused),
                        own_basis=survey["own_basis"],
                    )
                )
                for message in self._state_report.get("errors") or []:
                    self._apply_errors.append(message)
                await self._drop_withdrawn_slices()

            self._buckets_to_reindex = [
                (target_id, path)
                for target_id, path in imported_files.items()
                if target_id not in merged_ids
            ]
            await self._schedule_reindex()

            invalidate = getattr(self._bucket_mgr, "_invalidate_bm25", None)
            if callable(invalidate):
                invalidate()
            self._phase = PHASE_DONE
            job.update(phase=PHASE_DONE, finished_at=now_iso(), done=self._apply_done,
                       imported=self._apply_imported, refused=len(self._refused))
            self._save_job(job)

        except asyncio.CancelledError:
            self._phase = PHASE_ERROR
            self._error_message = "导入任务已取消"
            raise
        except Exception as e:
            self._phase = PHASE_ERROR
            self._error_message = str(e)
            logger.error(f"[migrate] apply failed: {e}", exc_info=True)
            if job:
                job.update(phase=PHASE_ERROR, error=str(e)[:500], finished_at=now_iso(),
                           done=self._apply_done, imported=self._apply_imported)
                self._save_job(job)
        finally:
            with self._state_guard:
                if self._apply_reservation == reservation_id:
                    self._apply_reservation = ""
            self._cleanup_parse_artifacts()
            self._parsed_buckets = []
            self._buckets_to_reindex = []

    async def _existing_entries(self) -> list[dict]:
        list_all = getattr(self._bucket_mgr, "list_all", None)
        if not callable(list_all):
            return []
        try:
            found = await list_all(include_archive=True)
        except TypeError:
            found = await list_all()
        return [b for b in found or [] if isinstance(b, dict)]

    def _member_bytes(self, pb: _ParsedBucket) -> bytes:
        return self._read_member(
            pb.md_bytes if pb.md_bytes is not None else pb.md_path,
            limit=(self._bucket_content_limit() + self._metadata_limit()
                   + _FRONTMATTER_OVERHEAD_BYTES),
            label=pb.arc_path)

    def _survey(self, existing: list[dict]) -> dict[str, Any]:
        """(Runs in a thread.) What of the receiving library the import has to respect:

            identical   package ids whose file in the library is byte for byte the
                        package's (already imported)
            own_basis   the sources the library's own entries stand on — every entry but
                        the identical ones, which are the package's — that the package's
                        registry rows may not settle (export_package._merge_registry)
            blocked     the library's entries blocked by a source change (an open
                        source_gone, source_held or source_restored record, or a source
                        its registry records as withdrawn or deleted)
            registry    the library's source registry; package_registry the package's own
        """
        from . import _invalidation as _I
        from . import _sources as _src
        from . import export_package as _ep

        registry = getattr(self._bucket_mgr, "sources", None)
        by_id = {str(b.get("id") or ""): b for b in existing}
        identical: set[str] = set()
        for pb in self._parsed_buckets:
            have = by_id.get(pb.bucket_id)
            if have and have.get("path") and _same_file(str(have["path"]),
                                                       self._member_bytes(pb)):
                identical.add(pb.bucket_id)
        own_basis: list = []
        blocked: set[str] = set()
        for bid, b in by_id.items():
            if bid in identical:
                continue
            meta = b.get("metadata") or {}
            for rec in _src.basis_records(meta):
                try:
                    own_basis.append(_src.record_id(rec))
                except (KeyError, TypeError, AttributeError):
                    continue
            if any(r.get("kind") in (_I.SOURCE_GONE, _I.SOURCE_HELD, _I.SOURCE_RESTORED)
                   for r in _I.open_records(meta)) or _ep.withdrawn(meta, registry):
                blocked.add(bid)
        return {"identical": identical, "own_basis": own_basis, "blocked": blocked,
                "registry": registry, "package_registry": self._package_registry()}

    def _package_registry(self):
        """The package's own source registry, read from its state members in a scratch
        folder: a package whose entries stand on a source its own registry withdrew is not
        one an export writes, and is not trusted either."""
        from . import _sources as _src
        from . import export_package as _ep

        rows = {name: _ep.STATE_PREFIX + f"{_src.SOURCES_DIR}/{name}"
                for name in (_src.CHANGES_FILE, _src.HELD_FILE, _src.LINE_ORDERS_FILE)}
        if not any(member in self._package_members for member in rows.values()):
            return None
        scratch = tempfile.mkdtemp(prefix="loci-package-registry-",
                                   dir=self._parse_temp_dir or None)
        folder = os.path.join(scratch, _src.SOURCES_DIR)
        os.makedirs(folder, exist_ok=True)
        for name, member in rows.items():
            source = self._package_members.get(member)
            if source is None:
                continue
            if isinstance(source, bytes):
                data = source
            else:
                with open(source, "rb") as handle:
                    data = handle.read()
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(data)
        return _src.SourceRegistry(scratch)

    def _refusals(self, decisions: dict[str, str], survey: dict[str, Any]) -> dict[str, str]:
        """(Runs in a thread.) package id -> why it is not written (REFUSAL_WORDS)."""
        from . import _invalidation as _I
        from . import _sources as _src
        from . import export_package as _ep

        registry = survey["registry"]
        package_registry = survey["package_registry"]
        refused: dict[str, str] = {}
        for pb in self._parsed_buckets:
            meta = pb.meta
            why = ""
            if (_ep.withdrawn(meta, None) or _ep.withdrawn(meta, registry)
                    or (package_registry is not None
                        and _ep.withdrawn(meta, package_registry))):
                why = "withdrawn"
            elif registry is not None and not _I.open_records(meta, _I.SOURCE_HELD):
                # An entry that already carries the package's own hold arrives blocked by
                # it; one the library's hold would reach without a record is not written.
                for raw in _src.basis_records(meta):
                    try:
                        [rec] = _src.normalize_sources([raw])
                    except (_src.SourceRecordError, ValueError):
                        continue
                    if registry.read_state(rec) == _src.HELD:
                        why = "held"
                        break
            if (not why and pb.bucket_id in survey["blocked"]
                    and pb.bucket_id not in survey["identical"]
                    and decisions.get(pb.bucket_id, "skip") in ("overwrite", "keep_both")):
                why = "blocked_here"
            if why:
                refused[pb.bucket_id] = why
        # Whatever is derived from a refused entry, or from one the library has blocked,
        # every generation.
        stop = set(refused) | survey["blocked"]
        grew = True
        while grew:
            grew = False
            for pb in self._parsed_buckets:
                if pb.bucket_id in refused or pb.bucket_id in survey["identical"]:
                    continue
                if set(read_from_ids(pb.meta)) & stop:
                    refused[pb.bucket_id] = "derived"
                    stop.add(pb.bucket_id)
                    grew = True
        return refused

    def _plan_ids(self, decisions: dict[str, str], survey: dict[str, Any]) -> dict[str, str]:
        """package id -> the id it is written under: its own, or for keep_both a fresh id
        in the library's shape that neither the library nor the package uses."""
        finder = getattr(self._bucket_mgr, "_find_bucket_file", None)
        package_ids = {pb.bucket_id for pb in self._parsed_buckets}
        given: set[str] = set()

        def taken(candidate: str) -> bool:
            return (candidate in package_ids or candidate in given
                    or (callable(finder) and bool(finder(candidate))))

        planned: dict[str, str] = {}
        for pb in self._parsed_buckets:
            if pb.bucket_id in self._refused:
                continue
            if (pb.bucket_id in self._conflict_ids_at_parse
                    and pb.bucket_id not in survey["identical"]
                    and decisions.get(pb.bucket_id) == "keep_both"):
                new_id = _new_entry_id(taken)
                given.add(new_id)
                planned[pb.bucket_id] = new_id
            else:
                planned[pb.bucket_id] = pb.bucket_id
        return planned

    async def _drop_withdrawn_slices(self) -> None:
        """Pending slices restored from the package over lines the library's registry
        holds as withdrawn or deleted lose their gist and draft at once, as a withdrawal's
        own clearing does (PendingSlices.withdraw_lines)."""
        from . import _sources as _src
        slices = getattr(self._bucket_mgr, "slices", None)
        registry = getattr(self._bucket_mgr, "sources", None)
        if slices is None or registry is None:
            return
        try:
            batches = slices.open_batches()
        except Exception as exc:                     # noqa: BLE001 - reported
            self._apply_errors.append(f"待核切片读不出来：{exc}")
            return
        for batch in batches:
            source = batch.get("source") or {}
            for one in batch.get("slices") or []:
                span = one.get("span") or {}
                first, last = str(span.get("first") or ""), str(span.get("last") or "")
                try:
                    sid = _src.SourceId(str(source.get("system")), str(source.get("instance")),
                                        str(source.get("container")), first,
                                        last if last and last != first else None)
                    state = registry.state_of(sid)
                except Exception:                    # noqa: BLE001 - not a source it can read
                    continue
                if state in (_src.WITHDRAWN, _src.DELETED):
                    await slices.withdraw_lines(source, [first])

    async def _apply_one_bucket(
        self,
        pb: _ParsedBucket,
        requested_decision: str,
        buckets_dir: str,
        *,
        conflicted_at_parse: bool,
        target_id: str | None = None,
        link_map: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        """Recheck and commit one ID while holding its normal mutation lock. `target_id`
        is the id a keep_both copy is written under (`_plan_ids`); `link_map` the package
        ids written under another id, which the entry's links are pointed at."""

        turn_factory = getattr(self._bucket_mgr, "_bucket_turn", None)
        turn = turn_factory(pb.bucket_id) if callable(turn_factory) else _noop_bucket_turn()
        async with turn:
            finder = getattr(self._bucket_mgr, "_find_bucket_file", None)
            existing_path = finder(pb.bucket_id) if callable(finder) else None

            # The filesystem state under this lock is authoritative.  A caller
            # cannot forge overwrite/keep_both for an ID that was conflict-free
            # in the parse snapshot: a newly-created collision always wins.
            if existing_path:
                if await _to_thread_reaped(_same_file, existing_path,
                                           self._member_bytes(pb)):
                    # Already the package's, byte for byte: imported, nothing written.
                    return pb.bucket_id, existing_path
                if not conflicted_at_parse:
                    message = (
                        f"[{pb.bucket_id}] apply 时出现新冲突，已跳过；"
                        "请重新解析后再选择冲突策略"
                    )
                    logger.warning("[migrate] %s", message)
                    self._apply_errors.append(message)
                    return None
                if requested_decision == "overwrite":
                    return await _to_thread_reaped(
                        self._overwrite_bucket_transaction,
                        pb,
                        pb.bucket_id,
                        buckets_dir,
                        existing_path,
                        link_map or {},
                    )
                if requested_decision != "keep_both":
                    return None
                target_id = target_id if target_id and target_id != pb.bucket_id else (
                    _new_entry_id(lambda c: bool(finder(c)) if callable(finder) else False))
            elif not target_id:
                target_id = pb.bucket_id

            return await _to_thread_reaped(
                self._write_bucket_file,
                pb,
                target_id,
                buckets_dir,
                link_map or {},
            )

    def _render_bucket(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str,
        link_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str, str]:
        """(Runs in a thread.) Pure computation: parse the frontmatter, work out the target
        path and the serialised markdown. Apart from os.makedirs creating directories, it
        performs no disk writes at all. `link_map`: package ids written under another id,
        which the entry's links are pointed at (`_remap_links`).

        Returns (content, target_path, rendered).
        """
        raw = self._read_member(
            pb.md_bytes if pb.md_bytes is not None else pb.md_path,
            limit=(
                self._bucket_content_limit()
                + self._metadata_limit()
                + _FRONTMATTER_OVERHEAD_BYTES
            ),
            label=pb.arc_path,
        )
        meta, content = _parse_md_meta(raw)
        written_id = str(meta.get("id") or "")
        meta = self._normalize_import_metadata(meta)
        content_size = len(content.encode("utf-8"))
        if content_size > self._bucket_content_limit():
            raise BackupArchiveError(
                f"{pb.arc_path} 正文过大（{content_size} bytes > {self._bucket_content_limit()}）"
            )

        relinked = _remap_links(meta, link_map or {})

        # A file that already names itself by the id it is restored under, and links to
        # nothing that moved, goes back byte for byte, at its path in the library it came
        # from: the Markdown is the library, and re-serialising it would be a second writer
        # of every field.
        if written_id == target_id and not relinked:
            verbatim_path = self._package_path(pb.arc_path, buckets_dir)
            if verbatim_path:
                os.makedirs(os.path.dirname(verbatim_path), exist_ok=True)
                return content, verbatim_path, raw.decode("utf-8")

        # Always write an explicit ID; restoring never depends on guessing from a filename.
        meta["id"] = target_id

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
        target_path = str(safe_path(target_dir, f"{safe_name}_{safe_id}.md"))

        # Re-serialise the frontmatter plus body
        post = frontmatter.Post(content, **meta)
        rendered = frontmatter.dumps(post)
        return content, target_path, rendered

    @staticmethod
    def _package_path(arc_path: str, buckets_dir: str) -> str:
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

    @staticmethod
    def _atomic_write(path: str, rendered: str) -> None:
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

    @staticmethod
    def _atomic_create(path: str, rendered: str) -> None:
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

    def _write_bucket_file(
        self, pb: _ParsedBucket, target_id: str, buckets_dir: str,
        link_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str]:
        """Write one new bucket without replacing an existing path."""
        _content, target_path, rendered = self._render_bucket(pb, target_id, buckets_dir,
                                                              link_map)
        self._atomic_create(target_path, rendered)
        logger.debug(f"[migrate] wrote {target_path} (id={target_id})")
        return target_id, target_path

    def _write_bucket_file_staged(
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
        content, target_path, rendered = self._render_bucket(pb, target_id, buckets_dir,
                                                             link_map)
        staged_path = f"{target_path}.staging-{uuid.uuid4().hex}"
        self._atomic_write(staged_path, rendered)
        logger.debug(f"[migrate] staged {staged_path} (id={target_id}, target={target_path})")
        return content, target_path, staged_path

    def _write_historical_copy(
        self,
        existing_path: str,
        bucket_id: str,
        buckets_dir: str,
    ) -> str:
        """Create the pre-overwrite version under a fresh archived ID in the library's own
        shape (12 hex characters), so it reads by id like any other entry."""

        post = frontmatter.load(existing_path)
        finder = getattr(self._bucket_mgr, "_find_bucket_file", None)
        new_id = _new_entry_id(lambda c: bool(finder(c)) if callable(finder) else False)
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
        self._atomic_create(target_path, frontmatter.dumps(post))
        return target_path

    def _overwrite_bucket_transaction(
        self,
        pb: _ParsedBucket,
        target_id: str,
        buckets_dir: str,
        existing_path: str,
        link_map: Optional[dict[str, str]] = None,
    ) -> tuple[str, str]:
        """Preserve old data and commit an overwrite with rollback on failure."""

        _content, target_path, staged_path = self._write_bucket_file_staged(
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
            historical_path = self._write_historical_copy(
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
            return target_id, target_path
        except Exception:
            if target_created:
                _safe_unlink(target_path)
            if historical_path:
                _safe_unlink(historical_path)
            raise
        finally:
            _safe_unlink(staged_path)

    def _merge_embeddings(self, db_bytes: bytes, id_map: dict[str, str]) -> set[str]:
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
            return self._merge_embeddings_path(tmp_path, id_map)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _merge_embeddings_path(
        self,
        source_db: str,
        id_map: dict[str, str],
    ) -> set[str]:
        """Merge a validated snapshot in bounded, validated batches.

        The SQLite file is untrusted.  Never ``fetchall`` vector cells and never
        copy arbitrary SQLite values into the live index.  SQL ``CASE`` keeps an
        oversized cell out of the Python result entirely; valid JSON vectors are
        normalized before batches are committed.
        """

        current_db = getattr(self._embedding_engine, "db_path", "")
        if not current_db or not os.path.isfile(current_db):
            logger.warning("[migrate] 当前 embeddings.db 路径无效，跳过向量合并")
            return set()

        safe_id_map = {
            source_id: target_id
            for source_id, target_id in id_map.items()
            if (
                isinstance(source_id, str)
                and isinstance(target_id, str)
                and 0 < len(source_id) <= 200
                and 0 < len(target_id) <= 200
            )
        }
        if not safe_id_map:
            return set()

        src = sqlite3.connect(source_db, timeout=30)
        dst = sqlite3.connect(current_db, timeout=30)
        try:
            table = src.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'embeddings'"
            ).fetchone()
            if table is None:
                logger.warning("[migrate] 导入包 embeddings.db 缺少 embeddings 表，跳过")
                return set()

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

            src.execute(
                "CREATE TEMP TABLE loci_migrate_wanted_ids "
                "(source_id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            source_ids = list(safe_id_map)
            for offset in range(0, len(source_ids), _EMBEDDING_FETCH_BATCH):
                batch = source_ids[offset:offset + _EMBEDDING_FETCH_BATCH]
                src.executemany(
                    "INSERT INTO loci_migrate_wanted_ids (source_id) VALUES (?)",
                    ((source_id,) for source_id in batch),
                )

            def bounded_column(column: str | None, limit: int) -> str:
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

            # All interpolated identifiers are selected from the fixed schema
            # names above; user data remains parameterized in the temp table.
            query = (
                f"SELECT e.{id_column}, "  # nosec B608
                f"{bounded_column(vector_column, _MAX_EMBEDDING_CELL_BYTES)}, "
                f"{bounded_column(updated_column, _MAX_EMBEDDING_TIMESTAMP_BYTES)}, "
                f"{bounded_column(hash_column, _MAX_EMBEDDING_HASH_BYTES)}, "
                f"{bounded_column(meaning_column, _MAX_EMBEDDING_CELL_BYTES)} "
                "FROM embeddings AS e "
                f"JOIN loci_migrate_wanted_ids AS wanted "
                f"ON e.{id_column} = wanted.source_id"
            )
            cursor = src.execute(query)
            expected_dim = self._expected_embedding_dimension()
            fallback_time = now_iso()
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
                    normalized_vector = self._normalize_embedding_vector(
                        vector_value,
                        vector_type,
                        vector_size,
                        expected_dim,
                    )
                    updated_at = self._normalize_embedding_text(
                        updated_value,
                        updated_type,
                        updated_size,
                        _MAX_EMBEDDING_TIMESTAMP_BYTES,
                    )
                    content_hash = self._normalize_embedding_text(
                        hash_value,
                        hash_type,
                        hash_size,
                        _MAX_EMBEDDING_HASH_BYTES,
                    )
                    # No meaning vector is no meaning vector; a malformed one is dropped
                    # alone and the entry's next meaning write computes it again.
                    meaning_vector = (
                        self._normalize_embedding_vector(
                            meaning_value, meaning_type, meaning_size, expected_dim)
                        if meaning_value is not None else None
                    )
                    if (
                        target_id is None
                        or normalized_vector is None
                        or updated_at is None
                        or content_hash is None
                    ):
                        skipped += 1
                        if skipped <= 5:
                            logger.warning(
                                "[migrate] 跳过非法或过大的 embedding 行: %r",
                                source_id,
                            )
                        continue
                    normalized_rows.append(
                        (
                            target_id,
                            normalized_vector,
                            updated_at or fallback_time,
                            content_hash,
                            meaning_vector,
                        )
                    )
                    normalized_ids.append(target_id)

                if normalized_rows:
                    if meaning_column is not None:
                        dst.executemany(
                            """INSERT OR REPLACE INTO embeddings
                               (bucket_id, embedding, updated_at, content_hash,
                                meaning_embedding)
                               VALUES (?, ?, ?, ?, ?)""",
                            normalized_rows,
                        )
                    else:
                        dst.executemany(
                            """INSERT OR REPLACE INTO embeddings
                               (bucket_id, embedding, updated_at, content_hash)
                               VALUES (?, ?, ?, ?)""",
                            [r[:4] for r in normalized_rows],
                        )
                    dst.commit()
                    merged.update(normalized_ids)
                # Drop the current SQLite payloads before fetchmany builds the
                # next batch; otherwise Python briefly retains two batches.
                rows.clear()

            logger.info(
                "[migrate] 合并了 %d 条 embedding 向量，跳过 %d 条",
                len(merged),
                skipped,
            )
            return merged
        finally:
            src.close()
            dst.close()

    def _expected_embedding_dimension(self) -> int:
        if 0 < self._import_model_dim <= _MAX_EMBEDDING_DIMENSIONS:
            return self._import_model_dim
        backend = getattr(self._embedding_engine, "_backend", None)
        try:
            dimension = int(backend.vector_dim()) if backend else 0
        except (TypeError, ValueError, OverflowError):
            dimension = 0
        return dimension if 0 < dimension <= _MAX_EMBEDDING_DIMENSIONS else 0

    @staticmethod
    def _normalize_embedding_text(
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

    @classmethod
    def _normalize_embedding_vector(
        cls,
        value: Any,
        value_type: Any,
        declared_size: Any,
        expected_dimension: int,
    ) -> str | None:
        payload = cls._normalize_embedding_text(
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

    async def _schedule_reindex(self) -> None:
        """Durably queue missing derived indexes; only legacy runtimes index inline."""
        self._reindex_total = len(self._buckets_to_reindex)
        self._reindex_done = 0
        self._reindex_errors = 0
        if not self._buckets_to_reindex:
            return

        outbox = getattr(self._bucket_mgr, "embedding_outbox", None)
        if outbox is not None and callable(getattr(outbox, "enqueue", None)):
            for bucket_id, bucket_path in self._buckets_to_reindex:
                try:
                    content = await _to_thread_reaped(
                        self._read_bucket_content,
                        bucket_path,
                    )
                    if content.strip():
                        outbox.enqueue(bucket_id, content)
                except Exception as exc:
                    self._reindex_errors += 1
                    self._apply_errors.append(f"[{bucket_id}] 无法加入向量队列: {exc}")
                self._reindex_done += 1
            self._buckets_to_reindex = []
            return

        self._phase = PHASE_REINDEXING
        await self._reindex_all()
        self._buckets_to_reindex = []

    @staticmethod
    def _read_bucket_content(bucket_path: str) -> str:
        return frontmatter.load(bucket_path).content or ""

    async def _reindex_all(self) -> None:
        """Regenerate vectors for the buckets that were imported while the embedding model
        did not match."""
        emb = self._embedding_engine
        if not getattr(emb, "enabled", False):
            logger.warning("[migrate] embedding engine 未启用，跳过重新向量化")
            self._phase = PHASE_DONE
            return

        for bucket_id, bucket_path in self._buckets_to_reindex:
            try:
                content = await _to_thread_reaped(
                    self._read_bucket_content,
                    bucket_path,
                )
            except Exception as e:
                logger.warning(f"[migrate] read reindex source {bucket_id[:12]}: {e}")
                self._reindex_errors += 1
                self._reindex_done += 1
                continue
            if not content.strip():
                self._reindex_done += 1
                continue
            try:
                await emb.generate_and_store(bucket_id, content)
            except Exception as e:
                logger.warning(f"[migrate] reindex {bucket_id[:12]}: {e}")
                self._reindex_errors += 1
            self._reindex_done += 1

        logger.info(
            f"[migrate] 重新向量化完成: "
            f"{self._reindex_done - self._reindex_errors} 成功, "
            f"{self._reindex_errors} 失败"
        )
        self._phase = PHASE_DONE
