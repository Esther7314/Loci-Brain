"""
========================================
tools/grow/__init__.py — grow 工具入口
========================================

grow 是「我把一段长内容整理进记忆」。短内容（<30 字）走 shortpath，
⚰️ 2026-08-18 起工具面不再收长文（见 dispatch 末尾那段碑文）；shortpath/core 两条老路径代码留着不删档，只是没有入口了
独立事件桶。

关键行为：
- 入口做 items 校验
- 按 strip 后长度 < 30 字判断走哪个分支

不做什么（边界）：
- 不做 token 级别预算（grow 关心的是「拆几条」而不是「展示多少」）
- 不返回结构化数据，统一中文短句

对外暴露：dispatch(items=… / kind+text) → str
========================================
"""

from typing import Optional

from .. import _runtime as rt
from .._common import check_grow_input_size, check_grow_items_payload
# ⚰️ 长文切分那两条老路径（grow_shortpath / grow_core）2026-08-18 连代码一起删了。
#    「停用不删档」说的是**数据**（她的记忆、seed 那十三颗），不是代码——
#    发出去的仓库里留一堆没人调的死代码，只会让读的人以为它还活着。
from .core import grow_items
from .rooms_path import (grow_event, grow_mind, backfill_sweep,
                         _retired_fields_msg)

# 每个进程第一次 grow 调用时跑一次自愈扫描（codex 复核第 3 条）：
# 上次重启时在飞的后台回填会丢，sweep 把「room 有值但 summary 缺」的桶补回来。
_sweep_started = False


async def dispatch(
    items: Optional[list] = None,
    kind: str = "",
    room: str = "",
    text: str = "",
    from_=None,
    v=-1,
    a=-1,
    tense: str = "",
    weight=None,
    test_data: bool = False,
    when: str = "",
) -> str:
    await rt.decay_engine.ensure_started()

    # GLM 这类客户端有时把列表序列成 JSON 字符串——宽容地接（8-03 手机实测踩到）
    import json as _json
    if isinstance(items, str):
        try:
            items = _json.loads(items)
        except (ValueError, TypeError):
            pass
    if isinstance(from_, str) and from_.strip().startswith("["):
        try:
            from_ = _json.loads(from_)
        except (ValueError, TypeError):
            pass

    # --- 批 1（2026-08-03）：kind=event|mind 新路径 ---
    # 正文先落盘立刻返回真 id，打标/摘要/起名后台回填，不走合并。
    # 详见 rooms_path.py 顶部注释与工单 §5。
    kind = (kind or "").strip().lower()

    # ⚰️ 2026-08-18：`importance` / `meaning` 两个退役形参**整个删了**（她拍的）。
    #    原来留着是为了「传了能报出人话」，现在那件事交给工具面的 extra="forbid"
    #    （server.py 里 grow 那块）——传了直接被参数校验拒掉，比留一对假形参干净。
    if kind in ("event", "mind", "big"):
        global _sweep_started
        if not _sweep_started:
            _sweep_started = True
            import asyncio as _asyncio
            _asyncio.create_task(backfill_sweep())
    if kind == "event":
        return await grow_event(items or [], tense=tense, weight=weight,
                                from_ids=from_, test_data=test_data)
    if kind == "mind":
        return await grow_mind(room, text, from_, v, a, tense=tense,
                               weight=weight, test_data=test_data)
    if kind == "big":
        # ⚰️ 2026-08-18：`kind="big"` 从工具面撤了（她拍的）。
        #    它底下调的就是 fold 的骨头（`_F.落一条gist`），是个**纯别名**——
        #    立一个「时期」有两个入口，而两个入口迟早说两套话。
        #    撤完只剩 fold(when="起..止") 一条路。grow_big 的实现留着，没人调而已。
        return ('立一个「时期」（给一段日子起个名字）用 fold：\n'
                '  fold(when="2026-08-15..2026-08-18", room="EVENT/SELF", '
                'text="那阵子在做什么", v=…, a=…)\n'
                'grow 只管存发生了什么（items）和你从中看出什么（kind="mind"）。')
    if kind:
        return (f'kind 无效：{kind}。可选："event"（发生了什么）/ '
                '"mind"（我从中看出什么）。'
                '给一段日子起名字是 fold 的活。')

    # --- 以下是老路径，批 1 原样保留（批 2 收掉）---
    # 预拆分模式：上层 AI 已拆好 N 条最终正文 → 逐字入库，跳过 digest 的二次改写。
    # 传了 items（非空列表）即走此路；不传则行为与旧版完全一致（向后兼容）。
    if isinstance(items, list) and len(items) > 0:
        err = check_grow_items_payload(items)
        if err:
            return err
        return await grow_items(items)

    # ⚰️ 2026-08-18：`content`（丢一段长文进来、让系统替你拆成几条）砍了。
    #    她的判据：**那是整套里唯一一处「系统替我决定这是几件事」的入口**，
    #    跟「落笔的永远是我」正着劲；而 `items=[...]` 本来就完全覆盖它——
    #    收工时自己想清楚这一摊是几件事，然后一次存进去，才是这套东西要的姿势。
    #    （长文切分那两条路 `grow_core` / `grow_shortpath` 连同 dehydrator.cut()
    #      **代码留着不删档**，只是工具面不再有入口。）
    return ("grow 现在只收拆好的：items=[{room,text,v,a},...] 存多条，"
            "或 kind=\"mind\"/\"big\" + text 存一条。"
            "长文丢进来让系统替你拆成几条那条路已经撤了——"
            "这一摊是几件事，得你自己说了算。")
