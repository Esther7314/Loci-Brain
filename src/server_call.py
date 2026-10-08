# -*- coding: utf-8 -*-
"""
========================================
server_call.py — the wrapper around every MCP tool call
========================================

Every tool face (server_tools/<tool>.py) hands its coroutine to `_with_notice`, which:
resolves who is calling (the request's host, read scope and write key, core/scope.py) and
refuses a call that may not run; logs the three phases (entry / ok / err); collects the
call's W/I notices; turns an exception into the readable OB-E004 string; and appends a
poke from the panel's muse page waiting to be said (core/_nudge.py).

server.py binds the MCP instance (its request context says who is calling) and the store
(the poke line reads it) with bind(), before any tool is mounted, and re-exports
`_with_notice` for callers that reach it on server.
========================================
"""

import logging
import os
from typing import Awaitable

from core.errors import (
    begin_warnings,
    format_error,
    format_warnings_suffix,
    pop_warnings,
    record_error,
)

logger = logging.getLogger("loci_brain")

# Bound once by server.py (bind), before any tool is mounted.
mcp = None
bucket_mgr = None


def bind(mcp_instance, store) -> None:
    """Called once by server.py: the MCP instance whose request context says who is
    calling, and the store the poke line reads."""
    global mcp, bucket_mgr
    mcp, bucket_mgr = mcp_instance, store


# =============================================================
# Structured operation-log helpers.
# Every MCP tool entry point logs the same three phases — entry, ok, err — which is what
# makes client-side invalid_arguments reports and silent failures diagnosable.
# Output format: op=<name> phase=entry|ok|err key=value...
# Any field that might contain PII (memory content) is logged as a length only,
# never as content.
# =============================================================
def _fmt_log_val(v: object) -> str:
    """Safe formatting for a log value: bool/int/float as-is; str truncated to 40
    characters with newlines stripped; anything else str()'d."""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.replace("\n", "\\n").replace(" ", "_")
        return s if len(s) <= 40 else s[:37] + "..."
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """Join an args dict into a `k1=v1 k2=v2` string."""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _log_op_err(op: str, exc: BaseException) -> None:
    # .exception puts the traceback into server.log, which is what makes it findable later
    logger.exception(f"op={op} phase=err err={type(exc).__name__}:{exc}")


# =============================================================
# Who is calling: the request's host, read scope and write key (core/scope.py)
# -------------------------------------------------------------
# Under streamable-http the tools run in the session's own task, so nothing set in the
# ASGI middleware reaches them; the request that carried this call does (FastMCP's
# request context). The middleware leaves the host it recognised in the ASGI scope
# (server_app.MCPAuthMiddleware); `Loci-Scope` and `Loci-Turn` are read from the same
# request here, once per call, and set for the call inside `_with_notice`. Under stdio
# there is no request: `LOCI_SCOPE` and `LOCI_HOST_TOKEN` are read once, at start.
# =============================================================
from core import scope as _scope_mod
from core import _sources as _src_mod

_READ_OPS = frozenset({"breath", "recall", "muse"})
_WRITE_OPS = frozenset({"grow", "fold", "regrow", "trace"})
_STDIO_SCOPE = os.environ.get(_scope_mod.SCOPE_ENV)
_STDIO_HOST_TOKEN = str(os.environ.get(_scope_mod.HOST_TOKEN_ENV) or "").strip()


def _hosts():
    from web import panel_auth as _pa
    return _pa.hosts()


def _call_request():
    """The HTTP request carrying this tool call: a Starlette Request, None under stdio,
    or False outside any MCP call (a direct call: nothing is resolved)."""
    try:
        return mcp.get_context().request_context.request
    except (LookupError, ValueError, AttributeError):
        return False


def _resolve_call() -> tuple["_scope_mod.RequestScope | None", str | None, str]:
    """(the request resolved, the write key, why the write key cannot be read)."""
    request = _call_request()
    if request is False:
        return None, None, ""
    hosts = _hosts()
    if request is None:
        host = hosts.by_token(_STDIO_HOST_TOKEN) if _STDIO_HOST_TOKEN else hosts.default
        req = _scope_mod.RequestScope.resolve(
            host, _STDIO_SCOPE,
            no_host=(f"{_scope_mod.HOST_TOKEN_ENV} 对不上 hosts 表里任何一个宿主"
                     if _STDIO_HOST_TOKEN else "没带宿主凭据，部署里也没有 legacy 宿主"))
        return req, None, ""
    headers = request.headers
    rs = getattr(request, "scope", {}) or {}
    if "loci.host" in rs:
        host = hosts.get(rs["loci.host"]) if rs["loci.host"] else None
    else:
        host = hosts.default
    req = _scope_mod.RequestScope.resolve(host, headers.get(_scope_mod.SCOPE_HEADER))
    turn = headers.get(_scope_mod.TURN_HEADER)
    if turn is None or not str(turn).strip():
        return req, None, ""
    try:
        t, n = _scope_mod.parse_turn(turn)
    except _scope_mod.ScopeError as e:
        return req, None, e.zh
    return req, _src_mod.write_key(t, n, host.name if host is not None else ""), ""


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """The wrapper around every MCP tool call.

    Responsibilities, per the unified error convention:
    1. On entry: begin_warnings() initializes this call's W/I channel, and the request
       is resolved (who is calling, what it may read, its write key) and set for the call.
    2. On exit: the concatenation order is [scope line, on a read] + [deletion notice] +
       [tool output] + [the W/I notices this call produced] + [a poke from the panel's
       muse page waiting to be said, said once (core/_nudge.py)].
    3. On exception: catch it, record OB-E004, and return the standard format (including
       the last 15 log lines), so the MCP protocol layer never sees a bare exception string.
    4. When op is non-empty, emit the structured log at all three points.

    A refused request (a restricted host without a scope, a scope past its host's
    ceiling or unreadable, no host at all) runs nothing: a read gets the refusal line, a
    write the same line and that nothing was written. A Loci-Turn that cannot be read
    refuses a write the same way: a write the host meant to key is never run unkeyed.
    """
    try:
        req, key, turn_err = _resolve_call()
    except Exception as e:                      # noqa: BLE001 — never run a call unresolved
        coro.close()
        logger.exception(f"op={op} phase=refused err=resolve:{type(e).__name__}:{e}")
        return f"❌ 这次请求认不出是谁、能读什么（{type(e).__name__}），什么都没做。"
    if req is not None and op in (_READ_OPS | _WRITE_OPS):
        refusal = ""
        if req.refused:
            refusal = req.first_line() + ("\n这次什么都没写。" if op in _WRITE_OPS else "")
        elif turn_err and op in _WRITE_OPS:
            refusal = f"{turn_err}。这次什么都没写。"
        if refusal:
            coro.close()
            if op:
                logger.info(f"op={op} phase=refused scope={req.refusal or 'turn'}")
            return refusal
    if req is None:
        return await _run_with_notice(coro, op, args)
    with _scope_mod.request_scope(req), _src_mod.write_key_scope(key):
        body = await _run_with_notice(coro, op, args)
    return f"{req.first_line()}\n{body}" if op in _READ_OPS else body


async def _run_with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004: an MCP tool raised. Do not swallow it; hand the model a string it can
        # actually read.
        try:
            record_error("OB-E004", f"{type(e).__name__}: {e}")
            err_str = format_error("OB-E004", f"{type(e).__name__}: {e}")
        except Exception:
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{type(e).__name__}: {e}"
        # Still append whatever notices accumulated in the channel.
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        return err_str + extras
    # The normal path
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    body = result + extras if extras else result
    return body + await _nudge_lines()


async def _nudge_lines() -> str:
    """The poke line: a poke from the panel's muse page, said once, at the end of the next
    tool reply that may see the whole cluster (core/_nudge.py). Never fails the call."""
    from core import _nudge, _when
    from tools._common import read_scope
    try:
        lines = await _nudge.take_lines(bucket_mgr.base_dir, bucket_mgr, read_scope,
                                        _when.now())
    except Exception as e:                      # noqa: BLE001
        logger.warning(f"[nudge] could not hand out the poke line: {type(e).__name__}: {e}")
        return ""
    return "".join(f"\n\n{line}" for line in lines)
