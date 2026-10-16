"""
========================================
web/loci_periods.py — the regrow/fold page's 日子 / 想法 column
========================================

    GET  /api/loci/periods            -> {periods, thoughts} (core/periods.overview)
    GET  /api/loci/periods/{id}       -> one period or thought with its members
                                         (core/periods.detail); 404 when it is neither
    POST /api/loci/periods/edit       -> {id, name?, when?}: a period's name, its span
    POST /api/loci/periods/delete     -> {id}: a period put away, its members untouched
    POST /api/loci/periods/merge      -> {keep, drop, name?}: one span over both
    POST /api/loci/periods/day        -> {id, day}: a memory's day (a period's ➕ / ➖), or
                                         a thought's ("" clears it)
    POST /api/loci/periods/cover      -> {id, add?: [ids], remove?: [ids]}: a merged
                                         thought's roster

The writes go through `_write_body` (same origin, JSON) and through trace where trace
does it; each leaves a line the model hears on his next tool call
(core/_panel_changes.py). A refusal is 409 with the words to show.
========================================
"""

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from ._guards import _write_body

logger = sh.logger


async def _listing() -> list:
    return await sh.bucket_mgr.list_all(include_archive=False)


async def api_loci_periods(request: Request) -> Response:
    from core import periods as _P
    try:
        return JSONResponse(_P.overview(await _listing()))
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] periods 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_period(request: Request) -> Response:
    from core import periods as _P
    bid = str(request.path_params.get("bucket_id") or "").strip()
    try:
        out = _P.detail(await _listing(), bid)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] periods/{bid} 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    if out is None:
        return JSONResponse({"error": f"不是日子也不是想法，或者已经收起来了：{bid}"},
                            status_code=404)
    return JSONResponse(out)


def _ids(raw) -> list:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


async def _write(request: Request, act) -> Response:
    """Read the body through the panel's write guard, run `act(body)`, answer."""
    from core import periods as _P
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        return JSONResponse(await act(body))
    except _P.PeriodError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] periods write 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_period_edit(request: Request) -> Response:
    from core import periods as _P
    return await _write(request, lambda b: _P.edit_period(
        str(b.get("id") or ""), name=b.get("name"), when=b.get("when")))


async def api_loci_period_delete(request: Request) -> Response:
    from core import periods as _P
    return await _write(request, lambda b: _P.delete_period(str(b.get("id") or "")))


async def api_loci_period_merge(request: Request) -> Response:
    from core import periods as _P
    return await _write(request, lambda b: _P.merge_periods(
        str(b.get("keep") or ""), str(b.get("drop") or ""), name=b.get("name")))


async def api_loci_period_day(request: Request) -> Response:
    from core import periods as _P
    return await _write(request, lambda b: _P.set_day(
        str(b.get("id") or ""), str(b.get("day") or "")))


async def api_loci_period_cover(request: Request) -> Response:
    from core import periods as _P
    return await _write(request, lambda b: _P.change_cover(
        str(b.get("id") or ""), add=_ids(b.get("add")), remove=_ids(b.get("remove"))))
