"""
========================================
web/library_api.py — export, bringing a package back, and the embedding recompute
========================================

The handlers behind six panel routes, registered in web/loci.py (whose header is the one
list of what the panel can reach):

    GET  /api/loci/export               the export package as a download
                                        (core/export_package.py)
    GET  /api/loci/export/originals     「导出原话」 as a download: imported conversations,
                                        one file each, and sunk originals, one file per
                                        day (core/export_originals.py)
    POST /api/loci/import-package       multipart `file`: check and parse a package, write
                                        nothing, answer with its collisions;
                                        JSON {job_id, decisions, default}: write it in the
                                        background (core/package_import.py);
                                        JSON {job_id, cancel: true}: drop the parse
    GET  /api/loci/import-package       where that stands
    GET  /api/loci/embedding/migration  the recompute after a change of embedding model
                                        (core/embedding_switch.py)
    POST /api/loci/embedding/migration  {action: resume | skip | abandon}; skip leaves
                                        `ids` (default: what the last run failed on)
                                        for the new model to compute after the swap

All of them sit behind the panel gate; the two POSTs also take only same-origin requests
(web/loci._origin_reject), like every panel write, and so do the two exports
(server_app.OriginCSRFGuardMiddleware._GUARDED_READS): each hands out the library's words.

Exports: export · export_originals · import_package · import_status · reembed_status ·
         reembed_action
========================================
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import _shared as sh

logger = sh.logger

_DECISIONS = ("skip", "overwrite", "keep_both")
_COPY_CHUNK = 1024 * 1024
# Background applies are kept here until they finish, so the task is not collected.
_running: set = set()


def _embedding_db_path() -> str:
    engine = sh.embedding_engine
    return str(getattr(engine, "db_path", "") or os.path.join(
        str(sh.config.get("buckets_dir") or ""), "embeddings.db"))


def _export_meta() -> dict:
    engine = sh.embedding_engine
    backend = getattr(engine, "_backend", None)
    try:
        dim = int(backend.vector_dim()) if backend is not None else 0
    except Exception:                                # noqa: BLE001 - a label only
        dim = 0
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": str(getattr(sh, "version", "") or ""),
        "embedding": {"model": str(getattr(engine, "model", "") or ""), "dim": dim,
                      "backend": str(getattr(engine, "api_format", "") or "")},
    }


async def export(request: Request) -> Response:
    from core import export_package
    from core.backup_archive import BackupArchiveError

    try:
        path, manifest = await export_package.build_package(
            sh.bucket_mgr, embedding_db_path=_embedding_db_path(),
            export_meta=_export_meta())
    except BackupArchiveError as e:
        return JSONResponse({"error": f"导出包没能打完整：{e}"}, status_code=500)
    except Exception as e:                           # noqa: BLE001 - said, not swallowed
        logger.error("[export] failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"导出失败：{type(e).__name__}: {e}"}, status_code=500)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    package = manifest.get("package") or {}
    headers = {
        "X-Loci-Entries": str(package.get("entries", 0)),
        "X-Loci-Filtered": str(sum(len(v) for v in (package.get("filtered") or {}).values())),
    }
    return FileResponse(path, media_type="application/zip",
                        filename=f"loci-export-{stamp}.zip", headers=headers,
                        background=BackgroundTask(_unlink, path))


async def export_originals(request: Request) -> Response:
    from core import export_originals as eo

    try:
        path, counts = await eo.build(sh.bucket_mgr)
    except Exception as e:                           # noqa: BLE001 - said, not swallowed
        logger.error("[export-originals] failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"导出原话失败：{type(e).__name__}: {e}"},
                            status_code=500)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    headers = {"X-Loci-Conversations": str(counts["conversations"]),
               "X-Loci-Sunk": str(counts["sunk"]),
               "X-Loci-Withheld": str(counts["lines_withheld"] + counts["sunk_left_out"])}
    return FileResponse(path, media_type="application/zip",
                        filename=f"loci-originals-{stamp}.zip", headers=headers,
                        background=BackgroundTask(_unlink, path))


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _upload_to_disk(upload, limit: int) -> str:
    fd, path = tempfile.mkstemp(prefix="loci-package-", suffix=".zip")
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(_COPY_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise ValueError(f"包超过 {limit // 1024 // 1024} MB 的上限")
                out.write(chunk)
        if not written:
            raise ValueError("文件是空的")
        return path
    except BaseException:
        _unlink(path)
        raise


async def import_package(request: Request) -> Response:
    from core.backup_archive import MAX_ARCHIVE_BYTES

    from .loci import _origin_reject, _write_body

    engine = sh.migrate_engine
    if engine is None:
        return JSONResponse({"error": "导入引擎没起来"}, status_code=503)
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        why = _origin_reject(request)
        if why:
            return JSONResponse({"error": why}, status_code=403)
        job = engine.reserve_parse()
        if job is None:
            return JSONResponse({"error": f"正在{engine.phase}，等它完了再传"}, status_code=409)
        path = ""
        try:
            # One file and a few fields at most: the body as a whole is bounded by the
            # upload ceiling (bridge/request_limits.UPLOAD_CEILINGS) while it streams.
            form = await request.form(max_files=1, max_fields=8)
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise ValueError("没收到文件（表单里要有一个叫 file 的字段）")
            path = await _upload_to_disk(upload, MAX_ARCHIVE_BYTES)
        except Exception as e:                       # noqa: BLE001 - answered as a 400
            engine.abandon_parse(job, f"上传失败：{e}")
            if path:
                _unlink(path)
            return JSONResponse({"error": f"上传失败：{e}"}, status_code=400)
        try:
            out = await engine.parse_zip_file(path, reservation_id=job)
        finally:
            _unlink(path)
        if not out.get("ok"):
            return JSONResponse(out, status_code=409 if out.get("busy") else 400)
        return JSONResponse(out)

    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    job_id = str(body.get("job_id") or "")
    if body.get("cancel"):
        if not engine.abandon_parsed(job_id, "导入取消了，什么都没写"):
            return JSONResponse({"error": "没有这一次解析好的包可以取消"}, status_code=409)
        return JSONResponse(engine.get_status())
    default = str(body.get("default") or "skip")
    raw = body.get("decisions") or {}
    if default not in _DECISIONS or not isinstance(raw, dict) or any(
            str(v) not in _DECISIONS for v in raw.values()):
        return JSONResponse({"error": "撞号的处理只有三种：skip / overwrite / keep_both"},
                            status_code=400)
    status = engine.get_status()
    decisions = {c["bucket_id"]: default for c in status.get("conflicts") or []}
    decisions.update({str(k): str(v) for k, v in raw.items()})
    reservation = engine.reserve_apply(job_id)
    if reservation is None:
        return JSONResponse({"error": "没有这一次解析好的包，或者已经在写了"}, status_code=409)
    try:
        task = asyncio.get_running_loop().create_task(
            engine.apply(decisions, reservation_id=reservation))
    except Exception as e:                           # noqa: BLE001
        engine.abandon_apply(reservation, f"没能开始写：{e}")
        return JSONResponse({"error": f"没能开始写：{e}"}, status_code=500)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return JSONResponse(engine.get_status(), status_code=202)


async def import_status(request: Request) -> Response:
    engine = sh.migrate_engine
    if engine is None:
        return JSONResponse({"error": "导入引擎没起来"}, status_code=503)
    return JSONResponse(engine.get_status())


def _buckets_dir() -> str:
    return str(sh.config.get("buckets_dir") or "")


async def reembed_status(request: Request) -> Response:
    from core import embedding_switch as es
    if not _buckets_dir():
        return JSONResponse({"error": "没有记忆库目录"}, status_code=500)
    live = str(getattr(sh.embedding_engine, "model", "") or "")
    return JSONResponse(es.status(_buckets_dir(), live))


async def reembed_action(request: Request) -> Response:
    from core import embedding_switch as es

    from .config_api import publish_embedding
    from .loci import _write_body

    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    action = str(body.get("action") or "")
    try:
        if action == "resume":
            out = await es.resume(config=sh.config, store=sh.bucket_mgr,
                                  db_path=_embedding_db_path(), publish=publish_embedding)
        elif action == "skip":
            ids = body.get("ids")
            if ids is not None and not (isinstance(ids, list)
                                        and all(isinstance(i, str) for i in ids)):
                return JSONResponse({"error": "ids 是记忆 id 的列表（不给就跳过上一轮失败的全部）"},
                                    status_code=400)
            out = await es.skip(config=sh.config, store=sh.bucket_mgr,
                                db_path=_embedding_db_path(), publish=publish_embedding,
                                ids=ids or None)
        elif action == "abandon":
            out = await es.abandon(_buckets_dir(), _embedding_db_path())
        else:
            return JSONResponse({"error": "action 只有 resume / skip / abandon"},
                                status_code=400)
    except es.SwitchBusy as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:                           # noqa: BLE001 - said to the person
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return JSONResponse(out)
