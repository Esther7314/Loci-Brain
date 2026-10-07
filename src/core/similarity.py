"""
========================================
core/similarity.py — suspected duplicates: every vector against every other
========================================

The similarity page (web/loci_similar.py) asks one question: which pairs of live
memories score close enough that they may be the same thing written twice. This file
reads the vector store, computes the pairwise cosine once, and keeps the result until
the store or any bucket's metadata changes, so dragging the threshold slider never
recomputes anything. The pair cache, the bucket-directory revision cache and their lock
live here, beside the code that fills and reads them; `invalidate()` is how a verdict
on the page drops them.

The caller hands in the bucket manager and the buckets directory it reads at call
time; nothing here reaches for a locator.

Exports: SIM_DEFAULT / SIM_FLOOR / PAIRS_CAP · emb_db_path(buckets_dir) ·
         stored_ids(buckets_dir) · buckets_rev(buckets_dir) · load_vectors(buckets_dir) ·
         visible(meta) · pairs(bucket_mgr, buckets_dir) · above(data, threshold, limit, skip) ·
         has_pair(data, a, b) · invalidate()
========================================
"""

import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from typing import Callable

from . import _fold as _F

# The default is 88. Below 85 the thirteen emotional-root seeds start mixing in, and those
# are supposed to resemble each other.
SIM_DEFAULT = 88.0
SIM_FLOOR = 60.0       # pairs scoring below this are not even computed, to keep hundreds of thousands of them out of memory
# Memory gate: at most this many pairs are kept. At 628 entries that is currently ~29,000
# pairs; the same ~15% ratio at ten thousand entries would be 7.5 million tuples, roughly
# 0.7 GB.
PAIRS_CAP = 200_000

_sim_lock = threading.Lock()
_sim_cache: dict = {"key": None, "pairs": [], "hist": [], "n": 0, "total_pairs": 0}

_rev_cache: dict = {"at": 0.0, "val": (0, 0.0)}
_REV_TTL = 2.0          # seconds. While the slider is being dragged, do not walk nine hundred files on every tick.


def emb_db_path(buckets_dir: str) -> str:
    return os.path.join(buckets_dir, "embeddings.db")


def stored_ids(buckets_dir: str) -> set:
    """Every bucket id the vector store holds a row for (the health check's coverage)."""
    con = sqlite3.connect(f"file:{emb_db_path(buckets_dir)}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("select bucket_id from embeddings")}
    finally:
        con.close()


def buckets_rev(buckets_dir: str) -> tuple:
    """The bucket directory's "version": file count plus the most recent modification time.

    Why the cache key cannot be `embeddings.db` alone: changing only name / room / tags /
    importance / domain **does not touch the vector store**. Tag an entry `__seed__`, for
    instance — it should vanish from this page — and it stays in the cached pairs, with a
    stale name and summary on its card. But every one of those changes does rewrite that
    bucket's .md.

    WARNING: **this must recurse.** There is another level below `dynamic/`, and a top-level
    listdir sees only 117 files where there are really more than 900 — which means missing
    nine tenths of all changes, so the fix would be no fix at all.
    """
    now_s = time.monotonic()
    if now_s - _rev_cache["at"] < _REV_TTL:
        return _rev_cache["val"]

    root = str(buckets_dir or "")
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


def load_vectors(buckets_dir: str) -> tuple[list, object]:
    """Read every vector from embeddings.db; returns (ids, the normalized matrix)."""
    import numpy as np
    ids, vecs = [], []
    con = sqlite3.connect(f"file:{emb_db_path(buckets_dir)}?mode=ro", uri=True)
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


def visible(meta: dict) -> bool:
    """Which buckets appear on the similarity page.

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
    if (_F.is_covered(meta)
            or meta.get("tombstone") or meta.get("deleted_at")):
        return False
    return True


async def pairs(bucket_mgr, buckets_dir: str) -> dict:
    """Compute pairwise cosine across the whole store once, and cache it until
    embeddings.db or the bucket directory changes.

    Returns {key, pairs: [(score 0~100, id_a, id_b)] by descending score, hist (20 buckets
    of 5 points), n, capped, total_pairs, info: {id: card fields}, no_vectors}.
    """
    import numpy as np
    try:
        key = (os.path.getmtime(emb_db_path(buckets_dir)), os.path.getsize(emb_db_path(buckets_dir)),
               buckets_rev(buckets_dir))      # a metadata change must invalidate this too
    except OSError:
        key = None

    with _sim_lock:
        if key is not None and _sim_cache["key"] == key:
            return _sim_cache

    all_buckets = await bucket_mgr.list_all(include_archive=False)
    info: dict[str, dict] = {}
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or not visible(meta):
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

    ids, M = load_vectors(buckets_dir)
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
    found = []
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
                for j in np.where(row >= SIM_FLOOR / 100.0)[0]:
                    found.append((float(row[j]) * 100.0, ids[i], ids[int(j)]))
            # The cap. At 628 entries this keeps ~29,000 pairs, which is nothing; but the
            # same ratio at ten thousand entries is 7.5 million tuples, roughly 0.7 GB, and
            # the process would not survive it.
            # Only the most similar pairs are kept — duplicate review reads from the top
            # score downwards anyway, and nobody will ever scroll through the millions of
            # pairs sitting around 60.
            # WARNING: the cost is that once the cap trips, dragging the threshold very low
            # shows fewer pairs than really exist. So a `capped` flag is returned below;
            # the number on the page must not lie.
            if len(found) > PAIRS_CAP * 2:
                found.sort(key=lambda p: -p[0])
                del found[PAIRS_CAP:]
                capped = True
    found.sort(key=lambda p: -p[0])
    if len(found) > PAIRS_CAP:
        del found[PAIRS_CAP:]
        capped = True
    result = {"key": key, "pairs": found, "hist": hist, "n": n, "capped": capped,
              "total_pairs": n * (n - 1) // 2, "info": info, "no_vectors": False}
    with _sim_lock:
        _sim_cache.update(result)
    return result


def above(data: dict, threshold: float, limit: int,
          skip: Callable[[str, str], bool] | None = None) -> tuple[list, int]:
    """The pairs at or above `threshold` whose two ends are both on the page, at most
    `limit` of them -> (those (score, a, b) pairs, how many pairs score at or above the line
    in all). `skip(a, b)` true leaves a pair out of both (the pairs the owner kept,
    core/similar_kept.py)."""
    info = data.get("info") or {}
    out = []
    for score, a, b in data.get("pairs", []):
        if score < threshold:
            break            # pairs is already sorted by descending score, so stop once below the line
        if a not in info or b not in info:
            continue
        if skip is not None and skip(a, b):
            continue
        out.append((score, a, b))
        if len(out) >= limit:
            break
    counted = sum(1 for score, a, b in data.get("pairs", [])
                  if score >= threshold and not (skip is not None and skip(a, b)))
    return out, counted


def has_pair(data: dict, a: str, b: str) -> bool:
    """Whether this pair was actually computed (order does not matter)."""
    return any((x == a and y == b) or (x == b and y == a)
               for _s, x, y in data.get("pairs", []))


def invalidate() -> None:
    """Drop the pair cache: something on the page changed the store, so the next read
    recomputes."""
    with _sim_lock:
        _sim_cache["key"] = None
