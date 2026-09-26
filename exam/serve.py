# -*- coding: utf-8 -*-
"""
exam/serve.py — start the real Loci MCP server (stdio) with the exam's fake clock.

The runner launches this instead of src/server.py. Everything the server reads comes
from the environment the runner sets:

    LOCI_BUCKETS_DIR    the throwaway library for this item
    LOCI_CONFIG_PATH    the exam config (no model keys, no embeddings)
    EXAM_CLOCK_FILE     where the fake "now" lives (see clock.py)
    EXAM_SEED           seed for random: breath's "suddenly remembered" and dream picks

One more difference, and it is a declared seam: BM25 is rebuilt before a search when a
write has made it stale. Live Loci rebuilds it in the background and lets that one search
score against the old index; with embeddings off, that would make every item's first
search see an empty index and match whole-query substrings only.

Nothing else differs from a real start: same tools, same argument checks, same output.
"""

import asyncio
import os
import random
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from exam import clock  # noqa: E402

clock.install(Path(os.environ["EXAM_CLOCK_FILE"]))
random.seed(int(os.environ.get("EXAM_SEED", "0")))

import core.bucket_manager as _bm  # noqa: E402

if _bm._BM25Index is None:
    sys.exit("exam: BM25 is unavailable (rank_bm25 / jieba not installed) — "
             "search would silently fall back to whole-query substrings")

_search = _bm.BucketManager.search


async def _search_on_fresh_bm25(self, query, *args, **kwargs):
    if self._bm25 is not None and self._bm25_dirty:
        include_archive = kwargs.get("include_archive", False)
        buckets = await self.list_all(include_archive=include_archive)
        self._bm25 = await asyncio.to_thread(self._build_bm25_index, buckets)
        self._bm25_dirty = False
    return await _search(self, query, *args, **kwargs)


_bm.BucketManager.search = _search_on_fresh_bm25

runpy.run_path(str(ROOT / "src" / "server.py"), run_name="__main__")
