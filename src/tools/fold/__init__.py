# -*- coding: utf-8 -*-
"""
========================================
tools/fold/ — fold: one act, three ways of drawing the circle
========================================

The bones live in `tools/_fold.py` (shared by all three entry points); this file
only handles **the gates and the wording at the entrance**: validate arguments ->
pick one of the three circles -> write one gist -> say it in plain language.

Rejection paths follow the shape of `_rooms.py`: **say what is wrong and give a
way out**. An error that only says "invalid" gets treated as noise and routed
around — which is exactly what we are guarding against.

------------------------------------------------------------
🔴 Two gates on how the circle may be drawn — **events use time, minds use snapshots**
------------------------------------------------------------
| gate | rejects | why |
|---|---|---|
| ① | `cover` with **several ids** + **events** | "cover a group of events" was cut entirely: a thread is read with `recall(query=)`, a stretch of days becomes a period |
| ② | `when` + **MIND** | thinking does not go by the calendar — a mind fold names its ids with `cover` |

The two forms that stay: `cover` with a **single event** is the opening for
"I got an event wrong, so I cover it myself"; mind's n=1 (re-versioning) and
n>=2 (merging after a spell of musing) are unchanged.
📌 Gate ① judges by **the rooms of the covered entries themselves**, not only by
   the `room` argument — `room` can be wrong or missing, whereas whether I am
   circling events or thoughts is something the circled entries already know.

Exports: dispatch(text, room, v, a, cover, when, from_, test_data) -> str
========================================
"""

from .. import _runtime as rt
from core import _fold as F
from .._common import check_content_size, resolve_bucket_ids
from core._rooms import check_room, _rooms_help, is_event_room, is_mind_room
from ..grow.rooms_path import _normalize_from


async def dispatch(text: str = "", room: str = "", v=-1, a=-1,
                   cover=None, when: str = "", from_=None,
                   test_data: bool = False) -> str:
    text = str(text or "")          # stored verbatim: never strip the body (constitutional)
    room = str(room or "").strip()
    when = str(when or "").strip()

    # Clients like GLM serialise the list as a JSON string — accept it leniently
    # (grow has the same allowance)
    import json as _json
    if isinstance(cover, str) and cover.strip().startswith("["):
        try:
            cover = _json.loads(cover)
        except (ValueError, TypeError):
            pass
    if isinstance(from_, str) and from_.strip().startswith("["):
        try:
            from_ = _json.loads(from_)
        except (ValueError, TypeError):
            pass
    if isinstance(cover, str):
        cover = [s.strip() for s in cover.split(",") if s.strip()]
    cover = [str(x).strip() for x in (cover or []) if str(x).strip()]

    if not text.strip():
        return ("text 不能为空——gist 的第一行就是那句话（「这几条在讲同一件事」/"
                "「那阵子我们在做什么」）。🔴 这句话永远是你自己写的，不过模型。")
    size_err = check_content_size(text)
    if size_err:
        return size_err

    # ---- One circle out of three: cover and when are mutually exclusive ----
    # 🔴 Passing both = one act doing two jobs, which is precisely the thing this
    #    design keeps trying to stamp out.
    if cover and when:
        return ('folds 和 when 只能给一个——两种折法二选一：\n'
                '  · 一组认知 folds=["a1","b2","c3"]  （这几条在讲同一件事，只给 mind）\n'
                '  · 一段日子 when="2026-08-13..2026-08-16"（时期：给那几天起个名字）\n'
                '🔴 **用笔画圈写名字；想法合并才记账**：时期只落名字 + 范围，谁在里面按日期'
                '现场算；快照才落名单。')
    if not cover and not when:
        return ('fold 总得折起点什么：folds=[id...]（几条认知收成一句）或 '
                'when="起..止"（时期：给一段日子起名字）。\n'
                '🔴 别拿 from 当 folds：from=我**从**哪几条长出来的（底下继续独立活着）；'
                'folds=我**折起**哪几条（底下不再独立冒头）。两个参数可以同时带。')

    # ---- Gate ②: when + MIND -> reject (thinking does not go by the calendar) ----
    if when and is_mind_room(room):
        return ('认知不认日历——mind 用 cover 点名。\n'
                '  这几条在讲同一件事 → fold(folds=["a1","b2"], room="' + room + '", text=…)\n'
                '  想给一段日子起名字 → 那是时期，room 填 EVENT 两间。\n'
                '（一条认知是哪天想到的不改变它是什么；日子是事件的坐标，不是想法的。'
                '发呆给认知配的团靠的是 v/a 坐标和 from 链，一条都不靠日期。）')

    # ---- v/a: I set them myself, never outsourced (same rule as mind / regrow) ----
    try:
        v = float(v)
        a = float(a)
    except (TypeError, ValueError):
        return "v/a 必填：这条 gist 此刻的坐标是你自己打的（0~1）。v=效价 a=唤醒。"
    if not (0 <= v <= 1 and 0 <= a <= 1):
        return f"v/a 必须在 0~1 之间（收到 v={v}, a={a}；没传会是 -1）。"

    # ---- from: which entries this grew out of (optional; a different meaning
    # from cover, and the two must never be merged) ----
    from_ids, from_err = await _normalize_from(from_)
    if from_err:
        return from_err
    if from_ids:
        missing = [fid for fid in from_ids
                   if not await rt.bucket_mgr.get_including_archive(fid)]
        if missing:
            return f"from 里这些 id 不存在：{', '.join(missing)}。"

    # ---- The folded ids may be the handles breath prints; from here on every
    # gate and every message speaks of the full ids, which are what get stored ----
    cover, cover_err = await resolve_bucket_ids(cover, "folds")
    if cover_err:
        return cover_err

    # ---- Gate: folding exactly one = re-versioning, which is regrow's job ----
    if len(cover) == 1 and not when:
        return (f"折一条就是给它换个版本，那是 regrow 的活："
                f'regrow(bucket_id="{cover[0]}", text="新版全文", v=…, a=…)。\n'
                "fold 收的是几条讲同一件事的认知，或者给一段日子起个名字。")

    # ---- Circle ③: a stretch of days = a **period**. Only the span is checked;
    # not one id is resolved ----
    # 🔴 A period is a pure naming layer: it stores a name plus a span, and who
    #    falls inside is counted live.
    #    The first version resolved the span into a frozen cover list here — that
    #    was bookkeeping copied from consolidation/ACP. Here nothing is ever
    #    deleted and all we do is give it a name, so the bookkeeping half of that
    #    design was copied for nothing, and it was sent back the same day.
    members_now = 0
    if when:
        _t0, _t1, span_err = F.check_span(when)
        if span_err:
            return span_err
        members_now = len(await F.span_members(_t0, _t1))

    # ---- Circles ①②: check each given id exists; archived ones cannot be
    # covered (update will not write an archived bucket, so forcing it would
    # leave half a chain) ----
    inherit_from = ""
    covered_rooms: list[str] = []
    for cid in cover:
        live = await rt.bucket_mgr.get(cid)
        if live:
            inherit_from = inherit_from or cid
            covered_rooms.append(str((live.get("metadata", {}) or {}).get("room") or ""))
            continue
        arch = await rt.bucket_mgr.get_including_archive(cid)
        if arch:
            return (f'{cid} 在归档区，盖不上（盖上了只会留半条链）。'
                    f'先 trace(bucket_id="{cid}", restore=True) 把它捞回来，再 fold。')
        return f"cover 里这些 id 不存在：{cid}。填真 bucket_id。"

    # ---- Gate ①: several covers + events -> reject ("cover a group of events"
    # was cut) ----
    # It judges by **the rooms of the covered entries themselves**, not only by
    # the room argument (room can be wrong or missing, whereas whether I am
    # circling events or thoughts is something the circled entries know).
    if len(cover) >= 2 and (is_event_room(room)
                            or any(is_event_room(r) for r in covered_rooms)):
        return ('盖一组事件不存在——日子用 when 画圈，看一条线用 recall(query)。\n'
                '  那几天在做一件什么事 → fold(when="2026-08-13..2026-08-16", '
                'room="EVENT/SELF", text=我写的那句)\n'
                '  「这些事是一条线」→ recall(query="青岛") 本来就是线的查看器\n'
                '（八个字：**Event 用时间，mind 用快照**。事件的一组没有「上一版」也不该'
                '被压住；一条事件记错了要盖掉，那是 cover 单条，那个口子留着。）')

    # ---- Room: at n=1 it is inherited from the covered entry (which is exactly
    # what regrow does); in every other case it must be chosen deliberately ----
    if not room and len(cover) == 1 and inherit_from:
        old = await rt.bucket_mgr.get(inherit_from)
        room = str((old or {}).get("metadata", {}).get("room") or "")
    if not room:
        return ("room 必填（只有 cover 恰好一条时才从被盖那条继承）。\n"
                "gist 住在它盖的那批东西的房间里：时期（when）填 EVENT 两间，"
                "盖认知填 MIND 两间。\n"
                + _rooms_help())
    room_err = check_room(room, "")
    if room_err:
        return room_err

    # ⚰️ **The "fold exactly one" branch was pulled from the tool face.**
    #    A cover of exactly one used to mean re-versioning — the same thing regrow
    #    does, through the same code. One thought with two entry points cannot be
    #    explained, so it now splits as:
    #      regrow = this entry has a new version (thinking / events / periods alike)
    #      fold   = fold it up (several collapsed into one sentence / naming a
    #               stretch of days)
    #    🔴 The "n=1 writes the version chain" code below was **not** deleted:
    #       regrow still uses it (regrow is fold's n=1 special case). Only fold's
    #       entry point into it was withdrawn.
    supersedes = ""

    new_id, report = await F.save_gist(
        text, room, v, a, cover, when=when, from_ids=from_ids,
        supersedes=supersedes, test_data=bool(test_data))

    # ---- Say it plainly. A period and a snapshot get two different sentences,
    # because they really are two different things ----
    if when:
        # "◈period -> id, span, name" plus a live count for scale (never stored)
        head = f"◈时期→{new_id} {when} {room}「{text.strip().splitlines()[0][:38]}」"
        tail = [f"（范围内现在有 {members_now} 条——**现场数的**，只给个手感，不落盘。）",
                "（时期只给这段日子起了个名字：谁在里面按日期现算，"
                "补记自动归队、交叉和嵌套天然成立；**一条都没被压住**"
                "（照旧独立冒头、照旧搜得到）。recall 那段时间时它盖在顶上。）",
                "（边界想改就 regrow 换 when——边界本来就是糊的。）"]
        return head + "\n" + "\n".join(tail)

    n = len(report["cover"])
    head = f"▣gist→{new_id} {room}（盖着 {n} 条"
    if n:
        head += "：" + "、".join(report["cover"][:8]) + ("…" if n > 8 else "")
    head += "）"
    tail = [F.format_report(report)]
    if supersedes:
        tail.append("（= 换版：旧版留档不浮现，id 直查仍能看）")
    else:
        tail.append("（被盖的不再独立冒头，但 query 照样搜得到、id 直查钻得到）")
    return head + "\n" + "\n".join(x for x in tail if x)
