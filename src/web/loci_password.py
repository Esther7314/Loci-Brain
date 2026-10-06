"""
========================================
web/loci_password.py — this panel's own password
========================================

    GET  /api/loci/auth/state         -> where the password currently lives, and whether one needs setting
    POST /api/loci/auth/set-password  -> sets the password guarding remote MCP access

Both paths are on the panel gate's allowlist (web/panel_auth.py).
========================================
"""

import os

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from ._guards import _write_body

logger = sh.logger


# ---------------------------------------------------------
# This panel's own password — the point being to depend on no other panel at all.
# ---------------------------------------------------------
async def api_loci_auth_state(request: Request) -> Response:
    """Where this password currently lives and whether one needs to be set. Public, and
    carries no information about the password itself.

    WARNING: there is no `authed` field: /api/* is not authenticated at this layer, and
    there is no such thing as a session here. This password governs exactly one thing:
    whether the remote MCP OAuth authorization page (bridge/oauth.py) accepts you. It does
    not govern access to this panel screen.
    """
    from starlette.responses import JSONResponse
    try:
        has_file_password = sh._load_password_hash() is not None
    except sh.AuthPersistenceError as e:
        # An unreadable auth file is **not** "no password here". This route is public
        # and the gate overlay reads it; reporting "nothing set" would tell the owner
        # (and anyone else) that the panel is free to initialize.
        logger.error(f"[loci] auth 存储损坏，读不出口令 hash：{e}")
        has_file_password = True
    return JSONResponse({
        "setup_needed": sh._is_setup_needed(),
        # True = the password still comes from an environment variable, so it cannot be
        # changed here and the security question is unavailable
        "env_locked": bool(os.environ.get("LOCI_DASHBOARD_PASSWORD", "")),
        "has_file_password": has_file_password,
        "question": str(sh._load_auth_data().get("security_question") or ""),
    }, headers={"Cache-Control": "no-store"})


async def api_loci_set_password(request: Request) -> Response:
    """Write the password into a file (`.dashboard_auth.json`), taking over from the
    environment variable.

    Why this needed its own endpoint: both of the official routes were dead here.
      - `/auth/change-password` refuses outright while the environment variable exists.
      - `/auth/setup` accepts loopback only, but a request forwarded through Docker
        arrives with a container-network client IP, so **even opening localhost on your
        own machine gets a 403**.
    So a file-based password has to exist before the environment variable can be removed,
    or nobody can get in at all.

    WARNING: there are no cookie sessions (/api/* is not authenticated at this layer),
    **and this gate must not be loose for that reason** — this password is not the
    panel's door, it is the door to the remote MCP OAuth authorization page
    (bridge/oauth.py), and taking it over means being handed an MCP token that reads and
    writes every memory.
    The gate does not depend on a session: **first-time setup**
    (`_is_setup_needed()`, meaning neither the file nor the environment holds a password)
    is allowed through; **when a password already exists**, the body must carry the
    correct `current_password`, verified through the same `_verify_password_for_rotation`
    the OAuth page uses, behind the same login rate limiting
    (`_login_retry_after` / `_reserve_global_login_attempt`), with a compare-and-swap
    write-back against concurrent changes. Hashing and persistence all go through
    _shared; no cryptography is implemented here.
    **The password travels from the browser straight into this endpoint and passes
    through no one's hands on the way.**
    """
    from starlette.responses import JSONResponse
    try:
        body = await _write_body(request)   # same-origin check plus Content-Type
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    pw = body.get("password", "")
    if not isinstance(pw, str):
        return JSONResponse({"error": "密码得是字符串"}, status_code=400)
    pw = pw.strip()
    if not 6 <= len(pw) <= 1024:
        return JSONResponse({"error": "密码 6~1024 位"}, status_code=400)

    proof = None
    if not sh._is_setup_needed():
        # A password is already standing guard: changing it requires proving knowledge of
        # the old one. There is no session to fall back on any more.
        retry = sh._login_retry_after(request)
        if retry:
            return JSONResponse({"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                                status_code=429, headers={"Retry-After": str(retry)})
        global_retry = sh._reserve_global_login_attempt()
        if global_retry:
            return JSONResponse({"error": f"登录服务繁忙，请 {global_retry} 秒后重试"},
                                status_code=429, headers={"Retry-After": str(global_retry)})
        current = body.get("current_password", "")
        if not isinstance(current, str) or len(current) > 1024:
            sh._record_login_failure(request)
            return JSONResponse({"error": "current_password 格式无效"}, status_code=400)
        verified, queued_retry = await sh._run_public_password_verification(
            request, sh._verify_password_for_rotation, current
        )
        if queued_retry:
            return JSONResponse({"error": f"尝试过于频繁，请 {queued_retry} 秒后再试"},
                                status_code=429, headers={"Retry-After": str(queued_retry)})
        if not verified:
            sh._record_login_failure(request)
            return JSONResponse({"error": "当前密码不对"}, status_code=401)
        sh._record_login_success(request)
        proof = verified  # CredentialProof, used for the compare-and-swap write-back against a concurrent change
    try:
        # PBKDF2 blocks the event loop for roughly 100ms. This is a button pressed a
        # handful of times in a lifetime, so that is accepted rather than introducing a
        # thread pool for it. The high-frequency login path uses the
        # _password_work_semaphore in web/_shared.py instead.
        if proof is not None:
            ok = sh._save_password_hash(
                pw, expected_hash=proof.value, expected_generation=proof.generation,
            )
        else:
            ok = sh._save_password_hash(pw)
    except Exception as e:
        logger.warning(f"[loci] 存密码失败: {e}")
        return JSONResponse({"error": f"写不进去：{e}"}, status_code=500)
    if not ok:
        return JSONResponse({"error": "写不进去（并发改动？再试一次）"}, status_code=409)
    return JSONResponse({
        "ok": True,
        "env_locked": bool(os.environ.get("LOCI_DASHBOARD_PASSWORD", "")),
        "next": ("密码已经存进文件了。现在去启动 Loci 的地方（docker-compose 文件，"
                 "或者你设环境变量的地方）删掉 LOCI_DASHBOARD_PASSWORD、重启，"
                 "新密码才真正接管（环境变量还在的时候它优先）。"),
    })
