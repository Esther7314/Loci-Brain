"""
========================================
package/launcher.py — start Loci and its gateway together, and keep both running
========================================

The portable download runs this (start.bat, shipped as 「启动.bat」). One window holds
both processes:
  · Loci     python app/src/server.py, bound to 127.0.0.1
  · gateway  node app/gateway/server.js
Whichever one stops is started again after a few seconds; one that keeps dying right
after it starts waits a minute between tries, so a broken setup does not spin. Closing
the window stops both: they share its console, and they also sit in a Windows job object
that is killed with this process, so a crashed launcher leaves nothing behind.

Package layout (package/windows/build.ps1 makes it):

    <root>/
      start.bat           what the person double-clicks
      python/             embeddable Python 3.12 with the dependencies installed
      node/node.exe
      app/                this repository's runtime files; this file is app/package/
      data/               made on the first run; everything that is the person's
        settings.env      the generated keys, plus any LOCI_* the person adds
        buckets/          the library; config.yaml lives here, as under Docker
        gateway/          the gateway's private state

First run: generates three keys into data/settings.env and writes config.yaml from
config.default.yaml plus a `hosts:` table pairing Loci with the gateway (the same table
gateway/README.md section 二·2 has people write by hand). With a table, the panel works
only after a password is set — the panel's first page asks for one.

Everything here is the standard library: it runs before any dependency is known to work.

    python app/package/launcher.py                  run both
    python app/package/launcher.py --autostart on   also start at login (off to undo)
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.environ.get("LOCI_PACKAGE_ROOT") or os.path.dirname(APP)
DATA = os.path.join(ROOT, "data")
SETTINGS = os.path.join(DATA, "settings.env")
BUCKETS = os.path.join(DATA, "buckets")
CONFIG = os.path.join(BUCKETS, "config.yaml")
GATEWAY_DATA = os.path.join(DATA, "gateway")
LOCK = os.path.join(DATA, "launcher.lock")

GATEWAY_NAME = "gateway"
DEFAULT_LOCI_PORT = 18002
DEFAULT_GATEWAY_PORT = 3100

# The three keys, one per direction (gateway/README.md 二·2). The legacy one is what a
# directly connected MCP client presents; the other two must differ from each other.
KEYS = ("LOCI_HOOK_TOKEN", "LOCI_HOST_TOKEN_GATEWAY", "LOCI_FETCH_TOKEN_GATEWAY")

QUICK_DEATH_S = 30      # died this soon after starting: counts toward the slow retry
RETRY_S = 5
SLOW_RETRY_S = 60
QUICK_DEATHS_BEFORE_SLOW = 3

STARTUP_SHORTCUT = "Loci.lnk"


# ── settings.env ───────────────────────────────────────────────────────────────

def read_settings(path: str = SETTINGS) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments skipped. Missing file: empty."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def ensure_keys(path: str = SETTINGS) -> dict[str, str]:
    """Settings with every key in KEYS present; missing ones are generated and appended
    (never rewriting what the person wrote)."""
    have = read_settings(path)
    missing = [k for k in KEYS if not have.get(k)]
    if missing:
        fresh = not os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            if fresh:
                f.write("# Loci's settings for this folder. Generated keys below; keep this file private.\n"
                        "# Add LOCI_* lines here to change a setting (LOCI_PORT, LOCI_GATEWAY_PORT, LOCI_TZ ...),\n"
                        "# then close the window and start again.\n")
            for k in missing:
                v = secrets.token_urlsafe(32)
                f.write(f"{k}={v}\n")
                have[k] = v
    return have


def ports(settings: dict[str, str]) -> tuple[int, int]:
    return (int(settings.get("LOCI_PORT") or DEFAULT_LOCI_PORT),
            int(settings.get("LOCI_GATEWAY_PORT") or DEFAULT_GATEWAY_PORT))


# ── config.yaml ────────────────────────────────────────────────────────────────

def hosts_block(gateway_port: int) -> str:
    base = f"http://127.0.0.1:{gateway_port}"
    return (
        "\n"
        "# Written by the package launcher on the first run: Loci and the gateway beside it.\n"
        "# The key values live in data/settings.env, never here.\n"
        "hosts:\n"
        "  legacy:\n"
        "    token_env: LOCI_HOOK_TOKEN\n"
        "    scope_mode: open\n"
        f"  {GATEWAY_NAME}:\n"
        "    token_env: LOCI_HOST_TOKEN_GATEWAY\n"
        "    scope_mode: open\n"
        "    authority:\n"
        f"      - {{system: gateway, instance: {GATEWAY_NAME}}}\n"
        "    provides:\n"
        f"      - {{system: gateway, instance: {GATEWAY_NAME}}}\n"
        f'    fetch_url: "{base}/loci/source"\n'
        "    fetch_token_env: LOCI_FETCH_TOKEN_GATEWAY\n"
        f'    present_url: "{base}"\n'
    )


def ensure_config(gateway_port: int) -> bool:
    """Write config.yaml when there is none. True when it was written now (first run)."""
    if os.path.exists(CONFIG):
        return False
    with open(os.path.join(APP, "config.default.yaml"), encoding="utf-8") as f:
        default = f.read()
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(default.rstrip("\n") + "\n" + hosts_block(gateway_port))
    os.replace(tmp, CONFIG)
    return True


# ── the two processes ──────────────────────────────────────────────────────────

def node_exe() -> str:
    bundled = os.path.join(ROOT, "node", "node.exe")
    return bundled if os.path.exists(bundled) else "node"


def machine_zone(node: str) -> str:
    """The machine's IANA zone, asked of Node (Windows has no IANA name of its own
    that Python's standard library can read). Empty when Node cannot say."""
    try:
        out = subprocess.run([node, "-e", "console.log(Intl.DateTimeFormat().resolvedOptions().timeZone)"],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def child_envs(settings: dict[str, str], zone: str) -> tuple[dict, dict]:
    loci_port, gateway_port = ports(settings)
    common = dict(os.environ)
    common.update({k: v for k, v in settings.items() if k not in ("LOCI_GATEWAY_PORT",)})
    if zone and not settings.get("LOCI_TZ"):
        common["LOCI_TZ"] = zone
    common["PYTHONUTF8"] = "1"
    common["PYTHONIOENCODING"] = "utf-8"

    loci = dict(common)
    loci.update({
        "LOCI_CONFIG_PATH": CONFIG,
        "LOCI_VAULT_DIR": BUCKETS,
        "LOCI_TRANSPORT": "streamable-http",
        "LOCI_PORT": str(loci_port),
        "LOCI_BIND_HOST": settings.get("LOCI_BIND_HOST") or "127.0.0.1",
    })

    gateway = dict(common)
    gateway.update({
        "PORT": str(gateway_port),
        "LOCI_MCP": f"http://127.0.0.1:{loci_port}/mcp",
        # The gateway's own names for the two keys (gateway/README.md 二·1).
        "LOCI_HOOK_TOKEN": settings["LOCI_HOST_TOKEN_GATEWAY"],
        "LOCI_GATEWAY_TOKEN": settings["LOCI_FETCH_TOKEN_GATEWAY"],
        "LOCI_GATEWAY_NAME": GATEWAY_NAME,
        "LOCI_GATEWAY_DATA": GATEWAY_DATA,
        "LOCI_GATEWAY_DAYS": os.path.join(BUCKETS, "_hosts", GATEWAY_NAME),
    })
    return loci, gateway


class Child:
    def __init__(self, name: str, argv: list[str], env: dict, job):
        self.name, self.argv, self.env, self.job = name, argv, env, job
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.restart_at = 0.0
        self.quick_deaths = 0

    def start(self) -> None:
        self.proc = subprocess.Popen(self.argv, env=self.env, cwd=APP)
        self.started = time.monotonic()
        if self.job is not None:
            self.job.add(self.proc)

    def check(self, now: float) -> None:
        if self.proc is None:
            if now >= self.restart_at:
                self.start()
            return
        code = self.proc.poll()
        if code is None:
            return
        self.proc = None
        self.quick_deaths = self.quick_deaths + 1 if now - self.started < QUICK_DEATH_S else 0
        wait = SLOW_RETRY_S if self.quick_deaths >= QUICK_DEATHS_BEFORE_SLOW else RETRY_S
        say(f"{self.name} 停了（退出码 {code}），{wait} 秒后重新起")
        self.restart_at = now + wait

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class KillOnCloseJob:
    """A Windows job object whose processes die when this process's handle to it closes,
    which happens however this process ends."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._k = k = ctypes.WinDLL("kernel32", use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(wintypes.HANDLE(self.handle), 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")

    def add(self, proc: subprocess.Popen) -> None:
        self._k.AssignProcessToJobObject(self.handle, int(proc._handle))  # noqa: SLF001


def make_job():
    if os.name != "nt":
        return None
    try:
        return KillOnCloseJob()
    except OSError as e:
        say(f"（没能把两个进程绑到这个窗口上：{e}。关窗口一般照样会停，任务管理器里留下的话手动结束）")
        return None


# ── one launcher per folder ────────────────────────────────────────────────────

def hold_lock():
    """An open handle with a byte locked, or None when another launcher holds it."""
    f = open(LOCK, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


# ── start at login ─────────────────────────────────────────────────────────────

def startup_dir() -> str:
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def set_autostart(on: bool) -> str:
    """A shortcut in the Startup folder that runs this launcher minimized. Made through
    PowerShell's -EncodedCommand (UTF-16), so a folder path in any script survives —
    a .cmd file would be read in the console's code page."""
    target = os.path.join(startup_dir(), STARTUP_SHORTCUT)
    if not on:
        if os.path.exists(target):
            os.remove(target)
        return "开机不再自动启动"
    script = "\n".join([
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(target) + ")",
        "$s.TargetPath = " + _ps_quote(sys.executable),
        "$s.Arguments = " + _ps_quote(f'"{os.path.abspath(__file__)}"'),
        "$s.WorkingDirectory = " + _ps_quote(ROOT),
        "$s.WindowStyle = 7",
        "$s.Save()",
    ])
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], check=True)
    return "开机会自动启动（窗口最小化）"


# ── main ───────────────────────────────────────────────────────────────────────

def say(line: str) -> None:
    print(f"[loci] {line}", flush=True)


def wait_until_up(url: str, timeout_s: float = 90) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def run() -> int:
    os.makedirs(BUCKETS, exist_ok=True)
    os.makedirs(GATEWAY_DATA, exist_ok=True)
    lock = hold_lock()
    loci_port, _ = ports(read_settings())
    panel = f"http://127.0.0.1:{loci_port}/loci"
    if lock is None:
        say(f"这个文件夹的 Loci 已经在另一个窗口里跑着了。面板：{panel}")
        webbrowser.open(panel)
        return 1

    settings = ensure_keys()
    loci_port, gateway_port = ports(settings)
    first_run = ensure_config(gateway_port)
    node = node_exe()
    loci_env, gateway_env = child_envs(settings, machine_zone(node))

    job = make_job()
    children = [
        Child("Loci", [sys.executable, os.path.join(APP, "src", "server.py")], loci_env, job),
        Child("网关", [node, os.path.join(APP, "gateway", "server.js")], gateway_env, job),
    ]
    for c in children:
        c.start()

    say(f"面板：{panel}")
    say(f"网关：http://127.0.0.1:{gateway_port}/v1")
    say(f"MCP：http://127.0.0.1:{loci_port}/mcp")
    say("这个窗口开着它们就开着；关掉窗口，两个一起停。")
    if first_run and wait_until_up(panel):
        webbrowser.open(panel)

    try:
        while True:
            now = time.monotonic()
            for c in children:
                c.check(now)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for c in children:
            c.stop()
        lock.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Start Loci and its gateway, and keep both running.")
    p.add_argument("--autostart", choices=("on", "off"), help="start at login, or stop doing so")
    a = p.parse_args(argv)
    if a.autostart:
        say(set_autostart(a.autostart == "on"))
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
