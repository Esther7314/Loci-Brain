"""
========================================
web/loci_similar.py — the similarity check: suspected duplicates and the verdict on them
========================================

    GET  /api/loci/similar            -> suspected-duplicate pairs + score distribution
                                         (adjustable threshold), less the pairs kept
    POST /api/loci/similar/action     -> a human verdict on a suspected duplicate: keep
                                         both (remembered), or sink one (a soft delete)

The pairs, their cache and the rule for which buckets take part are core/similarity.py's;
the pairs kept, with each entry's version marker, are core/similar_kept.py's. These two
routes turn a request into a call on them and the result into JSON, and a sink drops the
pair cache (`invalidate`).
========================================
"""

import re

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from ._guards import _write_body
from core import _when as _w
from core import similar_kept as _kept
from core import similarity as _sim
from core.profile import _BIGEVENT_TAG, _PROFILE_TAG

logger = sh.logger


async def _pairs() -> dict:
    """The computed pairs (core/similarity.pairs) over the library and directory this
    process serves, read off `sh` at call time."""
    return await _sim.pairs(sh.bucket_mgr, sh.config["buckets_dir"])


def _marker_of(info: dict):
    """id -> the entry's version marker now (core/similar_kept.marker), None when it is
    not on the page."""
    def marker_of(bid: str):
        it = info.get(bid)
        return _kept.marker(it.get("body") or "") if it is not None else None
    return marker_of


# ---------------------------------------------------------
# Similarity check
# ---------------------------------------------------------
async def api_loci_similar(request: Request) -> Response:
    from starlette.responses import JSONResponse
    try:
        th = float(request.query_params.get("threshold") or _sim.SIM_DEFAULT)
    except (TypeError, ValueError):
        th = _sim.SIM_DEFAULT
    th = max(_sim.SIM_FLOOR, min(99.9, th))
    try:
        limit = int(request.query_params.get("limit") or 120)
    except (TypeError, ValueError):
        limit = 120
    try:
        data = await _pairs()
    except Exception as e:
        logger.warning(f"[loci] similar 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    info = data.get("info") or {}

    def _side(bid: str) -> dict:
        it = info.get(bid) or {}
        body = it.get("body") or ""
        return {
            "id": bid,
            "short": it.get("short") or bid[:6],
            "label": it.get("summary") or it.get("name") or body[:40],
            "room": it.get("room") or "",
            "created": it.get("created") or "",
            "preview": re.sub(r"\s+", " ", body)[:400],
            "len": len(body),
        }

    held = _kept.kept_now(sh.config["buckets_dir"], _marker_of(info))
    shown, counted = _sim.above(data, th, limit, skip=lambda a, b: _kept.key(a, b) in held)
    kept = sum(1 for score, a, b in data.get("pairs", [])
               if score >= th and _kept.key(a, b) in held)
    out = [{"score": round(score, 1), "a": _side(a), "b": _side(b),
            # whether the automatic tagger (threshold 80, and not the same ruler as this
            # page) has already flagged this pair
            "tagged": b in (info[a].get("tagged") or [])
                      or a in (info[b].get("tagged") or [])}
           for score, a, b in shown]
    return JSONResponse({
        "threshold": th,
        "default": _sim.SIM_DEFAULT,
        "floor": _sim.SIM_FLOOR,
        "pairs": out,
        "matched": counted,
        # pairs at or above the line left out because the owner kept them (留着)
        "kept": kept,
        "truncated": counted > len(out),
        "n": data.get("n", 0),
        "total_pairs": data.get("total_pairs", 0),
        # histogram: 20 buckets, 5 points each
        "hist": data.get("hist", []),
        "no_vectors": bool(data.get("no_vectors")),
        # True = the memory cap was hit, so the low-score range is incomplete and
        # `matched` reads lower than reality
        "capped": bool(data.get("capped")),
    })


async def api_loci_similar_action(request: Request) -> Response:
    """The human verdict. **The only write endpoint here.**

    keep = both stay, and the pair is remembered (core/similar_kept.py): GET similar
           leaves it out until either entry's text changes. Only the two ids and their
           version markers are written.
    sink = sink one: go through trace(delete=True), a soft delete into the archive that a
           direct id lookup always recovers.

    WARNING: **both ends of the pair must be supplied, and the server verifies for itself
    that the pair really exists**, for either verdict. Accepting a single `id` and calling
    `trace(delete=True)` on it would be the hole: trace's delete branch runs **before**
    its protected check, so once logged in, anyone could construct
    `{"action":"sink","id":<any bucket id>}` and sink the profile fact, a big event, or a
    pinned core bucket, even one that never appeared on the similarity page at all.
    """
    from starlette.responses import JSONResponse
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    action = str(body.get("action") or "").strip().lower()
    if action == "keep":
        return await _keep(str(body.get("a") or "").strip(), str(body.get("b") or "").strip())
    if action != "sink":
        return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

    bucket_id = str(body.get("id") or "").strip()
    a = str(body.get("a") or "").strip()
    b = str(body.get("b") or "").strip()
    if not bucket_id or not a or not b:
        return JSONResponse(
            {"error": "sink 必须同时给 a、b（这一对的两端）和 id（要沉的那个）"},
            status_code=400)
    if bucket_id not in (a, b):
        return JSONResponse({"error": "id 必须是 a 或 b 其中之一"}, status_code=400)
    if a == b:
        return JSONResponse({"error": "a 和 b 不能是同一个"}, status_code=400)

    try:
        data = await _pairs()
        info = data.get("info", {})
        # 1. Both ends must be buckets that really exist and are visible on this page.
        if a not in info or b not in info:
            return JSONResponse(
                {"error": "这一对里有一端不在相似度页上（可能已归档、已换版或是情绪种子）"},
                status_code=409)
        # 2. This pair must actually have been computed (order does not matter).
        if not _sim.has_pair(data, a, b):
            return JSONResponse(
                {"error": "这一对不在当前的相似结果里，不能从这儿沉"},
                status_code=409)
        # 3. Nothing that should never have been up for duplicate review may be sunk
        #    through this endpoint.
        target = await sh.bucket_mgr.get(bucket_id)
        if not target:
            return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)
        tmeta = target.get("metadata", {}) or {}
        ttags = [str(t) for t in (tmeta.get("tags") or [])]
        if tmeta.get("pinned") or tmeta.get("protected"):
            return JSONResponse(
                {"error": "这是 pinned/protected 的核心桶，不从判重这儿沉"},
                status_code=409)
        if _PROFILE_TAG in ttags or _BIGEVENT_TAG in ttags:
            return JSONResponse(
                {"error": "档案事实 / 大 event 不从判重这儿沉"}, status_code=409)

        from tools.trace.core import trace_core
        msg = str(await trace_core(bucket_id=bucket_id, delete=True))
        # trace's delete branch returns one sentence and no structured result — a
        # "not found" reply means the delete did not happen
        ok = not msg.startswith("未找到")
        if not ok:
            return JSONResponse({"ok": False, "action": "sink", "id": bucket_id,
                                 "msg": msg}, status_code=404)
        _sim.invalidate()      # one entry sank, so recompute next time
        return JSONResponse({"ok": True, "action": "sink", "id": bucket_id,
                             "msg": msg})
    except Exception as e:
        logger.warning(f"[loci] 裁决失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def _keep(a: str, b: str) -> Response:
    """留着: remember the pair (a, b) with both entries' markers now. The pair must be one
    the page computed, both ends on it, the same check a sink makes."""
    from starlette.responses import JSONResponse
    if not a or not b:
        return JSONResponse({"error": "keep 必须同时给 a、b（这一对的两端）"}, status_code=400)
    if a == b:
        return JSONResponse({"error": "a 和 b 不能是同一个"}, status_code=400)
    try:
        data = await _pairs()
        info = data.get("info", {})
        if a not in info or b not in info:
            return JSONResponse(
                {"error": "这一对里有一端不在相似度页上（可能已归档、已换版或是情绪种子）"},
                status_code=409)
        if not _sim.has_pair(data, a, b):
            return JSONResponse({"error": "这一对不在当前的相似结果里"}, status_code=409)
        _kept.keep(sh.config["buckets_dir"], a, b, _marker_of(info), _w.now())
    except Exception as e:
        logger.warning(f"[loci] similar keep failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "action": "keep", "a": a, "b": b})
