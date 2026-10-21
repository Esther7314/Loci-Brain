"""
允许手机连 — the panel switch (web/loci_phone.py) and the launcher that acts on it
(package/launcher.py).

The switch exists only under the launcher: anywhere else the panel does not offer it and
a write is refused, because nothing could rebind a process someone else started. Under the
launcher it writes `phone_access` to config.yaml, the launcher reads that line back, and
restarts both processes on the network with the gateway behind its passphrase.
"""
import importlib.util
import os

import yaml

from _panel_kit import make_store, routes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _launcher():
    spec = importlib.util.spec_from_file_location("loci_launcher", os.path.join(ROOT, "package", "launcher.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _world(tmp_path, monkeypatch, launcher=True):
    make_store(tmp_path, monkeypatch)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("panel_auth: true\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(cfg))
    for k, v in {"LOCI_PORT": "18002", "LOCI_GATEWAY_PORT": "3100", "LOCI_BIND_HOST": "127.0.0.1",
                 "LOCI_GATEWAY_PASSPHRASE": "p" * 20, "LOCI_HOOK_TOKEN": "legacy-key"}.items():
        monkeypatch.setenv(k, v)
    if launcher:
        monkeypatch.setenv("LOCI_LAUNCHER", "1")
    else:
        monkeypatch.delenv("LOCI_LAUNCHER", raising=False)
    from web import loci_phone
    monkeypatch.setattr(loci_phone, "_private_ipv4s", lambda: ["192.168.1.20"])
    return routes(monkeypatch), cfg


def test_without_the_launcher_there_is_no_switch(tmp_path, monkeypatch):
    call, _cfg = _world(tmp_path, monkeypatch, launcher=False)
    out = call("GET", "/api/loci/phone")
    assert out.status == 200 and out.json == {"available": False}
    assert call("POST", "/api/loci/phone", body={"on": True}).status == 409


def test_the_switch_is_saved_and_the_addresses_carry_the_passphrase_and_key(tmp_path, monkeypatch):
    call, cfg = _world(tmp_path, monkeypatch)
    out = call("GET", "/api/loci/phone").json
    assert out["available"] and out["on"] is False and out["live"] is False
    out = call("POST", "/api/loci/phone", body={"on": True})
    assert out.status == 200 and out.json["on"] is True
    # Still listening on loopback: the launcher has not restarted it yet.
    assert out.json["live"] is False
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["phone_access"] is True
    [a] = out.json["addresses"]
    assert a == {"ip": "192.168.1.20", "gateway": f"http://192.168.1.20:3100/{'p' * 20}/v1",
                 "mcp": "http://192.168.1.20:18002/mcp"}
    assert out.json["key"] == "legacy-key"
    # A client on this computer: loopback, no passphrase, the same key — switch on or off.
    assert out.json["here"] == {"gateway": "http://127.0.0.1:3100/v1",
                                "mcp": "http://127.0.0.1:18002/mcp"}
    monkeypatch.setenv("LOCI_BIND_HOST", "0.0.0.0")
    assert call("GET", "/api/loci/phone").json["live"] is True
    assert call("POST", "/api/loci/phone", body={"on": "yes"}).status == 400


def test_in_the_docker_image_it_is_always_open_with_no_switch(tmp_path, monkeypatch):
    call, _cfg = _world(tmp_path, monkeypatch)
    monkeypatch.setenv("LOCI_LAUNCHER_BIND", "0.0.0.0")
    monkeypatch.setenv("LOCI_BIND_HOST", "0.0.0.0")
    out = call("GET", "/api/loci/phone").json
    assert out["fixed"] and out["on"] and out["live"]
    # The container's own address means nothing outside it: the server's is left to fill in.
    [a] = out["addresses"]
    assert a["gateway"] == f"http://<服务器地址>:3100/{'p' * 20}/v1" and a["mcp"] == "http://<服务器地址>:18002/mcp"
    assert call("POST", "/api/loci/phone", body={"on": False}).status == 409


def test_the_launcher_reads_the_switch_the_panel_wrote(tmp_path, monkeypatch):
    L = _launcher()
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(L, "CONFIG", str(cfg))
    assert L.phone_on() is False                       # no file yet
    cfg.write_text(yaml.safe_dump({"panel_auth": True, "phone_access": True}), encoding="utf-8")
    assert L.phone_on() is True
    cfg.write_text(yaml.safe_dump({"phone_access": False, "x": {"phone_access": True}}), encoding="utf-8")
    assert L.phone_on() is False                       # a nested key is not the switch


def test_the_launcher_binds_both_by_the_switch_and_loci_reaches_the_gateway_through_the_passphrase(tmp_path):
    L = _launcher()
    settings = {"LOCI_HOOK_TOKEN": "a", "LOCI_HOST_TOKEN_GATEWAY": "b", "LOCI_FETCH_TOKEN_GATEWAY": "c",
                "LOCI_GATEWAY_PASSPHRASE": "q" * 32}
    loci, gateway = L.child_envs(settings, "Asia/Shanghai", phone=False)
    assert (loci["LOCI_BIND_HOST"], gateway["LOCI_GATEWAY_BIND"]) == ("127.0.0.1", "127.0.0.1")
    assert loci["LOCI_LAUNCHER"] == "1" and loci["LOCI_GATEWAY_PORT"] == "3100"
    # Each side holds the other's key under its own name.
    assert (gateway["LOCI_HOOK_TOKEN"], gateway["LOCI_GATEWAY_TOKEN"]) == ("b", "c")
    assert gateway["LOCI_GATEWAY_PASSPHRASE"] == loci["LOCI_GATEWAY_PASSPHRASE"] == "q" * 32
    loci, gateway = L.child_envs(settings, "Asia/Shanghai", phone=True)
    assert (loci["LOCI_BIND_HOST"], gateway["LOCI_GATEWAY_BIND"]) == ("0.0.0.0", "0.0.0.0")
    # Unset LOCI_TZ: both get the same zone, never the gateway's own guess.
    assert loci["LOCI_TZ"] == gateway["LOCI_TZ"] == "Asia/Shanghai"
    assert L.child_envs(settings, "", phone=False)[1]["LOCI_TZ"] == L.LOCI_DEFAULT_TZ
    hosts = yaml.safe_load(L.hosts_block(3100, "q" * 32))["hosts"]
    assert hosts["gateway"]["present_url"] == f"http://127.0.0.1:3100/{'q' * 32}"
    assert hosts["gateway"]["fetch_url"] == f"http://127.0.0.1:3100/{'q' * 32}/loci/source"
