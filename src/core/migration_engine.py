"""
========================================
migration_engine.py — embedding 迁移引擎（2.0.3 新增）
========================================

切 embedding 后端（local ↔ api）时，需要把 embeddings.db 里所有 bucket 的向量
用新后端重算一遍。这个模块负责后台跑这件事：

- 备份 embeddings.db → embeddings.db.backup（只在第一次启动时）
- 把新向量先写入 embeddings.db.migrating，避免半截状态污染主表
- 全部跑完后 atomically swap：主 db 替成 .migrating 文件
- 单条失败跳过 + 记录到 failed_items[:50]，不中断整体
- 进度文件 _pending_migration_status.json，前端 3s 轮询
- 断点续传：_migration_checkpoint.json 记录已完成 id 集合
- 限速：每批 10 条，间隔 0.5s（避免本地推理打爆 CPU 或 API 限流）
- 失败时附最近 15 行 errors.jsonl，提示她/他「这是本地环境相关问题」

不做：
- 不做 bucket 迁移、桶文件重写
- 不切换 global embedding_engine —— 那是 server.py 调用方的事
- 不做配置写盘
- 不做"导入别的 OB 实例导出的完整备份包"——那是 migrate_engine.py 的事。
  两个文件名高度相似，改代码前务必确认自己改的是哪一个。
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


# ---- 常量 ----

_STATUS_FILE_NAME = "_pending_migration_status.json"
_CHECKPOINT_FILE_NAME = "_migration_checkpoint.json"

# 每批 10 条，间隔 0.5s
BATCH_SIZE = 10
BATCH_INTERVAL_SEC = 0.5

# failed_items 上限（避免 status JSON 无限膨胀）
MAX_FAILED_ITEMS = 50

# 失败时附带的 errors.jsonl 末尾行数
TAIL_LOG_LINES = 15

# 进程级锁：同一时刻只允许一个迁移任务
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
# 路径与状态
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
    """迁移目标的唯一签名：断点续传只在「跟上次同一个目标」时才生效。

    checkpoint 原来只存 done_ids，不记目标是谁——先迁到 backend A 失败一半，
    再改迁到 backend B，会把 A 模型的 done_ids 当成 B 已完成，连带复用同一份
    staging db 里 A 的向量，直接原子替换进主库。签名不一致就必须整个重来。
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
    """迁移过程中间向量只写这个文件，绝不碰 live db，直到全部成功才原子替换。"""
    return f"{db_path}.migrating"


def reset_stale_migration_state(buckets_dir: str, db_path: str, signature: str) -> None:
    """启动新一轮迁移前调用：checkpoint 目标签名对不上就整个清掉。

    必须在调用方构造 target_engine（从而在 staging db 路径上跑
    ``_init_db()``）**之前**调用，否则会在一份带着上一个目标模型向量的
    staging db 上继续写，签名检查形同虚设。
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
    """读 errors.jsonl 末尾 n 行。失败返回空列表。"""
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
# 备份与提交
# ============================================================

def backup_db_once(db_path: str) -> str:
    """如果 .backup 不存在则备份 db_path，返回备份文件路径。

    已存在 .backup 则不重复备份（避免覆盖更早版本）。
    """
    backup = db_path + ".backup"
    if os.path.exists(backup):
        return backup
    if not os.path.exists(db_path):
        return backup
    shutil.copy2(db_path, backup)
    return backup


# ============================================================
# 迁移核心
# ============================================================

@dataclass
class MigrationConfig:
    """迁移参数。"""
    buckets_dir: str
    db_path: str
    target_backend: str          # 'local' | 'api'
    target_model: str
    target_dim: int
    # source/target engine 都已由调用方实例化好
    target_engine: Any           # EmbeddingEngine 实例（迁移目标）
    # bucket 内容来源：返回 list[(bucket_id, content)] 的 awaitable
    fetch_buckets: Callable[[], Awaitable[list[tuple[str, str]]]]


async def _run_migration(
    cfg: MigrationConfig,
    on_complete: Callable[[bool], None] | None = None,
) -> None:
    """实际跑迁移的协程。"""
    status_path = status_path_for(cfg.buckets_dir)
    ckpt_path = checkpoint_path_for(cfg.buckets_dir)

    # 1) 备份原 db
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

    # 2) 拉所有 bucket
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
    done_ids = _read_checkpoint(ckpt_path, signature)  # 断点续传（目标不一致则整个重来）
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

    # 3) 分批跑
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

        # 每批写一次 checkpoint + status
        _write_checkpoint(ckpt_path, done_ids, signature)
        cur = read_status(status_path)
        cur["done"] = len(done_ids)
        cur["failed_count"] = failed_count
        cur["failed_items"] = failed_items
        cur["message"] = f"已完成 {len(done_ids)} / {total}（失败 {failed_count}）"
        write_status(status_path, cur)

        # 限速
        if i + BATCH_SIZE < len(pending):
            await asyncio.sleep(BATCH_INTERVAL_SEC)

    # 4) 全部成功才原子替换进主库——docstring 承诺的「先写 .migrating，全部跑完
    #    再原子 swap」真正落地点。循环全程只写 cfg.target_engine 自己的 staging
    #    db（由调用方在构造 target_engine 时把 db_path 指到 staging_db_path_for()
    #    返回的路径），从未碰过 cfg.db_path，所以任何一步失败/崩溃，live db
    #    都还是迁移前的样子，不会出现新旧模型向量混杂的半截状态。
    all_done = failed_count == 0 and len(done_ids) >= total
    swap_error = ""
    if all_done:
        staged_path = getattr(cfg.target_engine, "db_path", "")
        if staged_path and os.path.abspath(staged_path) != os.path.abspath(cfg.db_path):
            try:
                os.replace(staged_path, cfg.db_path)
                # 后续任何用这个 target_engine 发起的操作都必须落在刚替换好的
                # live 路径——继续指着已经被 rename 走的旧 staging 路径，下一次
                # sqlite3.connect() 会在那里悄悄建一个空库，看起来"正常"实则
                # 全部向量重新归零。
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
        # 失败时附 log + 引导提示
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

    # 成功后把 embeddings_meta 更新为目标后端的 model/dim，
    # 否则 db_meta 还是旧值（如 gemini/768），重启会误报 OB-W005 维度不一致。
    if success:
        try:
            cfg.target_engine._write_meta("model_name", cfg.target_model or "")
            cfg.target_engine._write_meta("vector_dim", str(cfg.target_dim or 0))
        except Exception as e:
            logger.warning(f"[migration] update meta failed: {e}")

    # 完成后清掉 checkpoint（下次切换从头开始）——只有真正 swap 成功才清，
    # swap 失败时必须留着，好让下次重试从断点续传，而不是把 staging db 里
    # 已经算完的向量再重算一遍。
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
    """在指定 event loop 上启动后台迁移任务。

    同一时刻只允许一个迁移任务，重复调用返回 None。
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
    """测试用：强制释放锁。"""
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
