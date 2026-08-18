"""
========================================
tools/recall/ — 回想（批 2a，2026-08-03）
========================================

「读」的第一个动作：我伸手去够。三个门可以叠着用，出来的东西一律走同一套缩放。

    recall(when="7月底")                          时间门
    recall(room="MIND/TRAITS")                    房间门（支持前缀，如 room="MIND"）
    recall(query="跟海有关的那件事")               搜（关键词+向量）→ 按时间＋分数
    recall(query="学代码", view="scene")            按共享场景词聚成簇（一路过来）
    recall(when="那阵子", tag="Home")             叠着用；tag 是白送的第四个筛子
    recall(when="上月", slices=1)                   那段我要看多粗／多细

🔪 **`by` 2026-08-17 砍了**（施工 5 · C 件）：`by="touched"`（她：我们没有「消化」
   这个动作）· `by="回看"`（`slices` 覆盖）。判据在开工单 5.4：
   **每个参数必须对得上一句我心里真会冒出来的话。**

三档密度（她 8-03 上午拍的）：
    A 概览（≥4 格）：每格两行——统计行 + 突出的点（带 id）
    B 单格卡（1~3 格）：在做什么/围着什么/心里/扎眼的 完整卡
    C 逐条列（≤20 条）：id · 摘要（🧠=认知），只给摘要不给正文

设计出处：交接/2-记忆系统-新设计.md 规格 B。
核心原则：输出是碎片不是句子，**不过模型**——每个字都是存的时候写下的，
或者是模板里的死字（说人话那层壳，开工单 5.3）。

对外暴露：dispatch(when, room, tag, query, slices, view) → str
========================================
"""

from .core import recall_core


async def dispatch(
    when: str = "",
    room: str = "",
    tag: str = "",
    query: str = "",
    slices: int = 0,
    view: str = "",
) -> str:
    try:
        slices = int(slices or 0)
    except (TypeError, ValueError):
        slices = 0
    kwargs = {}
    if 1 <= slices <= 20:
        kwargs["max_cells"] = slices
    return await recall_core(
        when=str(when or ""),
        room=str(room or ""),
        tag=str(tag or ""),
        query=str(query or ""),
        view=str(view or ""),
        **kwargs,
    )
