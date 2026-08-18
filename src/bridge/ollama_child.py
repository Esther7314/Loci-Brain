"""
========================================
bridge/ollama_child.py — 本地 Ollama 子进程常驻（脱壳 E2 从 web/ollama_local.py 搬来）
========================================

原 web/ollama_local.py 是「一键搭建」面板：检测宿主 → 免提权自动装 ollama →
常驻子进程 → 面板路由。E2 砍上游面板时，**面板那半整个砍了**——下载/校验/解压
ollama 发行包、进度条、`/api/embedding/local/*` 三个路由，一个按钮都够不着了，
留着就是死代码。

**子进程常驻这半留着**：开源版文档写死「本地 embedding 需要本地 ollama」，
这是核心配套机制，server.py 的 lifespan 直接调
`ensure_child_on_boot()` / `stop_child()`（不是哪个面板按钮触发的）。
我们自己走独立容器用不到（`sh.in_docker()` 为真时两个函数都直接跳过），
但机制要留着——这跟 E2 「auth 连锁」是同一条判据：**这一半不是面板，是运行时**。

对外暴露：
- ensure_child_on_boot() / stop_child()：server.py lifespan 启停调用
- find_ollama_bin()：只在没装的时候原样返回 None，不再触发自动安装向导
========================================
"""

import os
import sys
import shutil
import asyncio
import subprocess

import httpx

from web import _shared as sh

logger = sh.logger

_OLLAMA_PORT = 11434
_LOCAL_BASE = f"http://127.0.0.1:{_OLLAMA_PORT}"

# 子进程管理
_child_proc: "subprocess.Popen | None" = None
_child_managed = False
_child_monitor_task: "asyncio.Task | None" = None


# ============================================================
# 环境探测（只留子进程常驻用得到的这几个；`_arch()`/`_detect()`/`_recommend()`
# 那些是面板专用，跟着安装向导一起砍了）
# ============================================================

def _os_key() -> str:
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def _user_install_root() -> str:
    """免提权安装目标根目录（用户家目录下，不需 sudo/管理员）。"""
    return os.path.join(os.path.expanduser("~"), ".ollama", "local")


def find_ollama_bin() -> "str | None":
    """找 ollama 可执行文件：PATH 优先，再查各系统免提权安装位置。

    找不到就返回 None——**不再触发自动安装**（那是面板向导的活，已经砍了）。
    """
    p = shutil.which("ollama")
    if p:
        return p
    osk = _os_key()
    home = os.path.expanduser("~")
    cands = []
    if osk == "windows":
        cands += [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
            os.path.join(_user_install_root(), "ollama.exe"),
        ]
    elif osk == "macos":
        cands += [
            os.path.join(_user_install_root(), "Ollama.app", "Contents", "Resources", "ollama"),
            "/Applications/Ollama.app/Contents/Resources/ollama",
        ]
    cands += [
        os.path.join(_user_install_root(), "bin", "ollama"),
        os.path.join(home, ".ollama", "bin", "ollama"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


async def _is_running(base: str = _LOCAL_BASE) -> bool:
    # trust_env=False：本地 ollama 必须绕过系统代理（Clash/V2Ray 等会把 127.0.0.1
    # 也丢给代理 → 502，明明 serve 在跑却判定挂了）。
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as c:
            r = await c.get(f"{base}/api/version")
            return r.status_code == 200
    except Exception:
        return False


# ============================================================
# 子进程常驻
# ============================================================

def _spawn() -> "subprocess.Popen | None":
    binp = find_ollama_bin()
    if not binp:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _os_key() == "windows" else 0
    env = os.environ.copy()
    env.setdefault("OLLAMA_HOST", f"127.0.0.1:{_OLLAMA_PORT}")
    return subprocess.Popen(
        [binp, "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, creationflags=flags,
    )


async def ensure_child() -> dict:
    """确保 ollama 在跑：已可达→直接用；装了没跑→拉起子进程并等就绪；没装→报缺失。"""
    global _child_proc, _child_managed
    if await _is_running():
        return {"running": True, "managed": _child_managed, "reason": "already_running"}
    if sh.in_docker():
        return {"running": False, "managed": False, "reason": "in_docker"}
    if not find_ollama_bin():
        return {"running": False, "managed": False, "reason": "not_installed"}
    try:
        _child_proc = _spawn()
    except Exception as e:
        return {"running": False, "managed": False, "reason": f"spawn_failed: {e}"}
    if _child_proc is None:
        return {"running": False, "managed": False, "reason": "not_installed"}
    # 等就绪：首次冷启动很慢——实测 Windows 全新安装后第一次 `ollama serve`
    # 要做运行时/GPU 探测，可能 >150s 才开始监听 11434。给到 ~180s，
    # 每秒探一次（_is_running 自带 3s 超时，端口已开但慢响应时不会误判失败）。
    for _ in range(180):
        if await _is_running():
            _child_managed = True
            _start_monitor()
            logger.info("[ollama] child serve started & ready")
            return {"running": True, "managed": True, "reason": "spawned"}
        await asyncio.sleep(1)
    return {"running": False, "managed": False, "reason": "spawn_timeout"}


def _start_monitor() -> None:
    global _child_monitor_task
    if _child_monitor_task is None or _child_monitor_task.done():
        _child_monitor_task = asyncio.create_task(_monitor())


async def _monitor() -> None:
    """子进程挂了自动拉起（仅限我们托管的那只）。"""
    global _child_proc
    while _child_managed:
        await asyncio.sleep(5)
        try:
            if _child_proc is not None and _child_proc.poll() is not None:
                logger.warning("[ollama] managed child exited, respawning")
                _child_proc = _spawn()
        except Exception as e:
            logger.warning(f"[ollama] monitor respawn failed: {e}")


async def stop_child() -> None:
    """OB 关停时一并停掉我们托管的 ollama 子进程。"""
    global _child_proc, _child_managed
    _child_managed = False
    if _child_monitor_task:
        _child_monitor_task.cancel()
    if _child_proc is not None and _child_proc.poll() is None:
        try:
            _child_proc.terminate()
            try:
                _child_proc.wait(timeout=5)
            except Exception:
                _child_proc.kill()
        except Exception:
            pass
    _child_proc = None


async def ensure_child_on_boot() -> None:
    """server.py lifespan 调用：仅当裸机 + 配置成本地向量化时，开机就把子进程拉起来。
    Docker / 云端向量化 → 不动（裸机才有「OB 托管 ollama」一说）。"""
    try:
        if sh.in_docker():
            return
        emb = (sh.config.get("embedding") or {})
        fmt = (emb.get("api_format") or "").strip().lower()
        if not emb.get("enabled", True) or fmt not in ("ollama", "local"):
            return
        if not find_ollama_bin():
            return
        res = await ensure_child()
        logger.info(f"[ollama] boot ensure_child: {res}")
    except Exception as e:
        logger.warning(f"[ollama] ensure_child_on_boot failed: {e}")
