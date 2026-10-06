"""
========================================
reembed.py — recomputing every vector after a change of embedding model
========================================

Switching the embedding model (or backend) means recomputing the vector of every bucket
in embeddings.db with the new one. core/embedding_switch.py starts it when the panel
changes the model; this module runs it in the background:

- Write new vectors into embeddings.db.migrating, so a half-finished state cannot
  contaminate the main table; the live db is untouched until the swap, so no backup copy
  is made (one an earlier version left is removed at the swap: it keeps vectors of entries
  withdrawn since)
- Once everything is through, swap atomically: the main db is replaced by the .migrating
  file. Right before, the library is read again: an entry withdrawn, deleted or cleared
  meanwhile loses its new vector, and a meaning written meanwhile is computed again
- A bucket's meaning vector (its own column) is recomputed with its content vector; an
  entry is done only when both are
- A single failure is recorded (the checkpoint keeps every one, the status the first 50)
  without stopping the run; the run does not swap while any failed. Resuming tries them
  again; the person can also skip them (`skipped_ids`), and the new model computes those
  in the background after the swap
- Progress lives in _pending_migration_status.json, which the front end polls every 3s
- Resume after interruption: _migration_checkpoint.json records the set of finished ids
- Rate limiting: batches of 10 with a 0.5s gap, so local inference cannot peg the CPU and
  an API cannot rate-limit us
- On failure it attaches the last 15 lines of errors.jsonl, pointing the user at the fact
  that this is usually a local-environment problem

What it does not do:
- It does not migrate buckets or rewrite bucket files
- It does not switch the global embedding_engine — that belongs to the caller
  (core/embedding_switch.py, through the publish callback it is given)
- It does not write configuration to disk
- It does not import a full backup package exported from another instance — that is
  package_import.py's job.
========================================
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
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

    With done_ids alone and nothing about the target, a migration to backend A that failed
    halfway, followed by a migration to backend B, would treat A's done_ids as already
    finished for B, reuse A's vectors sitting in the same staging db, and swap them
    atomically into the main store. A signature mismatch has to mean starting over
    entirely.
    """
    return f"{target_backend}:{target_model}:{target_dim}"


def _digest(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def read_checkpoint(path: str, signature: str) -> dict[str, Any]:
    """The checkpoint of a recompute to `signature`: {done: set of ids with their new
    vectors, skipped: set of ids the person chose to leave without one, failed: {id: why},
    meanings: {id: digest of the meaning its meaning vector was computed from}}. Empty for
    another target: a mismatched target starts over."""
    out: dict[str, Any] = {"done": set(), "skipped": set(), "failed": {}, "meanings": {}}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict) or data.get("target_signature") != signature:
        return out
    for key, name in (("done_ids", "done"), ("skipped_ids", "skipped")):
        value = data.get(key, [])
        out[name] = set(map(str, value)) if isinstance(value, list) else set()
    for key in ("failed", "meanings"):
        value = data.get(key, {})
        out[key] = {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
    return out


def write_checkpoint(path: str, state: dict[str, Any], signature: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"done_ids": sorted(state.get("done") or ()),
                 "skipped_ids": sorted(state.get("skipped") or ()),
                 "failed": dict(state.get("failed") or {}),
                 "meanings": dict(state.get("meanings") or {}),
                 "target_signature": signature},
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
# The swap
# ============================================================

def backup_path_for(db_path: str) -> str:
    """Where an earlier version kept a copy of the vectors before a switch. Nothing writes
    it now — the live database is never touched until the swap, so it is its own backup
    until then, and after the swap the old model's vectors mean nothing to the new one —
    and a copy left behind (it holds vectors of entries withdrawn since) is removed by the
    next swap or abandon."""
    return db_path + ".backup"


def drop_backup(db_path: str) -> None:
    path = backup_path_for(db_path)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"[migration] could not remove {path}: {e}")


def _prune(db_path: str, keep: set[str]) -> int:
    """Delete the rows of every entry not in `keep` (withdrawn, deleted or cleared while
    the recompute ran), overwriting their pages. Returns how many went."""
    if not db_path or not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        have = [r[0] for r in conn.execute("SELECT bucket_id FROM embeddings")]
        gone = [bid for bid in have if bid not in keep]
        for bid in gone:
            conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bid,))
        conn.commit()
        return len(gone)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return 0
        raise
    finally:
        conn.close()


def _clear_meaning(db_path: str, ids: Iterable[str]) -> None:
    ids = list(ids)
    if not ids or not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany("UPDATE embeddings SET meaning_embedding = NULL WHERE bucket_id = ?",
                         [(bid,) for bid in ids])
        conn.commit()
    finally:
        conn.close()


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
    # or list[(bucket_id, content, newest meaning)] — every entry that gets a vector now.
    # It is asked again at the swap, so what was withdrawn or edited meanwhile is seen.
    fetch_buckets: Callable[[], Awaitable[list[tuple]]]


def _meaning_of(item: tuple) -> str:
    return str(item[2]) if len(item) > 2 and item[2] else ""


async def _store_meaning(engine: Any, bucket_id: str, meaning: str) -> str:
    """Compute one meaning vector into the staging database. "" when it is stored, else
    why not (a False from the engine is a failure, said as one)."""
    store_meaning = getattr(engine, "generate_and_store_meaning", None)
    if not callable(store_meaning):
        return ""
    try:
        ok = await store_meaning(bucket_id, meaning)
    except Exception as exc:
        return f"meaning vector: {type(exc).__name__}: {exc}"
    return "" if ok else "meaning vector: generate_and_store_meaning returned False"


async def _run_migration(
    cfg: MigrationConfig,
    on_complete: Callable[[bool], None] | None = None,
) -> None:
    """The coroutine that actually runs the migration."""
    status_path = status_path_for(cfg.buckets_dir)
    ckpt_path = checkpoint_path_for(cfg.buckets_dir)

    # 1) Fetch every bucket
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
    # Resume from the checkpoint (a mismatched target starts over). What failed last time
    # is tried again; what the person chose to skip is left out.
    state = read_checkpoint(ckpt_path, signature)
    done_ids: set[str] = state["done"]
    skipped: set[str] = state["skipped"]
    failed: dict[str, str] = {}
    meanings: dict[str, str] = state["meanings"]
    state["failed"] = failed

    def failed_items() -> list[dict[str, str]]:
        return [{"bucket_id": bid, "error": why}
                for bid, why in list(failed.items())[:MAX_FAILED_ITEMS]]

    write_status(status_path, {
        **_empty_status(),
        "phase": "running",
        "total": total,
        "done": len(done_ids),
        "skipped_count": len(skipped),
        "failed_count": 0,
        "current_id": "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target_backend": cfg.target_backend,
        "target_model": cfg.target_model,
        "target_dim": cfg.target_dim,
        "message": f"开始迁移 {total} 个 bucket（已完成 {len(done_ids)}）",
    })

    # 2) Run in batches
    pending = [item for item in buckets if item[0] not in done_ids and item[0] not in skipped]
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        for item in batch:
            bucket_id, content = item[0], item[1]
            meaning = _meaning_of(item)
            cur = read_status(status_path)
            cur["current_id"] = bucket_id
            write_status(status_path, cur)

            try:
                ok = await cfg.target_engine.generate_and_store(bucket_id, content)
                if not ok:
                    failed[bucket_id] = "generate_and_store returned False"
                    continue
                if meaning:
                    # The meaning vector lives in its own column of the same row; an entry
                    # is done only with both, or a "0 failed" would drop meaning vectors.
                    why = await _store_meaning(cfg.target_engine, bucket_id, meaning)
                    if why:
                        failed[bucket_id] = why
                        continue
                done_ids.add(bucket_id)
                meanings[bucket_id] = _digest(meaning) if meaning else ""
            except Exception as e:
                failed[bucket_id] = f"{type(e).__name__}: {e}"

        # Write the checkpoint and status once per batch
        write_checkpoint(ckpt_path, state, signature)
        cur = read_status(status_path)
        cur["done"] = len(done_ids)
        cur["failed_count"] = len(failed)
        cur["failed_items"] = failed_items()
        cur["message"] = f"已完成 {len(done_ids)} / {total}（失败 {len(failed)}）"
        write_status(status_path, cur)

        # Rate limiting
        if i + BATCH_SIZE < len(pending):
            await asyncio.sleep(BATCH_INTERVAL_SEC)

    # 3) Only a completely successful run swaps atomically into the main store. Throughout
    #    the loop only cfg.target_engine's own staging db is written (the caller points its
    #    db_path at whatever staging_db_path_for() returned when constructing target_engine),
    #    and cfg.db_path is never touched. So any failure or crash at any step leaves the
    #    live db exactly as it was before the migration, with no half-finished state mixing
    #    vectors from two models.
    swap_error = ""
    pruned = 0
    staged_path = getattr(cfg.target_engine, "db_path", "")
    all_done = not failed and all(item[0] in done_ids or item[0] in skipped
                                  for item in buckets)
    if all_done:
        # What the library holds now, not when the run began: an entry withdrawn, deleted
        # or cleared meanwhile does not get its vector back at the swap, and a meaning
        # written meanwhile is computed again before the new vectors go live.
        try:
            now = await cfg.fetch_buckets()
            current = {item[0] for item in now}
            pruned = _prune(staged_path, current)
            stale, cleared = [], []
            for item in now:
                bid, meaning = item[0], _meaning_of(item)
                if bid not in done_ids:
                    continue
                if not meaning and meanings.get(bid):
                    cleared.append(bid)
                elif meaning and meanings.get(bid) != _digest(meaning):
                    stale.append((bid, meaning))
            _clear_meaning(staged_path, cleared)
            for bid in cleared:
                meanings[bid] = ""
            for bid, meaning in stale:
                why = await _store_meaning(cfg.target_engine, bid, meaning)
                if why:
                    failed[bid] = why
                    done_ids.discard(bid)
                else:
                    meanings[bid] = _digest(meaning)
            write_checkpoint(ckpt_path, state, signature)
        except Exception as e:
            swap_error = f"re-reading the library before the swap: {type(e).__name__}: {e}"
        all_done = not failed and not swap_error
    if all_done:
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
        if not swap_error:
            # A withdrawal that cleared the old live database between the re-read and the
            # rename: read the library once more against the database now live.
            try:
                pruned += _prune(cfg.db_path, {item[0] for item in await cfg.fetch_buckets()})
            except Exception as e:
                logger.warning(f"[migration] pruning after the swap failed: {e}")
            drop_backup(cfg.db_path)

    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    success = all_done and not swap_error
    final_phase = "completed" if success else "failed"
    if swap_error:
        final_msg = f"迁移没能换上：{swap_error}"
    else:
        final_msg = f"迁移完成：{len(done_ids)} 成功 / {len(failed)} 失败"
        if skipped:
            final_msg += f" / {len(skipped)} 跳过（这几条换上以后由新模型在后台补算）"
        if failed:
            final_msg += "。失败的那几条在 failed_items 里：接着算会再试，也可以跳过它们"
    tail = []
    if failed or swap_error:
        # On failure, attach the log and a pointer to what to do
        tail = _tail_errors_log(cfg.buckets_dir)

    cur = read_status(status_path)
    cur.update({
        "phase": final_phase,
        "current_id": "",
        "done": len(done_ids),
        "skipped_count": len(skipped),
        "failed_count": len(failed) if not swap_error else max(len(failed), 1),
        "failed_items": failed_items(),
        "pruned": pruned,
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
    "backup_path_for",
    "drop_backup",
    "read_checkpoint",
    "write_checkpoint",
    "MigrationReservation",
    "reserve_migration",
    "owns_migration_reservation",
    "release_migration_reservation",
    "start_migration",
    "is_running",
    "reset_for_test",
    "BATCH_SIZE",
    "BATCH_INTERVAL_SEC",
]
