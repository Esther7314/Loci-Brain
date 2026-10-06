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

Where the steps live. This module holds the engine: its state machine, reservations,
status, the job on disk, and the order of the steps. The steps themselves are its
siblings:
- core/_package_read.py     reading and validating the package (parse)
- core/_package_plan.py     collisions, what the library refuses, the ids entries get
- core/_package_write.py    writing one entry: create, or overwrite with the old archived
- core/_package_vectors.py  merging the package's embeddings.db into the live index

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
import os
import shutil
import tempfile
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager
from typing import Any, Optional

import frontmatter

from .backup_archive import (
    BackupArchiveError,
    extract_backup_archive_file,
)
from . import _package_plan as _plan
from . import _package_read as _read
from . import _package_vectors as _vectors
from ._package_plan import ConflictInfo
from ._package_read import _ParsedBucket
# The vector-row cap is read here (tests/test_package_into_a_living_library.py holds it
# against the archive's member cap).
from ._package_vectors import _MAX_EMBEDDING_ROWS  # noqa: F401
from ._package_write import EntryWriter, _new_entry_id, _safe_unlink, _same_file

from utils import atomic_write_text, now_iso  # type: ignore

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

_PARSED_WORKSPACE_TTL_SECONDS = 3600.0
_PARSED_WORKSPACE_SWEEP_SECONDS = 60.0

# The import job, kept on disk (`<buckets>/_state/import_package.json`) so that a restart
# shows it and a second run of the same package carries the first one's mode: a library
# that was empty when the package started coming in is still restored whole, although half
# the entries are already there.
JOB_FILE = os.path.join("_state", "import_package.json")
_JOB_SAVE_EVERY = 50
_REFUSED_SHOWN = 200
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
# Helpers
# ============================================================

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

    # ----------------------------------------------------------
    # Reservations and the parsed generation
    # ----------------------------------------------------------

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
    # Step one: parse the zip (core/_package_read.py)
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
            self._fail_parse("zip 预检已取消")
            raise
        except Exception as e:
            self._fail_parse(f"zip 预检失败: {e}")
            logger.error(f"[migrate] parse_zip error: {e}", exc_info=True)
            return {"ok": False, "error": self._error_message}

        return self._parsed_reply()

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
            self._fail_parse("zip 预检已取消")
            raise
        except Exception as e:
            shutil.rmtree(workspace, ignore_errors=True)
            self._fail_parse(f"zip 预检失败: {e}")
            logger.error(f"[migrate] parse_zip_file error: {e}", exc_info=True)
            return {"ok": False, "error": self._error_message}

        return self._parsed_reply()

    def _fail_parse(self, message: str) -> None:
        """A parse that did not finish: nothing of it is kept."""
        self._cleanup_parse_artifacts()
        self._phase = PHASE_ERROR
        self._error_message = message

    def _parsed_reply(self) -> dict:
        """The parse is done: its generation waits for apply, and the TTL clock starts."""
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
        return _read.parse_package(package, disk_backed=disk_backed, config=self._config,
                                   bucket_mgr=self._bucket_mgr)

    async def _identify_conflicts(self) -> None:
        """The package ids that collide with the library's entries at parse time
        (core/_package_plan.identify_conflicts); apply honours a decision only for these."""
        conflicts = await _plan.identify_conflicts(self._bucket_mgr, self._parsed_buckets)
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

        The steps, in order: survey the library, open the job on disk (backing the
        library up first when it has entries), decide what is refused and which ids the
        entries get, write the entries, merge the vectors, restore the library's state,
        queue what still needs vectors, close the job.
        """
        if reservation_id is None:
            reservation_id = self.reserve_apply(self._job_id)
        self._check_apply_reservation(reservation_id)
        self._reset_apply_progress()

        embedding_matches = self._embedding_match()
        buckets_dir = self._config.get("buckets_dir", "buckets")
        self._state_report = None
        job: dict[str, Any] = {}

        try:
            existing = await _plan.library_entries(self._bucket_mgr)
            survey = await _to_thread_reaped(
                _plan.survey_library, self._bucket_mgr, self._config, self._parsed_buckets,
                existing, self._package_members, self._parse_temp_dir)
            fresh = await self._open_job(job, existing, buckets_dir)

            self._refused = await _to_thread_reaped(
                _plan.refusals, self._parsed_buckets, decisions, survey)
            planned = _plan.plan_ids(self._bucket_mgr, self._parsed_buckets, self._refused,
                                     self._conflict_ids_at_parse, decisions, survey)
            imported_id_map, imported_files = await self._write_entries(
                decisions, buckets_dir, planned, job)

            merged_ids = await self._merge_vectors(embedding_matches, imported_id_map)
            if self._package_members:
                await self._restore_library_state(fresh, imported_id_map, survey)

            self._buckets_to_reindex = [
                (target_id, path)
                for target_id, path in imported_files.items()
                if target_id not in merged_ids
            ]
            await self._schedule_reindex()
            self._close_job(job)

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
            self._release_apply(reservation_id)

    def _check_apply_reservation(self, reservation_id: str | None) -> None:
        """Only the holder of the current apply reservation may apply."""
        with self._state_guard:
            apply_valid = bool(
                reservation_id
                and self._phase == PHASE_APPLYING
                and reservation_id == self._apply_reservation
            )
            current_phase = self._phase
        if not apply_valid:
            raise RuntimeError(f"当前状态为 {current_phase}，apply 需要先完成并占用 parse_zip")

    def _reset_apply_progress(self) -> None:
        self._apply_total = len(self._parsed_buckets)
        self._apply_done = 0
        self._apply_imported = 0
        self._apply_skipped = 0
        self._apply_errors = []
        self._buckets_to_reindex = []
        self._refused = {}
        self._backup_path = ""
        self._resumed = False

    def _release_apply(self, reservation_id: str | None) -> None:
        """Whatever way the apply ended: the reservation and the parsed generation go."""
        with self._state_guard:
            if self._apply_reservation == reservation_id:
                self._apply_reservation = ""
        self._cleanup_parse_artifacts()
        self._parsed_buckets = []
        self._buckets_to_reindex = []

    async def _open_job(self, job: dict[str, Any], existing: list[dict],
                        buckets_dir: str) -> bool:
        """Fill `job` and save it before anything is written; returns whether the package's
        state is restored whole (`fresh`) rather than merged.

        Judged before anything is written: a library with no entries takes a package's
        state whole, one with entries has it merged. A second run of an interrupted import
        keeps the first run's judgement, and its backup."""
        earlier = self._read_job()
        resuming = bool(
            earlier and earlier.get("phase") == PHASE_APPLYING
            and self._package_sha and earlier.get("package_sha256") == self._package_sha
        )
        self._resumed = resuming
        if resuming:
            fresh = earlier.get("mode") == "fresh"
            self._backup_path = str(earlier.get("backup") or "")
        else:
            fresh = bool(self._package_members) and not existing
        if existing and not fresh and not self._backup_path:
            from . import schema as _schema
            self._backup_path = str(await _to_thread_reaped(
                _schema.backup, buckets_dir, "before-import-package"))
        job.update({"phase": PHASE_APPLYING, "mode": "fresh" if fresh else "merge",
                    "package_sha256": self._package_sha, "job_id": self._job_id,
                    "started_at": (earlier or {}).get("started_at") if resuming else now_iso(),
                    "backup": self._backup_path, "done": 0, "total": self._apply_total,
                    "imported": 0, "refused": 0})
        self._save_job(job)
        return fresh

    async def _write_entries(
        self, decisions: dict[str, str], buckets_dir: str, planned: dict[str, str],
        job: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Write every entry that is not refused, one at a time under its own lock; one
        entry's failure is counted and the rest go on. The job on disk follows every
        _JOB_SAVE_EVERY entries. Returns (package id -> id written under, written id ->
        its file)."""
        imported_id_map: dict[str, str] = {}
        imported_files: dict[str, str] = {}
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
        return imported_id_map, imported_files

    async def _merge_vectors(self, embedding_matches: bool,
                             imported_id_map: dict[str, str]) -> set[str]:
        """The ids whose vectors came from the package's snapshot (core/_package_vectors).
        A failed merge is reported and those entries are re-vectorised instead."""
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
        return merged_ids

    def _merge_embeddings(self, db_bytes: bytes, id_map: dict[str, str]) -> set[str]:
        """(Runs in a thread.) The bytes compatibility path of _merge_embeddings_path."""
        return _vectors.merge_embeddings(db_bytes, id_map,
                                         embedding_engine=self._embedding_engine,
                                         import_model_dim=self._import_model_dim)

    def _merge_embeddings_path(self, source_db: str, id_map: dict[str, str]) -> set[str]:
        """(Runs in a thread.) The snapshot's vectors into the live embedding engine's
        index, under the ids the entries were written under."""
        return _vectors.merge_embeddings_path(source_db, id_map,
                                              embedding_engine=self._embedding_engine,
                                              import_model_dim=self._import_model_dim)

    async def _restore_library_state(self, fresh: bool, imported_id_map: dict[str, str],
                                     survey: dict[str, Any]) -> None:
        """The package's library state, after the entries: whole into a library that had
        none, merged into one that had (export_package.restore_library_state); then the
        pending slices over lines this library has withdrawn are cleared."""
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
        await _plan.drop_withdrawn_slices(self._bucket_mgr, self._apply_errors)

    def _close_job(self, job: dict[str, Any]) -> None:
        """The apply finished: search indexes are rebuilt lazily, the job is done."""
        invalidate = getattr(self._bucket_mgr, "_invalidate_bm25", None)
        if callable(invalidate):
            invalidate()
        self._phase = PHASE_DONE
        job.update(phase=PHASE_DONE, finished_at=now_iso(), done=self._apply_done,
                   imported=self._apply_imported, refused=len(self._refused))
        self._save_job(job)

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
        is the id a keep_both copy is written under (`_package_plan.plan_ids`); `link_map`
        the package ids written under another id, which the entry's links are pointed at."""

        writer = EntryWriter(self._config, self._bucket_mgr)
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
                                           _read.read_bucket_member(self._config, pb)):
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
                        writer.overwrite_bucket_transaction,
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
                writer.write_bucket_file,
                pb,
                target_id,
                buckets_dir,
                link_map or {},
            )

    # ----------------------------------------------------------
    # Step three: vectors for what the snapshot did not cover
    # ----------------------------------------------------------

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
