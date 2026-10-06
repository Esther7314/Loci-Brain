# -*- coding: utf-8 -*-
"""
========================================
core/embedding_switch.py — changing the embedding model: every vector again, never mixed
========================================

A vector only means something next to vectors from the same model. An engine rebuilt on a
new model over the old vectors would compare new query vectors with old memory vectors and
return noise without a word, so the model is never switched that way.

Now a change of model (a different model name than the one the library's vectors carry,
with vectors present) goes like this:

  1. ask    `POST /api/config` with the new model and no confirmation is refused (409)
            with `preview()`: how many vectors are now invalid and will be recomputed, an
            estimated time (one real call to the new model, timed, times the count, plus
            the pacing between batches), and the similarity lines that were tuned on the
            old model and may need retuning — read from the code that uses them.
  2. start  the same request with `embedding.reembed: "confirm"` starts the recompute
            (`start`, core/reembed.py): the new model's vectors are written to a
            staging database beside the live one; the live one is not touched.
  3. finish when every entry has its new vector, the staging database replaces the live
            one in one rename, and only then is the new model published to the running
            engine and (when the request asked to persist) written to config.yaml. What
            was written or edited during the recompute is queued for the new model by the
            vector outbox's reconcile (a missing vector or a content hash that moved).

Until the swap the old model stays the live one: searches use old query vectors against
old memory vectors, writes are embedded by the old model into the live database. Nothing
is blocked and nothing is mixed. A failure leaves the live library exactly as it was; the
staging database and the checkpoint stay, so `resume` carries on from the last batch,
`skip` leaves the entries that keep failing for the new model to compute after the swap
and carries on, and `abandon` throws the attempt away.

The recompute's progress and failure are the migration engine's status file, read by
`status()` for `GET /api/loci/embedding/migration`; what was asked for (the target model,
whether to persist) is kept beside it (`_embedding_switch.json`, no keys in it), so an
attempt interrupted by a restart can be resumed with the same target.

Exports: thresholds · vectors_in · target_config · needs_reembed · resolved_model · busy ·
         preview · start · resume · skip · abandon ·
         status · SwitchBusy · ENGINE_FACTORY
========================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Optional

from . import reembed as ME

logger = logging.getLogger("loci_brain.embedding_switch")

TARGET_FILE = "_embedding_switch.json"
PROBE_TIMEOUT_SECONDS = 20.0
# The settings that say which model a vector came from; the rest (timeouts) do not move
# the vector space. A key is never written to the target file.
_TARGET_KEYS = ("enabled", "model", "base_url", "api_format", "backend", "dim",
                "timeout_seconds")


def _engine_factory(config: dict):
    from .embedding_engine import EmbeddingEngine
    return EmbeddingEngine(config)


# Builds an embedding engine from a whole config. Tests put a fake engine here.
ENGINE_FACTORY: Callable[[dict], Any] = _engine_factory


class SwitchBusy(RuntimeError):
    """A recompute is already running."""


def busy() -> bool:
    """Is a recompute running in this process?"""
    return ME.is_running()


def _norm(name: str) -> str:
    from .embedding_engine import _norm_model
    return _norm_model(str(name or ""))


# ============================================================
# The similarity lines tuned on the current model
# ============================================================

def thresholds() -> list[dict]:
    """Each line, its value read from the code that uses it, and what it decides."""
    from . import _reconsolidation, _slicer
    from . import bucket_manager
    from tools import fold
    from tools.grow import _backfill
    from tools.recall import core as recall_core
    rows = [
        (_reconsolidation.SIMILARITY_LINE, "余弦", "core/_reconsolidation.SIMILARITY_LINE",
         "回望：新写的东西跟哪条旧看法算「撞意思」"),
        (fold._MERGE_COS_THRESHOLD, "余弦", "tools/fold._MERGE_COS_THRESHOLD",
         "两条概括说的是不是一回事、要不要问合并"),
        (_backfill._DUP_COS_THRESHOLD, "余弦", "tools/grow/_backfill._DUP_COS_THRESHOLD",
         "回填时「可能是同一件事」的提示"),
        (bucket_manager._VECTOR_RECALL_THRESHOLD, "余弦",
         "core/bucket_manager._VECTOR_RECALL_THRESHOLD", "recall 里「意思」那个标记"),
        (_slicer.DEFAULT_GUESS_THRESHOLD, "余弦", "core/_slicer.DEFAULT_GUESS_THRESHOLD",
         "切片「白天像是已经记过哪条」的猜测"),
        (recall_core.RELEVANCE_FLOOR, "综合分（0–100）", "tools/recall/core.RELEVANCE_FLOOR",
         "recall 的相关线：低于它的不摆出来（语义占 2.5 份）"),
    ]
    return [{"value": v, "scale": scale, "where": where, "decides": what}
            for v, scale, where, what in rows]



# ============================================================
# What the library's vectors are
# ============================================================

def vectors_in(db_path: str) -> dict:
    """{count, model, dim} of the content vectors in a vector database (0 / "" when it
    has none or cannot be read)."""
    out = {"count": 0, "model": "", "dim": 0}
    if not db_path or not os.path.isfile(db_path):
        return out
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "embeddings" in tables:
            out["count"] = int(con.execute(
                "SELECT COUNT(*) FROM embeddings WHERE TRIM(embedding) <> ''").fetchone()[0])
        if "embeddings_meta" in tables:
            meta = dict(con.execute("SELECT key, value FROM embeddings_meta").fetchall())
            out["model"] = str(meta.get("model_name") or "")
            try:
                out["dim"] = int(meta.get("vector_dim") or 0)
            except ValueError:
                out["dim"] = 0
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def needs_reembed(db_path: str, target_model: str, live_model: str = "") -> bool:
    """Does serving `target_model` invalidate the vectors the library holds? Yes when it
    holds some and they came from another model (the database's own record of its model;
    the live engine's model for an older database that never recorded one)."""
    have = vectors_in(db_path)
    if not have["count"] or not str(target_model or "").strip():
        return False
    source = have["model"] or live_model
    return bool(source) and _norm(source) != _norm(target_model)


def target_config(current: dict, changes: dict) -> dict:
    """The embedding section the change asks for: the current one with the request's
    fields over it."""
    target = dict(current or {})
    for key in (*_TARGET_KEYS, "api_key"):
        if key in changes and (key != "api_key" or changes[key]):
            target[key] = changes[key]
    return target


def _target_engine(config: dict, target: dict, db_path: str):
    return ENGINE_FACTORY({**config, "embedding": {**target, "db_path": db_path}})


def resolved_model(config: dict, target: dict) -> str:
    """The model an engine built from `target` would serve (the engine fills in its own
    default when no model is named). No call to the model is made. "" when the target is
    switched off."""
    from utils import parse_bool
    if not parse_bool(target.get("enabled", True), default=True):
        return ""
    scratch = tempfile.mkdtemp(prefix="loci-embed-name-")
    try:
        engine = _target_engine(config, target, os.path.join(scratch, "name.db"))
        return str(getattr(engine, "model", "") or target.get("model") or "")
    except Exception:                                # noqa: BLE001 - a name only
        return str(target.get("model") or "")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _probe(engine) -> tuple[list, float]:
    """One real vector from the engine, and how long it took. Raises on failure."""
    if not getattr(engine, "enabled", False):
        raise RuntimeError("新模型没能启用（多半是缺 key，或者模型名不对）")
    started = time.monotonic()
    vector = await asyncio.wait_for(engine._generate_async("向量模型试一下"),
                                    timeout=PROBE_TIMEOUT_SECONDS)
    elapsed = time.monotonic() - started
    if not vector:
        raise RuntimeError("新模型回了一个空向量")
    return list(vector), elapsed


def _estimate(count: int, seconds_per_vector: float) -> int:
    batches = math.ceil(count / ME.BATCH_SIZE) if count else 0
    return int(round(count * seconds_per_vector + max(0, batches - 1) * ME.BATCH_INTERVAL_SEC))


async def preview(config: dict, target: dict, db_path: str, live_model: str) -> dict:
    """What switching to `target` means, for the person to read before confirming. Calls
    the new model once (to know it works, its vector size, and how long a vector takes)."""
    have = vectors_in(db_path)
    scratch = tempfile.mkdtemp(prefix="loci-embed-probe-")
    try:
        engine = _target_engine(config, target, os.path.join(scratch, "probe.db"))
        model = str(getattr(engine, "model", "") or target.get("model") or "")
        probe_error = ""
        dim, per_vector = 0, 0.0
        try:
            vector, per_vector = await _probe(engine)
            dim = len(vector)
        except Exception as exc:                     # noqa: BLE001 - shown to the person
            probe_error = f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    seconds = _estimate(have["count"], per_vector) if not probe_error else None
    return {
        "needs_confirmation": True,
        "vectors": have["count"],
        "from_model": have["model"] or live_model,
        "from_dim": have["dim"],
        "to_model": model,
        "to_dim": dim,
        "seconds_per_vector": round(per_vector, 3) if not probe_error else None,
        "estimated_seconds": seconds,
        "probe_error": probe_error,
        "thresholds": thresholds(),
        "while_running": "重算期间旧模型照常用：搜索拿旧向量比旧向量，新写的也先用旧模型算；"
                         "新向量写在暂存库里，全部算完才一次换上——不会新旧混着比。",
        "say": _say(have["count"], have["model"] or live_model, model, seconds,
                    probe_error),
    }


def _say(count: int, old: str, new: str, seconds: Optional[int], probe_error: str) -> str:
    """The confirmation text the panel puts in front of the person."""
    lines = [f"换向量模型：{old or '（没记下是哪个）'} → {new}",
             f"库里现有的 {count} 条向量全部作废，要用新模型重算一遍。"]
    if probe_error:
        lines.append(f"新模型刚才试了一次没通：{probe_error}。通了再换。")
    elif seconds is not None:
        lines.append(f"估计要 {_duration(seconds)}（按刚才试的一次算；"
                     "模型热了以后通常更快）。")
    lines.append("这几条线是按旧模型调的，换了以后可能要重调："
                 + "；".join(f"{t['value']:g}（{t['decides']}）" for t in thresholds()))
    lines.append("算完之前旧模型照常用，不会新旧混着比；中途失败旧库原样不动，可以接着算。")
    return "\n".join(lines)


def _duration(seconds: int) -> str:
    if seconds < 90:
        return f"约 {max(1, seconds)} 秒"
    if seconds < 5400:
        return f"约 {round(seconds / 60)} 分钟"
    return f"约 {seconds / 3600:.1f} 小时"


# ============================================================
# The recompute
# ============================================================

def _target_path(buckets_dir: str) -> str:
    return os.path.join(os.path.dirname(ME.status_path_for(buckets_dir)), TARGET_FILE)


def _read_target(buckets_dir: str) -> Optional[dict]:
    try:
        with open(_target_path(buckets_dir), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_target(buckets_dir: str, data: dict) -> None:
    path = _target_path(buckets_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _drop_target(buckets_dir: str) -> None:
    try:
        os.remove(_target_path(buckets_dir))
    except OSError:
        pass


async def _entries(store) -> list[tuple[str, str, str]]:
    """(id, body, newest meaning) of every entry that gets a vector: not soft-deleted,
    with a body (the vector outbox's own rule), and not withdrawn — an entry standing on a
    withdrawn or deleted source has had its vectors removed and its body cleared, and a
    recompute must not give it a vector back (core/export_package.withdrawn)."""
    from .export_package import withdrawn
    registry = getattr(store, "sources", None)
    cleared = str(getattr(store, "CLEARED_BODY", "") or "")
    out = []
    for b in await store.list_all(include_archive=True):
        meta = b.get("metadata") or {}
        content = str(b.get("content") or "")
        if not content.strip() or meta.get("deleted_at"):
            continue
        if (cleared and content.strip() == cleared) or withdrawn(meta, registry):
            continue
        meaning = meta.get("meaning") or []
        if isinstance(meaning, str):
            meaning = [meaning]
        out.append((str(b.get("id")), content, str(meaning[-1]) if meaning else ""))
    return out


async def start(*, config: dict, store, db_path: str, target: dict, persist: bool,
                publish: Callable[[dict, bool], None],
                loop: Optional[asyncio.AbstractEventLoop] = None) -> dict:
    """Start recomputing every vector with `target`. `publish(target, persist)` is called
    once, after the staging database has replaced the live one, to make the new model the
    running one (and write it to config.yaml when `persist`). Raises SwitchBusy while
    another recompute runs, RuntimeError when the new model does not answer."""
    buckets_dir = str(config.get("buckets_dir") or store.base_dir)
    reservation = ME.reserve_migration()
    if reservation is None:
        raise SwitchBusy("正在重算向量，等它跑完，或者先放弃这一次")
    try:
        scratch = tempfile.mkdtemp(prefix="loci-embed-probe-")
        try:
            probe_engine = _target_engine(config, target, os.path.join(scratch, "probe.db"))
            vector, _elapsed = await _probe(probe_engine)
            model = str(getattr(probe_engine, "model", "") or target.get("model") or "")
            backend = str(getattr(probe_engine, "api_format", "") or
                          target.get("api_format") or "api")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        dim = len(vector)
        signature = ME.target_signature(backend, model, dim)
        ME.reset_stale_migration_state(buckets_dir, db_path, signature)
        staging = ME.staging_db_path_for(db_path)
        engine = _target_engine(config, {**target, "dim": dim}, staging)
        _write_target(buckets_dir, {
            "target": {k: target[k] for k in _TARGET_KEYS if k in target},
            "model": model, "backend": backend, "dim": dim, "persist": bool(persist),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")})

        async def fetch():
            return await _entries(store)

        cfg = ME.MigrationConfig(buckets_dir=buckets_dir, db_path=db_path,
                                 target_backend=backend, target_model=model,
                                 target_dim=dim, target_engine=engine,
                                 fetch_buckets=fetch)

        def on_complete(success: bool) -> None:
            if not success:
                return
            try:
                publish({**target, "dim": dim}, bool(persist))
            except Exception as exc:                 # noqa: BLE001 - reported in status
                logger.error("[embedding-switch] publishing the new model failed: %s", exc)
                st = ME.read_status(ME.status_path_for(buckets_dir))
                st["message"] = (f"向量都换好了，但新模型没能接上运行中的服务：{exc}。"
                                 "重启一次服务就会按新模型起来。")
                ME.write_status(ME.status_path_for(buckets_dir), st)
            _drop_target(buckets_dir)

        task = ME.start_migration(cfg, loop or asyncio.get_running_loop(), on_complete,
                                  reservation=reservation)
    except BaseException:
        ME.release_migration_reservation(reservation)
        raise
    if task is None:
        raise SwitchBusy("正在重算向量，等它跑完，或者先放弃这一次")
    return status(buckets_dir)


async def resume(*, config: dict, store, db_path: str,
                 publish: Callable[[dict, bool], None]) -> dict:
    """Carry on an interrupted or failed recompute with the target it was started with.
    The key comes from the running configuration (it is never written down)."""
    buckets_dir = str(config.get("buckets_dir") or store.base_dir)
    saved = _read_target(buckets_dir)
    if not saved:
        raise RuntimeError("没有可以接着算的那一次（没有记下的目标模型）")
    current = dict(config.get("embedding") or {})
    target = {**current, **saved.get("target", {})}
    if current.get("api_key") and "api_key" not in saved.get("target", {}):
        target["api_key"] = current["api_key"]
    return await start(config=config, store=store, db_path=db_path, target=target,
                       persist=bool(saved.get("persist")), publish=publish)


async def abandon(buckets_dir: str, db_path: str) -> dict:
    """Throw the attempt away: stop it if it runs, remove the staging database and the
    checkpoint. The live vectors and the live model are as they were."""
    task = ME._migration_task
    if ME.is_running() and task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:                        # noqa: BLE001 - cancelled on purpose
            pass
    for path in (ME.checkpoint_path_for(buckets_dir), ME.staging_db_path_for(db_path),
                 ME.backup_path_for(db_path)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("[embedding-switch] could not remove %s: %s", path, exc)
    _drop_target(buckets_dir)
    ME.write_status(ME.status_path_for(buckets_dir), {
        **ME._empty_status(), "phase": "idle",
        "message": "这次换模型放弃了：旧模型和旧向量照用。"})
    return status(buckets_dir)


async def skip(*, config: dict, store, db_path: str, publish: Callable[[dict, bool], None],
               ids: Optional[list[str]] = None) -> dict:
    """Leave the entries that keep failing without a new vector and carry on: `ids`, or
    every entry the last run failed on. They are kept in the checkpoint as skipped; after
    the swap the vector outbox finds them without a vector and the new model computes them
    in the background, so one entry the model cannot take never holds the switch back."""
    buckets_dir = str(config.get("buckets_dir") or store.base_dir)
    if ME.is_running():
        raise SwitchBusy("正在重算向量，等这一轮停下来再跳过")
    saved = _read_target(buckets_dir)
    if not saved:
        raise RuntimeError("没有可以接着算的那一次（没有记下的目标模型）")
    signature = ME.target_signature(str(saved.get("backend") or ""),
                                    str(saved.get("model") or ""), int(saved.get("dim") or 0))
    path = ME.checkpoint_path_for(buckets_dir)
    state = ME.read_checkpoint(path, signature)
    chosen = [str(i) for i in ids] if ids else list(state["failed"])
    if not chosen:
        raise RuntimeError("上一轮没有失败的条目可以跳过")
    state["skipped"] |= set(chosen)
    for bid in chosen:
        state["failed"].pop(bid, None)
    ME.write_checkpoint(path, state, signature)
    return await resume(config=config, store=store, db_path=db_path, publish=publish)


def status(buckets_dir: str, live_model: str = "") -> dict:
    """Progress and failure of the recompute, for the panel. `phase` is idle, running,
    completed, failed, or interrupted (the status file says running but no task is: the
    process stopped mid-way); failed and interrupted can be resumed."""
    st = ME.read_status(ME.status_path_for(buckets_dir))
    running = ME.is_running()
    phase = str(st.get("phase") or "idle")
    if phase == "running" and not running:
        phase = "interrupted"
    saved = _read_target(buckets_dir)
    return {
        **st,
        "phase": phase,
        "running": running,
        "resumable": phase in ("failed", "interrupted") and saved is not None,
        # Entries that failed can be skipped (`skip`) so the rest goes live.
        "skippable": (phase in ("failed", "interrupted") and saved is not None
                      and bool(st.get("failed_count")) and not st.get("error")),
        "target": ({"model": saved.get("model"), "dim": saved.get("dim"),
                    "base_url": (saved.get("target") or {}).get("base_url", ""),
                    "persist": saved.get("persist")} if saved else None),
        "live_model": live_model,
    }
