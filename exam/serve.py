# -*- coding: utf-8 -*-
"""
exam/serve.py — start the real Loci MCP server (stdio) with the exam's fake clock.

The runner launches this instead of src/server.py. Everything the server reads comes
from the environment the runner sets:

    LOCI_BUCKETS_DIR    the throwaway library for this item
    LOCI_CONFIG_PATH    the exam config (no model keys; embeddings only from a local
                        Ollama, when EXAM_EMBED_URL is set — see runner.exam_config)
    EXAM_CLOCK_FILE     where the fake "now" lives (see clock.py)
    EXAM_SEED           seed for random: breath's "suddenly remembered" and dream picks

One more difference, and it is a declared seam: the first BM25 build happens before the
first search instead of in the background. Live Loci builds it in the background and lets
that one search score without BM25; with embeddings off, that would make every item's
first search match whole-query substrings only. After that the exam runs what live Loci
runs: a write leaves BM25 dirty and the next search brings it level before scoring.

And a second seam: a call step may carry a write key (`write_key:` in the item), sent in the
request's _meta and set for that call the way the request layer will set it from the
host's turn header.

And a third: an item's `side_model:` ({phrase: answer}, handed over in EXAM_SIDE_MODEL_FILE)
answers the side model's chat — the backfill's one call — with the answer whose phrase
appears in the request (the entry's body), and "" when none does. Without the file the
chat is untouched: no key, so it answers "".

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
    if self._bm25 is not None and not self._bm25.built:
        buckets = await self.list_all()
        self._bm25 = await asyncio.to_thread(self._build_bm25_index, buckets)
    return await _search(self, query, *args, **kwargs)


_bm.BucketManager.search = _search_on_fresh_bm25

# A write key per call: the runner sends it in the request's _meta (`loci_write_key`), and
# it is set for that one call the way the request layer will set it from the host's turn.
# Without one nothing changes, which is every call that does not ask for it.
from core import _sources as _src  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

_call_tool = FastMCP.call_tool


async def _call_tool_under_write_key(self, name, arguments):
    try:
        meta = self.get_context().request_context.meta
    except (LookupError, ValueError, AttributeError):
        meta = None
    key = getattr(meta, "loci_write_key", None) if meta is not None else None
    with _src.write_key_scope(key):
        return await _call_tool(self, name, arguments)


FastMCP.call_tool = _call_tool_under_write_key

_side_model_file = os.environ.get("EXAM_SIDE_MODEL_FILE", "").strip()
if _side_model_file:
    import json  # noqa: E402

    from core.dehydrator import Dehydrator  # noqa: E402

    _side_answers = json.loads(Path(_side_model_file).read_text(encoding="utf-8"))

    async def _side_model_chat(self, system, user, **_kwargs):
        for phrase, answer in _side_answers.items():
            if phrase in user:
                return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return ""

    Dehydrator._chat = _side_model_chat

runpy.run_path(str(ROOT / "src" / "server.py"), run_name="__main__")
