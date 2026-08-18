# -*- coding: utf-8 -*-
"""
========================================
tools/fold/ — fold：一个动作，三种圈法（二改施工 3，2026-08-16）
========================================

骨头在 `tools/_fold.py`（三个入口共用），这儿只做**入口的闸和话**：
参数校验 → 圈法三选一 → 落一条 gist → 说人话。

拒绝路径照 `_rooms.py` 的样子：**说清 + 给出路**。
一条只说「不合法」的错误会被当成噪音绕过去——那正是我们在防的。

------------------------------------------------------------
🔴 两道圈法闸（她 8-17 14:30 终稿的施工半）——**八个字：Event 用时间，mind 用快照**
------------------------------------------------------------
| 闸 | 拒什么 | 为什么 |
|---|---|---|
| ① | `cover` **多条** + **事件** | 「盖一组 event」整个砍掉：线归 `recall(query=)` 看，日子归时期 |
| ② | `when` + **MIND** | 认知不认日历（她拍的）——mind 用 `cover` 点名 |

保留的两条：`cover` **单条 event** = 第 8 节「事件改错，我自己盖掉」的口子；
mind 的 n=1（换版）/ n≥2（发呆合并）照旧。
📌 闸①的判据看**被盖那几条自己的房间**，不只看 `room` 参数——`room` 可以填错、
   可以不填，而「我圈的到底是事件还是想法」这件事，被圈的那几条自己知道。

对外暴露：dispatch(text, room, v, a, cover, when, from_, test_data) → str
========================================
"""

from .. import _runtime as rt
from core import _fold as F
from .._common import check_content_size
from core._rooms import check_room, _rooms_help, is_event_room, is_mind_room
from ..grow.rooms_path import _normalize_from


async def dispatch(text: str = "", room: str = "", v=-1, a=-1,
                   cover=None, when: str = "", from_=None,
                   test_data: bool = False) -> str:
    text = str(text or "")          # 逐字落盘：不 strip 正文（宪法）
    room = str(room or "").strip()
    when = str(when or "").strip()

    # GLM 这类客户端会把列表序列成 JSON 字符串——宽容地接（grow 那边同样的口子）
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

    # ---- 圈法三选一：cover 和 when 只能给一个 ----
    # 🔴 两个都给 = 一个动作管两件事，正是这一轮在骂的东西（开工单 2.2 第 2 条同款判据）。
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

    # ---- 闸②：when + MIND → 拒（认知不认日历，她 8-17 拍的）----
    if when and is_mind_room(room):
        return ('认知不认日历——mind 用 cover 点名。\n'
                '  这几条在讲同一件事 → fold(folds=["a1","b2"], room="' + room + '", text=…)\n'
                '  想给一段日子起名字 → 那是时期，room 填 EVENT 两间。\n'
                '（一条认知是哪天想到的不改变它是什么；日子是事件的坐标，不是想法的。'
                '发呆给认知配的团靠的是 v/a 坐标和 from 链，一条都不靠日期。）')

    # ---- v/a：我自己打，不外包（跟 mind / regrow 一条规矩）----
    try:
        v = float(v)
        a = float(a)
    except (TypeError, ValueError):
        return "v/a 必填：这条 gist 此刻的坐标是你自己打的（0~1）。v=效价 a=唤醒。"
    if not (0 <= v <= 1 and 0 <= a <= 1):
        return f"v/a 必须在 0~1 之间（收到 v={v}, a={a}；没传会是 -1）。"

    # ---- from：从哪几条长出来的（可选，跟 cover 语义不同，不许合并）----
    from_ids, from_err = _normalize_from(from_)
    if from_err:
        return from_err
    if from_ids:
        missing = [fid for fid in from_ids
                   if not await rt.bucket_mgr.get_including_archive(fid)]
        if missing:
            return f"from 里这些 id 不存在：{', '.join(missing)}。"

    # ---- 闸：折一条 = 换版，那是 regrow 的活（2026-08-18 她拍的）----
    if len(cover) == 1 and not when:
        return (f"折一条就是给它换个版本，那是 regrow 的活："
                f'regrow(bucket_id="{cover[0]}", text="新版全文", v=…, a=…)。\n'
                "fold 收的是几条讲同一件事的认知，或者给一段日子起个名字。")

    # ---- 圈法③：一段日子 = **时期**。只校验边界，一个 id 都不解析 ----
    # 🔴 8-17 14:30 终稿：时期=纯命名层（只落名字 + 范围，谁在里面现场算）。
    #    第一版在这儿把范围解析成 cover 存死——那是从 consolidation/ACP 抄的记账，
    #    我们一个字不删、只起名字，记账那一半白抄，她当天退了货。
    现有 = 0
    if when:
        _t0, _t1, span_err = F.check_span(when)
        if span_err:
            return span_err
        现有 = len(await F.span_members(_t0, _t1))

    # ---- 圈法①②：给的 id 逐个验存在；归档的不给盖（update 不写归档桶，硬做必留半条链）----
    inherit_from = ""
    被盖房间: list[str] = []
    for cid in cover:
        live = await rt.bucket_mgr.get(cid)
        if live:
            inherit_from = inherit_from or cid
            被盖房间.append(str((live.get("metadata", {}) or {}).get("room") or ""))
            continue
        arch = await rt.bucket_mgr.get_including_archive(cid)
        if arch:
            return (f'{cid} 在归档区，盖不上（盖上了只会留半条链）。'
                    f'先 trace(bucket_id="{cid}", restore=True) 把它捞回来，再 fold。')
        return f"cover 里这些 id 不存在：{cid}。填真 bucket_id。"

    # ---- 闸①：cover 多条 + 事件 → 拒（「盖一组 event」8-17 砍掉）----
    # 判据看**被盖那几条自己的房间**，不只看 room 参数（room 可以填错、可以不填，
    # 而「我圈的是事件还是想法」被圈的那几条自己知道）。
    if len(cover) >= 2 and (is_event_room(room)
                            or any(is_event_room(r) for r in 被盖房间)):
        return ('盖一组事件不存在——日子用 when 画圈，看一条线用 recall(query)。\n'
                '  那几天在做一件什么事 → fold(when="2026-08-13..2026-08-16", '
                'room="EVENT/SELF", text=我写的那句)\n'
                '  「这些事是一条线」→ recall(query="青岛") 本来就是线的查看器\n'
                '（八个字：**Event 用时间，mind 用快照**。事件的一组没有「上一版」也不该'
                '被压住；一条事件记错了要盖掉，那是 cover 单条，那个口子留着。）')

    # ---- 房间：n=1 从被盖那条继承（regrow 就是这么干的）；其余必须自己判 ----
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

    # ⚰️ 2026-08-18：**「折一条」这支从工具面撤了**（她拍的）。
    #    以前 cover 恰好一条 = 换版，跟 regrow 做的是同一件事、走的也是同一段代码。
    #    同一句心里话两个入口，讲不清楚；所以现在分成：
    #      regrow = 这一条有了新版（认知/事件/时期都用它）
    #      fold   = 折起来（几条收成一句 / 一段日子起个名）
    #    🔴 底下那段「n=1 写版本链」的代码**没有删**：regrow 一直在用它
    #       （regrow 就是 fold 的 n=1 特例），撤掉的只是 fold 这个入口。
    supersedes = ""

    new_id, 报告 = await F.落一条gist(
        text, room, v, a, cover, when=when, from_ids=from_ids,
        supersedes=supersedes, test_data=bool(test_data))

    # ---- 说人话。时期和快照是两种话，因为它们真的是两件事 ----
    if when:
        # 「◈时期→id 范围 名字」+ 现场数的手感（不落盘）
        head = f"◈时期→{new_id} {when} {room}「{text.strip().splitlines()[0][:38]}」"
        tail = [f"（范围内现在有 {现有} 条——**现场数的**，只给个手感，不落盘。）",
                "（时期只给这段日子起了个名字：谁在里面按日期现算，"
                "补记自动归队、交叉和嵌套天然成立；**一条都没被压住**"
                "（照旧独立冒头、照旧搜得到）。recall 那段时间时它盖在顶上。）",
                "（边界想改就 regrow 换 when——边界本来就是糊的。）"]
        return head + "\n" + "\n".join(tail)

    n = len(报告["cover"])
    head = f"▣gist→{new_id} {room}（盖着 {n} 条"
    if n:
        head += "：" + "、".join(报告["cover"][:8]) + ("…" if n > 8 else "")
    head += "）"
    tail = [F.报告成话(报告)]
    if supersedes:
        tail.append("（= 换版：旧版留档不浮现，id 直查仍能看）")
    else:
        tail.append("（被盖的不再独立冒头，但 query 照样搜得到、id 直查钻得到）")
    return head + "\n" + "\n".join(x for x in tail if x)
