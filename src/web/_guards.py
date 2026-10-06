"""
========================================
web/_guards.py — the checks the panel's write routes and the host routes run themselves
========================================

The panel gate is wrapped onto every route at registration (web/__init__ `_Gated`). What
lives here is what a route still has to check on its own:

- the same-origin check every panel write runs before it reads its body
  (`_origin_reject`, `_write_body`). web/library_api.py and web/import_api.py use it too,
  through web.loci;
- the hook request's scope as the guard resolved it, and what a refused or scoped request
  gets back (`_request_of`, `_scope_refusal`, `_scope_withholds`).
========================================
"""

from starlette.requests import Request


def _origin_reject(request: Request) -> str:
    """Same-origin check. An empty string means allow; anything else is the reason to refuse.

    Why a cookie alone is not enough: `SameSite=Lax` only blocks **cross-site** requests,
    not **same-site cross-origin** ones. A page on another port of the same host counts as
    the same site, so if it posts valid JSON as `text/plain` the browser attaches the cookie
    anyway. These two write endpoints therefore have to check Origin themselves.

    A missing Origin header is always refused: a browser always sends one on a POST, so its
    absence means the request did not come from a browser (curl, a script). These endpoints
    exist only for buttons on the page, so refusing is correct.
    """
    origin = request.headers.get("origin") or ""
    host = request.headers.get("host") or ""
    if not origin:
        return "缺 Origin 头（这个写口只接受页面上的按钮）"
    if not host:
        return "缺 Host 头"
    from urllib.parse import urlsplit
    try:
        o = urlsplit(origin)
    except ValueError:
        return f"Origin 解析不了：{origin}"
    if o.scheme not in ("http", "https"):
        return f"Origin 的协议不对：{origin}"
    # netloc includes the port, so "another port on the same machine" is caught here too —
    # which is exactly what needs catching
    if not o.netloc or o.netloc != host:
        return f"Origin 和 Host 对不上：{o.netloc} ≠ {host}"
    return ""


async def _write_body(request: Request) -> dict:
    """Body reading for write endpoints: verify the origin and the Content-Type first, then
    parse.

    Raising `PermissionError` means "answer 403"; raising `ValueError` means "answer 400",
    so malformed JSON never bubbles all the way out as a 500.
    """
    why = _origin_reject(request)
    if why:
        raise PermissionError(why)
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        raise ValueError(f"Content-Type 必须是 application/json（收到 {ct or '空'}）")
    try:
        body = await request.json()
    except Exception:
        raise ValueError("body 不是合法 JSON")
    if not isinstance(body, dict):
        raise ValueError("body 必须是一个 JSON 对象")
    return body


def _request_of(request: Request):
    """The hook request as the guard resolved it (web/__init__ `_Gated`, core/scope.py);
    None outside the guard (a route called directly)."""
    return getattr(request.state, "loci_request", None)


def _scope_refusal(request: Request):
    """A refused request (no scope from a restricted host, a scope past its host's
    ceiling, one that cannot be read): the response saying so, else None. Nothing is read."""
    from starlette.responses import JSONResponse
    from core import scope as _scope
    req = _request_of(request)
    if req is None or not req.refused:
        return None
    line = req.first_line()
    return JSONResponse({"error": line, "scope": line},
                        status_code=400 if req.refusal == _scope.MALFORMED else 403)


def _scope_withholds(request: Request, what: str):
    """A road this version cannot filter by scope gives nothing to a scoped request, and
    says so: the response, else None (a refused request gets its refusal)."""
    from starlette.responses import JSONResponse
    from core import scope as _scope
    refused = _scope_refusal(request)
    if refused is not None:
        return refused
    req = _request_of(request)
    if req is None or req.whole_library:
        return None
    return JSONResponse({"scope": _scope.unsupported_line(what)})
