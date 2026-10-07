"""
========================================
web/loci_activity.py — the panel's reads of what Loci handed out and took in: turns,
recall's timeline, usage, grow, muse, the missing vectors, and 「现在补」
========================================

    GET  /api/loci/turns/{window}       -> one window turn by turn (core/activity.turns)
    GET  /api/loci/recall/timeline      -> the last three days of searches and cards
                                           (core/activity.timeline)
    GET  /api/loci/usage                -> shown / found / stood on, per memory
                                           (core/activity.usage_counts)
    GET  /api/loci/grow/today           -> written since today began (core/grow_view)
    GET  /api/loci/grow/slices          -> every batch of slices, with states (core/grow_view)
    GET  /api/loci/muse                 -> clusters or unnamed days (core/muse_view)
    GET  /api/loci/embedding/missing    -> what has no vector, and why (core/vector_view)
    POST /api/loci/embedding/backfill   -> 「现在补」 (core/vector_view.backfill)

Each route reads the library, logs and engines off `web/_shared` at call time, hands them
to its core function and returns the dict as JSON. Every list takes `offset` / `limit` /
`as_of` (core/activity.page_args). Every route here is the panel's alone (no entry in
panel_auth.HOOK_PATHS): a search's query text comes back on these routes and on no host
route (core/_usage.py).
========================================
"""

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from ._guards import _request_of, _write_body
from core import _when as _w
from core import activity as _act

logger = sh.logger


def _scope_line(request: Request) -> str:
    from core import scope as _scope
    req = _request_of(request)
    return req.first_line() if req is not None else _scope.OPEN_LINE


def _paging(request: Request, now):
    q = request.query_params
    return _act.page_args(q.get("offset"), q.get("limit"), q.get("as_of"), now=now)


def _usage_rows() -> list:
    usage = getattr(sh.bucket_mgr, "usage", None)
    return usage.read() if usage is not None else []


def _failed(what: str, e: Exception) -> Response:
    logger.warning(f"[loci] {what} failed: {e}")
    return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------
# What each turn was handed (core/activity.py)
# ---------------------------------------------------------
async def api_loci_turns(request: Request) -> Response:
    window = str(request.path_params.get("window") or "").strip()
    host = (request.query_params.get("host") or "").strip()
    if not window:
        return JSONResponse({"error": "缺窗口的编号"}, status_code=400)
    if not host:
        return JSONResponse({"error": "要带 host：哪个宿主的窗口"}, status_code=400)
    try:
        now = _w.now()
        offset, limit, as_of = _paging(request, now)
        out = _act.turns(sh.bucket_mgr.cues, _usage_rows(),
                         await sh.bucket_mgr.list_all(include_archive=True),
                         host=host, window=window, now=now, offset=offset, limit=limit,
                         as_of=as_of, panel=_request_of(request) is None)
    except Exception as e:                       # noqa: BLE001
        return _failed("turns", e)
    if out is None:
        return JSONResponse({"error": f"{host} 的窗口 {window} 没要过卡",
                             "scope": _scope_line(request)}, status_code=404)
    return JSONResponse({**out, "scope": _scope_line(request)})


async def api_loci_recall_timeline(request: Request) -> Response:
    try:
        now = _w.now()
        offset, limit, as_of = _paging(request, now)
        out = _act.timeline(sh.bucket_mgr.cues.events(), _usage_rows(),
                            await sh.bucket_mgr.list_all(include_archive=True),
                            now=now, offset=offset, limit=limit, as_of=as_of,
                            panel=_request_of(request) is None)
    except Exception as e:                       # noqa: BLE001
        return _failed("recall/timeline", e)
    return JSONResponse({**out, "scope": _scope_line(request)})


async def api_loci_usage(request: Request) -> Response:
    from datetime import timedelta
    try:
        now = _w.now()
        offset, limit, as_of = _paging(request, now)
        since = _w.parse_date_or_none((request.query_params.get("since") or "").strip())
        if since is None:
            usage = getattr(sh.bucket_mgr, "usage", None)
            days = getattr(usage, "retain_days", 30)
            since = _w.to_local(now).replace(hour=0, minute=0, second=0,
                                             microsecond=0) - timedelta(days=days)
        out = _act.usage_counts(_usage_rows(),
                                await sh.bucket_mgr.list_all(include_archive=True),
                                since=since, offset=offset, limit=limit, as_of=as_of)
    except Exception as e:                       # noqa: BLE001
        return _failed("usage", e)
    return JSONResponse({**out, "scope": _scope_line(request)})


# ---------------------------------------------------------
# grow's page (core/grow_view.py)
# ---------------------------------------------------------
async def api_loci_grow_today(request: Request) -> Response:
    from core import grow_view as _gv
    try:
        now = _w.now()
        offset, limit, as_of = _paging(request, now)
        since, since_from = _gv.day_cut(now, request.query_params.get("since"))
        out = _gv.written_since(await sh.bucket_mgr.list_all(include_archive=False), since,
                                now=now, offset=offset, limit=limit, as_of=as_of)
    except Exception as e:                       # noqa: BLE001
        return _failed("grow/today", e)
    return JSONResponse({"since": _act.stamp(since), "since_from": since_from, **out,
                         "scope": _scope_line(request)})


async def api_loci_grow_slices(request: Request) -> Response:
    from core import grow_view as _gv
    from . import panel_auth as _pa
    from .host_api import _slices_config
    try:
        now = _w.now()
        offset, limit, as_of = _paging(request, now)
        _max_lines, threshold = _slices_config()
        out = _gv.slice_batches(sh.bucket_mgr.slices,
                                await sh.bucket_mgr.list_all(include_archive=True),
                                hosts=_pa.hosts(), threshold=threshold, offset=offset,
                                limit=limit, as_of=as_of)
    except Exception as e:                       # noqa: BLE001
        return _failed("grow/slices", e)
    return JSONResponse({**out, "scope": _scope_line(request)})


# ---------------------------------------------------------
# muse's page (core/muse_view.py, over core/_muse.both_sides)
# ---------------------------------------------------------
async def api_loci_muse(request: Request) -> Response:
    from core import _muse as M
    from core import muse_view as _mv
    part = (request.query_params.get("part") or _mv.CLUSTERS).strip()
    if part not in _mv.PARTS:
        return JSONResponse({"error": f"part 只能是 {' / '.join(_mv.PARTS)}"},
                            status_code=400)
    try:
        now = _w.now()
        offset, limit, as_of = _paging(request, now)
        clusters, _scattered, _default, fingers, _stats = await M.both_sides()
        out = _mv.panel_view(clusters, fingers, part=part, offset=offset, limit=limit,
                             as_of=as_of)
    except Exception as e:                       # noqa: BLE001
        return _failed("muse", e)
    return JSONResponse({**out, "scope": _scope_line(request)})


# ---------------------------------------------------------
# The vectors (core/vector_view.py, over the embedding outbox)
# ---------------------------------------------------------
async def api_loci_embedding_missing(request: Request) -> Response:
    from core import vector_view as _vv
    outbox = sh.embedding_outbox
    engine = sh.embedding_engine
    try:
        now = _w.now()
        offset, limit, as_of = _paging(request, now)
        index_ids = None
        lister = getattr(engine, "list_content_ids", None)
        if callable(lister):
            try:
                index_ids = list(lister())
            except Exception as e:               # noqa: BLE001 - unknown is not empty
                logger.warning(f"[loci] embedding index unreadable: {e}")
        status = outbox.status() if outbox is not None else {}
        out = _vv.missing(outbox.items_view() if outbox is not None else [],
                          await sh.bucket_mgr.list_all(include_archive=True), index_ids,
                          circuit=(status.get("circuit") or {}).get("state", "closed"),
                          provider_ready=bool(engine and getattr(engine, "enabled", False)),
                          now=now, offset=offset, limit=limit, as_of=as_of)
    except Exception as e:                       # noqa: BLE001
        return _failed("embedding/missing", e)
    return JSONResponse({**out, "scope": _scope_line(request)})


async def api_loci_embedding_backfill(request: Request) -> Response:
    """「现在补」: queue every memory with no vector and make every waiting item due now.
    Same-origin JSON only (web/_guards._write_body); the body may be `{}`."""
    from core import vector_view as _vv
    try:
        await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    outbox = sh.embedding_outbox
    if outbox is None:
        return JSONResponse({"error": "向量队列没起来，补不了"}, status_code=503)
    try:
        out = await _vv.backfill(outbox)
    except Exception as e:                       # noqa: BLE001
        return _failed("embedding/backfill", e)
    return JSONResponse({"ok": True, **out})
