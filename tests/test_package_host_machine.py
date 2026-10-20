"""
The package launcher on a machine that already has things of its own (package/launcher.py):
another program on one of the two ports, Node variables set for the person's own projects,
and a gateway port moved after the first run.
"""
import importlib.util
import os
import socket

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = {"LOCI_HOOK_TOKEN": "a", "LOCI_HOST_TOKEN_GATEWAY": "b", "LOCI_FETCH_TOKEN_GATEWAY": "c",
            "LOCI_GATEWAY_PASSPHRASE": "q-" * 16}


def _launcher():
    spec = importlib.util.spec_from_file_location("loci_launcher", os.path.join(ROOT, "package", "launcher.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _listening():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_port_someone_listens_on_is_taken_and_a_free_one_is_not():
    L = _launcher()
    with _listening() as s:
        port = s.getsockname()[1]
        assert L.port_taken("127.0.0.1", port)
        assert L.port_taken("0.0.0.0", port)
    assert not L.port_taken("127.0.0.1", _free_port())


def test_a_taken_port_stops_the_launcher_before_anything_is_written_or_started(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    for k in ("LOCI_VAULT_DIR", "LOCI_CONFIG_PATH", "LOCI_LAUNCHER_BIND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCI_DATA_DIR", str(data))
    L = _launcher()
    monkeypatch.setattr(L, "make_job", lambda: (_ for _ in ()).throw(AssertionError("started")))
    with _listening() as s:
        port = s.getsockname()[1]
        monkeypatch.setenv("LOCI_PORT", str(_free_port()))
        monkeypatch.setenv("LOCI_GATEWAY_PORT", str(port))
        assert L.run() == 1
    out = capsys.readouterr().out
    assert f"端口 {port}" in out and f"LOCI_GATEWAY_PORT={port + 1}" in out
    assert "LOCI_PORT=" not in out              # the free one is not named
    assert not (data / "buckets" / "config.yaml").exists()
    # The lock was let go: the next start, after the port is moved, is not refused.
    lock = L.hold_lock()
    assert lock is not None
    lock.close()


def test_the_gateway_node_does_not_get_the_persons_node_variables(monkeypatch):
    L = _launcher()
    monkeypatch.setenv("NODE_OPTIONS", "--require C:/someones/hook.js")
    monkeypatch.setenv("NODE_PATH", "C:/someones/modules")
    monkeypatch.setenv("SOMETHING_ELSE", "kept")
    loci, gateway = L.child_envs(SETTINGS, "Asia/Shanghai", phone=False)
    assert "NODE_OPTIONS" not in gateway and "NODE_PATH" not in gateway
    assert gateway["SOMETHING_ELSE"] == "kept" and gateway["LOCI_GATEWAY_BIND"] == "127.0.0.1"


def test_moving_the_gateway_port_after_the_first_run_moves_the_hosts_table_with_it(tmp_path, monkeypatch):
    L = _launcher()
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(L, "CONFIG", str(cfg))
    passphrase = SETTINGS["LOCI_GATEWAY_PASSPHRASE"]
    # As the panel leaves it after writing 允许手机连: yaml.safe_dump, the URLs quoted.
    cfg.write_text(yaml.safe_dump({"phone_access": True,
                                   **yaml.safe_load(L.hosts_block(3100, passphrase))}), encoding="utf-8")
    L.follow_gateway_port(3101, passphrase)
    hosts = yaml.safe_load(cfg.read_text(encoding="utf-8"))["hosts"]
    assert hosts["gateway"]["present_url"] == f"http://127.0.0.1:3101/{passphrase}"
    assert hosts["gateway"]["fetch_url"] == f"http://127.0.0.1:3101/{passphrase}/loci/source"
    before = cfg.read_text(encoding="utf-8")
    L.follow_gateway_port(3101, passphrase)
    assert cfg.read_text(encoding="utf-8") == before
