# -*- coding: utf-8 -*-
"""
tests/test_side_model_local_and_ollama.py — 模型 · 副模型 在哪儿跑 / Thinking, and finding Ollama.

The setting board gives the side model 在哪儿跑 (云端 / 本地) and a Thinking switch, and
both local modes list what the local Ollama holds:

  · `dehydration.runs_on: local` points the side model at the local Ollama's
    OpenAI-compatible endpoint with a placeholder key (core/dehydrator.endpoint): it needs
    no key, a saved cloud key is never sent there, and the cloud settings stay saved for
    switching back. POST /api/config sets it live and persists it; GET says which.
  · `dehydration.thinking`: on, Gemini gets no thinking cap and Anthropic gets extended
    thinking (its answer read from the text block after the thinking one); off, as before.
  · GET /api/loci/ollama asks the Ollama root core/ollama_local.root names (LOCI_OLLAMA_URL,
    else the embedding's local base_url, else the default) for its models, once, short.
"""

import asyncio
import json

import httpx
import pytest
import yaml

from core import dehydrator as Dm
from core import ollama_local as O

from _config_kit import config_world

CLOUD_KEY = "test-cloud-side-key-0123456789"


def _dehydrator(tmp_path, **dehy):
    return Dm.Dehydrator({"buckets_dir": str(tmp_path), "dehydration": dehy})


# ---------------------------------------------------------------- where it runs

def test_local_runs_on_ollama_with_a_placeholder_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCI_OLLAMA_URL", raising=False)
    d = _dehydrator(tmp_path, runs_on="local", api_key=CLOUD_KEY, api_format="anthropic",
                    base_url="https://api.example.com", model="qwen3")
    try:
        assert d.runs_on == "local" and d.api_available
        assert d.api_format == "openai_compat"
        assert d.base_url == O.default_root() + "/v1"
        assert d.api_key == "ollama", "a cloud key never goes to a local address"
        assert d.client is not None
    finally:
        d.close()


def test_cloud_is_as_configured_and_an_aq_key_goes_native(tmp_path):
    fmt, base, key = Dm.endpoint({"api_format": "openai_compat", "api_key": CLOUD_KEY,
                                  "base_url": "https://api.deepseek.com/v1"})
    assert (fmt, base, key) == ("openai_compat", "https://api.deepseek.com/v1", CLOUD_KEY)
    fmt, _base, _key = Dm.endpoint({"api_format": "openai_compat", "api_key": "AQ.xyz",
                                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"})
    assert fmt == "gemini"


def test_the_ollama_root_order(monkeypatch):
    monkeypatch.delenv("LOCI_OLLAMA_URL", raising=False)
    assert O.root({}) == O.default_root()
    local_emb = {"embedding": {"api_format": "ollama", "base_url": "http://box:11434/v1"}}
    assert O.root(local_emb) == "http://box:11434"
    cloud_emb = {"embedding": {"api_format": "openai_compat", "base_url": "http://box:11434/v1"}}
    assert O.root(cloud_emb) == O.default_root(), "a cloud embedding's address is not Ollama's"
    monkeypatch.setenv("LOCI_OLLAMA_URL", "http://env-host:11434/v1/")
    assert O.root(local_emb) == "http://env-host:11434"
    assert O.openai_base(local_emb) == "http://env-host:11434/v1"


@pytest.fixture
def world(tmp_path, monkeypatch):
    from web import _shared as sh
    monkeypatch.delenv("LOCI_OLLAMA_URL", raising=False)
    w = config_world(tmp_path, monkeypatch, "dehydration:\n  model: side-model\n",
                     {"dehydration": {"model": "side-model", "api_format": "openai_compat",
                                      "base_url": "https://api.deepseek.com/v1",
                                      "api_key": CLOUD_KEY}})
    d = Dm.Dehydrator(w["config"])
    monkeypatch.setattr(sh, "dehydrator", d)
    yield {**w, "dehydrator": d}
    d.close()


def _saved(world) -> dict:
    return yaml.safe_load(world["path"].read_text(encoding="utf-8")) or {}


def test_switching_to_local_and_back_through_the_config_route(world):
    d = world["dehydrator"]
    status, out = world["call"]("POST", "/api/config", {"persist": True, "dehydration": {
        "runs_on": "local", "model": "qwen3"}})
    assert status == 200, out
    assert "dehydration.runs_on" in out["updated"]
    assert (d.runs_on, d.api_format, d.api_key, d.model) == ("local", "openai_compat", "ollama", "qwen3")
    assert d.base_url == O.default_root() + "/v1" and d.api_available
    assert _saved(world)["dehydration"]["runs_on"] == "local"
    got = world["call"]("GET", "/api/config")[1]["dehydration"]
    assert got["runs_on"] == "local" and got["base_url"] == "https://api.deepseek.com/v1", \
        "the cloud address stays saved for switching back"

    status, out = world["call"]("POST", "/api/config", {"persist": True, "dehydration": {
        "runs_on": "cloud", "model": "deepseek-chat"}})
    assert status == 200, out
    assert (d.runs_on, d.api_key, d.base_url) == ("cloud", CLOUD_KEY, "https://api.deepseek.com/v1")
    assert _saved(world)["dehydration"]["runs_on"] == "cloud"


def test_runs_on_is_cloud_or_local(world):
    status, out = world["call"]("POST", "/api/config", {"dehydration": {"runs_on": "moon"}})
    assert status == 400 and "runs_on" in out["error"]
    assert "runs_on" not in world["config"]["dehydration"]


def test_the_thinking_switch_is_set_live_and_persisted(world):
    assert world["call"]("GET", "/api/config")[1]["dehydration"]["thinking"] is False
    status, out = world["call"]("POST", "/api/config", {"persist": True, "dehydration": {
        "thinking": True}})
    assert status == 200, out
    assert world["dehydrator"].thinking is True
    assert _saved(world)["dehydration"]["thinking"] is True
    assert world["call"]("GET", "/api/config")[1]["dehydration"]["thinking"] is True


# ---------------------------------------------------------------- what thinking sends

class _Sent:
    def __init__(self, reply):
        self.reply = reply
        self.payloads = []


def _fake_client(monkeypatch, sent):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return sent.reply

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.payloads.append(json)
            return _Resp()
    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_gemini_thinking_lifts_the_cap(tmp_path, monkeypatch):
    sent = _Sent({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    _fake_client(monkeypatch, sent)
    off = _dehydrator(tmp_path, api_format="gemini", api_key=CLOUD_KEY, model="gemini-2.5-flash")
    on = _dehydrator(tmp_path, api_format="gemini", api_key=CLOUD_KEY, model="gemini-2.5-flash",
                     thinking=True)
    try:
        assert asyncio.run(off._chat_once("s", "u")) == "ok"
        assert asyncio.run(on._chat_once("s", "u")) == "ok"
    finally:
        off.close()
        on.close()
    assert sent.payloads[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingConfig" not in sent.payloads[1]["generationConfig"]


def test_anthropic_thinking_sends_a_budget_and_reads_the_text_block(tmp_path, monkeypatch):
    sent = _Sent({"content": [{"type": "thinking", "thinking": "hmm"},
                              {"type": "text", "text": "the answer"}]})
    _fake_client(monkeypatch, sent)
    d = _dehydrator(tmp_path, api_format="anthropic", api_key=CLOUD_KEY, model="claude-x",
                    max_tokens=1024, thinking=True)
    try:
        assert asyncio.run(d._chat_once("s", "u")) == "the answer"
    finally:
        d.close()
    p = sent.payloads[0]
    assert p["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert p["max_tokens"] > p["thinking"]["budget_tokens"]
    assert "temperature" not in p


def test_anthropic_without_thinking_is_unchanged(tmp_path, monkeypatch):
    sent = _Sent({"content": [{"type": "text", "text": "plain"}]})
    _fake_client(monkeypatch, sent)
    d = _dehydrator(tmp_path, api_format="anthropic", api_key=CLOUD_KEY, model="claude-x",
                    max_tokens=900, temperature=0.1)
    try:
        assert asyncio.run(d._chat_once("s", "u")) == "plain"
    finally:
        d.close()
    p = sent.payloads[0]
    assert "thinking" not in p and p["temperature"] == 0.1 and p["max_tokens"] == 900


# ---------------------------------------------------------------- finding Ollama

def _mock_ollama(monkeypatch, handler):
    real = httpx.AsyncClient
    seen = {}

    def client(*a, **k):
        seen.update(k)
        k.pop("trust_env", None)
        return real(*a, transport=httpx.MockTransport(handler), **k)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


def test_ollama_found_with_its_models(world, monkeypatch):
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"},
                                                    {"name": "qwen3:8b"}]})
    seen = _mock_ollama(monkeypatch, handler)
    status, out = world["call"]("GET", "/api/loci/ollama")
    assert status == 200
    assert out == {"running": True, "base_url": O.default_root(), "models": ["bge-m3", "qwen3:8b"]}
    assert asked == [O.default_root() + "/api/tags"]
    assert seen["trust_env"] is False and seen["timeout"] <= 3, "short, and around any proxy"


def test_ollama_not_there(world, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)
    _mock_ollama(monkeypatch, handler)
    status, out = world["call"]("GET", "/api/loci/ollama")
    assert status == 200
    assert out["running"] is False and out["models"] == [] and out["error"]


def test_the_setup_row_counts_a_local_side_model_as_configured(monkeypatch):
    from core import health as Hh
    monkeypatch.delenv("LOCI_OLLAMA_URL", raising=False)
    rows = Hh._Rows()
    Hh.setup_summariser(rows, {"dehydration": {"runs_on": "local", "model": "qwen3"}})
    row = rows.rows[0]
    assert row["ok"] and "qwen3" in row["now"] and O.default_root() in row["now"]
    json.dumps(rows.rows)
