"""
========================================
web/loci_names.py — the names page and the name card
========================================

    GET  /api/loci/names              -> the names the table knows, by kind, most mentioned
                                         first (`?kind=`), with how many wait to be
                                         recognised; paged
    GET  /api/loci/names/pending      -> the names the table does not know yet, each with
                                         the entry it first appeared in; paged
    GET  /api/loci/names/{name}       -> one name: what it is, where it hangs, its card, the
                                         entries it appears in (paged)
    POST /api/loci/names/action       -> not_person / merge / rename / set_kind: one name per
                                         click, written to aliases.yaml only

The counting is core/census.py's (names_page, pending_names, name_card), the one write
dispatcher core/census.name_action; these routes list the store, hand it over and turn
the dict into JSON. Lists page by offset / limit / as_of (web/loci_detail.paging_of).
POST /api/loci/subjects/action shares `api_loci_names_action`.
========================================
"""

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from ._guards import _write_body
from .loci_detail import paging_of, read_scope_of
from core import census as _census

logger = sh.logger


async def _listing() -> list:
    return await sh.bucket_mgr.list_all(include_archive=False)


async def api_loci_names(request: Request) -> Response:
    try:
        refused, view, line = await read_scope_of(request)
        if refused is not None:
            return refused
        try:
            offset, limit, as_of, as_of_text = paging_of(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        listing = [b for b in await _listing()
                   if view is None or view.permits(b.get("metadata") or {})]
        out = _census.names_page(listing, kind=(request.query_params.get("kind") or "").strip(),
                                 offset=offset, limit=limit, as_of=as_of)
        return JSONResponse({**out, "as_of": as_of_text, "scope": line})
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] names 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_names_pending(request: Request) -> Response:
    try:
        refused, view, line = await read_scope_of(request)
        if refused is not None:
            return refused
        try:
            offset, limit, as_of, as_of_text = paging_of(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        listing = [b for b in await _listing()
                   if view is None or view.permits(b.get("metadata") or {})]
        out = _census.pending_names(listing, offset=offset, limit=limit, as_of=as_of)
        return JSONResponse({**out, "as_of": as_of_text, "scope": line})
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] names/pending 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_name_card(request: Request) -> Response:
    try:
        refused, view, line = await read_scope_of(request)
        if refused is not None:
            return refused
        try:
            offset, limit, as_of, as_of_text = paging_of(request)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        name = str(request.path_params.get("name") or "").strip()
        card = _census.name_card(await _listing(), name, offset=offset, limit=limit,
                                 as_of=as_of, scope=view)
        if card is None:
            return JSONResponse({"error": f"名字表里没有「{name}」，也没有哪条记忆提到它"},
                                status_code=404)
        card["memories"]["as_of"] = as_of_text
        return JSONResponse({**card, "scope": line})
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] names/{{name}} 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_names_action(request: Request) -> Response:
    """One button on the names page. **A write** — buckets/aliases.yaml, never an entry,
    never the ledger. The system lays names out; which one to change is a human click, so
    there is no bulk request: one call changes one name."""
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        out = _census.name_action(body.get("action"), body.get("name"),
                                  target=body.get("target"), kind=body.get("kind"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] names/action 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, **out})
