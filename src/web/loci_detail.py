"""
========================================
web/loci_detail.py — the detail window's routes
========================================

    GET  /api/loci/bucket/{id}        -> one entry verbatim, its title (the line every
                                         list shows for it), its metadata, the tag row, the
                                         关联 counts, its source layer, the edits offered
    GET  /api/loci/lineage/{id}       -> 关联: what came after it and its signposts
    GET  /api/loci/source/{id}        -> 来源: its sources and their state (an import's
                                         with when its first and last line were said),
                                         what it stands on, how it is known; `?fetch=<n>`
                                         asks the host for source n's original
    POST /api/loci/entry/fix          -> 字写错了 (trace old_str/new_str) · 内容错了 (an
                                         event: a new version marked 人改的; a MIND entry:
                                         her `disputed` mark, nothing rewritten) · 删除
                                         (trace delete=True)

Everything the window shows is assembled and worded in core/detail.py; these routes list
the store, hand it over, and turn the dict into JSON. Reads say the request's scope line
(`scope`); an entry out of scope reads as one that does not exist. The three reads are
host reads too (panel_auth.HOST_READ_PATHS): a host's credential reads them under its own
scope, and `?fetch=` asks the serving host under that scope. The write goes through
`_write_body` (same origin, JSON) and through the same tools the model uses.

Also here: `correct_event`, the 内容错了 write that POST /api/loci/event/correct shares,
and the request helper web/loci_names.py reads with (`read_scope_of`).
========================================
"""

import re

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from ._guards import _request_of, _scope_refusal, _write_body
from core import _when as _w
from core import runtime as rt
from core import detail as _D
from core import visibility as _V
from core.scope import OPEN_LINE
from utils import read_from_ids

logger = sh.logger


# ---------------------------------------------------------
# Request helper: the scope a read runs under
# ---------------------------------------------------------

async def read_scope_of(request: Request):
    """(refusal response or None, the request's ScopeView, its scope line). A panel
    request reads the whole library, through a view that still reads the source registry
    (core/scope.view_of)."""
    refused = _scope_refusal(request)
    if refused is not None:
        return refused, None, ""
    from core import scope as _scope
    req = _request_of(request)
    if req is None or req.whole_library:
        return None, await _scope.view_of(sh.bucket_mgr, req), OPEN_LINE
    from tools._common import read_scope
    return None, await read_scope(), req.first_line()


async def library_view():
    """The whole library as a panel page reads it: nothing narrowed, the source registry
    still read (core/scope.view_of), so a page never shows what stands on a source the
    registry already says is withdrawn."""
    from core import scope as _scope
    return await _scope.view_of(sh.bucket_mgr, None)


def _gone(bucket_id: str) -> JSONResponse:
    return JSONResponse(
        {"error": f"查无此桶：{bucket_id}（可能已物理删除或打错，不做语义联想）"}, status_code=404)


async def _entry(request: Request, view):
    """(bucket id, bucket) for the path's id, or (id, None) when it does not exist or is
    out of the request's scope."""
    bucket_id = str(request.path_params.get("bucket_id") or "").strip()
    if not bucket_id:
        return bucket_id, None
    b = await sh.bucket_mgr.get_including_archive(bucket_id)
    if not b or _V.visible_for(b.get("metadata") or {}, view, road=_V.READ).out_of_scope:
        return bucket_id, None
    return bucket_id, b


async def _named(meta: dict, live: list) -> dict:
    """The entries this one names that are not in the live listing (its new version, what
    it stands on), fetched from the archive."""
    have = {str((b.get("metadata") or {}).get("id") or b.get("id") or "") for b in live}
    want = [i for i in [str(meta.get("superseded_by") or "").strip(), *read_from_ids(meta)]
            if i and i not in have]
    out = {}
    for bid in dict.fromkeys(want):
        row = await sh.bucket_mgr.get_including_archive(bid)
        if row:
            out[bid] = row
    return out


async def _lineage_of(b: dict, view) -> dict:
    meta = b.get("metadata") or {}
    live = await sh.bucket_mgr.list_all(include_archive=False)
    return _D.lineage(live, meta, _w.now(), scope=view, extra=await _named(meta, live))


# ---------------------------------------------------------
# The reads
# ---------------------------------------------------------

async def api_loci_bucket(request: Request) -> Response:
    try:
        refused, view, line = await read_scope_of(request)
        if refused is not None:
            return refused
        bucket_id, b = await _entry(request, view)
        if not bucket_id:
            return JSONResponse({"error": "missing id"}, status_code=400)
        if b is None:
            return _gone(bucket_id)
        lin = await _lineage_of(b, view)
        if _D.clearing_due(b.get("metadata") or {}, view):
            b = sh.bucket_mgr.as_cleared(b)
        return JSONResponse(_D.entry_view(b, lin, scope_line=line))
    except Exception as e:
        logger.warning(f"[loci] bucket 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_lineage(request: Request) -> Response:
    try:
        refused, view, line = await read_scope_of(request)
        if refused is not None:
            return refused
        bucket_id, b = await _entry(request, view)
        if b is None:
            return _gone(bucket_id)
        return JSONResponse({**await _lineage_of(b, view), "scope": line})
    except Exception as e:
        logger.warning(f"[loci] lineage 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_source(request: Request) -> Response:
    try:
        refused, view, line = await read_scope_of(request)
        if refused is not None:
            return refused
        bucket_id, b = await _entry(request, view)
        if b is None:
            return _gone(bucket_id)
        meta = b.get("metadata") or {}
        from core import _originals as _O
        from core import scope as _scope
        registry = getattr(sh.bucket_mgr, "sources", None)
        raw = (request.query_params.get("fetch") or "").strip()
        if raw:
            if not re.fullmatch(r"\d+", raw):
                return JSONResponse({"error": f"fetch 要是来源的序号（0 起），收到：{raw}"},
                                    status_code=400)
            try:
                out = await _D.fetch_original(
                    meta, int(raw), store=sh.bucket_mgr, hosts=_O.deployment_hosts(),
                    registry=registry, settings=_O.settings_from(rt.config),
                    request=_scope.current_request())
            except IndexError:
                return JSONResponse({"error": f"这条没有第 {raw} 个来源"}, status_code=404)
            return JSONResponse({**out, "scope": line})
        lookup = await _named(meta, [])
        return JSONResponse({**_D.source_view(meta, registry=registry,
                                              hosts=_O.deployment_hosts(), scope=view,
                                              lookup=lookup),
                             "scope": line})
    except Exception as e:
        logger.warning(f"[loci] source 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------
# The write: 编辑
# ---------------------------------------------------------

async def correct_event(old_id: str, new_text: str):
    """内容错了: **the original is left untouched**; the correction is a new event whose
    `from` points back at it, carrying the 人改的 tag (core.profile._EDITED_BY_USER_TAG).
    A person's edit does not become truth directly: whether to accept it is decided by
    folding it, and that hand is the model's — the new entry lands in breath's
    「依据变了的」 (core/profile.edited_by_user).

    A MIND entry has no such entry point, enforced here and not only by the window: a
    realization is the model's own judgement; the owner may disagree, but changing it is
    the model's act. Returns (status code, payload); 200 carries old_id / new_id / msg."""
    if not old_id:
        return 400, {"error": "缺 id"}
    if not str(new_text or "").strip():
        return 400, {"error": "text 不能为空——改完之后的完整正文"}
    old = await sh.bucket_mgr.get(old_id)
    if not old:
        return 404, {"error": f"查无此桶（也可能已归档）：{old_id}"}
    old_meta = old.get("metadata", {}) or {}
    from core._rooms import is_event_room, is_mind_room
    old_room = str(old_meta.get("room") or "")
    if is_mind_room(old_room):
        return 403, {"error": "mind 不能从这儿改——mind 是我的判断，你可以不同意，"
                              "但得由我自己改（跟我说，我认同了自己 regrow）"}
    if not is_event_room(old_room):
        return 409, {"error": f"这不是一条 event（room={old_room or '未分房'}），"
                              "改错这个动作只对 event 开放"}
    from tools.grow.rooms_path import grow_event
    from core.profile import _EDITED_BY_USER_TAG
    # v/a come from the old entry: a factual correction is not a new feeling.
    msg = await grow_event(
        items=[{"room": old_room, "text": new_text,
                "v": old_meta.get("valence", 0.5), "a": old_meta.get("arousal", 0.3)}],
        from_ids=[old_id],
    )
    m = re.search(r"📝([0-9a-f]{12})", msg)
    if not m:
        return 500, {"error": f"新桶落盘失败：{msg}"}
    new_id = m.group(1)
    # The tag is merged, not replaced: the background-filled tags may not have landed yet
    # and trace(tags=...) replaces the whole list, so this goes through bucket_mgr.update.
    fresh = await sh.bucket_mgr.get(new_id)
    cur_tags = [str(t) for t in ((fresh or {}).get("metadata", {}).get("tags") or [])]
    await sh.bucket_mgr.update(new_id, tags=list(dict.fromkeys(cur_tags + [_EDITED_BY_USER_TAG])))
    return 200, {"ok": True, "old_id": old_id, "new_id": new_id, "msg": msg}


async def dispute_judgement(bucket_id: str, note: str) -> dict:
    """内容错了 on a MIND entry: **nothing is rewritten**. A judgement is the model's own;
    the owner's word that it is wrong hangs on it as a `disputed` invalidation record
    with her note (core/_invalidation.py), and the entry comes up in breath's 依据变了的
    for the model to rewrite, put away or keep. The ledger records the record added
    (TraceUpdated naming `invalidation`)."""
    from core import _invalidation as _I
    stamp = _w.now().isoformat(timespec="seconds")
    await sh.bucket_mgr.add_invalidation_record(bucket_id,
                                                _I.dispute_record(bucket_id, note, stamp))
    return {"ok": True, "kind": _D.CONTENT, "id": bucket_id, "new_id": None,
            "mark": _I.DISPUTED,
            "msg": "挂上了「人说不对」，他下次开口前会在「依据变了的」里看到，改不改由他定"}


async def api_loci_entry_fix(request: Request) -> Response:
    """The window's 编辑 column, one kind per click: typo {old, new} · content {text} ·
    delete. The edits offered are asked again here (core/detail.edit_actions), so an id
    cannot be edited in a way the window would not offer. content on an event writes a
    corrected new version (`correct_event`); on a MIND entry `text` is her note, optional,
    and the entry only carries her mark (`dispute_judgement`)."""
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    bucket_id = str(body.get("id") or "").strip()
    kind = str(body.get("kind") or "").strip()
    if not bucket_id:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    if kind not in _D.FIX_KINDS:
        return JSONResponse({"error": "kind 只有三个：typo / content / delete"}, status_code=400)
    try:
        target = await sh.bucket_mgr.get_including_archive(bucket_id)
        if not target:
            return _gone(bucket_id)
        meta = target.get("metadata") or {}
        if kind == _D.CONTENT and _D.disputed_view(meta) is not None:
            return JSONResponse({"error": "这条已经挂着一个「人说不对」了，等他看过再说"},
                                status_code=409)
        if kind not in _D.edit_actions(meta):
            return JSONResponse(
                {"error": "这条现在不能这么改（在归档区要先恢复；旧版要在正在用的那一版上改）"},
                status_code=409)
        if kind == _D.CONTENT and _D.content_fix(meta) == _D.MARK:
            return JSONResponse(await dispute_judgement(bucket_id, str(body.get("text") or "")))
        if kind == _D.CONTENT:
            status, out = await correct_event(bucket_id, str(body.get("text") or ""))
            if status != 200:
                return JSONResponse(out, status_code=status)
            return JSONResponse({"ok": True, "kind": kind, "id": bucket_id,
                                 "new_id": out["new_id"], "msg": out["msg"]})
        from tools.trace.core import trace_core
        if kind == _D.TYPO:
            old, new = body.get("old"), body.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                return JSONResponse({"error": "字写错了要带 old（原文里那一段）和 new（改成什么）"},
                                    status_code=400)
            msg = str(await trace_core(bucket_id=bucket_id, old_str=old, new_str=new,
                                       closed_by="user"))
            done = msg.startswith("已修改记忆桶")
        else:
            msg = str(await trace_core(bucket_id=bucket_id, delete=True, closed_by="user"))
            done = msg.startswith("已将记忆桶存入档案")
        if not done:
            return JSONResponse({"error": msg}, status_code=409)
        return JSONResponse({"ok": True, "kind": kind, "id": bucket_id, "msg": msg})
    except Exception as e:
        logger.warning(f"[loci] entry/fix 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
