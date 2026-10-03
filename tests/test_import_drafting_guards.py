# -*- coding: utf-8 -*-
"""
tests/test_import_drafting_guards.py — what drafting and withdrawing an import must not do.

  · a batch half way through its withdrawal is not drafted again: its lines never reach
    the side model, no draft comes back, its status stays withdrawing;
  · a line withdrawn on its own never reaches the side model, and no draft spans it;
  · a ChatGPT export is read along the branch the person last saw (`current_node`): an
    abandoned answer, the system prompt, tool calls and their output are not lines;
  · a delete cut off half way leaves the batch findable, and withdrawing it again
    finishes; a batch folder left without its record is deleted by a withdrawal; the
    temporary copies a crash left beside a cleared entry go with the withdrawal.
"""

import json
import pathlib

import pytest

from core import _source_change as SC
from core import _sources as S
from core import import_memory as IM
from core.scope import LOCI_HOST
from test_import_two_steps import (EXPORT, Pipe, _event, by_topic, engine,  # noqa: F401
                                   run, store)


def _withdraw_line(store, batch, container, line):
    status, out = run(SC.handle(store, {
        "change_id": f"w-{container}-{line}", "source": f"import:{batch}/{container}#{line}",
        "host_seq": 1, "change": "withdrawn"}, LOCI_HOST))
    assert status == 200 and out["state"] == S.WITHDRAWN, out


def test_a_batch_being_withdrawn_is_not_drafted_again(store, monkeypatch):
    pipe = Pipe(by_topic)
    eng = engine(store, pipe, monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    meta = eng.store.meta(batch)
    meta["status"] = IM.WITHDRAWING
    eng.store.save_meta(meta)
    with pytest.raises(IM.ImportRefused) as refused:
        run(eng.draft(batch))
    assert "撤回" in str(refused.value)
    assert pipe.calls == [] and store.slices.pending_count() == 0
    assert eng.store.meta(batch)["status"] == IM.WITHDRAWING


def test_a_line_withdrawn_on_its_own_never_reaches_the_side_model(store, monkeypatch):
    pipe = Pipe(lambda system, user: [])
    eng = engine(store, pipe, monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    _withdraw_line(store, batch, "c0001", "l0002")
    for line in ("l0001", "l0002", "l0003"):
        _withdraw_line(store, batch, "c0002", line)
    out = run(eng.draft(batch))
    sent = "\n".join(user for _system, user in pipe.calls)
    assert "想！不过要看天气。" not in sent
    assert "水壶又坏了" not in sent, "a conversation withdrawn whole is not drafted"
    assert "周六想去海边吗" in sent and "天气预报说周六是晴天" in sent
    # The lines on either side of the withdrawn one are drafted apart.
    assert len(pipe.calls) == 2
    assert out["status"] == IM.DRAFTED and not out["failures"], out
    convs = {c["container"]: c for c in eng.store.meta(batch)["conversations"]}
    assert convs["c0002"].get("withdrawn") and not convs["c0002"]["drafted"]


def test_a_draft_never_spans_a_withdrawn_line(store, monkeypatch):
    def whole(system, user):
        # One draft over every line it was given ("下面是 N 行聊天原文").
        count = int(user.split("下面是 ", 1)[1].split(" ", 1)[0])
        return [{"from": 1, "to": count, "gist": "一段", "draft": "一段对话。"}]
    eng = engine(store, Pipe(whole), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    _withdraw_line(store, batch, "c0001", "l0002")
    run(eng.draft(batch))
    seen = 0
    for b in store.slices.open_batches():
        if b["source"]["container"] != "c0001":
            continue
        for s in b["slices"]:
            seen += 1
            run_ = S.SourceId("import", batch, "c0001", s["span"]["first"],
                              s["span"]["last"] if s["span"]["last"] != s["span"]["first"]
                              else None)
            assert store.sources.state_of(run_) == S.ACTIVE, s
    assert seen == 2, "one draft on each side of the withdrawn line"


# ───────────────────────── a ChatGPT export with branches ─────────────────────────

def _msg(role, text, t, **extra):
    content = extra.pop("content", {"content_type": "text", "parts": [text]})
    return {"author": {"role": role}, "create_time": t, "content": content, **extra}


def _branchy_export(with_current=True) -> str:
    nodes = {
        "root": {"id": "root", "parent": None, "children": ["sys"], "message": None},
        "sys": {"id": "sys", "parent": "root", "children": ["q1"],
                "message": _msg("system", "You are ChatGPT.", 1.0,
                                metadata={"is_visually_hidden_from_conversation": True})},
        "q1": {"id": "q1", "parent": "sys", "children": ["a1_old", "a1"],
               "message": _msg("user", "帮我算一下房租。", 2.0)},
        "a1_old": {"id": "a1_old", "parent": "q1", "children": [],
                   "message": _msg("assistant", "放弃掉的那个回答。", 3.0)},
        "a1": {"id": "a1", "parent": "q1", "children": ["q2"],
               "message": _msg("assistant", "好的，每人两千。", 4.0)},
        "q2": {"id": "q2", "parent": "a1", "children": ["call"],
               "message": _msg("user", "再按面积算一次。", 5.0)},
        "call": {"id": "call", "parent": "q2", "children": ["out"],
                 "message": _msg("assistant", "", 6.0, recipient="python",
                                 content={"content_type": "code", "text": "print(1)"})},
        "out": {"id": "out", "parent": "call", "children": ["a2"],
                "message": _msg("tool", "1", 7.0,
                                content={"content_type": "execution_output", "text": "1"})},
        "a2": {"id": "a2", "parent": "out", "children": [],
               "message": _msg("assistant", "按面积是一个两千二，一个一千八。", 8.0)},
    }
    conv = {"title": "房租", "id": "conv-r", "mapping": nodes}
    if with_current:
        conv["current_node"] = "a2"
    return json.dumps([conv], ensure_ascii=False)


@pytest.mark.parametrize("with_current", [True, False])
def test_a_chatgpt_export_is_read_along_the_branch_last_seen(with_current):
    fmt, convs = IM.parse_conversations(_branchy_export(with_current), "conversations.json")
    assert fmt == "chatgpt_json"
    assert [(t["role"], t["content"]) for t in convs[0]["turns"]] == [
        ("user", "帮我算一下房租。"), ("assistant", "好的，每人两千。"),
        ("user", "再按面积算一次。"), ("assistant", "按面积是一个两千二，一个一千八。")]


# ───────────────────────── deleting and sweeping ─────────────────────────

def test_a_delete_cut_off_half_way_is_finished_by_withdrawing_again(store, monkeypatch,
                                                                    tmp_path):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    real_unlink = pathlib.Path.unlink
    seen = {"n": 0}

    def dies_on_the_second(self, *args, **kwargs):
        seen["n"] += 1
        if seen["n"] == 2:
            raise KeyboardInterrupt("killed")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, "unlink", dies_on_the_second)
    with pytest.raises(KeyboardInterrupt):
        run(eng.withdraw(batch))
    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
    folder = tmp_path / "_sources" / "imports" / batch
    assert (folder / IM.BATCH_FILE).is_file(), "the record outlives the lines"
    status, out = run(eng.withdraw(batch))
    assert status == 200 and out["status"] == "withdrawn" and out["text_deleted"], out
    assert not folder.exists()


def test_a_batch_folder_without_its_record_is_deleted_by_a_withdrawal(store, monkeypatch,
                                                                      tmp_path):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    folder = tmp_path / "_sources" / "imports" / batch
    (folder / IM.BATCH_FILE).unlink()
    status, out = run(eng.withdraw(batch))
    assert status == 200 and out["text_deleted"], out
    assert not folder.exists()
    status, _out = run(eng.withdraw(batch))
    assert status == 404


def test_a_withdrawal_takes_the_temporary_copies_a_crash_left(store, monkeypatch, tmp_path):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    _msg_text, bid = run(_event("周六约好去海边，我带相机。",
                                from_=[f"import:{batch}/c0001#l0002..l0004"]))
    assert bid
    path = pathlib.Path(store._find_bucket_file(bid))
    left = path.with_name(path.name + ".0123abcd.tmp")
    left.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    status, out = run(eng.withdraw(batch))
    assert status == 200 and out["ok"] and out["temp_files_deleted"] == 1, out
    assert not left.exists()
    assert not any("相机" in p.read_text(encoding="utf-8", errors="replace")
                   for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".md", ".tmp"))
