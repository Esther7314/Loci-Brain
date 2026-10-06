# -*- coding: utf-8 -*-
"""
========================================
tools/muse/ — musing: find them · lay them out in front of me · then shut up
========================================

The bones live in `tools/_muse.py` (three ways of pointing, plus the three-layer
evidence rule); this file only handles **the wording at the entrance**: lay it
out -> shut up.

------------------------------------------------------------
🔴 Two lines, up front
------------------------------------------------------------
> # The system only retrieves and arranges. The one who writes it down is always me.
> # The system may not point at anything on a guess; every pointer carries a
> # trace that we left ourselves.

**Nothing here writes the summary, suggests phrasing, or offers an example
sentence.** The summarising sentence (`fold`'s `text`) has to be mine.
The division of labour: **the judgement stays with me; heavy, repetitive
retrieval can be handed off.**

⚠️ Any sentence in this file along the lines of "this looks like...", "you might
   want to...", "you could write it as..." is out of bounds.
   Every character laid out is either **what was written down at storage time**
   (body, tags, coordinates, dates) or **fixed text from a template**.
   **Every pointer must be followed by its evidence** — a suggestion without
   evidence is a guess, and guessing is exactly what this version removes.

------------------------------------------------------------
Two steps, mirroring `home_look()`: look at the scene first, then act
------------------------------------------------------------
    step one `muse()`        the system offers clusters and pointers, **never
                             memories** (evidence lines show only the first 6
                             characters of an id)
    step two `muse(cluster=N)` I pick one and read that batch **in full,
                             verbatim** (never blurred, never truncated)
    any time `muse(not_same=[ids])` "these are not the same thing" — noted, so
                             the same set stops being offered

📌 A stretch of time may hold three threads, or none at all — **what is scattered
   is allowed to stay scattered.**
🔴 **Not one word of this is added to breath**: laid out when I am musing ✅ /
   never laid out in breath ❌.

Exports: dispatch(cluster, not_same) -> str · layout() · step_one() · _step_two()
(The last three are **pure functions**: the dry-run script uses them to render
sample screens offline, and the rendering is identical to the live one — there is
no second implementation.)
========================================
"""

from core import _muse as M
from .. import _runtime as rt
from .._common import read_scope
from core import _when as _w

FINGER_ORDER = ("词爆发", "成分漂移", "空白记账")
RULE = "─" * 40


def _date_label(dt) -> str:
    if dt is None:
        return "没有日子"
    return dt.strftime("%m-%d") if dt.year == _w.now().year else dt.strftime("%Y-%m-%d")


def _partial_id(bid: str) -> str:
    """Step one shows only the first 6 characters — **it hands out directions,
    not memories** (the full 12 belong to step two)."""
    return f"{str(bid)[:6]}…"


def _mind_evidence(t: "M.Cluster") -> str:
    """A cluster's evidence line: shelf coordinates · the from chain · the
    semantic top-up. Each of the three says its own piece; none of them may blur
    into another."""
    parts = [f"架 v{t.shelf_v:.2f} a{t.shelf_a:.2f}"]
    if t.from_core:
        shared = ("共祖 " + "、".join(_partial_id(x) for x in t.shared_from[:2])) if t.shared_from else "同一条链"
        parts.append(f"from 链 {len(t.from_core)} 条（{shared}）")
    if t.semantic_add:
        parts.append(f"语义补 {len(t.semantic_add)} 条（最低 {t.min_sim:.2f}）")
    if not t.from_core:
        parts.append("没有 from 痕迹，全靠语义海选")
    return " · ".join(parts)


def _mind_line(n: int, t: "M.Cluster") -> str:
    return f"  [{n}] {len(t)} 条 · {_mind_evidence(t)}"


async def _both_sides() -> tuple[list, int, int, dict, dict]:
    """Both sides share a single full-library scan (the pools are separate, but
    the material arrives on the same truck — a full scan is not cheap).

    🔴 This pass now **goes through the view cache** (`M.both_sides()`): step one
    laying out clusters and step two's `cluster=N` full read need the same result
    (the [N] numbering has to line up), and without the cache that means scanning
    the whole library twice. Invalidation follows bucket writes, and **erring
    towards invalidating too often is the right side to err on**.
    """
    return await M.both_sides(scope=await read_scope())


async def dispatch(cluster: int = 0, not_same=None) -> str:
    cfg = M.muse_config(rt.config)

    # ---------- Entry ③: these are not the same thing ----------
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
        view = await read_scope()
        missing = [i for i in ids if not await rt.bucket_mgr.get_including_archive(i)
                   or (view is not None and not view.permits_id(i))]
        if missing:
            return f"这些 id 不存在：{'、'.join(missing)}。填真 bucket_id。"
        buckets_dir = str((rt.config or {}).get("buckets_dir") or "")
        key, cnt = M.record_rejection(buckets_dir, ids)
        # The rejection counter writes json under `_state/` and **touches no
        # bucket**, so the view cache's key does not change. Without clearing it
        # by hand, saying "these are not the same thing" would be followed by the
        # very next screen offering the same set again.
        M.clear_view_cache()
        return (f"记下了：这 {len(ids)} 条**不是一回事**（第 {cnt} 次）。\n"
                f"{'、'.join(sorted(set(ids)))}\n"
                f"这一组不再提。**组变了**（多一条、少一条）会重新出现——"
                f"那时候它确实是新的一组。")

    clusters, scattered, default_coords, fingers, stats = await _both_sides()
    clusters, shown_fingers, extra_clusters, everything = layout(clusters, fingers, stats, cfg)

    # ---------- Entry ②: that batch, in full and verbatim ----------
    if cluster:
        n = int(cluster)
        if n < 1 or n > len(everything):
            if not everything:
                return ("现在一个团、一指都没有——没什么可看的。\n"
                        "（认知：v/a 分架成团；事件：词爆发 / 成分漂移 / 空白记账，"
                        "都得先有痕迹。）")
            return f"没有第 {n} 个。现在只有 [1]~[{len(everything)}]。先调 muse() 看一眼。"
        return _step_two(n, everything[n - 1])

    return step_one(clusters, scattered, default_coords, extra_clusters, shown_fingers,
                    int(stats["event"]["主线"]))


def layout(clusters, fingers, stats, cfg) -> tuple[list, list, int, list]:
    """Truncation and numbering. **Step one and step two share this one
    function** — if the two ever diverge, [N] points at the wrong entry."""
    clusters = list(clusters)[:int(cfg["max_clusters"])]
    extra_clusters = max(0, int(stats["mind"]["团"]) - len(clusters))
    cap = int(cfg["max_fingers"])
    shown_fingers = [(name, list(fingers.get(name, []))[:cap], len(fingers.get(name, [])))
                     for name in FINGER_ORDER]
    everything = list(clusters) + [x for _name, lst, _n in shown_fingers for x in lst]
    return clusters, shown_fingers, extra_clusters, everything


def step_one(clusters, scattered: int, default_coords: int, extra_clusters: int,
             shown_fingers, era_n: int) -> str:
    """Clusters and pointers first, never memories. **A pure function** — the
    dry-run script uses it to render sample screens offline, identical to live."""
    out = ["▣发呆 · 先给团，不给记忆　（指点必须带痕迹：坐标是我打的、链是我连的、"
           "词是我存的时候写的；向量只当海选）"]
    out.append("")
    out.append("碎着的认知（MIND · 没被盖过 · v/a 分架 → from 链 → 语义补）")
    if clusters:
        out += [_mind_line(i + 1, t) for i, t in enumerate(clusters)]
    else:
        out.append("  （一个团都没有）")
    out.append(f"  另有 {scattered} 条散着，没成团")
    if default_coords:
        out.append(f"  另有 {default_coords} 条还在老默认坐标 (0.5, 0.3) 上——"
                   f"那是老默认值不是感觉，等主人亲手重打，不进架")
    if extra_clusters:
        out.append(f"  （还有 {extra_clusters} 个团没摆出来）")
    out.append("")
    out.append("没名字的日子（EVENT · 三种指法，全带证据）")
    idx = len(clusters)
    for name, lst, total in shown_fingers:
        out.append(f"  · {name}")
        if name == "空白记账" and era_n < 1:
            # 🔴 The failure this guards against: with no periods stored at all,
            #    this finger could cheerfully announce that the entire year is
            #    unnamed — **that must not happen**.
            out.append("    （库里还没有一条时期——这一指不说话。没有地图的时候"
                       "「哪儿没盖」是个假问题，那不是空白，是还没开始画。）")
            continue
        if not lst:
            out.append("    （没有）")
            continue
        for x in lst:
            idx += 1
            out.append(f"    [{idx}] {x.evidence}")
        if total > len(lst):
            out.append(f"    （还有 {total - len(lst)} 条没摆出来）")
    out.append("")
    out.append("muse(cluster=N) 看那一批的全条逐字 · "
               "muse(not_same=[\"id\",\"id\"]) 这几条不是一回事")
    return "\n".join(out)


def _step_two(n: int, x) -> str:
    if isinstance(x, M.Cluster):
        rooms: dict[str, int] = {}
        for it in x.items:
            rooms[it.room] = rooms.get(it.room, 0) + 1
        head = (f"▣[{n}] {len(x)} 条 · "
                + "、".join(f"{r} {c}" for r, c in sorted(rooms.items())))
        evidence = "证据：" + _mind_evidence(x)
        if x.shared_from:
            evidence += "\n　　共祖全 id：" + "、".join(x.shared_from)
        parts = []
        for it in x.items:
            # 🔴 **Time is never evidence here** (thinking does not go by the
            #    calendar) — so what is shown is coordinates, not dates.
            mark = "← from 链" if it.id in x.from_core else "← 语义补"
            parts.append(f"· {it.id}  v{it.v:.2f}/a{it.a:.2f}  {it.room}  {mark}\n{it.text}")
        next_step = (f'fold(folds={x.ids}, text=我写的那句)\n'
                     f'  不是一回事 → muse(not_same={x.ids})')
        return f"{head}\n{evidence}\n{RULE}\n" + f"\n\n{RULE}\n".join(parts) + f"\n{RULE}\n{next_step}"

    # ---- One pointer on the event side ----
    span = (f"{_date_label(x.start)}~{_date_label(x.end)}"
            if x.start and x.end and x.start != x.end else _date_label(x.start))
    head = f"▣[{n}] {x.name} · {span}"
    parts = []
    prev = None
    for it in x.items:
        if x.boundary is not None and prev is not None and it.ts is not None \
                and prev < x.boundary <= it.ts:
            parts.append(f"{'┈' * 14} {_date_label(x.boundary)} 这条线 {'┈' * 14}")
        # "already named" = its date falls inside the span of some living period
        # (computed live, not a stored field);
        # "already covered" = a real cover (the case where an event was recorded
        # wrongly and re-versioned). Two different things, said separately.
        mark = ("  ← 已经被盖着" if it.covered
                else ("  ← 已经有名字（落在一条时期的范围里）" if it.named else ""))
        parts.append(f"· {it.id}  {_date_label(it.ts)}  {it.room}{mark}\n{it.text}")
        prev = it.ts
    next_step = f"{x.next_step}\n  不是一回事 → muse(not_same={x.ids})"
    return (f"{head}\n证据：{x.evidence}\n{RULE}\n" + f"\n\n{RULE}\n".join(parts)
            + f"\n{RULE}\n{next_step}")
