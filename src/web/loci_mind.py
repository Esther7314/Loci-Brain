"""
========================================
web/loci_mind.py — the panel's breath, surface, trace and regrow/fold pages
========================================

    GET  /api/loci/breath/last        -> the last breath each host was handed, as it was
    GET  /api/loci/awake              -> the awake pool now, every reason on each entry
    GET  /api/loci/hanging            -> what still hangs open, awake (surface) or asleep (deep)
    POST /api/loci/trace              -> a trace button: done / drop / withdraw
    GET  /api/loci/changes/recent     -> how the library reshaped itself, newest first

Each read is a core function plus a thin route (core/breath_snapshot.py, core/profile.py
`awake_pool` / `hanging`, core/changes_feed.py): the route lists the store off `web/_shared`,
hands it over, pages the rows and adds the scope line. The panel reads its whole library,
so the line is always the open one, except on the breath page, which carries the line the
breath was handed out under.

Lists page by `offset` (default 0) and `limit` (default 5, at most 50), newest first, and
`as_of`: the first page's `as_of` sent back keeps rows that arrived since from shifting the
pages. The reply: {items, total, offset, limit, next_offset (null at the end), as_of}. The
reading and the page are core/paging.py's, the same for every panel list.

The one write goes through `_write_body` (the same-origin check) and through
tools.trace.core.trace_core with closed_by="user", the same road the model's trace takes.
========================================
"""

from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from ._guards import _request_of, _write_body
from core import _when as _w
from core import breath_snapshot as _snap
from core import changes_feed as _changes
from core import paging as _pg
from core import profile as _profile
from core import scope as _scope

logger = sh.logger

PARTS = ("surface", "deep")
ACTIONS = (_profile.DONE, _profile.DROP, _profile.WITHDRAW)


# ---------------------------------------------------------
# Paging and the scope line
# ---------------------------------------------------------
def _scope_line(request: Request) -> str:
    req = _request_of(request)
    return req.first_line() if req is not None else _scope.OPEN_LINE


def _paging(request: Request) -> tuple[int, int, datetime]:
    """(offset, limit, as_of) from the query (core/paging.py); BadPage says what is wrong."""
    return _pg.args_of(request.query_params)


def _page(rows: list[dict], offset: int, limit: int, as_of: datetime) -> dict:
    """One page of `rows` (newest first), leaving out rows whose `at` is after `as_of`."""
    kept = _pg.cut(rows, as_of, lambda r: _w.parse_stamp(r.get("at")))
    return _pg.page(kept, offset, limit, as_of)


def _bad(e: Exception, status: int = 400) -> Response:
    return JSONResponse({"error": str(e)}, status_code=status)


def _delivered_at():
    cues = getattr(sh.bucket_mgr, "cues", None)
    return cues.delivered_at if cues is not None else None


# ---------------------------------------------------------
# breath: the last one handed out
# ---------------------------------------------------------
async def build_breath_last(host: str | None) -> dict:
    """{at, host, scope, breath, hosts}: `host`'s last breath (the most recent of any host
    when None) with its titles read now; {breath: None, note, hosts} when there is none."""
    base_dir = str(sh.bucket_mgr.base_dir)
    known = _snap.hosts(base_dir)
    entry = _snap.load(base_dir, host)
    if entry is None:
        return {"breath": None, "note": "还没递过", "host": host, "hosts": known}
    all_buckets = await sh.bucket_mgr.list_all(include_archive=True)
    entry["breath"] = _snap.relabel(entry["breath"], all_buckets)
    return {**entry, "hosts": known}


async def api_loci_breath_last(request: Request) -> Response:
    host = str(request.query_params.get("host") or "").strip() or None
    try:
        return JSONResponse(await build_breath_last(host))
    except Exception as e:
        logger.warning(f"[loci] breath/last failed: {e}")
        return _bad(e, 500)


# ---------------------------------------------------------
# surface: the awake pool
# ---------------------------------------------------------
async def build_awake(reason: str = "") -> list[dict]:
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    return _profile.awake_pool(all_buckets, _w.now(),
                               settings=_profile.breath_settings(sh.config),
                               delivered_at=_delivered_at(), reason=reason,
                               scope=await _scope.view_of(sh.bucket_mgr, None))


async def api_loci_awake(request: Request) -> Response:
    reason = str(request.query_params.get("reason") or "").strip()
    if reason and reason not in _profile.AWAKE_WORDS:
        return _bad(ValueError(f"reason 只有：{' / '.join(_profile.AWAKE_WORDS)}"))
    try:
        offset, limit, as_of = _paging(request)
    except _pg.BadPage as e:
        return _bad(e)
    try:
        rows = await build_awake(reason)
    except Exception as e:
        logger.warning(f"[loci] awake failed: {e}")
        return _bad(e, 500)
    return JSONResponse({**_page(rows, offset, limit, as_of), "scope": _scope_line(request)})


# ---------------------------------------------------------
# trace: what still hangs open, and its buttons
# ---------------------------------------------------------
async def build_hanging() -> dict:
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    return _profile.hanging(all_buckets, _w.now(),
                            settings=_profile.breath_settings(sh.config),
                            delivered_at=_delivered_at(),
                            scope=await _scope.view_of(sh.bucket_mgr, None))


async def api_loci_hanging(request: Request) -> Response:
    part = str(request.query_params.get("part") or "surface").strip()
    if part not in PARTS:
        return _bad(ValueError("part 只有 surface / deep"))
    try:
        offset, limit, as_of = _paging(request)
    except _pg.BadPage as e:
        return _bad(e)
    try:
        halves = await build_hanging()
    except Exception as e:
        logger.warning(f"[loci] hanging failed: {e}")
        return _bad(e, 500)
    return JSONResponse({"part": part, **_page(halves[part], offset, limit, as_of),
                         "scope": _scope_line(request)})


def _closed(meta: dict, row: dict, action: str) -> bool:
    """Did the button's write land: closed for a promise or a hold, the cue gone for a
    cue."""
    from utils import is_closed
    if action == _profile.WITHDRAW and row["kind"] != "hold":
        cue = meta.get("cue")
        return not (isinstance(cue, dict) and str(cue.get("condition") or "").strip())
    return is_closed(meta)


async def api_loci_trace(request: Request) -> Response:
    """{id, action}: 做完了 (done) / 不做了 (drop) on a promise, 撤掉 (withdraw) on a hold or
    a cue. The hanging list is computed again first: only an id on it, with an action its
    row offers, is acted on — 404 when the id is not hanging, 409 when its row does not
    take the action. Then trace_core, as the model's trace would be called:

        done      status="resolved", closed_by="user"
        drop      status="abandoned", closed_by="user"
        withdraw  a hold: status="resolved", closed_by="user" (the agreement comes back)
                  a cue: cue="" (the condition comes off)

    {ok, id, action, msg}: `msg` is trace's own reply. 409 with it when trace wrote
    nothing."""
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return _bad(e, 403)
    except ValueError as e:
        return _bad(e)
    bucket_id = str(body.get("id") or "").strip()
    action = str(body.get("action") or "").strip()
    if not bucket_id:
        return _bad(ValueError("缺 id"))
    if action not in ACTIONS:
        return _bad(ValueError(f"action 只有：{' / '.join(ACTIONS)}"))
    try:
        halves = await build_hanging()
    except Exception as e:
        logger.warning(f"[loci] trace button: hanging failed: {e}")
        return _bad(e, 500)
    row = next((r for part in PARTS for r in halves[part] if r["id"] == bucket_id), None)
    if row is None:
        return JSONResponse({"error": f"这条不在挂着的单子上：{bucket_id}"}, status_code=404)
    if action not in row["actions"]:
        return JSONResponse({"error": f"这一行没有 {action} 这个按钮（只有 "
                                      f"{' / '.join(row['actions'])}）"}, status_code=409)

    from tools.trace.core import trace_core
    if action == _profile.DONE:
        kwargs = {"status": "resolved", "closed_by": "user"}
    elif action == _profile.DROP:
        kwargs = {"status": "abandoned", "closed_by": "user"}
    elif row["kind"] == "hold":
        kwargs = {"status": "resolved", "closed_by": "user"}
    else:
        kwargs = {"cue": ""}
    try:
        msg = str(await trace_core(bucket_id=bucket_id, **kwargs))
        fresh = await sh.bucket_mgr.get(bucket_id)
    except Exception as e:
        logger.warning(f"[loci] trace button failed: {e}")
        return _bad(e, 500)
    if not fresh or not _closed(fresh.get("metadata") or {}, row, action):
        return JSONResponse({"error": "trace 没写进去", "msg": msg}, status_code=409)
    return JSONResponse({"ok": True, "id": bucket_id, "action": action, "msg": msg})


# ---------------------------------------------------------
# regrow/fold: how the library reshaped itself
# ---------------------------------------------------------
async def build_changes(as_of: datetime) -> list[dict]:
    events = list(sh.bucket_mgr.ledger_mirror.iter_events())
    all_buckets = await sh.bucket_mgr.list_all(include_archive=True)
    return _changes.recent(events, all_buckets, as_of=as_of,
                           scope=await _scope.view_of(sh.bucket_mgr, None))


async def api_loci_changes_recent(request: Request) -> Response:
    try:
        offset, limit, as_of = _paging(request)
    except _pg.BadPage as e:
        return _bad(e)
    try:
        rows = await build_changes(as_of)
    except Exception as e:
        logger.warning(f"[loci] changes/recent failed: {e}")
        return _bad(e, 500)
    return JSONResponse({**_page(rows, offset, limit, as_of), "scope": _scope_line(request)})
