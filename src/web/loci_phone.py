"""
========================================
web/loci_phone.py — 「允许手机连」: this computer's Loci and gateway on the home network
========================================
    GET  /api/loci/phone   -> {available, on, live, addresses, here, key}
    POST /api/loci/phone   -> {on}: writes `phone_access` in config.yaml

Only under the portable package: package/launcher.py runs Loci with LOCI_LAUNCHER=1 and
hands it the gateway's port and passphrase (LOCI_GATEWAY_PORT, LOCI_GATEWAY_PASSPHRASE).
Anywhere else `available` is false and the switch is not offered — nothing here can
rebind a process someone else started.

In the Docker image the launcher runs with LOCI_LAUNCHER_BIND=0.0.0.0: both always
listen beyond the container, there is no switch (`fixed`), and the addresses carry
SERVER_PLACEHOLDER where the server's own address goes — from inside a container that
address cannot be known.

Loci never rebinds itself. The launcher reads `phone_access` and restarts both processes
on 0.0.0.0 (on) or 127.0.0.1 (off); `live` says which of the two this Loci was started
with, so the page can tell a switch that is on from a network that is open.

What a phone needs, per address of this machine on a private network:
  · gateway   http://<ip>:<gateway port>/<passphrase>/v1 — the passphrase is the gateway's
              door on a non-loopback bind (gateway/door.js)
  · MCP       http://<ip>:<loci port>/mcp, with the header `Authorization: Bearer <key>`;
              the key is the legacy host's (LOCI_HOOK_TOKEN), which MCP admits in either
              auth mode (server_app.identify_host)
`here` is the same pair for a client on this computer, switch on or off: loopback needs
no passphrase (gateway/door.js), and MCP needs the same key.
These are secrets and this is a panel route: behind the panel's lock, and never readable
with a host's credential (panel_auth.panel_refusal).
========================================
"""
import ipaddress
import os
import socket

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh
from ._guards import _write_body

logger = sh.logger

KEY = "phone_access"
SERVER_PLACEHOLDER = "<服务器地址>"
DEFAULT_LOCI_PORT = 18002
DEFAULT_GATEWAY_PORT = 3100


def available() -> bool:
    return os.environ.get("LOCI_LAUNCHER", "").strip() == "1"


def fixed() -> bool:
    """Under the launcher with its bind fixed (the Docker image): always open, no switch."""
    return bool(os.environ.get("LOCI_LAUNCHER_BIND", "").strip())


def switch_on(config) -> bool:
    raw = (config or {}).get(KEY)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def live() -> bool:
    """Whether this process listens beyond this machine."""
    bind = (os.environ.get("LOCI_BIND_HOST") or "0.0.0.0").strip()
    try:
        return not ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return bind != "localhost"


def _private_ipv4s() -> list:
    """This machine's addresses on private networks, the one the default route uses
    first. A UDP connect sends nothing; it only asks the OS which source address it would
    pick."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if a.is_private and not a.is_loopback and not a.is_link_local and ip not in out:
            out.append(ip)
    return out


def _port(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def view(config) -> dict:
    if not available():
        return {"available": False}
    loci_port = _port("LOCI_PORT", DEFAULT_LOCI_PORT)
    gateway_port = _port("LOCI_GATEWAY_PORT", DEFAULT_GATEWAY_PORT)
    passphrase = os.environ.get("LOCI_GATEWAY_PASSPHRASE", "").strip()
    hosts = [SERVER_PLACEHOLDER] if fixed() else _private_ipv4s()
    addresses = [{
        "ip": ip,
        "gateway": f"http://{ip}:{gateway_port}/{passphrase}/v1",
        "mcp": f"http://{ip}:{loci_port}/mcp",
    } for ip in hosts]
    return {
        "available": True,
        "fixed": fixed(),
        "on": True if fixed() else switch_on(config),
        "live": live(),
        "addresses": addresses,
        "here": {
            "gateway": f"http://127.0.0.1:{gateway_port}/v1",
            "mcp": f"http://127.0.0.1:{loci_port}/mcp",
        },
        "key": os.environ.get("LOCI_HOOK_TOKEN", "").strip(),
    }


async def api_loci_phone(request: Request) -> Response:
    return JSONResponse(view(sh.config), headers={"Cache-Control": "no-store"})


async def api_loci_phone_set(request: Request) -> Response:
    from utils import atomic_update_config_yaml
    if not available():
        return JSONResponse({"error": "只有用 Windows 包起的 Loci 能在这里开关"}, status_code=409)
    if fixed():
        return JSONResponse({"error": "Docker 里一直开着，没有开关"}, status_code=409)
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    want = body.get("on")
    if not isinstance(want, bool):
        return JSONResponse({"error": "on 是 true 或 false"}, status_code=400)

    def _mutate(cfg: dict) -> None:
        cfg[KEY] = want

    try:
        atomic_update_config_yaml(_mutate)
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] phone_access 写不进去: {e}")
        return JSONResponse({"error": f"写不进去：{e}"}, status_code=500)
    sh.config[KEY] = want
    return JSONResponse(view(sh.config))
