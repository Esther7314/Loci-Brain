"""
========================================
web/import_api.py — 导入的四条路由（2026-08-19）
========================================

**引擎一直是活的，缺的只有门。**

`core/import_memory.py`（53KB，认 Claude/ChatGPT 的 JSON 导出和 Markdown）从来没被动过，
`server.py` 也一直在建 `ImportEngine` 并注入 `tools/_runtime`。真正没了的是 HTTP 路由 ——
它们住在 E2（2026-08-17）砍掉的那 20 个上游模块里，跟着一起没了。
于是面板上「先看一眼 / 开始导入 / 暂停」三个按钮点下去打的是 404，
返回的是 HTML，前端 .json() 当场炸 —— 屏幕上就是那句「返回的不是 JSON」。

所以这个模块只干一件事：**把门装回去**，一行解析逻辑都不重写。

四条口（路径和请求形状都是**照前端现有代码抄的**，不是我另定的）：
    POST /api/import/preflight   先看一眼 —— 纯读，一条都不写
    POST /api/import/upload      开始导入 —— 单槽，抢不到就 409
    GET  /api/import/status      进度 —— 前端 1.5 秒轮一次
    POST /api/import/pause       暂停 —— 当前这块跑完就停

⚠️ 前端传的是 **multipart/form-data**（FormData 里一个 file 字段），不是 JSON ——
   所以不能用 loci._write_body（那个只收 application/json）。
   同源那道闸照旧走 loci._origin_reject，几个写口共用同一条判据。
⛔ MCP 工具面那九个不加不减。

对外暴露：register(mcp)
========================================
"""

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("loci_brain.web.import")

# 一次能传多大。**必须有个上限**：这两个口把整份文件读进内存，
# 没有闸的话一个几百 MB 的导出就能把容器撑爆 —— 而容器一死，
# 她正在跑的别的东西一起没。50MB 够放很长的对话历史了。
_MAX_UPLOAD = 50 * 1024 * 1024


def _engine():
    """导入引擎：server.py 建好之后注入在 tools/_runtime 上。"""
    from tools import _runtime as rt
    return getattr(rt, "import_engine", None)


async def _take_file(request: Request):
    """从 multipart 里拿那个文件，返回（正文, 文件名）。

    抛 PermissionError = 该回 403；抛 ValueError = 该回 400。
    """
    from .loci import _origin_reject
    why = _origin_reject(request)
    if why:
        raise PermissionError(why)
    try:
        form = await request.form()
    except Exception as e:                          # noqa: BLE001
        raise ValueError("读不出上传的表单：" + str(e))
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise ValueError("没收到文件（表单里要有一个叫 file 的字段）")
    raw = await f.read()
    if len(raw) > _MAX_UPLOAD:
        raise ValueError("文件 " + str(len(raw) // 1024 // 1024) + " MB，超过 "
                         + str(_MAX_UPLOAD // 1024 // 1024) + " MB 的上限")
    if not raw:
        raise ValueError("文件是空的")
    # errors="replace"：导出文件里混进一个坏字节，不该让整份导入失败 ——
    # 那一个字变成 U+FFFD，剩下几万字照样进得来。
    return raw.decode("utf-8", "replace"), str(getattr(f, "filename", "") or "")


def register(mcp) -> None:

    @mcp.custom_route("/api/import/preflight", methods=["POST"])
    async def api_import_preflight(request: Request) -> Response:
        """先看一眼：认出是什么格式、能切成多少块、要打多少次模型。

        **一条都不写。** 走的是 preview_import()，那个函数自己的注释就写着
        「Return a local-only preview without mutating state」。
        """
        from core.import_memory import preview_import
        try:
            raw, name = await _take_file(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        try:
            return JSONResponse(preview_import(raw, name))
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] preflight 失败: " + str(e))
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/import/upload", methods=["POST"])
    async def api_import_upload(request: Request) -> Response:
        """开始导入。**这个会真的往记忆库里写。**

        单槽：reserve_start() 抢不到就说明已经有一份在跑，回 409 ——
        两份同时往里灌，判重和 job 状态都会乱。
        抢到之后**不等它跑完**（一份历史能跑几十分钟），后台跑、前端轮 status。
        """
        eng = _engine()
        if eng is None:
            return JSONResponse({"error": "导入引擎还没起来（服务刚启动？等几秒再试）"},
                                status_code=503)
        try:
            raw, name = await _take_file(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        job_id = eng.reserve_start()
        if job_id is None:
            return JSONResponse(
                {"error": "已经有一份导入在跑了", "job_id": eng.active_job_id},
                status_code=409)

        async def _run():
            try:
                await eng.start(raw, filename=name, reservation_id=job_id)
            except Exception as e:                  # noqa: BLE001
                # 后台任务里抛出去没人接得住，日志是唯一的现场
                logger.error("[import] job " + job_id + " 炸了: "
                             + type(e).__name__ + ": " + str(e))
                try:
                    eng.release_start_reservation(job_id)
                except Exception:                   # noqa: BLE001
                    pass

        asyncio.create_task(_run())
        return JSONResponse({"ok": True, "job_id": job_id, "filename": name,
                             "note": "跑起来了。进度看 /api/import/status。"})

    @mcp.custom_route("/api/import/status", methods=["GET"])
    async def api_import_status(request: Request) -> Response:
        """进度。前端 1.5 秒轮一次，靠 is_running 判断跑完没 ——
        所以这个字段**必须由这儿显式给**（引擎的 to_dict 里没有它）。"""
        eng = _engine()
        if eng is None:
            return JSONResponse({"is_running": False, "status": "engine_not_ready"})
        try:
            st = dict(eng.get_status() or {})
            st["is_running"] = bool(eng.is_running)
            return JSONResponse(st)
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] status 失败: " + str(e))
            return JSONResponse({"error": str(e), "is_running": False}, status_code=500)

    @mcp.custom_route("/api/import/pause", methods=["POST"])
    async def api_import_pause(request: Request) -> Response:
        """暂停：当前这块跑完就停（不是当场砍断——砍断会留下半条记忆）。"""
        from .loci import _origin_reject
        why = _origin_reject(request)
        if why:
            return JSONResponse({"error": why}, status_code=403)
        eng = _engine()
        if eng is None:
            return JSONResponse({"error": "导入引擎还没起来"}, status_code=503)
        try:
            eng.pause()
            return JSONResponse({"ok": True, "note": "这一块跑完就停。"})
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] pause 失败: " + str(e))
            return JSONResponse({"error": str(e)}, status_code=500)
