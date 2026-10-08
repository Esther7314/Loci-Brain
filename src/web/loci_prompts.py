"""
========================================
web/loci_prompts.py — the prompt cards on grow's and dream's 高级设置
========================================

    GET  /api/loci/prompts   -> {"items": [card, ...], "scope": <line>}, one card per
                                rewritable prompt (core/prompts.card)
    POST /api/loci/prompts   -> {key, text}: keep the rewrite; {key, reset: true}: back to
                                the shipped text. {"ok": true, "item": card}

Both are the panel's alone: neither path is a host read or a hook path, so the gate's
panel refusal answers a host's credential (web/__init__ `_Gated`). The POST reads its
body through `_guards._write_body` (same origin, `application/json`). A rewrite that
lost a piece the parser reads is a 400 naming the pieces (core/prompts.missing), and
nothing is written. The rewrite goes into `<buckets>/_state/prompts.json`, not the ledger.
========================================
"""

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from ._guards import _request_of, _write_body
from core import prompts as _prompts
from core import _when as _w

logger = sh.logger


def _scope_line(request: Request) -> str:
    from core import scope as _scope
    req = _request_of(request)
    return req.first_line() if req is not None else _scope.OPEN_LINE


def _base_dir() -> str:
    return str(sh.bucket_mgr.base_dir)


async def api_loci_prompts(request: Request) -> Response:
    try:
        items = _prompts.cards(_base_dir())
    except Exception as e:                       # noqa: BLE001 - one read, said as an error
        logger.warning(f"[loci] prompts 读不出来: {e}")
        return JSONResponse({"error": f"提示词读不出来：{e}"}, status_code=500)
    return JSONResponse({"items": items, "scope": _scope_line(request)})


async def api_loci_prompts_save(request: Request) -> Response:
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    key = str(body.get("key") or "").strip()
    if key not in _prompts.keys():
        return JSONResponse({"error": f"没有这张提示词卡：{key or '（没带 key）'}"
                                      f"（有的是：{'、'.join(_prompts.keys())}）"},
                            status_code=400)
    try:
        if body.get("reset") is True:
            item = _prompts.reset(key, _base_dir())
        elif isinstance(body.get("text"), str):
            item = _prompts.save(key, body["text"], _base_dir(), _w.now())
        else:
            return JSONResponse({"error": "要带 text（改成的提示词），或者 reset: true（恢复默认）"},
                                status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:                       # noqa: BLE001 - one write, said as an error
        logger.warning(f"[loci] prompts 存不进去 {key}: {e}")
        return JSONResponse({"error": f"提示词存不进去：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "item": item})
