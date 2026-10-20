# -*- coding: utf-8 -*-
"""
tests/test_health_after_upgrade.py — what the 1.4.0 -> 2.0.0 rehearsal found the health
check could not see.

  · An embedding backend that does not answer: configured is not working. The engine
    keeps how its latest real call went, and the 向量 row goes red on a failure (and back
    to green on the next success) — no probe of its own, so a cold model load does not
    flash it red.
  · The gateway beside Loci: asked the way the present page asks it, so the two cannot
    disagree. Under the package launcher no pairing is a fault (an upgraded config never
    gets the hosts table); elsewhere no gateway is a choice and there is no row.
  · A library behind the code: the migration command in the form that runs where Loci
    runs (inside the Docker image, `docker compose run`; else with the library's path).
"""

import asyncio

import httpx
import pytest

from core import health as H
from core.embedding_engine import EmbeddingEngine
from web import _shared as sh
from web import loci_present as LP

from test_present_proxy import FakeGateway, IN_KEY, OUT_KEY, _gateway_host


def _rows(tmp_path, **kw):
    from core.bucket_manager import BucketManager
    out = asyncio.run(H.health(BucketManager({"buckets_dir": str(tmp_path)}),
                               {"buckets_dir": str(tmp_path),
                                "embedding": {"enabled": True, "model": "bge-m3",
                                              "base_url": "http://ollama:11434/v1"}},
                               lambda _bd: {"persistent": True}, **kw))
    return {c["key"]: c for c in out["checks"]}


# ───────────────────────── embedding ─────────────────────────

def test_a_failed_last_embedding_call_is_red_and_a_good_one_or_none_is_green(tmp_path):
    row = _rows(tmp_path, embedding_last={"ok": False, "at": "10-20 17:03", "why": ""})["embedding"]
    assert row["status"] == "error"
    assert "只剩字面" in row["message"] and "ollama:11434" in row["action"]
    assert _rows(tmp_path, embedding_last={"ok": True, "at": "10-20 17:04", "why": ""})["embedding"]["status"] == "ok"
    assert _rows(tmp_path, embedding_last=None)["embedding"]["status"] == "ok"


class _Backend:
    def __init__(self, answers):
        self.answers = list(answers)

    async def generate_async(self, text):
        return self.answers.pop(0)


def test_the_engine_remembers_how_its_last_real_call_went(tmp_path):
    engine = EmbeddingEngine({"buckets_dir": str(tmp_path), "embedding": {"enabled": False}})
    assert engine.last_call() is None
    engine._backend = _Backend([[], [0.1, 0.2]])
    engine.enabled = True

    async def two_calls():
        await engine._generate_async("a")
        first = engine.last_call()
        await engine._generate_async("b")
        return first, engine.last_call()
    first, second = asyncio.run(two_calls())
    assert first["ok"] is False and second["ok"] is True


# ───────────────────────── the library's version ─────────────────────────

def _behind(tmp_path):
    (tmp_path / "dynamic").mkdir()
    (tmp_path / "dynamic" / "a_aaaaaaaaaaaa.md").write_text(
        "---\nid: aaaaaaaaaaaa\nroom: EVENT/SELF\n---\nbody\n", encoding="utf-8")


def test_the_migration_command_is_the_docker_one_inside_the_image(tmp_path):
    _behind(tmp_path)
    row = _rows(tmp_path, in_docker=True)["schema"]
    assert row["status"] == "error"
    assert "docker compose run --rm --entrypoint python loci-brain scripts/migrate.py " \
           "--buckets /app/buckets" in row["action"]


def test_the_migration_command_names_the_library_outside_docker(tmp_path):
    _behind(tmp_path)
    row = _rows(tmp_path, in_docker=False)["schema"]
    assert f"python scripts/migrate.py --buckets {tmp_path}" in row["action"]


# ───────────────────────── the gateway ─────────────────────────

def test_no_gateway_is_no_row_unless_the_launcher_runs_one_beside(tmp_path):
    assert "gateway" not in _rows(tmp_path, gateway={"host": None, "why": "", "beside": False})
    row = _rows(tmp_path, gateway={"host": None, "why": "", "beside": True})["gateway"]
    assert row["status"] == "error" and "hosts 表" in row["action"]


def test_a_paired_gateway_is_green_and_one_that_does_not_answer_is_red(tmp_path):
    assert _rows(tmp_path, gateway={"host": "gateway", "why": "", "beside": True})["gateway"]["status"] == "ok"
    row = _rows(tmp_path, gateway={"host": "gateway", "why": "连不上网关（ConnectError）",
                                   "beside": True})["gateway"]
    assert row["status"] == "error" and "ConnectError" in row["message"]


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    from _panel_kit import make_store
    make_store(tmp_path / "lib", monkeypatch)
    gw = FakeGateway()
    monkeypatch.setattr(LP, "_transport", httpx.MockTransport(gw.handler))
    monkeypatch.setenv("T_GATEWAY_IN", IN_KEY)
    monkeypatch.setenv("T_GATEWAY_OUT", OUT_KEY)
    monkeypatch.setenv("LOCI_LAUNCHER", "1")
    return gw


def test_gateway_reach_asks_as_the_present_page_does(gateway):
    sh.config.pop("hosts", None)
    assert asyncio.run(LP.gateway_reach()) == {"host": None, "why": "", "beside": True}
    sh.config["hosts"] = {"gateway": _gateway_host()}
    assert asyncio.run(LP.gateway_reach()) == {"host": "gateway", "why": "", "beside": True}
    assert gateway.seen[-1]["path"].endswith("/present") and gateway.seen[-1]["auth"] == f"Bearer {OUT_KEY}"
    gateway.reply("GET", gateway.seen[-1]["path"], 401, b"{}")
    got = asyncio.run(LP.gateway_reach())
    assert got["host"] == "gateway" and "不认 Loci 的钥匙" in got["why"]
