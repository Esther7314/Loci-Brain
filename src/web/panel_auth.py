"""
========================================
web/panel_auth.py — the panel's gate
========================================

The strip-down removed the whole cookie-session family along with web/auth.py, on the
grounds that a machine on a trusted home LAN does not need authentication. That holds for
**one particular machine**; it does not hold for **software handed to other people** —
someone will put the port on the public internet. And the `#gate` overlay only appears
when an API returns 401, so with no authentication nothing ever returns 401 and the gate
never shows up again: **it looks like there is a lock, and there is not.**

So this module puts the gate back. **No wheels were reinvented**: password hashing, rate
limiting, security-question recovery and atomic persistence are all alive and well in
`_shared.py`. Only two things are added here — four routes, and a signed cookie.

Three security rules (read these before changing anything):

1. **No password set means no lock.** A fresh install must be usable the moment it opens;
   locking a door before a key exists locks the owner out of their own house. (That has
   actually happened once, for twenty minutes.)
2. **The session key is derived from the password hash** rather than stored in a file of
   its own: `sha256("loci-panel-v1:" + hash)`. That buys one thing for free — **changing
   the password invalidates every existing session**, with no separate revocation
   machinery to write.
3. **The bridge-facing endpoints do not pass through this gate** (dream/wake,
   muse/pending, dream/current, poke): the caller is another process, not a browser. It
   has no cookies and should not have any.

The switch is `panel_auth` in config.yaml, default **true** — the build that ships should
be the locked one. A deployment that really is confined to a trusted LAN can set it to
false explicitly; the reasoning has not changed, it just only applies inside that network.

The bridge-facing routes are where hosts come in (core/scope.py): `hook_caller` says which
host a request is — its credential in the key header, or the legacy host where today's
callers come without one — and `request_scope_of` resolves what that request may read.

Public surface: register(mcp) · has_session(request) · gate_needed() · PUBLIC_PATHS ·
                hosts() · hook_caller(request) · hook_ok(request) · request_scope_of()
========================================
"""

import hashlib
import hmac
import logging
import secrets
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh

logger = logging.getLogger("loci_brain.web.panel_auth")

_COOKIE = "loci_panel"
_TTL = 14 * 24 * 3600          # Two weeks. For a personal panel, typing the password
                               # once a fortnight is not a burden.

# Paths that skip the gate. **Only two kinds of entry belong on this list**:
#   1. what the gate itself needs (otherwise nobody can log in)
#   2. routes whose caller is not a browser (the bridge is a separate process; it has
#      no cookies)
PUBLIC_PATHS = frozenset([
    "/auth/login",
    "/auth/logout",
    "/auth/recovery-question",
    "/auth/recover",
    "/api/loci/auth/state",          # the gate reads this to learn whether a security question exists
    "/api/loci/auth/set-password",   # first-run password setup (it does its own loopback check)
    "/loci",                         # the page itself must open, or the gate has nowhere to appear
])

# **The four bridge-facing routes**, moved down out of the exemption list above.
#
#    They used to sit in PUBLIC_PATHS, justified as "the caller is the bridge, not a
#    browser, and it has no cookie". **The justification was right and the solution was
#    wrong** — that removes the door rather than giving the bridge a key. The consequence:
#    someone who set a panel password believed it was locked, while these four stood open
#    the whole time, **both readable and state-changing** (`dream/wake` moves recall_count
#    and the lifecycle along). Harmless enough on loopback; the moment a tunnel, a reverse
#    proxy, a LAN, or a misconfiguration is involved, the boundary is simply open.
#
# The rule now, in one line: **once it is locked, there are no exceptions.**
#      Gate unlocked (no password set) -> unchanged, anyone may call these four.
#      Gate locked                     -> these four need a key (in a header), or an
#                                         already-logged-in browser.
#    WARNING: **the key travels in a header, not in the URL** — URLs leak through logs,
#    Referer, and browser history.
#
# The host's intake of raw lines for slicing (`/api/v2/slices`, core/_slicer.py) and its
# window-opening read of breath (`/api/v2/breath`), its change notices about its material
# (`/api/v2/source/change`) and the lines of its runs (`/api/v2/source/lines`), its
# reconciliation read of the ledger (`/api/v2/changes`)
# and the strong reminder with its two acknowledgements (`/api/v2/cue`, `.../delivered`,
# `.../dropped`) are bridge-facing routes on the same terms: the host's process calls
# them, with the key.
HOOK_PATHS = frozenset([
    "/api/loci/dream/wake",
    "/api/muse/pending",
    "/api/dream/current",
    "/api/loci/poke",
    "/api/v2/slices",
    "/api/v2/breath",
    "/api/v2/source/change",
    "/api/v2/source/lines",
    "/api/v2/changes",
    "/api/v2/cue",
    "/api/v2/cue/delivered",
    "/api/v2/cue/dropped",
])
HOOK_HEADER = "x-loci-hook-token"

_PUBLIC_PREFIXES = ("/loci/vendor/",)   # the page's static assets


# **The auth file being unreadable is not the same as "no password was set".**
#    `_load_password_hash()` used to answer both with None, and every judgment below read
#    that None as rule 1 ("no key exists, so do not lock"). A corrupt or unreadable
#    `.dashboard_auth.json` therefore **unlocked the panel** — the one moment a lock matters
#    most is the moment its own storage is broken. It now raises instead, and everything
#    here leans the way `hook_ok` already leans: **broken means locked**, and the log says
#    how to fix it. Rule 1 is untouched: it applies to a store that reads fine and simply
#    holds no password.
_STORAGE_BROKEN_HINT = ("面板的口令存储读不出来（buckets 目录下的 .dashboard_auth.json 坏了、"
                        "被截断了、或者没权限读）。门按「锁着」处理，登录和会话一律拒。"
                        "修好那个文件，或者删掉它再从面板「账号」里重设一次口令。")
_STORAGE_BROKEN_LOG_EVERY = 60.0        # seconds; gate_needed() runs on every request
_storage_broken_logged_at = 0.0


def _log_storage_broken(exc: BaseException) -> None:
    """Say it loudly, but not once per request — a flood of identical lines is how the
    one line that explains the outage gets lost."""
    global _storage_broken_logged_at
    now = time.time()
    if now - _storage_broken_logged_at >= _STORAGE_BROKEN_LOG_EVERY:
        _storage_broken_logged_at = now
        logger.error("[panel_auth] auth 存储损坏：%s。%s", exc, _STORAGE_BROKEN_HINT)


def storage_broken() -> bool:
    """Whether the auth store is currently unreadable (as opposed to empty)."""
    try:
        sh._load_password_hash()
        return False
    except Exception as e:               # noqa: BLE001
        _log_storage_broken(e)
        return True


def gate_needed() -> bool:
    """Whether the gate should be locked right now.

    It locks only when both conditions hold: the switch is on **and** a password has been
    set. The second condition is a hard safety line — locking while no password exists
    shuts everyone out, the owner included.

    **Unless the store cannot be read at all**, in which case "has a password been set"
    has no answer, and an unanswered security question resolves to the locked side.
    """
    raw = sh.config.get("panel_auth", True)
    on = str(raw).strip().lower() not in ("0", "false", "no", "off", "none", "")
    if not on:
        return False
    try:
        return sh._load_password_hash() is not None
    except Exception as e:               # noqa: BLE001
        _log_storage_broken(e)
        return True                      # fail-closed; see the block above


# One random key per process, used whenever no password hash is available to derive from.
# It is never the empty string: `sha256("loci-panel-v1:")` is a constant anybody can
# compute, so an empty-string fallback hands out a valid signing key for free.
_UNAVAILABLE_KEY_SEED = secrets.token_bytes(32)


def _key() -> bytes:
    """The cookie-signing key: derived from the password hash, never stored separately.
    Change the password -> the key changes -> every old session dies.

    With no hash to derive from (none set, or the store unreadable) the fallback is a
    **random per-process key**: nothing can be signed that verifies, no existing cookie
    verifies, and a restart invalidates whatever was minted under it.
    """
    try:
        h = sh._load_password_hash()
    except Exception as e:               # noqa: BLE001
        _log_storage_broken(e)
        h = None
    if not h:
        return hashlib.sha256(b"loci-panel-v1-unavailable:" + _UNAVAILABLE_KEY_SEED).digest()
    return hashlib.sha256(("loci-panel-v1:" + h).encode("utf-8")).digest()


def _sign(exp: int) -> str:
    return hmac.new(_key(), str(exp).encode("ascii"), hashlib.sha256).hexdigest()[:32]


def _make_cookie() -> str:
    exp = int(time.time()) + _TTL
    return str(exp) + "." + _sign(exp)


def has_session(request: Request) -> bool:
    raw = request.cookies.get(_COOKIE) or ""
    if "." not in raw:
        return False
    exp_s, sig = raw.split(".", 1)
    try:
        exp = int(exp_s)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    # compare_digest: never compare signatures with ==, which leaks "how many characters
    # were right" through timing.
    return hmac.compare_digest(sig, _sign(exp))


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def is_hook(path: str) -> bool:
    return path in HOOK_PATHS


def hook_token() -> str:
    """The bridge's key. Environment variable first, then config. Empty string if unset."""
    import os
    v = str(os.environ.get("LOCI_HOOK_TOKEN") or "").strip()
    if v:
        return v
    try:
        return str(sh.config.get("hook_token") or "").strip()
    except Exception:                    # noqa: BLE001
        return ""


def hosts():
    """The deployment's hosts (core/scope.load_hosts): `hosts:` in config, or the one
    legacy host whose key is `hook_token()`. Read per request, like the key itself."""
    import os
    from core import scope as _scope
    return _scope.load_hosts(sh.config, os.environ, legacy_token=hook_token())


# Who a hook request is when it is not a host: a logged-in browser, the panel itself.
# The panel is the owner looking at their own library, not a host; it reads everything.
PANEL = "panel"


def hook_caller(request: Request):
    """Whether to let this bridge request through, and who is calling.
    Returns (allowed, why not, caller): caller is a `core.scope.Host`, or PANEL.

    Ways in, in this order:
      1. A host's credential in the key header -> that host, locked gate or not. A key
         that matches no host is ignored only when there is no `hosts:` table (the one
         legacy host; an unlocked gate has nothing to protect). With a table it is
         refused: a restricted host whose credential went wrong must never be taken for
         the open one.
      2. An already-logged-in browser -> the panel.
      3. **The gate is unlocked** -> the legacy host (who calls today, with no key).
      4. Otherwise refused, saying what to fix."""
    hs = hosts()
    got = str(request.headers.get(HOOK_HEADER) or "")
    if got.strip():
        host = hs.by_token(got)
        if host is not None:
            return True, "", host
        if not hs.implicit:
            return False, (f"请求头 `{HOOK_HEADER}` 里的凭据对不上 hosts 表里任何一个宿主"
                           "（看看 Loci 这边那个宿主的 token_env 环境变量设了没有、值对不对）。"), None
    if has_session(request):
        return True, "", PANEL
    if not gate_needed():
        if hs.default is None:
            return False, (f"没带宿主凭据（请求头 `{HOOK_HEADER}`），而 hosts 表里没有 legacy 宿主，"
                           "认不出是谁。"), None
        return True, "", hs.default
    ok, why = _key_check(got)
    if ok:
        # The hook key without a legacy host to be: a `hosts:` table that left it out.
        why = f"请求头 `{HOOK_HEADER}` 里的凭据对不上 hosts 表里任何一个宿主。"
    return False, why, None


def _key_check(got: str) -> tuple[bool, str]:
    want = hook_token()
    if not want:
        return False, ("面板锁着，而这条路由要一把给桥用的钥匙，但还没配。"
                       f"设一个 `LOCI_HOOK_TOKEN`（或 config.yaml 里的 `hook_token`），"
                       f"再让桥在请求头 `{HOOK_HEADER}` 里带上它。")
    if got and hmac.compare_digest(got, want):
        return True, ""
    return False, f"这条路由要钥匙：请求头 `{HOOK_HEADER}` 没带或者不对。"


def hook_ok(request: Request) -> tuple[bool, str]:
    """Whether to let this bridge request through. Returns (allowed, why not); who is
    calling is `hook_caller`'s.

    Three ways in:
      1. **The gate is unlocked** (panel password disabled, or none set yet) -> allow.
         There is no lock anywhere else, so there should not be one here.
      2. An already-logged-in browser -> allow (the panel itself reads these routes).
      3. The right key -> allow.

    **Locked but no key configured -> refuse.** That is deliberate (fail-closed).
       "Default to open when the config is incomplete" is the root of this entire class of
       bug: a lock that exists only when the configuration is correct is not a lock. Better
       to break loudly right here than to stand quietly open.
       WARNING: when it does break, it has to **say how to fix it** — see the 401 message
          below. Silent malfunction is far worse than an error. (Three separate instances
          of that same failure mode turned up while writing this.)
    """
    ok, why, _caller = hook_caller(request)
    return ok, why


def request_scope_of(request: Request, caller):
    """The hook request resolved (core/scope.RequestScope): the panel reads everything;
    a host reads by its mode and the request's Loci-Scope."""
    from core import scope as _scope
    if caller == PANEL:
        return _scope.RequestScope(None, _scope.OPEN)
    return _scope.RequestScope.resolve(caller, request.headers.get(_scope.SCOPE_HEADER))


def _set_cookie(resp: Response, value: str, max_age: int) -> Response:
    resp.set_cookie(_COOKIE, value, max_age=max_age, path="/",
                    httponly=True, samesite="lax")
    return resp


def register(mcp) -> None:

    @mcp.custom_route("/auth/login", methods=["POST"])
    async def auth_login(request: Request) -> Response:
        """Come in. Rate limiting reuses what `_shared` already has: a per-origin layer
        and a global one."""
        wait = sh._login_retry_after(request)
        if wait > 0:
            return JSONResponse({"error": f"试得太密了，{wait} 秒后再来"},
                                status_code=429)
        try:
            body = await request.json()
        except Exception:                # noqa: BLE001
            return JSONResponse({"error": "body 不是合法 JSON"}, status_code=400)
        pw = str((body or {}).get("password") or "")
        ok = False
        try:
            ok = sh._verify_any_password(pw)
        except Exception as e:           # noqa: BLE001
            logger.warning(f"[panel_auth] 校验出错: {e}")
        if not ok:
            sh._record_login_failure(request)
            # A password that cannot be read verifies against nothing, so this is already
            # a refusal — but "密码不对" would send the owner off to retype a password that
            # was never going to work. Say what actually broke.
            if storage_broken():
                return JSONResponse({"error": _STORAGE_BROKEN_HINT}, status_code=503)
            return JSONResponse({"error": "密码不对"}, status_code=401)
        sh._record_login_success(request)
        return _set_cookie(JSONResponse({"ok": True}), _make_cookie(), _TTL)

    @mcp.custom_route("/auth/logout", methods=["POST"])
    async def auth_logout(request: Request) -> Response:
        return _set_cookie(JSONResponse({"ok": True}), "", 0)

    @mcp.custom_route("/auth/recovery-question", methods=["GET"])
    async def auth_recovery_question(request: Request) -> Response:
        try:
            q = str(sh._load_auth_data().get("security_question") or "")
        except Exception:                # noqa: BLE001
            q = ""
        return JSONResponse({"question": q})

    @mcp.custom_route("/auth/recover", methods=["POST"])
    async def auth_recover(request: Request) -> Response:
        """Forgot the password: answer the security question correctly and set a new one."""
        wait = sh._login_retry_after(request)
        if wait > 0:
            return JSONResponse({"error": f"试得太密了，{wait} 秒后再来"},
                                status_code=429)
        try:
            body = await request.json()
        except Exception:                # noqa: BLE001
            return JSONResponse({"error": "body 不是合法 JSON"}, status_code=400)
        answer = str((body or {}).get("answer") or "")
        newpw = str((body or {}).get("password") or "")
        if len(newpw.strip()) < 6:
            return JSONResponse({"error": "新密码至少 6 位"}, status_code=400)
        proof = None
        try:
            proof = sh._verify_security_answer_for_rotation(answer)
        except Exception as e:           # noqa: BLE001
            logger.warning(f"[panel_auth] 安全问题校验出错: {e}")
        if not proof:
            sh._record_login_failure(request)
            if storage_broken():
                return JSONResponse({"error": _STORAGE_BROKEN_HINT}, status_code=503)
            return JSONResponse({"error": "答案不对"}, status_code=401)
        # `proof` carries the auth generation as of the moment the answer was verified;
        # passing it in makes this a compare-and-swap. If anyone changed the password in
        # between, this reset must not overwrite theirs — `_shared` refuses it for us.
        if not sh._save_password_hash(newpw, expected_generation=proof.generation):
            return JSONResponse({"error": "这中间密码被改过了，重来一次"},
                                status_code=409)
        sh._record_login_success(request)
        # New password = new key = every old session is dead, so hand out a fresh one here.
        return _set_cookie(JSONResponse({"ok": True}), _make_cookie(), _TTL)
