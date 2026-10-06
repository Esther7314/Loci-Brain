"""
========================================
web/loci_similar.py — the similarity check: suspected duplicates and the verdict on them
========================================

    GET  /api/loci/similar            -> suspected-duplicate pairs + score distribution (adjustable threshold)
    POST /api/loci/similar/action     -> a human verdict on a suspected duplicate: keep
                                         both, or sink one (a soft delete)

The pair cache, the bucket-directory revision cache and their lock live here, next to the
two routes that read and invalidate them.
========================================
"""

import json
import os
import re
import sqlite3
import threading
from collections import Counter

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from ._guards import _write_body
from .loci_reads import _BIGEVENT_TAG, _PROFILE_TAG

logger = sh.logger


# ============================================================
# Similarity: pairwise cosine across the whole store. Nothing is recomputed while the vector
# store is unchanged, so the slider stays responsive.
# ============================================================

# The default is 88. Below 85 the thirteen emotional-root seeds start mixing in, and those
# are supposed to resemble each other.
_SIM_DEFAULT = 88.0
_SIM_FLOOR = 60.0      # pairs scoring below this are not even computed, to keep hundreds of thousands of them out of memory
# Memory gate: at most this many pairs are kept. At 628 entries that is currently ~29,000
# pairs; the same ~15% ratio at ten thousand entries would be 7.5 million tuples, roughly
# 0.7 GB.
_PAIRS_CAP = 200_000

_sim_lock = threading.Lock()
_sim_cache: dict = {"key": None, "pairs": [], "hist": [], "n": 0, "total_pairs": 0}


def _emb_db_path() -> str:
    return os.path.join(sh.config["buckets_dir"], "embeddings.db")


_rev_cache: dict = {"at": 0.0, "val": (0, 0.0)}
_REV_TTL = 2.0          # seconds. While the slider is being dragged, do not walk nine hundred files on every tick.


def _buckets_rev() -> tuple:
    """The bucket directory's "version": file count plus the most recent modification time.

    Why the cache key cannot be `embeddings.db` alone: changing only name / room / tags /
    importance / domain **does not touch the vector store**. Tag an entry `__seed__`, for
    instance — it should vanish from this page — and it stays in the cached pairs, with a
    stale name and summary on its card. But every one of those changes does rewrite that
    bucket's .md.

    WARNING: **this must recurse.** There is another level below `dynamic/`, and a top-level
    listdir sees only 117 files where there are really more than 900 — which means missing
    nine tenths of all changes, so the fix would be no fix at all.
    (The first version did exactly that. It was the file count printed by the smoke test
    that caught it.)
    """
    import time
    now_s = time.monotonic()
    if now_s - _rev_cache["at"] < _REV_TTL:
        return _rev_cache["val"]

    root = str(sh.config.get("buckets_dir") or "")
    newest, count = 0.0, 0
    for sub in ("dynamic", "permanent", "feel", "archive"):
        for dirpath, _dirs, names in os.walk(os.path.join(root, sub)):
            for name in names:
                if not name.endswith(".md"):
                    continue
                count += 1
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    pass
    val = (count, round(newest, 3))
    _rev_cache.update({"at": now_s, "val": val})
    return val


def _load_vectors() -> tuple[list, object]:
    """Read every vector from embeddings.db; returns (ids, the normalized matrix)."""
    import numpy as np
    ids, vecs = [], []
    con = sqlite3.connect(f"file:{_emb_db_path()}?mode=ro", uri=True)
    try:
        for bid, emb in con.execute("select bucket_id, embedding from embeddings"):
            try:
                v = np.asarray(json.loads(emb), dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if v.ndim != 1 or v.shape[0] < 8:
                continue
            ids.append(str(bid))
            vecs.append(v)
    finally:
        con.close()
    if not vecs:
        return [], None
    dim = Counter(v.shape[0] for v in vecs).most_common(1)[0][0]
    keep = [i for i, v in enumerate(vecs) if v.shape[0] == dim]
    ids = [ids[i] for i in keep]
    M = np.vstack([vecs[i] for i in keep])
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    return ids, M


def _sim_visible(meta: dict) -> bool:
    """Which buckets appear on this page.

    Several categories are excluded. All of them are things that are *supposed* to look
    alike, so flagging them as duplicates is pure false positive:
      - emotional seeds (the thirteen roots score 87~89 against each other; they are a
        coordinate system, not memories)
      - the archive (already sunk once; do not ask again)
      - superseded realizations (regrow's version chain: old and new should resemble each
        other)
      - **anything folded under a gist**: it is supposed to resemble the gist that folded
        it, and it no longer surfaces independently, so raising it again here achieves
        nothing
    """
    if str(meta.get("type") or "") in ("archived",):
        return False
    if (meta.get("domain") or [""])[0] == "seed":
        return False
    if "__seed__" in [str(t) for t in (meta.get("tags") or [])]:
        return False
    from core import _fold as _F
    if (_F.is_covered(meta)
            or meta.get("tombstone") or meta.get("deleted_at")):
        return False
    return True


async def _compute_pairs() -> dict:
    """Compute pairwise cosine across the whole store once, and cache it until
    embeddings.db's mtime changes."""
    import numpy as np
    try:
        key = (os.path.getmtime(_emb_db_path()), os.path.getsize(_emb_db_path()),
               _buckets_rev())      # a metadata change must invalidate this too
    except OSError:
        key = None

    with _sim_lock:
        if key is not None and _sim_cache["key"] == key:
            return _sim_cache

    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    info: dict[str, dict] = {}
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or not _sim_visible(meta):
            continue
        info[bid] = {
            "id": bid,
            "short": bid[:6] if re.fullmatch(r"[0-9a-f]{12}", bid) else bid,
            "name": str(meta.get("name") or ""),
            "summary": str(meta.get("summary") or ""),
            "room": str(meta.get("room") or ""),
            "created": str(meta.get("created") or "")[:10],
            "body": str(b.get("content") or ""),
            "tagged": [str(t).split(":", 1)[1] for t in (meta.get("tags") or [])
                       if str(t).startswith("疑似同件:")],
        }

    ids, M = _load_vectors()
    if M is None:
        result = {"key": key, "pairs": [], "hist": [], "n": 0, "total_pairs": 0,
                  "info": info, "no_vectors": True}
        with _sim_lock:
            _sim_cache.update(result)
        return result

    keep = [i for i, b in enumerate(ids) if b in info]
    ids = [ids[i] for i in keep]
    M = M[keep]

    n = len(ids)
    hist = [0] * 20  # one bucket per 5 points, over 0~100
    pairs = []
    capped = False
    if n >= 2:
        # Compute in blocks rather than allocating one n x n matrix. At 671 entries it makes
        # no difference; at ten thousand it very much does.
        step = 256
        for s in range(0, n, step):
            block = M[s:s + step] @ M.T                      # (b, n)
            for r in range(block.shape[0]):
                i = s + r
                row = block[r]
                row[:i + 1] = -1.0                           # keep the upper triangle only; do not count each pair twice
                counts = np.bincount(
                    np.clip((np.maximum(row[i + 1:], 0) * 20).astype(int), 0, 19),
                    minlength=20)
                hist = [h + int(c) for h, c in zip(hist, counts)]
                for j in np.where(row >= _SIM_FLOOR / 100.0)[0]:
                    pairs.append((float(row[j]) * 100.0, ids[i], ids[int(j)]))
            # The cap. At 628 entries this keeps ~29,000 pairs, which is nothing; but the
            # same ratio at ten thousand entries is 7.5 million tuples, roughly 0.7 GB, and
            # the process would not survive it.
            # Only the most similar pairs are kept — duplicate review reads from the top
            # score downwards anyway, and nobody will ever scroll through the millions of
            # pairs sitting around 60.
            # WARNING: the cost is that once the cap trips, dragging the threshold very low
            # shows fewer pairs than really exist. So a `capped` flag is returned below;
            # the number on the page must not lie.
            if len(pairs) > _PAIRS_CAP * 2:
                pairs.sort(key=lambda p: -p[0])
                del pairs[_PAIRS_CAP:]
                capped = True
    pairs.sort(key=lambda p: -p[0])
    if len(pairs) > _PAIRS_CAP:
        del pairs[_PAIRS_CAP:]
        capped = True
    result = {"key": key, "pairs": pairs, "hist": hist, "n": n, "capped": capped,
              "total_pairs": n * (n - 1) // 2, "info": info, "no_vectors": False}
    with _sim_lock:
        _sim_cache.update(result)
    return result


# ---------------------------------------------------------
# Similarity check
# ---------------------------------------------------------
async def api_loci_similar(request: Request) -> Response:
    from starlette.responses import JSONResponse
    try:
        th = float(request.query_params.get("threshold") or _SIM_DEFAULT)
    except (TypeError, ValueError):
        th = _SIM_DEFAULT
    th = max(_SIM_FLOOR, min(99.9, th))
    try:
        limit = int(request.query_params.get("limit") or 120)
    except (TypeError, ValueError):
        limit = 120
    try:
        data = await _compute_pairs()
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

    out = []
    for score, a, b in data.get("pairs", []):
        if score < th:
            break            # pairs is already sorted by descending score, so stop once below the line
        if a not in info or b not in info:
            continue
        out.append({"score": round(score, 1), "a": _side(a), "b": _side(b),
                    # whether the automatic tagger (threshold 80, and not the same ruler
                    # as this page) has already flagged this pair
                    "tagged": b in (info[a].get("tagged") or [])
                              or a in (info[b].get("tagged") or [])})
        if len(out) >= limit:
            break

    counted = sum(1 for score, a, b in data.get("pairs", []) if score >= th)
    return JSONResponse({
        "threshold": th,
        "default": _SIM_DEFAULT,
        "floor": _SIM_FLOOR,
        "pairs": out,
        "matched": counted,
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

    keep = do nothing (both stay; WARNING: it merely stops showing in this page session,
           nothing is written to disk, and a refresh brings it back — stated honestly
           here because it was not obvious)
    sink = sink one: go through trace(delete=True), a soft delete into the archive that a
           direct id lookup always recovers.

    WARNING: **both ends of the pair must be supplied, and the server verifies for itself
    that the pair really exists.** Accepting a single `id` and calling
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
        return JSONResponse({"ok": True, "action": "keep", "persisted": False})
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
        data = await _compute_pairs()
        info = data.get("info", {})
        # 1. Both ends must be buckets that really exist and are visible on this page.
        if a not in info or b not in info:
            return JSONResponse(
                {"error": "这一对里有一端不在相似度页上（可能已归档、已换版或是情绪种子）"},
                status_code=409)
        # 2. This pair must actually have been computed (order does not matter).
        hit = any((x == a and y == b) or (x == b and y == a)
                  for _s, x, y in data.get("pairs", []))
        if not hit:
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
        with _sim_lock:
            _sim_cache["key"] = None      # one entry sank, so recompute next time
        return JSONResponse({"ok": True, "action": "sink", "id": bucket_id,
                             "msg": msg})
    except Exception as e:
        logger.warning(f"[loci] 裁决失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
