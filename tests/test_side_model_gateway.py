# -*- coding: utf-8 -*-
"""
tests/test_side_model_gateway.py — the side model "same as the gateway"
(`dehydration.api_format: gateway`, core/gateway_side.py).

  · /api/config takes the format with a model and refuses it without one; base_url and
    api_key are neither asked for nor used; the other formats keep their old rules;
  · a side call goes to the gateway that `hosts.<name>.present_url` names, at
    /loci/upstream/v1/chat/completions, with Loci's fetch credential as Bearer and an
    OpenAI-style body whose model is Loci's own; the answer's text comes back;
  · the gateway holding no key yet is said in its own words, once (not retried); a gateway
    that cannot be reached is retried like a provider's connection error, and no error
    carries the URL (a passphrase lives in it);
  · no gateway host is said before anything is sent;
  · a failure is an ordinary side-model failure: the backfill writes nothing it would have
    asked the model for (the contract in test_contract_backfill_failure_keeps_body.py);
  · POST /api/test/dehydration and POST /api/models go through the gateway too.

The gateway is an in-process httpx transport: no socket is opened.
"""

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from core import dehydrator as D
from core import gateway_side as G

FETCH_TOKEN = "loci-to-gateway-fetch-token-9f2c"
PASSPHRASE = "pass-side-4d2a"
NO_KEY_WORDS = "网关还没有 key：等 ta 先说一句，或者在网关上游里填一把固定的 key"


class FakeGateway:
    """The gateway's side door: records each request and answers what it is told to."""

    def __init__(self):
        self.seen = []
        self.answer = (200, {"choices": [{"index": 0, "message": {"role": "assistant",
                                                                    "content": "标签：海边"}}]}, {})
        self.fail = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append({"method": request.method, "path": request.url.path,
                          "auth": request.headers.get("authorization"),
                          "body": json.loads(request.content) if request.content else None})
        if self.fail is not None:
            raise self.fail
        status, body, headers = self.answer
        return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(G, "_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setenv("LOCI_FETCH_TOKEN_GATEWAY", FETCH_TOKEN)
    monkeypatch.setenv("LOCI_HOST_TOKEN_GATEWAY", "gateway-to-loci-host-token")
    monkeypatch.setattr(D, "_RETRY_BASE_DELAY", 0.0)
    return fake


def _config(tmp_path, **dehy):
    return {
        "buckets_dir": str(tmp_path),
        "dehydration": {"api_format": "gateway", "model": "side-x", **dehy},
        "hosts": {"gateway": {
            "token_env": "LOCI_HOST_TOKEN_GATEWAY", "scope_mode": "open",
            "fetch_token_env": "LOCI_FETCH_TOKEN_GATEWAY",
            "present_url": f"http://127.0.0.1:3100/{PASSPHRASE}/",
        }},
    }


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── the call itself ─────────────────────────

def test_a_side_call_goes_through_the_gateway_with_only_the_model_of_its_own(tmp_path, gateway):
    d = D.Dehydrator(_config(tmp_path, base_url="https://old.example/v1", api_key="sk-saved-for-later"))
    try:
        assert (d.api_format, d.base_url, d.api_key) == ("gateway", "", "")
        assert d.api_available is True and d.client is None
        d._require_api()
        assert run(d._chat("只回 JSON", "打个标签", max_tokens=64, temperature=0.0)) == "标签：海边"
    finally:
        d.close()
    assert len(gateway.seen) == 1
    got = gateway.seen[0]
    assert got["method"] == "POST"
    assert got["path"] == f"/{PASSPHRASE}/loci/upstream/v1/chat/completions"
    assert got["auth"] == f"Bearer {FETCH_TOKEN}"
    assert got["body"] == {"model": "side-x", "max_tokens": 64, "temperature": 0.0, "messages": [
        {"role": "system", "content": "只回 JSON"}, {"role": "user", "content": "打个标签"}]}
    assert "sk-saved-for-later" not in json.dumps(gateway.seen), "the saved cloud key is not used"


def test_the_slicer_model_rides_the_same_road(tmp_path, gateway):
    d = D.Dehydrator(_config(tmp_path))
    try:
        run(d._chat("s", "u", model="slicer-y"))
    finally:
        d.close()
    assert gateway.seen[0]["body"]["model"] == "slicer-y"


def test_no_key_at_the_gateway_is_said_in_its_words_once(tmp_path, gateway):
    gateway.answer = (409, {"error": {"message": NO_KEY_WORDS, "code": "no_key"}}, {})
    d = D.Dehydrator(_config(tmp_path))
    try:
        with pytest.raises(G.GatewaySideError) as got:
            run(d._chat("s", "u"))
    finally:
        d.close()
    assert str(got.value) == NO_KEY_WORDS
    assert len(gateway.seen) == 1, "not a transient error: asked once"


def test_an_unreachable_gateway_is_retried_and_never_named_by_its_url(tmp_path, gateway):
    gateway.fail = httpx.ConnectError("connect failed to http://127.0.0.1:3100/" + PASSPHRASE)
    d = D.Dehydrator(_config(tmp_path))
    try:
        with pytest.raises(G.GatewaySideError) as got:
            run(d._chat("s", "u"))
    finally:
        d.close()
    assert len(gateway.seen) == D._RETRY_MAX_ATTEMPTS
    assert "连不上网关" in str(got.value) and PASSPHRASE not in str(got.value)


def test_the_gateway_refusing_lokis_key_and_upstream_errors_are_told_apart(tmp_path, gateway):
    d = D.Dehydrator(_config(tmp_path))
    try:
        gateway.answer = (401, {"error": "要带 Authorization: Bearer <LOCI_GATEWAY_TOKEN>"},
                          {"www-authenticate": "Bearer"})
        with pytest.raises(G.GatewaySideError, match="网关不认 Loci 的钥匙"):
            run(d._chat("s", "u"))
        gateway.answer = (401, {"error": {"message": "Incorrect API key provided: ••••"}}, {})
        with pytest.raises(G.GatewaySideError, match=r"上游回了 HTTP 401：Incorrect API key"):
            run(d._chat("s", "u"))
        gateway.answer = (404, {"connected": False, "error": "关着"}, {})
        with pytest.raises(G.GatewaySideError, match="LOCI_GATEWAY_TOKEN"):
            run(d._chat("s", "u"))
        seen = len(gateway.seen)
        gateway.answer = (503, {"error": {"message": "overloaded"}}, {})
        with pytest.raises(G.GatewaySideError) as got:
            run(d._chat("s", "u"))
        assert got.value.status_code == 503
        assert len(gateway.seen) - seen == D._RETRY_MAX_ATTEMPTS, "an upstream 5xx is retried as any provider's"
    finally:
        d.close()


def test_no_gateway_host_is_said_before_anything_is_sent(tmp_path, gateway):
    config = _config(tmp_path)
    config.pop("hosts")
    d = D.Dehydrator(config)
    try:
        with pytest.raises(G.GatewaySideError, match="还没接上网关"):
            run(d._chat("s", "u"))
    finally:
        d.close()
    two = _config(tmp_path)
    two["hosts"]["second"] = dict(two["hosts"]["gateway"], token_env="LOCI_HOST_TOKEN_SECOND")
    with pytest.raises(G.GatewaySideError, match="好几个宿主"):
        G.find_gateway(two)
    assert gateway.seen == []


def test_a_failure_through_the_gateway_is_an_ordinary_side_model_failure(tmp_path, gateway, monkeypatch):
    # The backfill's contract (test_contract_backfill_failure_keeps_body.py) with the real
    # dehydrator on the gateway road: nothing it would have asked the model for is written.
    from core import names as S
    from core import runtime as rt
    from tools.grow import rooms_path as R
    from test_contract_backfill_failure_keeps_body import BODY, FakeLogger, FakeStore
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(tmp_path / "aliases.yaml"))
    monkeypatch.setattr(S, "_cache", None)
    gateway.answer = (409, {"error": {"message": NO_KEY_WORDS, "code": "no_key"}}, {})
    d = D.Dehydrator(_config(tmp_path))
    store = FakeStore()
    logger = FakeLogger()
    monkeypatch.setattr(rt, "dehydrator", d)
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "logger", logger)
    try:
        run(R._backfill_one("b1", BODY, "event"))
    finally:
        d.close()
    assert len(gateway.seen) == 1
    for invented in ("tags", "subjects", "domain", "aliases", "content"):
        assert all(invented not in u for u in store.updates), invented
    assert any("b1" in line for line in logger.lines)


# ───────────────────────── the panel's routes ─────────────────────────

@pytest.fixture
def panel(tmp_path, monkeypatch, gateway):
    from web import _shared as sh
    from web import config_api as CA
    from web import panel_auth as PA

    for name in ("LOCI_COMPRESS_API_KEY", "LOCI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dehydration:\n  model: side-model\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    config = _config(tmp_path, api_format="openai_compat", model="side-model",
                     base_url="https://api.example/v1", api_key="sk-cloud-key-1234")
    config["transport"] = "stdio"
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    d = D.Dehydrator(config)
    monkeypatch.setattr(sh, "dehydrator", d)

    found = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                found[(methods[0], path)] = fn
                return fn
            return keep
    CA.register(_Mcp())

    def call(method, path, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        async def go():
            req = Request({"type": "http", "method": method, "path": path, "query_string": b"",
                           "headers": [(b"host", b"127.0.0.1:8000"),
                                       (b"origin", b"http://127.0.0.1:8000"),
                                       (b"content-type", b"application/json")]}, receive)
            resp = await found[(method, path)](req)
            return resp.status_code, json.loads(resp.body)
        return asyncio.run(go())
    yield {"call": call, "config": config, "dehydrator": d, "path": config_path}
    d.close()


def test_config_takes_gateway_with_a_model_and_refuses_it_without_one(panel):
    status, out = panel["call"]("POST", "/api/config", {"dehydration": {"api_format": "gateway", "model": ""}})
    assert status == 400 and "模型名" in out["error"]
    assert panel["config"]["dehydration"]["api_format"] == "openai_compat", "nothing applied"

    status, out = panel["call"]("POST", "/api/config",
                                {"persist": True, "dehydration": {"api_format": " Gateway ", "model": "side-x"}})
    assert status == 200, out
    d = panel["dehydrator"]
    assert panel["config"]["dehydration"]["api_format"] == "gateway"
    assert (d.api_format, d.base_url, d.api_key, d.api_available, d.client) == ("gateway", "", "", True, None)
    import yaml
    saved = yaml.safe_load(panel["path"].read_text(encoding="utf-8"))["dehydration"]
    assert saved["api_format"] == "gateway" and saved["model"] == "side-x"
    assert panel["config"]["dehydration"]["api_key"] == "sk-cloud-key-1234", "kept for switching back"

    # back to a provider: its own address and key again, its old rules (no model required)
    status, _ = panel["call"]("POST", "/api/config", {"dehydration": {"api_format": "openai_compat", "model": ""}})
    assert status == 200
    assert (d.api_format, d.base_url, d.api_key) == ("openai_compat", "https://api.example/v1", "sk-cloud-key-1234")
    assert d.client is not None


def test_the_test_and_the_model_list_go_through_the_gateway(panel, gateway):
    panel["call"]("POST", "/api/config", {"dehydration": {"api_format": "gateway", "model": "side-x"}})
    status, out = panel["call"]("POST", "/api/test/dehydration", {})
    assert status == 200 and out["ok"] is True, out
    assert gateway.seen[-1]["path"].endswith("/loci/upstream/v1/chat/completions")
    assert gateway.seen[-1]["body"]["model"] == "side-x"

    gateway.answer = (409, {"error": {"message": NO_KEY_WORDS, "code": "no_key"}}, {})
    status, out = panel["call"]("POST", "/api/test/dehydration", {})
    assert out == {"ok": False, "error": NO_KEY_WORDS}

    gateway.answer = (200, {"object": "list", "data": [{"id": "zeta"}, {"id": "alpha"}]}, {})
    for body in ({"api_format": "gateway"}, {"api_key": "__use_current__"}):
        status, out = panel["call"]("POST", "/api/models", body)
        assert (status, out) == (200, {"ok": True, "models": ["alpha", "zeta"]}), body
        assert gateway.seen[-1]["method"] == "GET"
        assert gateway.seen[-1]["path"] == f"/{PASSPHRASE}/loci/upstream/v1/models"
        assert gateway.seen[-1]["auth"] == f"Bearer {FETCH_TOKEN}"
