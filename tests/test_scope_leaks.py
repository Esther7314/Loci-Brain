# -*- coding: utf-8 -*-
"""
tests/test_scope_leaks.py — an id the caller may not read is not named to it.

A hold card under a scope names the entry it hangs on only when the scope may read it; a
host with a ceiling finds in its change receipt and in `/changes` only the memories it may
reconcile (every source behind them inside its ceiling), never the ids of the others.
"""

import json

from core import _ledger as L
from core import _source_change as SC
from core import _sources as S
from core import scope as SCOPE
from core.bucket_manager import BucketManager
from core.scope import Host

from test_cue import Store, ask, bucket, clock, names  # noqa: F401
from test_source_change import change, run


TG = {"system": "telegram", "instance": "b", "container": "g"}


def _view(store):
    req = SCOPE.RequestScope.resolve(
        Host("bot", max_grant=None),
        json.dumps({"v": 1, "entry": TG, "venue": "group", "audience": ["user:U"],
                    "grant": [TG]}))
    return SCOPE.ScopeView(req, {r["id"]: r["metadata"] for r in store.rows}, store.sources)


def test_a_hold_card_names_its_target_only_when_the_scope_may_read_it(tmp_path, clock, names):
    trip = bucket("k", "考完带去海边玩。", direction_of_fit="telic", bound=["AI"])
    hold = bucket("l", "考完之前别提出去玩。", direction_of_fit="telic", exception_of=trip["id"],
                  hold="defer", cue={"condition": "考完", "phrasings": ["考完"]},
                  sources=[{**TG, "id": "m_1"}])
    store = Store(tmp_path, [trip, hold])
    out = ask(store, "考完了", scope=_view(store))
    assert [c["id"] for c in out["cards"]] == [hold["id"]]
    assert trip["id"][:6] not in out["text"], out["text"]
    open_ = ask(store, "考完了", window="w2")
    assert trip["id"][:6] in open_["text"], "unscoped: as before"


def test_a_receipt_and_the_changes_name_only_what_the_host_may_reconcile(tmp_path, monkeypatch):
    from tools import _runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    line = {**TG, "id": "m_1"}
    mine = run(store.create("群里说的话。", sources=[line]))
    mixed = run(store.create("群里和私聊一起说的。",
                             sources=[line, {"system": "lento", "instance": "home",
                                             "container": "p", "id": "x"}]))
    bot = Host("bot", max_grant=(S.Place("telegram", "b"),), authority=(S.Place("telegram", "b"),))
    hosts = SCOPE.Hosts([bot], implicit=False)
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1, source="telegram:b/g#m_1"),
                            bot, hosts=hosts))
    assert out["status"] == "applied"
    assert out["entries"] == [mine], "the mixed memory's id is not the bot's to see"
    seen = run(L.changes_since(store, bot))
    named = [x for r in seen["changes"] for x in r.get("entries", [])]
    assert mixed not in named and mine in named
    assert mixed not in json.dumps(seen)
    # The open owner sees both.
    owner = Host("life", scope_mode="open")
    rows = run(L.changes_since(store, owner))["changes"]
    assert mixed in [x for r in rows for x in r.get("entries", [])]
