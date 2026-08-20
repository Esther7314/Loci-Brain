"""
========================================
migration_engine.py — the embedding migration engine
========================================

Switching embedding backend (local <-> api) means recomputing the vector of every bucket
in embeddings.db with the new backend. This module runs that in the background:

- Back up embeddings.db -> embeddings.db.backup (only on the first run)
- Write new vectors into embeddings.db.migrating first, so a half-finished state cannot
  contaminate the main table
- Once everything is through, swap atomically: the main db is replaced by the .migrating file
- A single failure is skipped and recorded in failed_items[:50] without stopping the run
- Progress lives in _pending_migration_status.json, which the front end polls every 3s
- Resume after interruption: _migration_checkpoint.json records the set of finished ids
- Rate limiting: batches of 10 with a 0.5s gap, so local inference cannot peg the CPU and
  an API cannot rate-limit us
- On failure it attaches the last 15 lines of errors.jsonl, pointing the user at the fact
  that this is usually a local-environment problem

What it does not do:
- It does not migrate buckets or rewrite bucket files
- It does not switch the global embedding_engine — that belongs to the caller in server.py
- It does not write configuration to disk
- It does not import a full backup package exported from another instance — that is
  migrate_engine.py's job. The two filenames are very nearly the same, so make sure you
  know which one you are editing before you change anything.
========================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger("loci_brain.migration_engine")


# ---- Constants ----

_STATUS_FILE_NAME = "_pending_migration_status.json"
_CHECKPOINT_FILE_NAME = "_migration_checkpoint.json"

# Batches of 10, 0.5s apart
BATCH_SIZE = 10
BATCH_INTERVAL_SEC = 0.5

# Cap on failed_items, so the status JSON cannot grow without bound
MAX_FAILED_ITEMS = 50

# How many trailing lines of errors.jsonl travel with a failure
TAIL_LOG_LINES = 15

# A process-wide lock: only one migration job may run at a time
_migration_lock = threading.Lock()
_migration_owner_guard = threading.Lock()
_migration_owner: "MigrationReservation | None" = None
_migration_task: asyncio.Task | None = None
_v3_runtime: Any = None


@dataclass(frozen=True)
class MigrationReservation:
    """Opaque ownership token for one embedding migration attempt.

    The web route reserves the process-wide migration slot *before* it creates
    or resets the staging database and before it awaits the provider probe.
    ``start_migration`` then transfers that same reservation to the background
    task.  Keeping ownership explicit prevents a losing concurrent request
    from touching another job's staging/checkpoint/outbox lifecycle.
    """

    job_id: str


def reserve_migration() -> MigrationReservation | None:
    """Atomically reserve the single migration slot without starting a task."""

    global _migration_owner
    if not _migration_lock.acquire(blocking=False):
        logger.info("[migration] another migration already in progress; skip")
        return None
    reservation = MigrationReservation(job_id=uuid.uuid4().hex)
    with _migration_owner_guard:
        _migration_owner = reservation
    return reservation


def owns_migration_reservation(reservation: MigrationReservation) -> bool:
    """Return whether ``reservation`` is the active migration owner."""

    with _migration_owner_guard:
        return _migration_owner is reservation


def release_migration_reservation(reservation: MigrationReservation) -> bool:
    """Release ``reservation`` iff it still owns the migration slot."""

    global _migration_owner
    with _migration_owner_guard:
        if _migration_owner is not reservation:
            return False
        _migration_owner = None
        _migration_lock.release()
    return True


def attach_v3_runtime(runtime) -> None:
    global _v3_runtime
    _v3_runtime = runtime


def get_v3_runtime():
    return _v3_runtime


# ============================================================
# Paths and status
# ============================================================

def status_path_for(buckets_dir: str) -> str:
    log_dir = os.path.join(buckets_dir, ".logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, _STATUS_FILE_NAME)


def checkpoint_path_for(buckets_dir: str) -> str:
    log_dir = os.path.join(buckets_dir, ".logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, _CHECKPOINT_FILE_NAME)


def _empty_status() -> dict[str, Any]:
    return {
        "phase": "idle",      # idle | running | completed | failed
        "total": 0,
        "done": 0,
        "failed_count": 0,
        "current_id": "",
        "failed_items": [],
        "started_at": "",
        "finished_at": "",
        "target_backend": "",
        "target_model": "",
        "target_dim": 0,
        "message": "",
        "error": "",
        "tail_log": [],
    }


def read_status(status_path: str) -> dict[str, Any]:
    if not os.path.exists(status_path):
        return _empty_status()
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_status()
        return data
    except (OSError, json.JSONDecodeError):
        return _empty_status()


def write_status(status_path: str, status: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(status_path), exist_ok=True)
        tmp = status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, status_path)
    except OSError as e:
        logger.warning(f"[migration] failed to write status: {e}")


def target_signature(target_backend: str, target_model: str, target_dim: int) -> str:
    """A unique signature for the migration target: resuming only applies when the target
    is the same one as last time.

    The checkpoint used to store done_ids alone and record nothing about the target — so a
    migration to backend A that failed halfway, followed by a migration to backend B, would
    treat A's done_ids as already finished for B, reuse A's vectors sitting in the same
    staging db, and swap them atomically into the main store. A signature mismatch has to
    mean starting over entirely.
    """
    return f"{target_backend}:{target_model}:{target_dim}"


def _read_checkpoint(path: str, signature: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return set()
        if data.get("target_signature") != signature:
            return set()
        done = data.get("done_ids", [])
        return set(done) if isinstance(done, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def _write_checkpoint(path: str, done_ids: Iterable[str], signature: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"done_ids": sorted(done_ids), "target_signature": signature},
                f, ensure_ascii=False,
            )
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[migration] failed to write checkpoint: {e}")


def staging_db_path_for(db_path: str) -> str:
    """Intermediate vectors go into this file and this file only. The live db is never
    touched until everything has succeeded and the atomic replace happens."""
    return f"{db_path}.migrating"


def reset_stale_migration_state(buckets_dir: str, db_path: str, signature: str) -> None:
    """Called before starting a fresh migration: if the checkpoint's target signature does
    not match, wipe it entirely.

    It must be called **before** the caller constructs target_engine (and thereby runs
    ``_init_db()`` against the staging db path). Otherwise writing continues into a staging
    db still holding the previous target model's vectors, and the signature check becomes
    decorative.
    """
    ckpt_path = checkpoint_path_for(buckets_dir)
    if not os.path.exists(ckpt_path):
        return
    try:
        with open(ckpt_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        stale = not isinstance(data, dict) or data.get("target_signature") != signature
    except (OSError, json.JSONDecodeError):
        stale = True
    if not stale:
        return
    try:
        os.remove(ckpt_path)
    except OSError:
        pass
    staging_path = staging_db_path_for(db_path)
    try:
        if os.path.exists(staging_path):
            os.remove(staging_path)
    except OSError as e:
        logger.warning(f"[migration] failed to remove stale staging db {staging_path}: {e}")


def _tail_errors_log(buckets_dir: str, n: int = TAIL_LOG_LINES) -> list[str]:
    """Read the last n lines of errors.jsonl. An empty list on failure."""
    candidates = [
        os.path.join(buckets_dir, ".logs", "errors.jsonl"),
        os.path.join(buckets_dir, "errors.jsonl"),
    ]
    for p in candidates:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return [ln.rstrip("\n") for ln in lines[-n:]]
        except OSError:
            continue
    return []


# ============================================================
# Backup and commit
# ============================================================

def backup_db_once(db_path: str) -> str:
    """Back up db_path if no .backup exists yet, and return the backup's path.

    If a .backup is already there it is not made again, so an earlier version cannot be
    overwritten.
    """
    backup = db_path + ".backup"
    if os.path.exists(backup):
        return backup
    if not os.path.exists(db_path):
        return backup
    shutil.copy2(db_path, backup)
    return backup


# ============================================================
# The core of the migration
# ============================================================

@dataclass
class MigrationConfig:
    """The migration's parameters."""
    buckets_dir: str
    db_path: str
    target_backend: str          # 'local' | 'api'
    target_model: str
    target_dim: int
    # Both the source and target engines have already been constructed by the caller
    target_engine: Any           # an EmbeddingEngine instance: the migration target
    # Where bucket content comes from: an awaitable returning list[(bucket_id, content)]
    fetch_buckets: Callable[[], Awaitable[list[tuple[str, str]]]]


async def _run_migration(
    cfg: MigrationConfig,
    on_complete: Callable[[bool], None] | None = None,
) -> None:
    """The coroutine that actually runs the migration."""
    status_path = status_path_for(cfg.buckets_dir)
    ckpt_path = checkpoint_path_for(cfg.buckets_dir)

    # 1) Back up the original db
    try:
        backup_db_once(cfg.db_path)
    except Exception as e:
        write_status(status_path, {
            **_empty_status(),
            "phase": "failed",
            "error": f"backup failed: {type(e).__name__}: {e}",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": "迁移未启动：备份 embeddings.db 失败",
            "tail_log": _tail_errors_log(cfg.buckets_dir),
        })
        if on_complete:
            on_complete(False)
        return

    # 2) Fetch every bucket
    try:
        buckets = await cfg.fetch_buckets()
    except Exception as e:
        write_status(status_path, {
            **_empty_status(),
            "phase": "failed",
            "error": f"fetch buckets failed: {type(e).__name__}: {e}",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": "迁移未启动：列出桶失败",
            "tail_log": _tail_errors_log(cfg.buckets_dir),
        })
        if on_complete:
            on_complete(False)
        return

    total = len(buckets)
    signature = target_signature(cfg.target_backend, cfg.target_model, cfg.target_dim)
    done_ids = _read_checkpoint(ckpt_path, signature)  # resume from the checkpoint (a mismatched target starts over)
    failed_items: list[dict[str, str]] = []
    failed_count = 0

    write_status(status_path, {
        **_empty_status(),
        "phase": "running",
        "total": total,
        "done": len(done_ids),
        "failed_count": 0,
        "current_id": "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target_backend": cfg.target_backend,
        "target_model": cfg.target_model,
        "target_dim": cfg.target_dim,
        "message": f"开始迁移 {total} 个 bucket（已完成 {len(done_ids)}）",
    })

    # 3) Run in batches
    pending = [(bid, content) for bid, content in buckets if bid not in done_ids]
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        for bucket_id, content in batch:
            cur = read_status(status_path)
            cur["current_id"] = bucket_id
            write_status(status_path, cur)

            try:
                ok = await cfg.target_engine.generate_and_store(bucket_id, content)
                if not ok:
                    failed_count += 1
                    if len(failed_items) < MAX_FAILED_ITEMS:
                        failed_items.append({
                            "bucket_id": bucket_id,
                            "error": "generate_and_store returned False",
                        })
                else:
                    done_ids.add(bucket_id)
            except Exception as e:
                failed_count += 1
                if len(failed_items) < MAX_FAILED_ITEMS:
                    failed_items.append({
                        "bucket_id": bucket_id,
                        "error": f"{type(e).__name__}: {e}",
                    })

        # Write the checkpoint and status once per batch
        _write_checkpoint(ckpt_path, done_ids, signature)
        cur = read_status(status_path)
        cur["done"] = len(done_ids)
        cur["failed_count"] = failed_count
        cur["failed_items"] = failed_items
        cur["message"] = f"已完成 {len(done_ids)} / {total}（失败 {failed_count}）"
        write_status(status_path, cur)

        # Rate limiting
        if i + BATCH_SIZE < len(pending):
            await asyncio.sleep(BATCH_INTERVAL_SEC)

    # 4) Only a completely successful run swaps atomically into the main store. This is
    #    where the docstring's promise — "write .migrating first, swap atomically once
    #    everything is through" — actually lands. Throughout the loop only
    #    cfg.target_engine's own staging db is written (the caller points its db_path at
    #    whatever staging_db_path_for() returned when constructing target_engine), and
    #    cfg.db_path is never touched. So any failure or crash at any step leaves the live
    #    db exactly as it was before the migration, with no half-finished state mixing
    #    vectors from two models.
    all_done = failed_count == 0 and len(done_ids) >= total
    swap_error = ""
    if all_done:
        staged_path = getattr(cfg.target_engine, "db_path", "")
        if staged_path and os.path.abspath(staged_path) != os.path.abspath(cfg.db_path):
            try:
                os.replace(staged_path, cfg.db_path)
                # Anything this target_engine does from now on has to land on the live path
                # that was just swapped in. Left pointing at the old staging path, which has
                # been renamed away, the next sqlite3.connect() would quietly create an
                # empty database there — everything would look "fine" while every vector had
                # in fact been reset to nothing.
                cfg.target_engine.db_path = cfg.db_path
            except OSError as e:
                swap_error = f"{type(e).__name__}: {e}"
                logger.error(f"[migration] atomic swap staging→live failed: {swap_error}")

    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    success = all_done and not swap_error
    final_phase = "completed" if success else "failed"
    if swap_error:
        final_msg = f"迁移全部完成但原子替换主库失败，向量仍留在暂存文件：{swap_error}"
    else:
        final_msg = f"迁移完成：{len(done_ids)} 成功 / {failed_count} 失败"
    tail = []
    if failed_count > 0 or swap_error:
        # On failure, attach the log and a pointer to what to do
        tail = _tail_errors_log(cfg.buckets_dir)

    cur = read_status(status_path)
    cur.update({
        "phase": final_phase,
        "current_id": "",
        "done": len(done_ids),
        "failed_count": failed_count if not swap_error else max(failed_count, 1),
        "failed_items": failed_items,
        "finished_at": finished_at,
        "message": final_msg,
        "error": swap_error,
        "tail_log": tail,
    })
    write_status(status_path, cur)

    # On success, update embeddings_meta to the target backend's model/dim. Otherwise the
    # db meta still holds the old values and the next restart falsely reports an OB-W005
    # dimension mismatch.
    if success:
        try:
            cfg.target_engine._write_meta("model_name", cfg.target_model or "")
            cfg.target_engine._write_meta("vector_dim", str(cfg.target_dim or 0))
        except Exception as e:
            logger.warning(f"[migration] update meta failed: {e}")

    # Clear the checkpoint when it is done, so the next switch starts from scratch — but
    # only once the swap has genuinely succeeded. If the swap failed the checkpoint must
    # stay, so that a retry resumes rather than recomputing vectors the staging db already
    # holds.
    if success:
        try:
            if os.path.exists(ckpt_path):
                os.remove(ckpt_path)
        except OSError:
            pass

    if on_complete:
        try:
            on_complete(success)
        except Exception as e:
            logger.warning(f"[migration] on_complete callback failed: {e}")


def start_migration(
    cfg: MigrationConfig,
    loop: asyncio.AbstractEventLoop | None = None,
    on_complete: Callable[[bool], None] | None = None,
    *,
    reservation: MigrationReservation | None = None,
) -> asyncio.Task | None:
    """Start the background migration task on the given event loop.

    Only one migration may run at a time; a second call returns None.
    """
    global _migration_task
    active_reservation = reservation or reserve_migration()
    if active_reservation is None:
        return None
    if not owns_migration_reservation(active_reservation):
        logger.warning("[migration] rejected stale or foreign reservation")
        return None

    target_loop = loop or asyncio.get_event_loop()
    callback_called = False

    def _complete_once(success: bool) -> None:
        nonlocal callback_called
        if callback_called:
            return
        callback_called = True
        if on_complete:
            on_complete(success)

    async def _wrap():
        try:
            await _run_migration(cfg, on_complete=_complete_once)
        except BaseException:
            # Cancellation and unexpected worker failures must still restore
            # caller-owned resources such as the embedding outbox.
            _complete_once(False)
            raise
        finally:
            release_migration_reservation(active_reservation)

    try:
        task = target_loop.create_task(_wrap())
    except BaseException:
        release_migration_reservation(active_reservation)
        raise
    _migration_task = task
    return task


def is_running() -> bool:
    return _migration_lock.locked()


def reset_for_test() -> None:
    """For tests: force the lock to release."""
    global _migration_owner, _migration_task
    with _migration_owner_guard:
        _migration_owner = None
        if _migration_lock.locked():
            try:
                _migration_lock.release()
            except RuntimeError:
                pass
    _migration_task = None


__all__ = [
    "MigrationConfig",
    "status_path_for",
    "checkpoint_path_for",
    "read_status",
    "write_status",
    "backup_db_once",
    "MigrationReservation",
    "reserve_migration",
    "owns_migration_reservation",
    "release_migration_reservation",
    "start_migration",
    "is_running",
    "reset_for_test",
    "attach_v3_runtime",
    "get_v3_runtime",
    "BATCH_SIZE",
    "BATCH_INTERVAL_SEC",
]
