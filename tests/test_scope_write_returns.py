# -*- coding: utf-8 -*-
"""
tests/test_scope_write_returns.py — under a read scope, what a write's return finds out on
the spot (回望 and 场景常来, tools/_write_returns.py) comes only from what the scope may
read.

Written through the grow tool in the group (a Loci-Scope whose venue is the group); the
library holds views and days from the group (`use: venues [group]`) and from a private
chat (`venues [private]`). A private view 0.95 close is not named while a group view 0.9
close is. The scene question is not asked under a scope at all (its quiet days are kept
for the whole library); at the core, a cue out of scope hanging on the word does not keep
it back. Without a scope,
everything counts as before.
"""

import json
from datetime import timedelta
from pathlib import Path

from core import _case_recall as CR
from core import _sources as S
from core import scope as SC
from tools._common import sees_whole_library
from tools.grow import dispatch as grow

from test_write_returns import (LOOK_BACK, TODAY, clock, event, run, scene_line,  # noqa: F401
                                store, view)

GROUP = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
BOT = SC.Host("book-bot", max_grant=(S.Place("telegram", "bot-a"),), token="bot-key")
SCOPE = json.dumps({"v": 1, "entry": GROUP, "venue": "group", "audience": ["user:U"],
                    "grant": [GROUP]})
_LINE = iter(range(1, 10_000))


def seen_in(venue: str) -> list[dict]:
    return [{**GROUP, "id": f"m_{next(_LINE)}", "use": {"venues": [venue]}}]


def scoped_event(text: str) -> str:
    """An event written in the group: the write call runs under the group's scope."""
    async def go():
        with SC.request_scope(SC.RequestScope.resolve(BOT, SCOPE)):
            return await grow(kind="event", sources=seen_in("group"),
                              items=[{"room": "EVENT/SELF", "text": text, "v": 0.5, "a": 0.4}])
    return run(go())


def look_back(out: str) -> str:
    return "\n".join(ln for ln in out.splitlines() if LOOK_BACK in ln)


def test_a_view_out_of_scope_is_never_looked_back_at(store):
    private = view(store, "下雨天适合一个人待着。", sources=seen_in("private"))
    group = view(store, "雨天出门总会后悔。", sources=seen_in("group"))
    store.vectors.answers["下雨"] = {private: 0.95, group: 0.9}
    out = scoped_event("又下雨了，群里都没人出门。")
    assert "已落盘" in out, out
    lines = look_back(out)
    assert group[:6] in lines, out
    assert private[:6] not in out
    both = look_back(event("又下雨了。"))
    assert private[:6] in both and group[:6] in both


def _six_days(store, venue: str, word: str = "健身房") -> None:
    for k in range(6, 0, -1):
        day = (TODAY - timedelta(days=k)).strftime("%Y-%m-%d")
        run(store.create(f"下班去{word}练了一小时。", room="EVENT/SELF", tags=[word], when=day,
                         sources=seen_in(venue)))


def test_days_out_of_scope_do_not_earn_the_scene_question(store):
    _six_days(store, "private")
    assert not scene_line(scoped_event("今天又去健身房了。"))
    assert scene_line(event("健身房今天人很多。")), "unscoped, the six days count"


def _asked_file(store):
    return Path(store.base_dir) / "_state" / CR.ASKED_FILE


def test_a_ceilinged_write_is_never_asked_and_records_nothing(store):
    # Criterion: the quiet days live in one file for the whole library, so a call under a
    # ceiling is not asked at all, even on days it may read; the file is not written.
    _six_days(store, "group")
    out = scoped_event("今天又去健身房了。")
    assert "已落盘" in out, out
    assert not scene_line(out), out
    assert not _asked_file(store).exists()
    [line] = scene_line(event("健身房今天人很多。"))
    assert "「健身房」最近常出现（14 天里 6 天）" in line
    assert _asked_file(store).exists()


def test_an_open_host_sending_a_scope_is_not_asked_either(store):
    # Criterion: restricted is the request's scope, not the host's name.
    _six_days(store, "group")
    legacy = SC.Host(SC.LEGACY, scope_mode=SC.OPEN, token="home-key")

    async def go():
        with SC.request_scope(SC.RequestScope.resolve(legacy, SCOPE)):
            return await grow(kind="event", sources=seen_in("group"),
                              items=[{"room": "EVENT/SELF", "text": "今天又去健身房了。",
                                      "v": 0.5, "a": 0.4}])
    out = run(go())
    assert "已落盘" in out and not scene_line(out), out
    assert not _asked_file(store).exists()


def test_only_an_unceilinged_unscoped_call_sees_the_whole_library():
    legacy = SC.Host(SC.LEGACY, scope_mode=SC.OPEN)
    capped = SC.Host("capped", scope_mode=SC.OPEN, max_grant=(S.Place("telegram", "bot-a"),))
    assert sees_whole_library()
    cases = [(legacy, None, True), (legacy, SCOPE, False), (BOT, SCOPE, False),
             (capped, None, False), (None, None, False)]
    for host, header, whole in cases:
        with SC.request_scope(SC.RequestScope.resolve(host, header)):
            assert sees_whole_library() is whole, (host, header)


def test_at_the_core_a_cue_out_of_scope_does_not_keep_the_question_back(store):
    _six_days(store, "group")
    run(store.create("上次膝盖疼是没热身。", room="MIND/VIEWS", sources=seen_in("private"),
                     cue={"condition": "下次去健身房", "phrasings": ["健身房"]}))
    library = run(store.list_all(include_archive=True))
    metas = {b["metadata"]["id"]: b["metadata"] for b in library}
    scoped = SC.ScopeView(SC.RequestScope.resolve(BOT, SCOPE), metas, store.sources)
    q = CR.ask(library, ["今天又去健身房了。"], buckets_dir=str(store.base_dir), scope=scoped)
    assert q is not None and (q.word, q.days) == ("健身房", 6)
    assert CR.ask(library, ["今天又去健身房了。"], buckets_dir=str(store.base_dir)) is None


def test_without_a_scope_a_cue_on_the_word_still_keeps_it_back(store):
    _six_days(store, "group")
    run(store.create("上次膝盖疼是没热身。", room="MIND/VIEWS", sources=seen_in("private"),
                     cue={"condition": "下次去健身房", "phrasings": ["健身房"]}))
    assert not scene_line(event("今天又去健身房了。"))
