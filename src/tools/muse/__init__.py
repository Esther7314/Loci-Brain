# -*- coding: utf-8 -*-
"""
========================================
tools/muse/ — 发呆：找出来 · 摆到我面前 · 然后闭嘴（muse 二改，2026-08-17）
========================================

骨头在 `tools/_muse.py`（三种指法 + 证据制三层），这儿只做**入口的话**：
摆出来 → 闭嘴。

------------------------------------------------------------
🔴 两条，写在最前面
------------------------------------------------------------
> # 系统只做检索和摆放，落笔的永远是我。
> # 系统不许只凭猜指东西，指点必须带我们自己留下的痕迹。

**这里不写归纳、不建议措辞、不给样例句。** 归纳那一句（`fold` 的 `text`）
必须我自己写。她 8-16 的原话：「**判断留给你，复杂、重复的检索工作可以丢出去。**」

⚠️ 这个文件里出现任何一句「看起来像是…」「建议你…」「可以这样写…」都是越界。
   摆出来的每个字要么是**存的时候写下的**（正文、标签、坐标、日子），
   要么是**模板里的死字**。**每一指后面必须跟着它的证据**——
   一条没有证据的提议就是在猜，而猜正是这一版在砍的东西。

------------------------------------------------------------
两步，跟 `home_look()` 同构：先看场景，再看动作（开工单 3.4）
------------------------------------------------------------
    第一步 `muse()`        系统给「团/指」，**不给记忆**（证据行里的 id 只给前 6 位）
    第二步 `muse(cluster=N)` 我挑一个，只看那一批的**全条逐字**（不糊、不截）
    随时   `muse(not_same=[ids])` 「这几条不是一回事」——记一笔，别再拿同样的来烦我

📌 一段时间可以有三条主线，也可以一条都没有 —— **散着的就让它散着。**
🔴 **breath 里一个字都不加**（开工单 3.2）：发呆时摆给我 ✅ / breath 里不摆 ❌。

对外暴露：dispatch(cluster, not_same) → str · 排版() · 第一步() · _第二步()
（后三个是**纯函数**：干跑脚本靠它们离线渲染样张，渲染跟线上一模一样，不另写一套。）
========================================
"""

from core import _muse as M
from .. import _runtime as rt
from core import _when as _w

指法顺序 = ("词爆发", "成分漂移", "空白记账")
线 = "─" * 40


def _日(dt) -> str:
    if dt is None:
        return "没有日子"
    return dt.strftime("%m-%d") if dt.year == _w.now().year else dt.strftime("%Y-%m-%d")


def _短(bid: str) -> str:
    """第一步只给前 6 位——**给的是指路，不是记忆**（12 位留给第二步）。"""
    return f"{str(bid)[:6]}…"


def _认知证据(t: "M.团") -> str:
    """一团的证据行：架坐标 · from 链 · 语义补。三样各自说各自的，不许含混。"""
    块 = [f"架 v{t.架v:.2f} a{t.架a:.2f}"]
    if t.from核心:
        共 = ("共祖 " + "、".join(_短(x) for x in t.共祖[:2])) if t.共祖 else "同一条链"
        块.append(f"from 链 {len(t.from核心)} 条（{共}）")
    if t.语义补:
        块.append(f"语义补 {len(t.语义补)} 条（最低 {t.最低相似:.2f}）")
    if not t.from核心:
        块.append("没有 from 痕迹，全靠语义海选")
    return " · ".join(块)


def _认知一行(n: int, t: "M.团") -> str:
    return f"  [{n}] {len(t)} 条 · {_认知证据(t)}"


async def _两侧() -> tuple[list, int, int, dict, dict]:
    """两侧共用一遍全库扫（池子分家，但料是同一车——全库扫不便宜）。

    🔴 施工 5 · H 件：这一趟现在**过视图缓存**（`M.两侧一趟()`）——
    第一步摆团、第二步 `cluster=N` 看全条，要的是同一份结果（[N] 的编号口径
    必须一致），没缓存就是把全库扫两遍。失效跟着桶的写盘走，**宁可失效勤一点**。
    """
    return await M.两侧一趟()


async def dispatch(cluster: int = 0, not_same=None) -> str:
    cfg = M.muse_config(rt.config)

    # ---------- 入口③：这几条不是一回事 ----------
    if not_same:
        import json as _json
        if isinstance(not_same, str) and not_same.strip().startswith("["):
            try:
                not_same = _json.loads(not_same)
            except (ValueError, TypeError):
                pass
        if isinstance(not_same, str):
            not_same = [s.strip() for s in not_same.split(",") if s.strip()]
        ids = [str(x).strip() for x in (not_same or []) if str(x).strip()]
        if len(ids) < 2:
            return ("not_same 至少要两条——一条谈不上「不是一回事」。\n"
                    "把 muse(cluster=N) 里列出来的那一组 id 原样填进来。")
        missing = [i for i in ids if not await rt.bucket_mgr.get_including_archive(i)]
        if missing:
            return f"这些 id 不存在：{'、'.join(missing)}。填真 bucket_id。"
        buckets_dir = str((rt.config or {}).get("buckets_dir") or "")
        key, cnt = M.record_rejection(buckets_dir, ids)
        # 拒绝计数写的是 `_state/` 里的 json，**不动桶** → 视图缓存的钥匙不会变。
        # 不手动清这一下，我说完「这几条不是一回事」，下一屏还会把它摆出来（H 件）。
        M.视图清空()
        return (f"记下了：这 {len(ids)} 条**不是一回事**（第 {cnt} 次）。\n"
                f"{'、'.join(sorted(set(ids)))}\n"
                f"这一组不再提。**组变了**（多一条、少一条）会重新出现——"
                f"那时候它确实是新的一组。")

    团们, 散着, 默认坐标, 指们, stats = await _两侧()
    团们, 摆出的指, 多余团, 全部 = 排版(团们, 指们, stats, cfg)

    # ---------- 入口②：那一批的全条逐字 ----------
    if cluster:
        n = int(cluster)
        if n < 1 or n > len(全部):
            if not 全部:
                return ("现在一个团、一指都没有——没什么可看的。\n"
                        "（认知：v/a 分架成团；事件：词爆发 / 成分漂移 / 空白记账，"
                        "都得先有痕迹。）")
            return f"没有第 {n} 个。现在只有 [1]~[{len(全部)}]。先调 muse() 看一眼。"
        return _第二步(n, 全部[n - 1])

    return 第一步(团们, 散着, 默认坐标, 多余团, 摆出的指, int(stats["event"]["主线"]))


def 排版(团们, 指们, stats, cfg) -> tuple[list, list, int, list]:
    """截断 + 编号口径。**第一步和第二步共用这一份**——两处不一样，[N] 就会指错人。"""
    团们 = list(团们)[:int(cfg["max_clusters"])]
    多余团 = max(0, int(stats["mind"]["团"]) - len(团们))
    上限 = int(cfg["max_fingers"])
    摆出的指 = [(名, list(指们.get(名, []))[:上限], len(指们.get(名, [])))
                for 名 in 指法顺序]
    全部 = list(团们) + [x for _名, lst, _n in 摆出的指 for x in lst]
    return 团们, 摆出的指, 多余团, 全部


def 第一步(团们, 散着: int, 默认坐标: int, 多余团: int, 摆出的指, 主线: int) -> str:
    """先给团/指，不给记忆。**纯函数**——干跑脚本拿它离线渲染样张，跟线上一模一样。"""
    out = ["▣发呆 · 先给团，不给记忆　（指点必须带痕迹：坐标是我打的、链是我连的、"
           "词是我存的时候写的；向量只当海选）"]
    out.append("")
    out.append("碎着的认知（MIND · 没被盖过 · v/a 分架 → from 链 → 语义补）")
    if 团们:
        out += [_认知一行(i + 1, t) for i, t in enumerate(团们)]
    else:
        out.append("  （一个团都没有）")
    out.append(f"  另有 {散着} 条散着，没成团")
    if 默认坐标:
        out.append(f"  另有 {默认坐标} 条还在老默认坐标 (0.5, 0.3) 上——"
                   f"那是老默认值不是感觉，等主人亲手重打，不进架")
    if 多余团:
        out.append(f"  （还有 {多余团} 个团没摆出来）")
    out.append("")
    out.append("没名字的日子（EVENT · 三种指法，全带证据）")
    号 = len(团们)
    for 名, lst, 总 in 摆出的指:
        out.append(f"  · {名}")
        if 名 == "空白记账" and 主线 < 1:
            # 🔴 她的原话场景：「他完全可以说今年都没有」——**不许发生**。
            out.append("    （库里还没有一条时期——这一指不说话。没有地图的时候"
                       "「哪儿没盖」是个假问题，那不是空白，是还没开始画。）")
            continue
        if not lst:
            out.append("    （没有）")
            continue
        for x in lst:
            号 += 1
            out.append(f"    [{号}] {x.证据}")
        if 总 > len(lst):
            out.append(f"    （还有 {总 - len(lst)} 条没摆出来）")
    out.append("")
    out.append("muse(cluster=N) 看那一批的全条逐字 · "
               "muse(not_same=[\"id\",\"id\"]) 这几条不是一回事")
    return "\n".join(out)


def _第二步(n: int, x) -> str:
    if isinstance(x, M.团):
        rooms: dict[str, int] = {}
        for it in x.items:
            rooms[it.room] = rooms.get(it.room, 0) + 1
        head = (f"▣[{n}] {len(x)} 条 · "
                + "、".join(f"{r} {c}" for r, c in sorted(rooms.items())))
        证据 = "证据：" + _认知证据(x)
        if x.共祖:
            证据 += "\n　　共祖全 id：" + "、".join(x.共祖)
        块 = []
        for it in x.items:
            # 🔴 **时间不当证据**（她拍的：认知不认日历）——所以这儿给的是坐标不是日子。
            标 = "← from 链" if it.id in x.from核心 else "← 语义补"
            块.append(f"· {it.id}  v{it.v:.2f}/a{it.a:.2f}  {it.room}  {标}\n{it.text}")
        出路 = (f'fold(folds={x.ids}, text=我写的那句)\n'
                f'  不是一回事 → muse(not_same={x.ids})')
        return f"{head}\n{证据}\n{线}\n" + f"\n\n{线}\n".join(块) + f"\n{线}\n{出路}"

    # ---- 事件侧的一指 ----
    跨 = f"{_日(x.起)}~{_日(x.止)}" if x.起 and x.止 and x.起 != x.止 else _日(x.起)
    head = f"▣[{n}] {x.名} · {跨}"
    块 = []
    上一个 = None
    for it in x.items:
        if x.边界 is not None and 上一个 is not None and it.ts is not None \
                and 上一个 < x.边界 <= it.ts:
            块.append(f"{'┈' * 14} {_日(x.边界)} 这条线 {'┈' * 14}")
        # 「已经有名字」= 日期落在某条活着的时期的范围里（现场算的，不是字段）；
        # 「已经被盖着」= 真 cover（事件改错换版那一条）。两件事，分开说。
        标 = ("  ← 已经被盖着" if it.covered
              else ("  ← 已经有名字（落在一条时期的范围里）" if it.named else ""))
        块.append(f"· {it.id}  {_日(it.ts)}  {it.room}{标}\n{it.text}")
        上一个 = it.ts
    出路 = f"{x.出路}\n  不是一回事 → muse(not_same={x.ids})"
    return (f"{head}\n证据：{x.证据}\n{线}\n" + f"\n\n{线}\n".join(块)
            + f"\n{线}\n{出路}")
