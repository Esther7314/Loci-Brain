# -*- coding: utf-8 -*-
"""
tools/recall/core.py — the main remembering logic: filter -> zoom -> render

All of it is statistics (grouping, proportions, averages, maxima); zero LLM
calls, nothing precomputed.
"the more there is, the easier it is to forget" is not a bug but the correct
behaviour of zooming: where there are many entries they collapse into the
dominant colour, and the odd one out keeps its name.

Where each part lives:
- tools/recall/_collect.py     the four gates, top-k, and a search hit's roots
- tools/recall/_when_words.py  the `when` words recall accepts
- tools/recall/_cells.py       one cell's statistics, the zoom, the JSON skin's rows
- tools/recall/_words.py       the plain-language shell, and which tags a reader sees
- tools/recall/_browse.py      the browsing path (no query), 「今天」 included
- tools/recall/_search.py      the searching path (with a query), the relevance floor
- tools/recall/_read_by_id.py  a read by id: one entry whole, with what it links to
This file keeps the three entry points (recall_core, recall_data,
recall_text_and_data) and re-exports the names callers reach on it.
"""

import re

from core import _usage              # the usage log: what a lookup handed back
from core import visibility as _V     # the one gate: what may be put in front of the model
from core import _when as _w          # "today" as the user lives it (local timezone) — never call datetime.now() directly
from core import profile as _P        # what counts as an open promise; days since written
from core import runtime as rt
from core._slicer import _short_id    # the handle a read tool prints for an id
from .._common import read_scope, resolve_bucket_id

from ._browse import _gist_lines, _render_browse, _render_today
from ._cells import (_CELL_MAX, _cell_stats, _fmt_card, _fmt_header, _fmt_highlights,
                     _label_of, _room_cn, _split_cells, _stats_json, entry_json)
from ._collect import _collect, entries_of
from ._read_by_id import DREAMT_MARK, read_by_id
from ._search import (_PROMISE_MARK, _eff_score, _how_mark, _render_scene_clusters,
                      _render_search, relevance_floor, search_rows_json)
from ._when_words import _parse_when
from ._words import is_human_tag, kind_badge, mood_in_words, rooms_in_words, tags_in_words

# Reached on this module by callers (tools/breath/awaken, web/loci_reads, tests).
__all__ = [
    "recall_core", "recall_data", "recall_text_and_data",
    "entries_of", "entry_json", "search_rows_json", "relevance_floor", "DREAMT_MARK",
    "is_human_tag", "kind_badge", "mood_in_words", "rooms_in_words", "tags_in_words",
    "_collect", "_parse_when", "_render_browse", "_gist_lines", "_how_mark", "_PROMISE_MARK",
    "_label_of", "_room_cn", "_short_id",
]


async def recall_text_and_data(when: str, room: str, tag: str, query: str,
                               floor=None, view: str = "", max_cells: int = 0,
                               road: str = "") -> dict:
    """Collect once, serve both skins. For the panel, and for breath's 近三天, whose text
    and JSON skins are these two. `road`: what was collected also has to pass that road
    of the gate (breath's `recent`), so both skins are made from what it lets through.

    🔴 Calling `recall_data()` and `recall_core()` separately runs `_collect` in
       each — **the same search computed twice** (measured with a query: 3 seconds
       through the tool face, 8.6 through the panel). This collects once.

    ⚠️ Each skin gets its own **shallow copy** of entries: rendering sorts and
       slices, and sharing one list would mean whoever ran first decided the
       outcome.
    """
    collection = await _collect(when, room, tag, query)
    entries, err, ledger = collection
    if road:
        scope_view = await read_scope()
        entries = [e for e in entries if _V.visible_for(e["meta"], scope_view, road=road)]
    data = await recall_data(when, room, tag, query, floor=floor, view=view,
                             collected=(list(entries), err, dict(ledger)))
    if data.get("ok") and data.get("total"):
        kwargs = {"max_cells": max_cells} if 1 <= max_cells <= 20 else {}
        data["card"] = await recall_core(when, room, tag, query, floor=floor,
                                         view=view,
                                         collected=(list(entries), err, dict(ledger)),
                                         **kwargs)
    else:
        data["card"] = ""
    return data


async def recall_data(when: str, room: str, tag: str, query: str,
                      floor=None, view: str = "", collected=None) -> dict:
    """recall's **other skin**: the same four gates, the same _collect/_cell_stats,
    returning a dict for the front end.

    The text skin is recall_core(). Both skins share one collection and one set of
    statistics underneath and must never compute their own — the dominant colour
    shown on the page and the one the AI sees on waking have to be the same
    number, or there are two systems.
    ⚠️ `by` was cut; `view` affects only the text skin's shape, and this skin still
    returns everything.
    """
    entries, err, ledger = collected if collected is not None else await _collect(
        when, room, tag, query)
    if err:
        return {"ok": False, "error": err, "entries": [], "total": 0}
    if not entries:
        return {"ok": True, "entries": [], "total": 0, "stats": None,
                "gates": {"when": when, "room": room, "tag": tag, "query": query,
                          "view": view}}
    st = _cell_stats(entries)
    # The relevance floor only means anything while searching (no query gate, no
    # score).
    # It is dragged around on the panel's recall page, so a floor passed with the
    # request holds for that request only; without one the configured line runs
    # (core/thresholds `recall_floor`, which the setting page edits).
    fl = relevance_floor() if floor is None else float(floor)
    payload = []
    today = _w.now().date()
    for e in reversed(entries):  # newest first, the same direction as the text skin
        j = entry_json(e)
        j["written_days"] = _P.written_days_ago(e["meta"], today)
        if e.get("score") is not None:
            # The literal-hit floor: max(score, floor), so what the front end sees
            # is the score that actually took effect
            j["score"] = round(_eff_score(e, fl), 2)
            j["literal"] = bool(e.get("literal"))
            # The text skin's line facts, unrendered: how it matched, its roots (hits
            # sharing them are one line there), and whether it is a promise still open.
            j["meaning"] = bool(e.get("meaning"))
            j["words"] = bool(e.get("words"))
            j["how"] = _how_mark(e)
            j["roots"] = sorted(e.get("roots") or ())
            j["open_promise"] = _P.is_open_promise(e["meta"])
        payload.append(j)
    return {
        "ok": True,
        "total": len(entries),
        "stats": _stats_json(st),
        "entries": payload,
        "gates": {"when": when, "room": room, "tag": tag, "query": query, "view": view},
        "floor": fl,
        "floor_default": relevance_floor(),
        # How many top-k cut: the panel has to see it too (the same number as the
        # text skin's final line)
        "topk": ledger.get("topk"),
        "topk_dropped": ledger.get("topk砍掉", 0),
        "below": sum(1 for e in entries
                     if e.get("score") is not None and _eff_score(e, fl) < fl),
        # The lines the text skin lists, for the panel's search results (paged by the
        # route, web/loci_reads.api_loci_recall).
        "rows": search_rows_json(entries, fl),
    }


async def recall_core(when: str, room: str, tag: str, query: str,
                      max_cells: int = _CELL_MAX, floor=None, view: str = "",
                      collected=None) -> str:
    """The text skin. **The full parameter list**: when / room / tag / query /
    slices / view — those six and no more.

    🔪 **`by` was cut entirely** (from the code and the tool description alike):
       `by="touched"` — there is no such act as "digesting" here;
       `by="回看"`   — superseded by `slices` (slices=N is "how coarse or fine I
       want that stretch").
       The rule: **every parameter must map onto a sentence that actually surfaces
       in the mind.**
    🆕 `view="scene"`: scene clusters went from the default to something you
       **ask for explicitly**.
    """
    view = str(view or "").strip()
    # view="slices": the host's raw lines sliced and waiting to be handled
    # (tools/_slices.py). It reads the pending store, not the library, so the four
    # filters mean nothing to it and are refused rather than ignored.
    if view == "slices":
        if any(str(x or "").strip() for x in (when, room, tag, query)):
            return ('view="slices" 单独用：它列的是宿主交来、还没认领的切片，不在库里，'
                    "when / room / tag / query 管不到它。")
        from .. import _slices
        return await _slices.render_pending()
    # view="original" with query=<id>: ask the hosts for the original of that memory's
    # sources (tools/recall/original.py). Only on this explicit ask: it goes over the
    # network, so no other read does it. With an imported conversation's source string,
    # or with words, it reads or searches the material Loci holds itself.
    if view == "original":
        if any(str(x or "").strip() for x in (when, room, tag)) or not query.strip():
            return ('view="original" 只配 query 用："记忆的 id"（向宿主取那条依据的原话）、'
                    '导入对话的来源写法 "import:批次/对话#起..止"（翻那段原话），'
                    "或者几个词（搜导入的原话）。when / room / tag 管不到它。")
        from .original import render_original
        return await render_original(query)
    if view and view != "scene":
        return (f'view 无效：{view}。只有三种："scene"'
                "（按共享场景词聚成簇，看这件事怎么一路过来的）；"
                '"slices"（宿主交来、还没认领的切片）；'
                '"original"（配 query="记忆的 id" 向宿主取那条依据的原话；'
                '配导入对话的来源写法翻原话，配几个词搜导入的原话）。'
                "不给 view = 默认按时间＋分数排，找那件事。")
    if view and not query.strip():
        return ('view="scene" 要跟 query 一起用——簇是按**命中的记忆**共享的场景词聚的，'
                "没有 query 就没有命中，也就没有画面。"
                "只想翻一段时间：recall(when=…)（要更粗/更细加 slices=N）。")

    # --- Direct id lookup: the query is itself a full bucket_id -> return that
    # entry's verbatim text plus all of its metadata ---
    # This is the "click through to the original" door (the tier C list gives
    # gists; you come in here with an id).
    # A partial id is matched as a unique prefix (the same resolver the write
    # tools use); a collision lists the candidates and no match says so plainly
    # — never fall through to semantic search.
    q, id_err = await resolve_bucket_id(query)
    if id_err:
        return id_err
    if re.fullmatch(r"[0-9a-f]{12}", q) or re.fullmatch(r"feel_\d{12}_V\d{3}(_\d+)?", q):
        return await read_by_id(q)
    if not (when.strip() or room.strip() or tag.strip() or query.strip()):
        return ("recall 至少给一个门：when（时间）/ room（房间）/ tag（标签）/ query（扔词搜）。"
                "例：recall(when=\"上周\") · recall(room=\"MIND\") · recall(when=\"本月\", tag=\"Home\")")

    # 🔴 ONE fetch of the library for this whole call, and only on the path that needs it.
    #    Browsing needs it twice — once to filter down to what is being looked at, and
    #    once more for the periods, which have to be checked against the WHOLE library
    #    rather than the filtered result. If each fetched it itself, a single browse
    #    would scan everything twice and neither half could see the other doing it.
    #    A query does not need it at all: search returns its own hits.
    all_buckets = None
    if not query.strip():
        try:
            all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            rt.logger.warning(f"浏览要的全库没捞到: {e}")

    entries, err, ledger = collected if collected is not None else await _collect(
        when, room, tag, query, all_buckets)
    if err:
        return err
    _usage.offer([e["id"] for e in entries], "recall.search" if query.strip() else "recall.browse")
    if not entries:
        gates = "，".join(x for x in [when and f"when={when}", room and f"room={room}",
                                     tag and f"tag={tag}", query and f"query={query}"] if x)
        return f"这儿没有东西（{gates}）。门再开大一点试试。"

    gates = " ".join(x for x in [when and f"when={when}", room and f"room={room}",
                                 tag and f"tag={tag}", query and f"query={query}",
                                 view and f"view={view}"] if x)

    # ── The fork: **there is exactly one criterion, whether there is a query.**
    #    when/room/tag are a **range** (I am looking); query is a **target** (I am
    #    looking for).
    #    The score has always been available (no query gate means no score); what
    #    was missing is that the two paths produced identically shaped output.
    #    An explicit slices=N means "I want to see it by cells", takes neither
    #    path, and falls through to the older zoom below.
    if max_cells == _CELL_MAX:
        if query.strip():
            # 🔴 **The default is time plus score** (find that one thing).
            #    Scenes have to be asked for with `view="scene"` — and `when` now
            #    governs range only, so supplying it does not change the shape of
            #    the view at all (that "one parameter doing two jobs" is fixed).
            if view == "scene":
                return _render_scene_clusters(entries, gates, floor, ledger)
            return _render_search(entries, gates, floor, ledger)
        # 「今天」 never collapses: today's events still have me inside them, and
        # collapsing them into "some time ago + 2-3 representatives" pushes what
        # just happened far away.
        # 🔴 **Only 「今天」 counts** — 昨天 and 前天 collapse as before; those are
        # already history.
        if when.strip() == "今天":
            return await _render_today(entries, gates)
        return await _render_browse(entries, gates, room, tag, all_buckets)

    gname, slices = _split_cells(entries, max_cells)

    # B · 1-3 cells: the full card
    if len(slices) <= 3:
        blocks = [_fmt_card(label, _cell_stats(cell)) for label, cell in reversed(slices)]
        return f"〔{gates}〕{len(entries)} 条 · 粒度:{gname}\n\n" + "\n\n".join(blocks) + \
            "\n\n（钻：缩小 when / 加 room·tag；看原文：拿 id 搜）"

    # A · overview: two lines per cell
    lines = [f"〔{gates}〕{len(entries)} 条 · {len(slices)} 格 · 粒度:{gname} · 新→旧"]
    for label, cell in reversed(slices):
        st = _cell_stats(cell)
        lines.append(_fmt_header(label, st))
        hl = _fmt_highlights(st)
        if hl:
            lines.append(hl)
    lines.append("（钻：缩小 when / 加 room·tag；看原文：拿 id 搜）")
    return "\n".join(lines)
