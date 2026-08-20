"""
========================================
bridge/ollama_child.py — keeping the local Ollama child process alive
(moved here from web/ollama_local.py during the strip-down)
========================================

The old web/ollama_local.py was a one-click setup panel: detect the host -> install
Ollama without elevation -> keep the child process up -> panel routes. When the upstream
panels were cut, **the whole panel half went with them** — downloading, verifying and
unpacking an Ollama release, the progress bar, and the three `/api/embedding/local/*`
routes. Not one button could reach any of it any more, so keeping it would have been
keeping dead code.

**The keep-the-child-running half stays.** The docs state flatly that local embedding
requires a local Ollama, which makes this core supporting machinery: server.py's lifespan
calls `ensure_child_on_boot()` / `stop_child()` directly, not via any panel button. A
standalone-container deployment does not need it — both functions return immediately when
`sh.in_docker()` is true — but the mechanism has to stay. Same test as the auth interlock:
**this half is not panel, it is runtime.**

Public surface:
- ensure_child_on_boot() / stop_child(): called by server.py's lifespan on start and stop.
- find_ollama_bin(): returns None when Ollama is not installed, and no longer kicks off
  an automatic install wizard.
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

# Child-process state
_child_proc: "subprocess.Popen | None" = None
_child_managed = False
_child_monitor_task: "asyncio.Task | None" = None


# ============================================================
# Environment probing — only the few probes the child process actually needs.
# `_arch()` / `_detect()` / `_recommend()` were panel-only and went with the
# install wizard.
# ============================================================

def _os_key() -> str:
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def _user_install_root() -> str:
    """Root of the no-elevation install target: under the user's home directory, so
    neither sudo nor Administrator is needed."""
    return os.path.join(os.path.expanduser("~"), ".ollama", "local")


def find_ollama_bin() -> "str | None":
    """Locate the ollama executable: PATH first, then each platform's no-elevation
    install location.

    Returns None when it cannot be found — it **no longer triggers an install**. That
    was the panel wizard's job, and the wizard is gone.
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
    # trust_env=False: a local Ollama must bypass the system proxy. Proxy clients happily
    # route 127.0.0.1 through the proxy too, which comes back as a 502 — so `serve` is
    # running fine and we conclude it is dead.
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as c:
            r = await c.get(f"{base}/api/version")
            return r.status_code == 200
    except Exception:
        return False


# ============================================================
# Keeping the child process alive
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
    """Make sure Ollama is running. Already reachable -> use it. Installed but not
    running -> spawn the child and wait for readiness. Not installed -> report that."""
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
    # Wait for readiness. The very first cold start is slow: measured on a fresh Windows
    # install, the first `ollama serve` does runtime and GPU probing and can take more
    # than 150s before it starts listening on 11434. So allow ~180s, probing once a
    # second. (_is_running carries its own 3s timeout, so a port that is open but slow to
    # answer is not mistaken for a failure.)
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
    """Respawn the child if it dies — only the one we ourselves manage."""
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
    """On server shutdown, stop the Ollama child process we manage."""
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
    """Called by server.py's lifespan: start the child at boot, but only on bare metal
    and only when the config asks for local vectorization. Under Docker, or with cloud
    vectorization, do nothing — "the server manages Ollama" is a bare-metal idea only."""
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
