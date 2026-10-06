"""
========================================
tools/_slices.py — the main model handling pending slices
========================================

The host's raw lines of a day were sliced by the side model (core/_slicer.py); each
slice waits for the main model, who decides what it is:

    recall(view="slices")                          the slices waiting, with guesses
    grow(..., slice="sl_…")                        not recorded yet: write it; the slice
                                                   becomes one of the new memory's sources
    trace(bucket_id=…, slice="sl_…")               already recorded: append it to that
                                                   memory's sources
    trace(slice="sl_…", slice_span="m_a..m_b")     sliced wrong: move its span
    trace(slice="sl_…", drop_slice=True)           nothing worth keeping: drop it

A slice of any length stands for one source record, `first..last` (record_for), checked
like any write's (the registry, the grant). It is closed only on what the write actually
put on disk carrying that record, so a refused or deduplicated write leaves it open.
One slice is handled at a time (a lease on its id), and a closed one is refused by name.

Under a read scope (core/scope.py) a request sees, counts and handles only the slices of
containers its grant covers whole; any other slice reads like one that does not exist.

An imported conversation's slices (core/import_memory.py) are listed the same way, under
their import's 是不是同一个他, each with the side model's candidate entry and how to read
its original; they are the main model's to check and write, never merged into an
existing memory by anything here.

Exports: visible_batches · pending_seen · imports_seen · render_pending · write_from_slice ·
         with_records · trace_slice
========================================
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable

from core import _sources as _src
from core._slicer import SliceError
from core import runtime as rt
from ._common import read_scope

_LIST_MAX = 40          # slices shown by recall(view="slices"); the rest are counted


def _store():
    return getattr(rt.bucket_mgr, "slices", None)


def _span_text(span: dict) -> str:
    first, last, count = span["first"], span["last"], span["count"]
    head = first if first == last else f"{first}..{last}"
    return f"{head}（{count} 行）"


# ------------------------------------------------------------
# Reading
# ------------------------------------------------------------

async def visible_batches() -> list[dict]:
    """The open batches this request may see, newest first. Under a read scope: the
    batches of containers the grant covers whole (a slice is a run of the host's lines, so
    a grant of single pieces does not reach it), each slice's guesses cut to the memories
    the request may read."""
    store = _store()
    batches = store.open_batches() if store is not None else []
    view = await read_scope()
    if view is None:
        return batches
    out = []
    for b in batches:
        if not view.covers_container(b.get("source") or {}):
            continue
        slices = [{**s, "guesses": [g for g in s.get("guesses") or []
                                    if view.permits_id(str(g.get("id") or ""))]}
                  for s in b["slices"]]
        out.append({**b, "slices": slices})
    return out


async def pending_seen() -> int:
    """How many slices wait, as this request may count them (the store's own count when
    nothing is filtered)."""
    store = _store()
    if store is None:
        return 0
    if await read_scope() is None:
        return store.pending_count()
    return sum(len(b["slices"]) for b in await visible_batches())


async def imports_seen() -> int:
    """How many of the waiting slices are an import's drafts, as this request may count
    them (breath's 「有 N 段导入的原话还没核」)."""
    if _store() is None:
        return 0
    return sum(len(b["slices"]) for b in await visible_batches() if b.get("import"))


async def _out_of_reach(store, sid: str) -> str:
    """A slice the request's read scope does not reach is answered like one that does not
    exist ("" when it is in reach, or unknown and left to the store's own refusal)."""
    view = await read_scope()
    if view is None or store.get(sid) is None:
        return ""
    if view.covers_container(store.record_for(sid)):
        return ""
    return f"没有这片切片：{sid}。recall(view=\"slices\") 看还有哪些待认领。"


async def render_pending() -> str:
    """recall(view="slices"): the pending slices this request may see, newest batch
    first."""
    batches = await visible_batches()
    if not batches:
        return "没有待认领的切片。"
    total = sum(len(b["slices"]) for b in batches)
    lines = [f"待认领的切片 {total} 片："]
    shown = 0
    imported = False
    for b in batches:
        src = b["source"]
        origin = b.get("import")
        where = f"{src.get('system')}:{src.get('instance')}/{src.get('container')}"
        if origin:
            imported = True
            title = f"「{origin.get('title')}」" if origin.get("title") else ""
            who = ("是同一个他：「我」那边是我自己，写成 EVENT/SELF" if origin.get("same_self", True)
                   else "不是同一个他：「AI」那边是另一个 AI，写成 EVENT/WORLD")
            lines.append(f"── 导入的原话 {where}{title} · {b['day'] or '日子不详'} · {who}")
        else:
            lines.append(f"── {where} · {b['day']}")
        for s in b["slices"]:
            if shown >= _LIST_MAX:
                break
            shown += 1
            edited = "（改切过，gist 是原来的）" if s.get("edited") else ""
            lines.append(f"{s['slice_id']} · {_span_text(s['span'])} · {s['gist']}{edited}")
            if origin:
                if s.get("draft"):
                    lines.append(f"    候选（副模型起草的，核过再写）：{s['draft']}")
                span = s["span"]
                head = span["first"] if span["first"] == span["last"] else \
                    f"{span['first']}..{span['last']}"
                lines.append(f'    原话：recall(query="{where}#{head}", view="original")')
                continue
            guesses = " / ".join(f"{g['short']} {g['score']:.2f}" for g in s["guesses"])
            lines.append(f"    像是已经记过的：{guesses}" if guesses
                         else "    当天没有像的记忆")
    if shown < total:
        lines.append(f"（还有 {total - shown} 片没列出来，先处理上面的）")
    if imported:
        lines.append("导入的：先翻原话核对候选，漏的补上，用自己的话写——grow(..., slice=\"sl_…\")"
                     "（自动挂上引原话）；候选不对就照原话写，不值得留就丢掉。不合进已有的记忆。")
    lines.append('没记过的：grow(..., slice="sl_…")；记过了：trace(bucket_id=…, slice="sl_…")；'
                 '切错了：trace(slice="sl_…", slice_span="前id..后id")；'
                 '不值得留：trace(slice="sl_…", drop_slice=True)。')
    return "\n".join(lines)


# ------------------------------------------------------------
# Handling
# ------------------------------------------------------------

def with_records(given, records: list[dict]):
    """A write's own `sources` argument with a slice's records after it. Something that
    is not a list or a record is handed on unchanged for check_sources to refuse."""
    given = _src.coerce_sources_arg(given)
    if given in (None, "", []):
        return list(records)
    if isinstance(given, dict):
        return [given, *records]
    if isinstance(given, (list, tuple)):
        return [*given, *records]
    return given


async def _carrying(written: list[str], record: dict) -> list[str]:
    """The written ids whose stored sources name this record's piece."""
    want = _src.record_id(record)
    out: list[str] = []
    for bid in dict.fromkeys(written):
        b = await rt.bucket_mgr.get(bid)
        for rec in ((b or {}).get("metadata") or {}).get(_src.SOURCES_FIELD) or []:
            try:
                if isinstance(rec, dict) and _src.record_id(rec) == want:
                    out.append(bid)
                    break
            except KeyError:
                continue
    return out


async def write_from_slice(slice_id: str, how: str,
                           write: Callable[[list[dict]], Awaitable[str]]) -> str:
    """Run `write(records)` with the slice's record and close the slice on the memories
    it wrote carrying it. The write's own receipt comes back, plus one line saying
    where the slice went; a write that put nothing on disk leaves the slice open."""
    from core.bucket_manager import _filesystem_turn      # the lease, as the registry takes it

    store = _store()
    sid = str(slice_id or "").strip()
    if store is None:
        return "这个库没有切片。"
    async with _filesystem_turn(store.base_dir, f"slice-{sid}"):
        hidden = await _out_of_reach(store, sid)
        if hidden:
            return hidden + "本次什么都没写。"
        why = store.refusal(sid)
        if why:
            return why.zh + "本次什么都没写。"
        info = store.get(sid)
        record = store.record_for(sid)
        with _src.collect_written() as written:
            out = await write([record])
        carried = await _carrying(written, record)
        if not carried:
            return out
        try:
            await store.close(sid, how, carried)
        except SliceError as e:
            return f"{out}\n{e.zh}"
        return (f"{out}\n切片 {sid} 挂上了 → {'、'.join(carried)}，"
                f"{_span_text(info['span'])}。还有 {await pending_seen()} 片待认领。")


def _edits_given(kwargs: dict, trace_core) -> list[str]:
    """The trace arguments set to something other than their default."""
    params = inspect.signature(trace_core).parameters
    out = []
    for name, value in kwargs.items():
        param = params.get(name)
        if param is None or name == "bucket_id":
            continue
        if value is None or value == param.default or value in ("", [], {}):
            continue
        out.append(name)
    return out


async def trace_slice(slice_id: str, *, drop: bool, span: str, kwargs: dict,
                      trace_core) -> str:
    """trace's three slice moves: append to a memory (bucket_id), re-cut, drop."""
    store = _store()
    sid = str(slice_id or "").strip()
    bucket_id = str(kwargs.get("bucket_id") or "").strip()
    span = str(span or "").strip()
    if store is None:
        return "这个库没有切片。"
    hidden = await _out_of_reach(store, sid)
    if hidden:
        return hidden + "本次什么都没改。"
    if drop and span:
        return "drop_slice 和 slice_span 二选一：丢掉，或者改切。本次什么都没改。"
    if drop or span:
        if bucket_id or _edits_given(kwargs, trace_core):
            return ("drop_slice / slice_span 单独用：不带 bucket_id，也不带别的改动。"
                    "本次什么都没改。")
        if drop:
            try:
                await store.close(sid, "drop", [])
            except SliceError as e:
                return e.zh + "本次什么都没改。"
            info = store.get(sid)
            return (f"切片 {sid} 丢掉了：{_span_text(info['span'])}，不挂到任何记忆上。"
                    f"还有 {await pending_seen()} 片待认领。")
        first, dots, last = span.partition("..")
        first, last = first.strip(), (last.strip() if dots else first.strip())
        if not first or not last:
            return 'slice_span 写成 "前id..后id"（只有一行就写那一个 id）。本次什么都没改。'
        try:
            await store.recut(sid, first, last)
        except SliceError as e:
            return e.zh + "本次什么都没改。"
        info = store.get(sid)
        return (f"切片 {sid} 改切成 {_span_text(info['span'])}；gist 没变（原文已经不在了，"
                f"没法重写）：{info['gist']}")
    if not bucket_id:
        return ('slice 要配 bucket_id：挂到哪条记忆上。没记过的用 grow(..., slice="…")；'
                "切错了用 slice_span，不值得留用 drop_slice=True。")
    given = kwargs.get("sources_append")
    return await write_from_slice(
        sid, "trace",
        lambda records: trace_core(**{**kwargs, "sources_append": with_records(given, records)}))
