"""
========================================
web/panel_auth.py — 面板的门（2026-08-19）
========================================

E2（2026-08-17）把 cookie 会话那一族连同 web/auth.py 一起砍了，判据是她 8-05 拍的
「家里内网不鉴权」。那对**这一台机器**成立；对**发出去给别人用**不成立 ——
别人会把端口放到公网上，而 `#gate` 那道门只在 API 回 401 的时候才弹，
没有鉴权就永远不回 401，于是那道门再也不会出现：**看起来有锁，其实没有**。

所以这个模块把门装回去。**没有重造轮子**：密码哈希、限流、安全问题找回、
原子落盘全在 `_shared.py` 里活得好好的，这儿只补两样 ——
四条路由，和一个签名 cookie。

🔴 三条安全判据（改之前先读）：

1. **没设过密码就不锁。** 新装的人打开就该能用；锁一个还没有钥匙的门，
   等于把人关在自己家外面（8-03 夜真发生过一次，二十分钟）。
2. **会话密钥从密码哈希派生**，不另存文件：`sha256("loci-panel-v1:" + hash)`。
   白赚一件事 —— **改密码自动让所有旧会话失效**，不用再写一套撤销。
3. **桥用的那几条口不进这道门**（dream/wake、muse/pending、dream/current、poke）：
   调用方是另一个进程，不是浏览器，它没有 cookie 也不该有。

开关：config.yaml 的 `panel_auth`（默认 **true**，发出去那份就该是锁着的）。
她自己这台在 config 里显式写了 false —— 判据没变，只是那条判据只适用于内网。

对外暴露：register(mcp) · has_session(request) · gate_needed() · PUBLIC_PATHS
========================================
"""

import hashlib
import hmac
import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh

logger = logging.getLogger("loci_brain.web.panel_auth")

_COOKIE = "loci_panel"
_TTL = 14 * 24 * 3600          # 两周。家用面板，两周输一次密码不算烦。

# 不进这道门的路径。**只有两类能进这个名单**：
#   ① 门本身要用的（不然登不进来）
#   ② 调用方不是浏览器的（桥是另一个进程，它没有 cookie）
PUBLIC_PATHS = frozenset([
    "/auth/login",
    "/auth/logout",
    "/auth/recovery-question",
    "/auth/recover",
    "/api/loci/auth/state",          # 门要读它才知道有没有安全问题
    "/api/loci/auth/set-password",   # 首启设密那条路（它自己有 loopback 校验）
    "/loci",                         # 页面本身要能打开，否则门无处显示
    "/api/loci/dream/wake",          # ↓ 以下四条：调用方是桥，不是浏览器
    "/api/muse/pending",
    "/api/dream/current",
    "/api/loci/poke",
])

_PUBLIC_PREFIXES = ("/loci/vendor/",)   # 页面的静态件


def gate_needed() -> bool:
    """现在要不要锁。

    两个条件都成立才锁：开关开着 **且** 已经设过密码。
    第二个条件是硬安全线 —— 没有密码的时候锁上，谁都进不来，包括主人。
    """
    raw = sh.config.get("panel_auth", True)
    on = str(raw).strip().lower() not in ("0", "false", "no", "off", "none", "")
    if not on:
        return False
    try:
        return sh._load_password_hash() is not None
    except Exception:                    # noqa: BLE001
        return False


def _key() -> bytes:
    """签 cookie 的密钥：从密码哈希派生，不另存。改密码 → 密钥变 → 旧会话全失效。"""
    h = ""
    try:
        h = sh._load_password_hash() or ""
    except Exception:                    # noqa: BLE001
        h = ""
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
    # compare_digest：别用 == 比签名，那会把「对了几个字符」按时间漏出去
    return hmac.compare_digest(sig, _sign(exp))


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


def _set_cookie(resp: Response, value: str, max_age: int) -> Response:
    resp.set_cookie(_COOKIE, value, max_age=max_age, path="/",
                    httponly=True, samesite="lax")
    return resp


def register(mcp) -> None:

    @mcp.custom_route("/auth/login", methods=["POST"])
    async def auth_login(request: Request) -> Response:
        """进门。限流走 `_shared` 现成的那套（按来源 + 全局两层）。"""
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
        """忘了密码：安全问题答对了就能设一把新的。"""
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
            return JSONResponse({"error": "答案不对"}, status_code=401)
        # proof 带的是「校验答案那一刻的 auth 代次」，传进去做 compare-and-swap：
        # 这中间要是有人改过密码，这次重置就不该盖上去（_shared 那套自己会拒）。
        if not sh._save_password_hash(newpw, expected_generation=proof.generation):
            return JSONResponse({"error": "这中间密码被改过了，重来一次"},
                                status_code=409)
        sh._record_login_success(request)
        # 换了密码 = 密钥换了 = 旧会话全失效，这儿顺手发一把新的
        return _set_cookie(JSONResponse({"ok": True}), _make_cookie(), _TTL)
