"""
========================================
web/loci_present.py — the panel's present page, asked of the gateway through Loci
========================================

The present layer (rolling window, day report, wake, push) lives in a gateway, not in
Loci; its settings and status are the gateway's. The panel talks to Loci alone (one
login, one origin), so Loci forwards the present page's requests to the host whose
`hosts.<name>.present_url` names a gateway (core/scope.py):

    GET  /api/loci/present            -> GET  <present_url>/present
    POST /api/loci/present            -> POST <present_url>/present
    POST /api/loci/present/compress   -> POST <present_url>/present/compress
    POST /api/loci/present/report     -> POST <present_url>/present/report
    POST /api/loci/present/push-test  -> POST <present_url>/present/push-test
    GET  /api/loci/present/prompts    -> GET  <present_url>/present/prompts
    POST /api/loci/present/prompts    -> POST <present_url>/present/prompts

Every other path or method under /api/loci/present is a 404: the list above is the whole
allowlist, matched exactly, so the panel cannot be made to reach any other path of the
gateway. All of them are the panel's alone, behind its gate (web/__init__ `_Gated`); a
POST reads its body through `_guards._write_body` (same origin, `application/json`).

Which host: `host` (the query on a GET, the body on a POST; taken off before forwarding)
names it, and may be left out when exactly one host has a `present_url`. No host has
one -> `{"connected": false}` (the page shows 「还没接上」). Several and none named -> 400
with their names.

The credential: Loci's own toward that host, the one it fetches originals with
(`fetch_token_env`, `Host.fetch_token`), as `Authorization: Bearer`. The gateway takes
the same key on `/loci/source` and `/present/*`. A host with no such credential is not
asked.

The gateway's reply passes through as it came: its status and its JSON body, byte for
byte, with `Cache-Control: no-store`. Nothing is cached and no body is logged, either
way. What does not pass through, because it is not the gateway speaking for the page:
    the gateway cannot be reached · timed out · its reply is over MAX_REPLY_BYTES or is
      not JSON · it redirects (never followed)
    it refuses Loci's key (401 / 403), which passed on would read to the panel as its own
      login expiring
Each of these is `{"connected": false, "host": <name>, "error": <why>}`, 200 on a GET (the
page shows it in place) and 502 on a POST (the button did not do its work). The error
never carries the URL: a gateway on a non-loopback bind keeps a passphrase in its path.

Proxy settings from the environment are not used, so the request goes where
`present_url` says and nowhere else.

Exports: PRESENT_PATHS · TIMEOUT_SECONDS · MAX_REPLY_BYTES · api_loci_present
========================================
"""

import asyncio
import json

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from . import panel_auth
from ._guards import _write_body

logger = sh.logger

# (method, path after /present): the whole allowlist.
PRESENT_PATHS = frozenset([
    ("GET", ""),
    ("POST", ""),
    ("POST", "/compress"),
    ("POST", "/report"),
    ("POST", "/push-test"),
    ("GET", "/prompts"),
    ("POST", "/prompts"),
])

# Every gateway route here answers at once (a write only queues its work), so a slow
# answer is a gateway in trouble, and the panel should say so rather than hang.
TIMEOUT_SECONDS = 5.0
# The largest status reply carries one day report in full; this is far above that.
MAX_REPLY_BYTES = 2 * 1024 * 1024

# An httpx transport the tests put in place of the network; None = the real one.
_transport = None


class _TooBig(Exception):
    pass


def _not_connected(method: str, host_name: str, why: str) -> JSONResponse:
    return JSONResponse({"connected": False, "host": host_name, "error": why},
                        status_code=200 if method == "GET" else 502,
                        headers={"Cache-Control": "no-store"})


def _hosts_with_present() -> list:
    hs = panel_auth.hosts()
    return [h for h in hs.hosts.values() if h.present_url]


async def _forward(method: str, url: str, token: str, query: list,
                   body) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS),
                                 follow_redirects=False, trust_env=False,
                                 transport=_transport) as client:
        kwargs = {"headers": headers, "params": query or None}
        if method == "POST":
            kwargs["json"] = body
        async with client.stream(method, url, **kwargs) as resp:
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_REPLY_BYTES:
                raise _TooBig
            got = bytearray()
            async for chunk in resp.aiter_bytes():
                got.extend(chunk)
                if len(got) > MAX_REPLY_BYTES:
                    raise _TooBig
            return resp.status_code, bytes(got)


async def api_loci_present(request: Request) -> Response:
    method = request.method.upper()
    rest = request.path_params.get("rest")
    sub = "" if rest is None else "/" + str(rest)
    if (method, sub) not in PRESENT_PATHS:
        return JSONResponse({"error": "present 没有这个口"}, status_code=404)

    query: list = []
    body = None
    if method == "POST":
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        name = body.pop("host", None)
    else:
        name = request.query_params.get("host")
        query = [(k, v) for k, v in request.query_params.multi_items() if k != "host"]
    if name is not None and not isinstance(name, str):
        return JSONResponse({"error": "host 是宿主的名字（字符串）"}, status_code=400)
    name = (name or "").strip()

    candidates = _hosts_with_present()
    if not candidates:
        return JSONResponse({"connected": False}, headers={"Cache-Control": "no-store"})
    names = sorted(h.name for h in candidates)
    if name:
        host = next((h for h in candidates if h.name == name), None)
        if host is None:
            return JSONResponse({"connected": False, "error": f"没有叫 {name} 的宿主配了 "
                                 "present_url", "hosts": names}, status_code=404)
    elif len(candidates) == 1:
        host = candidates[0]
    else:
        return JSONResponse({"error": "好几个宿主配了 present_url，要带 host 说是哪一个",
                             "hosts": names}, status_code=400)

    if not host.fetch_token:
        return _not_connected(method, host.name, (
            f"Loci 对宿主 {host.name} 没有钥匙：hosts.{host.name}.fetch_token_env 要写上一个"
            "环境变量的名字，值跟网关的 LOCI_GATEWAY_TOKEN 一样（跟这个宿主自己的 token_env "
            "不能是同一个值）"))

    url = host.present_url + "/present" + sub
    try:
        status, raw = await asyncio.wait_for(
            _forward(method, url, host.fetch_token, query, body),
            timeout=TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        why = f"网关 {TIMEOUT_SECONDS:g} 秒内没回话"
    except _TooBig:
        why = f"网关的回执超过 {MAX_REPLY_BYTES // (1024 * 1024)} MiB，没收"
    except httpx.HTTPError as e:
        # The class name only: an httpx message can carry the URL, and the URL can
        # carry the gateway's passphrase.
        why = f"连不上网关（{type(e).__name__}），看看它起了没有、present_url 对不对"
    else:
        why = ""
        if 300 <= status < 400:
            why = f"网关要跳转（HTTP {status}），Loci 不跟"
        elif status in (401, 403):
            why = (f"网关不认 Loci 的钥匙（HTTP {status}）：hosts.{host.name}.fetch_token_env "
                   "那个环境变量的值要跟网关的 LOCI_GATEWAY_TOKEN 一样")
        else:
            try:
                json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                why = f"网关回的不是 JSON（HTTP {status}）"
        if not why:
            return Response(content=raw, status_code=status, media_type="application/json",
                            headers={"Cache-Control": "no-store"})
    logger.warning(f"[present] {method} /present{sub} on host {host.name}: {why}")
    return _not_connected(method, host.name, why)
