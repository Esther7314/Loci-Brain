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

And a second seam, the transport: a call step may carry what a host puts on its HTTP
request — its credential (`host:`), its read scope (`scope:`), its turn (`turn:` or
`write_key:`). Over stdio there is no HTTP request, so the runner sends those headers in
the call's _meta (`loci_headers`) and this file makes them the request the call carries,
exactly where FastMCP puts an HTTP request (`request_context.request`). Which host the
credential names is decided by the function the HTTP middleware runs
(server_app.identify_host). From there on the call takes the real path: src/server.py
resolves host, scope and write key from that request, as it does under streamable-http.
What the exam does not run is the HTTP transport and MCPAuthMiddleware around it
(tests/test_read_scope.py drives those through an ASGI client).

And a third: an item's `side_model:` ({phrase: answer}, handed over in EXAM_SIDE_MODEL_FILE)
answers the side model's chat — the backfill's one call — with the answer whose phrase
appears in the request (the entry's body), and "" when none does. Without the file the
chat is untouched: no key, so it answers "".

And a fourth: an item's `weaver:` (EXAM_WEAVER_FILE) answers the one model call weaving a
dream makes, so breath's upkeep weaves with no key (see the end of this file).

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

# The headers a host would send, carried in _meta (`loci_headers`), become the request
# this call carries. Without them nothing changes: the call is a stdio call, which is
# every call that does not ask for it.
import dataclasses  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.lowlevel.server import request_ctx  # noqa: E402
from starlette.requests import Request  # noqa: E402

_call_tool = FastMCP.call_tool


def _request_from(headers: dict) -> Request:
    from server_app import HOST_SCOPE_KEY, identify_host
    from web import panel_auth

    raw = [(str(k).lower().encode("latin-1"), str(v).encode("utf-8"))
           for k, v in headers.items()]
    hosts = panel_auth.hosts()
    kind, host = identify_host(dict(raw), hosts)
    if kind == "unknown":
        raise PermissionError("401 Unknown host credential (as MCPAuthMiddleware answers)")
    host = host if host is not None else hosts.default
    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": raw,
                    HOST_SCOPE_KEY: host.name if host is not None else ""})


async def _call_tool_as_request(self, name, arguments):
    rc = request_ctx.get()
    headers = getattr(rc.meta, "loci_headers", None) if rc.meta is not None else None
    if not headers:
        return await _call_tool(self, name, arguments)
    token = request_ctx.set(dataclasses.replace(rc, request=_request_from(headers)))
    try:
        return await _call_tool(self, name, arguments)
    finally:
        request_ctx.reset(token)


FastMCP.call_tool = _call_tool_as_request

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

# And a fourth: an item's `weaver:` (the JSON the weaving model answers, handed over in
# EXAM_WEAVER_FILE) stands in for the one model call a dream makes (core/_dream.call_model),
# so breath's upkeep weaves on the fake clock with no key. What is drawn, the pressure line,
# the parse, the record and everything after it are Loci's own. Without the file a dream
# cannot be woven here (no key), and breath's upkeep only sweeps.
_weaver_file = os.environ.get("EXAM_WEAVER_FILE", "").strip()
if _weaver_file:
    import json  # noqa: E402

    from core import _dream  # noqa: E402

    _weaver_answer = json.loads(Path(_weaver_file).read_text(encoding="utf-8"))

    async def _weaver(ingredients, c):
        return _dream.parse_dream(json.dumps(_weaver_answer, ensure_ascii=False))

    _dream.call_model = _weaver

runpy.run_path(str(ROOT / "src" / "server.py"), run_name="__main__")
