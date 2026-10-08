# -*- coding: utf-8 -*-
"""
tests/test_panel_muse_nudge.py — 「戳一下」 on muse's page (POST /api/loci/muse/nudge,
core/_nudge.py).

Poking a cluster writes one line the model is told once, at the end of the next Loci tool
reply: how many entries the cluster holds and the first one's label as the panel shows it.
Said once, it does not come back; poking again is the same poke. A call that may not see
every member is not told. The write is same-origin JSON, the panel's alone, and goes into
`_state/muse_nudges.json`, never the ledger.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _panel_kit import make_store, routes, run
from core import _muse as M
from core import _nudge as NG
from core import _when as W
from core import muse_view as MV
from core.profile import entry_label

ROOT = Path(__file__).resolve().parent.parent


def _cluster(store, n=3):
    texts = ["第一次觉得慢一点也可以。", "等回消息的时候不再刷新。", "原来停下来不是落后。"][:n]
    ids = [run(store.create(t, room="MIND/TRAITS", valence=0.6, arousal=0.3)) for t in texts]
    items = [M.Item(id=i, room="MIND/TRAITS", ts=None, created=W.now(), v=0.6, a=0.3,
                    tags=[], text="") for i in ids]
    return M.Cluster(ids=ids, items=items, shelf_v=0.6, shelf_a=0.3, from_core=[],
                     semantic_add=ids)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    cluster = _cluster(store)

    async def both_sides(force=False, scope=None):
        return [cluster], 0, 0, {}, {}
    monkeypatch.setattr(M, "both_sides", both_sides)
    return {"store": store, "cluster": cluster, "id": MV.cluster_id(cluster.ids),
            "call": routes(monkeypatch), "base": str(tmp_path)}


def _first_label(store, bid):
    b = run(store.get(bid))
    return entry_label(b["metadata"], b["content"])


async def _whole_library():
    return None


def _take(panel, scope_of=_whole_library):
    return run(NG.take_lines(panel["base"], panel["store"], scope_of, W.now()))


def test_a_poke_is_said_once_with_the_count_and_the_first_label(panel):
    call, cid = panel["call"], panel["id"]
    assert call("GET", "/api/loci/muse").json["items"][0]["nudge"] == {"state": "none"}
    out = call("POST", "/api/loci/muse/nudge", body={"cluster": cid})
    assert out.status == 200 and out.json == {"ok": True, "cluster": cid, "state": "poked"}
    nudge = call("GET", "/api/loci/muse").json["items"][0]["nudge"]
    assert nudge["state"] == "poked" and nudge["at"]

    label = _first_label(panel["store"], panel["cluster"].ids[0])
    # The owner's wording, word for word; "ta" because whoever pokes may be anyone.
    line = NG.line_of(3, label)
    assert line == f"ta戳了戳你：这团（3 条，第一条是《{label}》）"
    assert _take(panel) == [line]
    assert _take(panel) == [], "said once, never again"
    seen = call("GET", "/api/loci/muse").json["items"][0]["nudge"]
    assert seen["state"] == "seen" and seen["at"]
    # Poking it again changes nothing: it was said.
    again = call("POST", "/api/loci/muse/nudge", body={"cluster": cid})
    assert again.json["state"] == "seen" and _take(panel) == []


def test_several_pokes_are_one(panel):
    call, cid = panel["call"], panel["id"]
    first = call("POST", "/api/loci/muse/nudge", body={"cluster": cid})
    at = NG.states(panel["base"])[cid]["at"]
    second = call("POST", "/api/loci/muse/nudge", body={"cluster": cid})
    assert first.json == second.json and NG.states(panel["base"])[cid]["at"] == at
    assert len(_take(panel)) == 1


class _Hides:
    """A read scope that may not read one id."""

    def __init__(self, hidden):
        self.hidden = hidden

    def permits(self, meta):
        return str(meta.get("id")) != self.hidden


def test_a_call_that_may_not_see_a_member_is_not_told(panel):
    panel["call"]("POST", "/api/loci/muse/nudge", body={"cluster": panel["id"]})

    async def scoped():
        return _Hides(panel["cluster"].ids[-1])
    assert _take(panel, scoped) == []
    assert NG.states(panel["base"])[panel["id"]]["state"] == "poked", "still waiting"
    assert len(_take(panel)) == 1


def test_a_member_put_away_since_keeps_it_unsaid(panel):
    panel["call"]("POST", "/api/loci/muse/nudge", body={"cluster": panel["id"]})
    assert run(panel["store"].delete(panel["cluster"].ids[1]))
    assert _take(panel) == []


def test_the_write_is_same_origin_json_and_names_a_cluster_laid_out_now(panel, monkeypatch):
    call, cid = panel["call"], panel["id"]
    assert call("POST", "/api/loci/muse/nudge", body={"cluster": cid}, origin=False).status == 403
    assert call("POST", "/api/loci/muse/nudge", body={"cluster": cid},
                ctype="text/plain").status == 400
    assert call("POST", "/api/loci/muse/nudge", body={}).status == 400
    assert call("POST", "/api/loci/muse/nudge", body={"cluster": "c_0000000000"}).status == 404
    assert NG.load(panel["base"]) == {}
    from web import panel_auth as PA
    assert "/api/loci/muse/nudge" not in PA.HOOK_PATHS
    locked = routes(monkeypatch, locked=True)
    assert locked("POST", "/api/loci/muse/nudge", body={"cluster": cid},
                  key="s3cret").status == 401


def test_a_poke_writes_its_own_file_and_not_the_ledger(panel, tmp_path):
    ledger = list(panel["store"].ledger_mirror.iter_events())
    panel["call"]("POST", "/api/loci/muse/nudge", body={"cluster": panel["id"]})
    assert (tmp_path / "_state" / NG.FILE).exists()
    assert list(panel["store"].ledger_mirror.iter_events()) == ledger
    _take(panel)
    assert list(panel["store"].ledger_mirror.iter_events()) == ledger
    kept = json.loads((tmp_path / "_state" / NG.FILE).read_text(encoding="utf-8"))
    assert set(kept["nudges"][panel["id"]]) == {"ids", "poked_at", "seen_at"}


# ───────────────────────── the line rides on the next tool reply ─────────────────────────

_SERVER_RUN = r"""
import asyncio, json, sys
import server
from core import _nudge, _when
from core import muse_view

async def main():
    store = server.bucket_mgr
    ids = [await store.create(t, room="MIND/TRAITS", valence=0.6, arousal=0.3)
           for t in ("one thought about slowness", "another one", "a third")]
    _nudge.poke(store.base_dir, muse_view.cluster_id(ids), ids, _when.now())

    async def tool():
        return "tool output"
    first = await server._with_notice(tool(), op="tool")
    second = await server._with_notice(tool(), op="grow")
    print(json.dumps({"first": first, "second": second}, ensure_ascii=False))

asyncio.run(main())
"""


def test_the_next_tool_reply_carries_the_line_once(tmp_path):
    env = {**os.environ, "LOCI_BUCKETS_DIR": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    env.pop("LOCI_SCOPE", None)
    env.pop("LOCI_HOST_TOKEN", None)
    done = subprocess.run([sys.executable, "-c", _SERVER_RUN], cwd=str(ROOT / "src"), env=env,
                          capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
    out = json.loads(done.stdout.decode("utf-8").strip().splitlines()[-1])
    assert out["first"] == "tool output\n\n" + NG.line_of(3, "one thought about slowness")
    assert out["second"] == "tool output"
