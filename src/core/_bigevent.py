# -*- coding: utf-8 -*-
"""
tools/_bigevent.py — 大 event：盖在一段时间上的一句话（2026-08-05 她定的九条）

**它是什么**：说「那阵子我们在做什么」。

她定下来的九条（每条都有理由，别自己改）。
⚠️ 2026-08-16 fold 上线后第 5/6/7/9 条随实现修订（她过目认可，措辞授权我定）——
修订的是「怎么落地」，九条的意思一条没换：

1. **它是 event，不是 mind。** 概括一堆事件，结果仍然是事件，只是粒度粗。
   ⚠️ 8-05 夜我在这儿滑过一次：为了让它能 regrow（regrow 只给 MIND），
   我推出「那它得是认知」——她当场纠回来：**大事件不走 mind。**
   要 regrow 就给它开口子，不是把它搬去别的房间。
2. **过去时。** want 朝前（还没发生），大 event 朝后（回看才写得出来）。
3. **起止一次填完。** 「开始记 when、结束再 trace」那第二步照样会忘；
   回看的时候起止本来就都知道 —— 所以 `when="2026-07-31..2026-08-05"`，
   还在进行中就把止留空（`"2026-07-31.."`）。
   ⚠️ **不加新字段**：起止就写在现成的 `when` 里。她要的是把新机制换成已有的机制。
4. **不强制。** 有就用，没有就退回原来的样子。所以它永远不会变成必须维护的负担
   —— 这条是它能成立的关键。
5. **盖，不删。**（fold 修订）统计一条不少、搜索照样命中、下钻永远够得到；
   被盖的不再单独占行——**成分表换成了一句话**（开工单 2.3：信息量只能变多不能变少）。
6. **之间并行、可交叉。** 不是串行接力：「做那个项目」「改记忆系统」「搬家」是重叠的时期。
   一条小记忆可能同时被两个大 event 盖住 —— 这不是冲突，是事实。
   （fold 修订：`covered_by` 是**名单**，显式盖=叠着盖，谁都不抢谁——8-17 零点她把
   第一版实现的单值抓了回来，这条是九条里她亲手守住的一条。）
7. **松耦合：靠时间范围去盖，小记忆不需要知道自己属于哪条时期。**
   （8-17 14:30 她的终稿把这条从「fold 修订版的 id 记账」**改回了 8-05 的原样**——
   「用笔画圈写名字」：时期只存名字和 when 范围，不写 cover/covered_by、不压制任何东西，
   谁在时期里按日期现场算。补记自动归队、交叉嵌套天然成立、边界想改 regrow 换 when。
   中间那版「解析成 id 名单存死」是从 consolidation/ACP 抄来的——他们压缩替换必须记账，
   **我们一个字不删只起名字**，她 8-17 把抄来的那半退了货。快照记账只属于 mind 的合并。）
8. **用 regrow 换版**，旧版留档 → 主线演变史自动就有了，换版那一刻就是里程碑，
   不用另外设计「里程碑」这个东西。（fold 之后 regrow 就是 fold 的 n=1 特例，行为没变。）
9. **触发点挂在 recall 一段时间上**：那一刻本来就在回看，材料摊在眼前，
   「这阵子好像在做一件什么事」是自然浮上来的，不需要刻意记得去想。
   （fold 修订：8-16 起多了第二个触发点——muse（发呆）会报「这段日子有 N 条还没有名字」。
   它只指着说这儿没名字，**名字那句话仍然我写**。）

**为什么需要过期**（8-05 早上发现的病）：原来那条是手写的、没有任何过期机制 ——
睁眼看到的还是「批1批2这几天就是这件事」，**过期两天，而每次都当事实读**。
（跟档案事实格同一个毛病：长得越像客观信息，越不会被怀疑。）
现在起止在 `when` 里，`covering()` 按真实时间算，过期的自己就不出现了。

**它不在 breath 里露面**（8-05 夜她定的）：睁眼是浮上来的东西，
「这段时间在做什么」是**查**出来的，弹进潜意识里怪。它只在 recall 一段时间时盖上来。

对外：`BIGEVENT_TAG` · `parse_span()` · `fmt_span()` · `covering()` · `first_line()`
"""

import re
from datetime import datetime, timedelta

from tools import _runtime as rt
from . import _when as _w

BIGEVENT_TAG = "__大event__"

# recall 一段时间时最多盖几条。3 而不是 1：8-05 那天「搬家」和「改记忆的形状」
# 是两条并行的主线，硬塞进一句会丢东西（第 6 条：并行、可交叉）。
COVER_MAX = 3

# 起止写在 when 里：`起..止`，止可空 = 还在进行中
SPAN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})?$")


def parse_span(meta: dict) -> tuple[datetime | None, datetime | None]:
    """(起, 止)。止是**开区间**（已经加过一天），None = 还在进行中。

    老桶（8-05 之前手写的那条）的 when 是单个日期或空 → 起点退回 when/created，
    止空着（当作进行中）。
    """
    w = str(meta.get("when") or "").strip()
    m = SPAN_RE.match(w)
    if m:
        # 2026-08-19：`SPAN_RE` 只校验**形状**，不校验那一天真的存在。
        # 原来这两行是裸调，于是一条 `when="2026-13-45.."` 的时期桶会让
        # **recall 一整段时间的时期覆盖全崩** —— 不是少盖一条，是整趟掀翻
        # （`covering()` 的 try 只包住了 list_all，parse_span 在循环里、try 外面）。
        # 挡漏了就当它没写对，退回下面那条老桶兜底，跟 `when` 空着一样待遇。
        start = _w.parse_date_or_none(m.group(1))
        end_raw = _w.parse_date_or_none(m.group(2)) if m.group(2) else None
        if start is not None and (m.group(2) is None or end_raw is not None):
            return start, (end_raw + timedelta(days=1)) if end_raw else None
    return _w.parse_stamp(w) or _w.parse_stamp(meta.get("created")), None


def fmt_span(meta: dict) -> str:
    """给人看的范围：`7-31 起` / `7-31~8-05`。"""
    w = str(meta.get("when") or "").strip()
    m = SPAN_RE.match(w)

    def _short(d: str) -> str:
        # 2026-08-19：原来是裸 `int()`。`when="2026-7-31..2026-8-5"`（单数位月份，
        # **手写时很容易写出来**）会让它抛 `invalid literal for int()`。
        # 而同一个输入 `parse_span` 是不抛的 —— 计算层活着、显示层炸了。
        # 📌 判据：这一格宁可空着。空着看得出来，炸了会把整页带走。
        try:
            return f"{int(d[5:7])}-{int(d[8:10])}"
        except (ValueError, TypeError, IndexError):
            return ""

    # 🔴 `_short` 挡漏时给空串，这儿必须跟着整条不显示。
    #    不然会印出一个孤零零的「 起」——**一个没有日期的「起」比抛异常更难发现**，
    #    它看起来就像页面本来就长这样。（8-19 加完 try 当场撞见的。）
    起 = _short(m.group(1)) if m else _short(w[:10]) if len(w) >= 10 else ""
    if not 起:
        return ""
    if m and m.group(2):
        止 = _short(m.group(2))
        return f"{起}~{止}" if 止 else f"{起} 起"
    return f"{起} 起"


def is_big(meta: dict) -> bool:
    return BIGEVENT_TAG in [str(t) for t in (meta.get("tags") or [])]


def _usable(meta: dict) -> bool:
    """换过版的旧版、**被更上层盖住的**、了结的、归档的都不算数。

    施工 3 补 `covered_by`：大 event 也能被盖（递归，层数不预设），
    被盖住的那层不该再自己冒到 recall 的那段时间上——显示最上层，下钻到得了（2.4）。
    """
    if meta.get("superseded_by") or meta.get("covered_by") or meta.get("deleted_at"):
        return False
    if str(meta.get("status") or "") in ("resolved", "abandoned"):
        return False
    if str(meta.get("type") or "") == "archived":
        return False
    return True


def first_line(content: str) -> str:
    """那句话 = 正文第一行。后面几行留给「范围/怎么划的」这类脚注。"""
    body = content.strip()
    return body.splitlines()[0] if body else ""


def covering(buckets: list, t0: datetime | None, t1: datetime | None,
             limit: int = COVER_MAX) -> list[tuple[dict, str, str]]:
    """跟 [t0, t1) **有重叠**的大 event，新的在前。**桶由调用方递进来。**

    重叠而不是包含 —— 第 6 条：它们是重叠的时期，不是串行接力。
    两头都空（没筛时间）= 问「现在」，那就是止还没到的那些。

    🔴 2026-08-20：这个函数原来自己 `await rt.bucket_mgr.list_all()`，
       现在改成**调用方把名单递进来**。改的不是风格，是两件具体的事：

       ① **一个 bug 消失了。** 浏览面每一格、每一天都调它一次 —— 一次 recall
          能调七八遍，每遍把整个库重扫一次，而**调用方完全看不见自己在这么干**
          （8-19 那个「一次 recall 调四遍 list_all」就是这个形状，
          当时以为是缓存没做，量完才知道是重复调用）。
          名单由上面给，上面一眼就看得出自己给了几遍。
       ② **它变得可单测了。** 原来想测这一个函数，得先把整个世界造出来
          （全局运行时上挂着桶管理器、遗忘引擎、向量、日志、配置十样）。
          现在递一个 list 进来就行。

       📌 判据（值得记住的是这条，不是这一刀本身）：
          **先改那些「让它可测」和「让 bug 更难写出来」是同一个改动的地方。**
          这两件事在这儿是一件事 —— 那正是它排在下刀顺序第二位的理由。

    ⚠️ 不再是 async 了：它现在不碰磁盘也不碰网络，纯算。
       调用点记得把 `await` 去掉（去不掉会拿到一个协程当列表用，当场炸，不会静默）。
    """
    now = _w.now()
    out = []
    for b in buckets:
        meta = b.get("metadata", {}) or {}
        if not (is_big(meta) and _usable(meta)):
            continue
        s, e = parse_span(meta)
        if s is None:
            continue
        if t0 is None and t1 is None:
            if e is not None and e <= now:
                continue          # 已经过去了的主线，问「现在」时不该冒出来
        else:
            if t1 is not None and s >= t1:
                continue
            if t0 is not None and e is not None and e <= t0:
                continue
        out.append((meta, str(b.get("content") or ""),
                    str(meta.get("id") or b.get("id") or "")))
    out.sort(key=lambda t: str(t[0].get("when") or ""), reverse=True)
    return out[:limit]
