"""
========================================
web/loci_password.py — this panel's own password
========================================

    GET  /api/loci/auth/state         -> where the password currently lives, and whether one needs setting
    POST /api/loci/auth/set-password  -> sets the password guarding remote MCP access
    POST /api/loci/auth/security-question -> sets or changes the question the forgot-password
                                         page asks, and its answer
    POST /api/loci/auth/revoke-grants -> takes back every MCP OAuth grant handed out so far

The first two are on the panel gate's allowlist (web/panel_auth.py). The last two are not:
they sit behind the gate like every panel write (a host's credential is refused there), and
ask for a logged-in session besides.
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


# The longest question and answer kept: a sentence each, not a document.
_QA_MAX_CHARS = 200


async def api_loci_security_question(request: Request) -> Response:
    """Set or change the security question (setting page, 账号 · 设置安全问题). Body
    `{question, answer}`; the answer is kept only as a hash (`_save_security_qa`, the
    same hashing as the password, compared lower-cased and trimmed by /auth/recover).

    **A logged-in session, always** — not merely "the gate let it through". The answer
    resets the password, and the password is also the door to the remote MCP OAuth
    authorization page (bridge/oauth.py), which stands whether or not the panel is locked.
    A panel left unlocked (`panel_auth: false`) still has no business handing that door to
    whoever reaches the port; a session proves the password, and a 401 sends the page to
    the login, which works with the switch off too.

    Refused before that: no password yet (the question resets a password, so there must
    be one), and a password held by the environment variable (a reset writes the file,
    which the variable outranks, so the question would reset nothing)."""
    from starlette.responses import JSONResponse
    from . import panel_auth
    try:
        body = await _write_body(request)   # same-origin check plus Content-Type
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if os.environ.get("LOCI_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": ("密码现在由环境变量 LOCI_DASHBOARD_PASSWORD 管着，"
                                       "安全问题重设不了它：先在上面「改密码」把密码存进文件，"
                                       "再删掉那个环境变量、重启")}, status_code=409)
    if sh._is_setup_needed():
        return JSONResponse({"error": "还没有密码：先设一把密码，再设安全问题"},
                            status_code=409)
    if not panel_auth.has_session(request):
        return JSONResponse({"error": "请先登录"}, status_code=401)
    question = body.get("question", "")
    answer = body.get("answer", "")
    if not isinstance(question, str) or not isinstance(answer, str):
        return JSONResponse({"error": "问题和答案得是字符串"}, status_code=400)
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        return JSONResponse({"error": "问题和答案都要填"}, status_code=400)
    if len(question) > _QA_MAX_CHARS or len(answer) > _QA_MAX_CHARS:
        return JSONResponse({"error": f"问题和答案各 {_QA_MAX_CHARS} 字以内"}, status_code=400)
    try:
        sh._save_security_qa(question, answer)
    except Exception as e:                  # noqa: BLE001
        logger.warning(f"[loci] 存安全问题失败: {e}")
        return JSONResponse({"error": f"写不进去：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "question": question, "message": "安全问题存好了"})


async def api_loci_revoke_grants(request: Request) -> Response:
    """Take back every MCP OAuth grant (bridge/oauth.revoke_all_mcp_grants): every access
    and refresh token, on disk and in memory, and any authorization code still in flight.
    Each connected client has to go through the authorization page again. Body `{}`.

    For when a token may have leaked, or a device that was once authorized is gone.
    Changing the password does not do this: grants outlive the password that issued them.
    The static token (`mcp_auth_mode: token`) is a separate credential and is untouched.

    **A logged-in session, always**, for the same reason as the security question: the
    OAuth page stands whether or not the panel is locked, and an unlocked panel must not
    let whoever reaches the port log every client out.

    The revocation is durable or it is not claimed: if the empty grant state cannot be
    written, the answer is a 500 and the grants on disk are still the old ones."""
    from starlette.responses import JSONResponse
    from . import panel_auth
    try:
        await _write_body(request)          # same-origin check plus Content-Type
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not panel_auth.has_session(request):
        return JSONResponse({"error": "请先登录"}, status_code=401)
    from bridge import oauth
    try:
        oauth.revoke_all_mcp_grants()
    except Exception as e:                  # noqa: BLE001
        logger.error(f"[loci] 收回授权失败: {e}")
        return JSONResponse({"error": f"没收回来，授权都还在：{e}"}, status_code=500)
    logger.warning("[loci] 面板收回了全部 MCP 授权")
    return JSONResponse({"ok": True,
                         "message": "授权都收回了：接进来的客户端要重新授权一次才能再用"})
