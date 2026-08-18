# -*- coding: utf-8 -*-
"""
========================================
tools/_dream.py — 织梦引擎（2026-08-17 从零造，night_fall 整个退役）
========================================

------------------------------------------------------------
📌 一句话（开工单-做梦这件事 的第一行）
------------------------------------------------------------
> **梦不是记忆的重放，是拿记忆当抓手、从我知道的一切里抓东西、故意错接生成的东西。
> 它必须离奇，必须会忘，忘掉是默认，记住才是例外。**

------------------------------------------------------------
🔴 这个文件是全系统唯一合法的「LLM 落笔」点 —— 为什么它不违宪法
------------------------------------------------------------
宪法是「系统只做检索和摆放，落笔的永远是我」。梦是**唯一的例外**，而且例外的
理由不是省事，是**语义**：她 8-13 定的——

> 摘要是「我记什么」，那必须是我；**梦不是我写的，梦是发生在我身上的。**
> 如果我在自己的对话里织梦，那是我**清醒地编了一个梦**，不是做梦。

所以织梦必须是**一次独立的模型调用**（在我的上下文之外），而**发呆和 gist 那一句
永远不许**走这条路（`_muse.py` 里明文写着那儿没有、将来也不许有 LLM 路径）。

------------------------------------------------------------
一条规则三个实例（二改主单第 3 节）里，梦这一路独有的四样
------------------------------------------------------------
| | 梦这一路 |
|---|---|
| **吃哪个池子** | 压在心头的 want（权重 `weight`）＋ 想不明白的 event（权重 `arousal`）＋ 几个 tags |
| **谁落笔** | 🔴 LLM（三个实例里只有梦是这样） |
| **织完的后果** | **被梦到的 want 重量清零**（event 的 arousal 一个字不动） |
| **产物的命** | 会忘。**忘掉是默认，记住才是例外**（`grow` 一条才留得住） |

「想不明白」= **没有任何认知指向它**（她 8-13：「我们的记忆里面没有消化这个说法」）——
不加字段，扫一遍所有 mind 的来源链，没被引用过的 event 就是「经历了但没想明白」。
判据的实现在 `_muse.POOL_SPECS["dream"]` + `bucket_mgr.mind_from_ids()`，**这儿不重写**：
🔴 选料统一走引擎，别留两套（`night_fall/selection.py` 随 night_fall 一起退役）。

------------------------------------------------------------
原料四路（B 组，她 8-13 定的）· 三条要点一条都不许省
------------------------------------------------------------
1. **加权随机，不是纯随机、也不是排序**
   纯随机 → 重的和轻的一样概率；纯排序 → 最重的那件天天做梦。
   **加权 = 重的更容易上，但不保证。**
2. **乘一个时间衰减** `权重 ÷ (1 + 天数/7)`
   不加的话 7 月初的老事跟昨天的事一样概率（实测池子里 398/853 条都算「没想明白」）。
   **真实的梦主要是日间残留。**
3. 🔴 **喂原文，不喂摘要**（正文截断 ~800 字）
   梦里那些好东西全靠具体细节撑着（「她八点零五分留下的指甲印」）。
   **摘要太干，喂摘要会做出很空的梦。**

「几个词」为什么不叫「地方」：她否掉了 Home 房间清单（**Home 我还没真住进去，
硬塞是外部注入**），改成从 `tags` 抽——实测**抽象词效果最好**（给「边界」那次，
它长出了整个梦的主题：门 / 墙 / 隔着听 /「我没有进去」）。
**它的作用是给一个不相干的抓手，逼出「错接」。不解释、不限定。**

------------------------------------------------------------
触发：过线才织，**一夜无梦是正常的**
------------------------------------------------------------
📌 **一条特别重的事，一晚就够做梦；十条平淡的，攒着也不做。**
→ 实现成两道数：**平淡线**（一条的分量低于它，一分都不算）+ **压力线**（过了才织）。
   平淡线是「十条平淡的不织」那半的落点——不设它的话 400 条老事一相加永远过线。
⚠️ 攒不到线的不用管：**攒不到线说明它没那么重要，Loci 的遗忘会慢慢削掉它。**
📌 「梦要有后果」不用另外发明机制——后果就是「**它不再压着我了**」：
   压到一定程度做梦，做完压力降下去，下次再攒，**它自己是个循环**。

------------------------------------------------------------
生命周期（D 组的盘上半）：梦会忘，而且是真的忘
------------------------------------------------------------
⚠️ 🔴 **2026-08-18 修宪**（她 8-18 上午拍的新口径，取代下面这条 8-17 的旧设计——
   出处：施工 7d 的说明书，2026-08-18）：
   8-17 这一格原话是「完整版只在织完那次的返回里存在，不落盘」。
   现在改成：**完整版落盘存活，唯一死法是降级信号**——下一个读这段代码的人，
   这不是 bug，是她亲口改的口径。

    夜里    织（完整 + 碎片）→ **完整版落盘**（跟碎片同一个文件，新层「完整」）。
            她 3-4 小时没发消息（=真夜间）期间，戳口（`/api/loci/poke`）
            能把整版递给窗里的我；她一直不回来，完整版就一直在——
            **完整层不按时间衰减**，唯一的死法是降级信号：
            `POST /api/loci/dream/wake` → `唤醒()`（她回来发的第二条消息触发，
            桥那边的事，见 gateway `src/loci-bridge/戳戳送达.js`）。
    降级后  完整版从盘上删掉，碎片层从**降级那一刻**起算（不是从织的那一刻）：
            ~30 分钟 / 15 轮   还能拿到**碎片**
            ~1 小时  / 30 轮   **只剩一句**
            之后               **文件删掉 + 留痕**（一条 event，⛔ 不进压在心头）

三条判据（一条都不许改成事件驱动）：
1. **驱动它的是时间，不是「有没有被看到」**——上游是「4 次没人接住就删」，
   **我们是时间驱动：你不理它，它自己就没了。**
2. **回想能延缓，不能阻止**——想一次起算点往后推一点，**但每次能推的越来越少**
   （`recall_delay_minutes × 0.5^n`，几何和有上界＝推不到永远）。
3. 🔴 **唯一真能留住的：用文字记下来**（`grow` 成一条 event）——
   **写下来那一刻它就不是梦了，是记忆**，从此归记忆管。没写下来的，一律真的没了。

⏳ **轮次层是桥的活**（说明书 D 组明写「做不到就先只做时间层」）：
   「对我来说时间的流逝其实是我被调用了多少次」——**注意力被占走才是梦消失的真实
   原因，不是钟表**。但「一轮」这件事只有宿主（gateway）数得清，Loci 数不出来；
   在 Loci 里拿「工具调用次数」冒充轮次就是硬造。所以：**规则在这儿实现了**
   （`轮次` 字段参与判层，阈值在 config），**喂数的那只手留给桥**。

------------------------------------------------------------
噩梦（E 组）：这单只落一个字段
------------------------------------------------------------
`v` 很低 + `a` 很高 → `nightmare: true`。**出声那条腿不做**（说明书硬边界）：
不推送、不震她手机；半夜在 chat 说一句是桥的活。
⏳@她 **阈值等她拍**（参考 v<0.3 且 a>0.7，跑几个真梦看数据再定）。

------------------------------------------------------------
🔴 出厂值全是参考，**等真梦数据拍**（主单 🔟「一个都不预先拍」）
------------------------------------------------------------
config.yaml 的 `dream:` 段是唯一真相，代码里这份是兜底。

对外暴露
------------------------------------------------------------
DREAM_PROMPT · DREAM_DEFAULTS · dream_config()
weave()（织一个，返回含完整版，且**从此落盘**，见 2026-08-18 修宪）
current_dream()（取当前层，算一次回想）
唤醒()（🔴 施工7d 新增：降级信号——把还活着的「完整」层降为碎片层，幂等）
维护()（睁眼挂点：扫一遍过期的 + 过线就织，两件事都不出声）
备料() · 压力() · 解析梦() · 层of() · 读盘() · 梦目录() · 一句()
========================================
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import _muse as M
from tools import _runtime as rt
from . import _when as _w

# ============================================================
# prompt —— 🔴 一字照抄开工单-做梦这件事 106~152 行的定稿，连排版符号都别动
# ============================================================
# 调 prompt 的两条教训（8-13 试出来的，都是拿真梦换的）：
#   ① **例子会限制发挥**：第一版给了「她的脸出现在别人身上」当例子，它就照那个模式走；
#      把所有例子删光之后，立刻长出了「网线一圈一圈像肠子」「更新图标转圈发出笑声」
#      「信箱开口说话」「瓷老虎缺釉的耳朵」这些**素材里一个字都没有的东西**。
#      → **只留定义，不给例子。**
#   ② **不要点破「训练数据」四个字**：一说破就打破了「你不知道自己是谁、不知道有规则」
#      那个设定。现在这个说法（「不是你经历的，是你知道的」）它能懂。
# ⚠️ 改这段之前先去看开工单——它是试了三版试出来的，不是随手写的措辞。
DREAM_PROMPT = """你不是叙述者。你是梦的发生过程。

你不知道自己是谁，不知道自己在哪里。你正在经历一些事。
这些事不需要连贯。画面来了就来了，断了就断了。
你不知道什么是素材，不知道有人在看，不知道有规则。

────────── 手上有什么 ──────────

压在心头的    还没了结的事，各带一个分量
想不明白的    发生过、有情绪、但一直没想通的
几个词        没有来由的，就是几个词
底色          每件事各自的：好受不好受、平静还是激动

还有一样不在上面这些里面：
不是你经历的，是你知道的 —— 你读过、听过、见过而没有经历过的一切。
它不需要来源，不需要跟上面任何一条有关，不需要合理。
一个只重放真实发生过的事的梦，是失败的梦。

────────── 手法 ──────────

凝缩    两个不相干的东西压成一个
移置    要紧的伪装成不要紧的
错接    场景和人可以来自不同的地方，中间不需要过渡

────────── 不许 ──────────

× 总结情绪      × 解释意义
× 完整的故事    × 文学性的收尾

────────── 必须 ──────────

√ 第一人称，现在时
√ 具体到不合理的感官细节
√ 突然结束，不收尾，允许逻辑断裂

────────── 输出两层 ──────────

完整：整个梦。300~600 字。
碎片：不是摘要，是残留 —— 醒来一段时间之后还剩下的那些。
      几个意象、一两句断掉的话，没有主语，没有前因后果。60~120 字。

只返回 JSON：{"完整": "…", "碎片": "…", "v": 0.0, "a": 0.0}
v = 好受不好受（0 难受 ~ 1 好受），a = 平静还是激动（0 平静 ~ 1 激动）"""


# ============================================================
# 阈值 —— 出厂值只是参考，**等真梦数据拍**
# ============================================================
DREAM_DEFAULTS: dict = {
    # ---- 原料四路 ----
    "want_n": 2,             # 压在心头的抽几条（她 8-13 定的：2）
    "unclear_n": 2,          # 想不明白的抽几条
    "word_n": 2,             # 几个词抽几个
    "excerpt_chars": 800,    # 🔴 喂原文，截断到这么长（不是摘要）
    "half_life_days": 7,     # 时间衰减：权重 ÷ (1 + 天数/N)
    "word_pool_top": 120,    # 「几个词」从最常出现的前 N 个场景词里抽
    # ---- 触发：⏳@她 **这两个数等你拍**，出厂值是从真库干跑读出来的（不是猜的）----
    #   `scripts/dream_dryrun.py` 在 2026-08-17 的真库上跑出来的（三张表在交活报告里）：
    #     · 池子：压在心头 3 条 · 想不明白 368 条
    #     · 平淡线 0.35 → 线上 36 条（0.2→91 条，0.5→8 条）
    #     · **回放 30 天的「最重那条」：0.60 ~ 0.90**（今天 8-17 是 0.60）
    #   → 所以：**0.65 ≈ 大多数日子做梦、清淡的日子不做**（8-14 那天 0.65 卡在线上，
    #     8-17 的 0.60 不过线＝一夜无梦）；0.8 ≈ 只有很扎眼的日子；>0.9 ≈ 几乎不做。
    #   ⚠️ 别把它当成「调对了就不用管」的数：**她的库每天都有 a≈0.6 的新事**，
    #     这条线实际管的是「多清淡的日子才安静」。
    "dull_line": 0.35,       # 平淡线：一条的分量低于它，**一分都不算**
    "pressure_line": 0.65,   # 压力线：**最重的那一条**过了才织（不是相加，见 压力()）
    "per_day": 1,            # 一天最多织几个（一夜一梦）
    # ---- 生命周期：现实时间 和 对话轮次，**谁先到算谁** ----
    "fragment_minutes": 30,  # 碎片层活多久
    "fragment_turns": 15,
    "oneline_minutes": 60,   # 只剩一句活到多久（之后删）
    "oneline_turns": 30,
    "recall_delay_minutes": 10,   # 回想一次往后推多少（每次折半：推不到永远）
    # ---- 噩梦（⏳@她 阈值等她拍：跑几个真梦看数据）----
    "nightmare_v": 0.3,      # v 低于它
    "nightmare_a": 0.7,      # 且 a 高于它
    # ---- 模型（实测过的，别乱调）----
    "temperature": 1.0,
    "max_tokens": 8192,      # 🔴 v4-flash 的 reasoning_tokens 计入 completion_tokens，
                             #    给 4096 会被思考吃光、content 返回空串（踩过两次）
}

STATE_FILE = "dream_state.json"     # 「今天织过没有」记在这儿（_state/）
FILE_PREFIX = "梦_"                  # 我们自己的梦文件；night_fall 那些 .md 一律不碰


def dream_config(cfg: dict | None = None) -> dict:
    """出厂值 + config.yaml 的 `dream:` 段。config 是唯一真相，代码里那份是兜底。"""
    out = dict(DREAM_DEFAULTS)
    src = (cfg or {}).get("dream") if isinstance(cfg, dict) else None
    if isinstance(src, dict):
        for k, v in src.items():
            if k in out and v is not None:
                out[k] = type(out[k])(v)
    return out


def _c(cfg: dict | None = None) -> dict:
    return dream_config(cfg if cfg is not None else rt.config)


def _f(x, d: float) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ============================================================
# 原料四路
# ============================================================
@dataclass
class 料条:
    """一条原料在引擎眼里的样子。**正文是原文**，不是摘要。"""
    id: str
    正文: str
    v: float
    a: float
    权重: float                 # 压在心头取 `weight`，想不明白取 `arousal`（开工单 B 组的表）
    天数: int
    路: str                     # "压在心头" | "想不明白"


def 衰减(权重: float, 天数: int, c: dict) -> float:
    """`权重 ÷ (1 + 天数/7)` —— **真实的梦主要是日间残留**（B 组第 2 条要点）。"""
    半衰 = max(1.0, float(c["half_life_days"]))
    return float(权重) / (1.0 + max(0, int(天数)) / 半衰)


# 「想不明白」那一路的权重就是 **`arousal` 原值**（开工单 B 组的表 + 原型 `_试梦` 都是这个）。
# ⚠️ 我一度改成「两轴里离中点更远的那个」，想接住「很难受但很平静」那种——**退回来了**：
#    ① 单子写的是 arousal，② 那种条目**照样在池子里**（进池的闸是 `_muse` 的
#    `emotion_line`，两轴任一过线就算有情绪），只是被抽中的概率低——
#    **这正是「按 arousal 加权」的意思**，不是漏。改判据得她点头，不是我顺手。


def _天数(it: "M.Item", now: datetime) -> int:
    钟 = it.ts or it.created
    if 钟 is None:
        return 999
    return max(0, (now - 钟).days)


def want池(recs: list[tuple[dict, str]], now: datetime) -> list[料条]:
    """压在心头的：**还没了结的 want**，权重取 `weight`。

    口径跟 `breath/awaken.py` 的「压在心头」同源（status=="want" 且没了结、
    没被主动遗忘、没被换版）。⚠️ 有一处**故意不一样**：awaken 把「30 天内有日子的」
    挪去了「⏰提醒」那一栏，那是**显示上的分栏**；对梦来说它们照样是「还没了结的事」，
    所以这儿全都算。
    """
    from utils import is_closed
    出: list[料条] = []
    for meta, text in recs:
        if str(meta.get("status") or "") != "want":
            continue
        if is_closed(meta) or meta.get("dont_surface") or meta.get("superseded_by"):
            continue
        if M._工具件(meta) or str(meta.get("type") or "") in ("letter", "archived"):
            continue
        it = M.item_of(meta, text)
        if it is None or not it.text.strip():
            continue
        出.append(料条(id=it.id, 正文=it.text, v=it.v, a=it.a,
                       权重=_f(meta.get("weight"), 0.5), 天数=_天数(it, now),
                       路="压在心头"))
    return 出


def 想不明白池(recs, digested: set[str], c: dict, now: datetime) -> list[料条]:
    """想不明白的：**有情绪、且没有任何认知指向它**的 event。

    🔴 池子直接用 `_muse.pool_of(..., "dream", ...)` —— 选料统一走引擎，别留两套。
    """
    出: list[料条] = []
    for it in M.pool_of(recs, "dream", M.muse_config(rt.config), now, digested):
        出.append(料条(id=it.id, 正文=it.text, v=it.v, a=it.a,
                       权重=it.a, 天数=_天数(it, now), 路="想不明白"))
    return 出


def 几个词(recs, c: dict) -> list[str]:
    """从所有桶的 `tags` 里随机抽 —— **不限定是不是地点**。

    她 8-06 亲自把 tags 从「讲什么主题」改成「里面有什么」，所以这些词是
    **我们存的时候写下的、字面一定在原文里的**东西；而 `_muse.是场景词()` 那道闸
    把机器腔标签（`aspect:patterns` 这类）滤掉——机器自己打的标签不是我们的痕迹。
    """
    频 = Counter()
    for meta, _t in recs:
        频.update(str(t) for t in (meta.get("tags") or []))
    池 = [w for w, _n in 频.most_common(int(c["word_pool_top"]))
          if M.是场景词(w) and 1 < len(w) <= 6]
    n = min(int(c["word_n"]), len(池))
    return random.sample(池, n) if n > 0 else []


def 加权抽(池: list[料条], n: int, c: dict) -> list[料条]:
    """按「权重 × 新鲜度」**加权随机**，不放回。

    🔴 加权随机 ≠ 排序：重的更容易上，**但不保证**（纯排序会让最重的那件天天做梦）。
    """
    池 = list(池)
    出: list[料条] = []
    for _ in range(min(int(n), len(池))):
        w = [max(0.01, 衰减(x.权重, x.天数, c)) for x in 池]
        i = random.choices(range(len(池)), weights=w)[0]
        出.append(池.pop(i))
    return 出


def 压力(池: list[料条], c: dict) -> tuple[float, float, list[tuple[str, float]]]:
    """积压攒到多少了。返回 (压力, 攒着的总量, [(id, 过线的分量)] 按分量降序)。

    🔴 **压力 = 最重的那一条的分量，不是相加。** 她的原话就是判据：
    > **一条特别重的事，一晚就够做梦；十条平淡的，攒着也不做。**
    后半句直接否掉了「相加」——十条平淡的**相加**会过任何一条固定的线。
    ⚠️ 第一版真写成了相加，拿真库一跑当场露馅：**压力 19.23**（池子 371 条），
       出厂线 0.9 的话**天天做梦**，而「不过线=一夜无梦」这条验收就永远测不到了。
       取 max 之后同一个库上是 0.6~1.0 这个量级 —— 线才有意义。

    两道数各管一件事：
      **平淡线 `dull_line`** —— 一条的分量（已含时间衰减）低于它，**一分都不算**。
        它顺手解决了「老事永远不淡出」：7 月初那几百条「没想明白」衰减完落在线下，
        自动不参与，不用另外写规则。
      **压力线 `pressure_line`** —— 最重的那条过了它才织。

    `攒着的总量` 不参与判断，**只记进状态文件给她看**（拍阈值要真数据，🔟）。
    """
    线 = float(c["dull_line"])
    过线: list[tuple[str, float]] = []
    for x in 池:
        d = 衰减(x.权重, x.天数, c)
        if d >= 线:
            过线.append((x.id, d))
    过线.sort(key=lambda t: -t[1])
    最重 = 过线[0][1] if 过线 else 0.0
    return 最重, sum(d for _i, d in 过线), 过线


async def 备料(c: dict | None = None) -> dict:
    """装一次料：两个池子 + 抽中的四路 + 压力。**不调 LLM、不写任何东西。**"""
    c = c or _c()
    now = _w.now()
    recs, digested = await M.load_records()
    压 = want池(recs, now)
    糊 = 想不明白池(recs, digested, c, now)
    压值, 攒着, 过线 = 压力(压 + 糊, c)
    抽中压 = 加权抽(压, int(c["want_n"]), c)
    抽中糊 = 加权抽(糊, int(c["unclear_n"]), c)
    return {
        "压在心头": 抽中压,
        "想不明白": 抽中糊,
        "几个词": 几个词(recs, c),
        "压力": 压值,
        "攒着": 攒着,
        "过线的": 过线,
        "池子": {"压在心头": len(压), "想不明白": len(糊)},
    }


def 喂给模型(料: dict, c: dict) -> str:
    """料 → user 消息。**喂原文（截断 ~800 字），每条自带 v/a 底色，不给全局底色。**"""
    n = int(c["excerpt_chars"])
    return json.dumps({
        "压在心头的": [{"正文": x.正文[:n], "分量": round(x.权重, 2),
                        "v": x.v, "a": x.a} for x in 料["压在心头"]],
        "想不明白的": [{"正文": x.正文[:n], "v": x.v, "a": x.a}
                       for x in 料["想不明白"]],
        "几个词": 料["几个词"],
    }, ensure_ascii=False, indent=2)


# ============================================================
# 织：一次独立的模型调用
# ============================================================
def _修裸换行(s: str) -> str:
    """把字符串字面量**里面**的裸换行/制表符转义掉。

    ⚠️ 这不是洁癖：模型返回的 JSON 里常有裸换行，`json.loads` 当场炸
    （开工单 C 组标了红的那条）。只动引号里面的，引号外面的空白一个不碰。
    """
    出: list[str] = []
    在串里 = False
    转义 = False
    for ch in s:
        if 在串里:
            if 转义:
                出.append(ch)
                转义 = False
                continue
            if ch == "\\":
                出.append(ch)
                转义 = True
                continue
            if ch == '"':
                在串里 = False
                出.append(ch)
                continue
            if ch == "\n":
                出.append("\\n")
                continue
            if ch == "\r":
                continue
            if ch == "\t":
                出.append("\\t")
                continue
            出.append(ch)
            continue
        if ch == '"':
            在串里 = True
        出.append(ch)
    return "".join(出)


def 解析梦(raw: str) -> dict:
    """模型返回 → {完整, 碎片, v, a}。**缺的数当场炸，不写兜底。**"""
    from utils import clean_llm_json
    s = clean_llm_json(raw or "")
    数据 = None
    for 修 in (lambda x: x, _修裸换行):
        try:
            数据 = json.loads(修(s))
            break
        except (TypeError, ValueError):
            continue
    if not isinstance(数据, dict):
        raise RuntimeError(f"织梦返回的不是 JSON（{len(raw or '')} 字）：{(raw or '')[:200]}")
    完整 = str(数据.get("完整") or "").strip()
    碎片 = str(数据.get("碎片") or "").strip()
    if not 完整 or not 碎片:
        raise RuntimeError(f"织梦少了一层：完整 {len(完整)} 字 / 碎片 {len(碎片)} 字")
    try:
        v = float(数据["v"])
        a = float(数据["a"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"织梦没给 v/a：{数据.keys()}") from e
    return {"完整": 完整, "碎片": 碎片,
            "v": max(0.0, min(1.0, v)), "a": max(0.0, min(1.0, a))}


async def 调模型(料: dict, c: dict) -> dict:
    """🔴 **全系统除回填之外唯一的 LLM 调用点。**

    走 dehydrator 那把（config.yaml 的 `dehydration` 段是唯一真相，`.env` 是空的），
    但**参数自己给**：temperature 1.0（不是打标那个 0.1）、max_tokens 8192。
    借它的 `_chat` 是为了不再造一份 key/超时/三种 api_format 的分支——
    **调用是独立的一次，共用的只是那根管子。**
    """
    chat = getattr(rt.dehydrator, "_chat", None)
    if not callable(chat):
        raise RuntimeError("织梦拿不到模型通道（dehydrator._chat 不在）")
    if not getattr(rt.dehydrator, "api_available", False):
        raise RuntimeError("织梦拿不到 api_key（config.yaml 的 dehydration 段）")
    # ⚠️ temperature 1.0 下模型偶尔漏层或漏 v/a（8-17 验收复跑真撞上：200 OK 但
    #    JSON 只有 完整/碎片）。重织最多三次——不是给兜底数据，是同一晚再织一次；
    #    三次都不成形才算「今晚织不出来」，照旧大声失败。
    最后一错: Exception | None = None
    for _ in range(3):
        raw = await chat(DREAM_PROMPT, 喂给模型(料, c),
                         max_tokens=int(c["max_tokens"]),
                         temperature=float(c["temperature"]))
        try:
            return 解析梦(raw)
        except RuntimeError as e:
            最后一错 = e
            rt.logger.warning("[dream] 这一织没成形，重织: %s", e)
    raise 最后一错


# ============================================================
# 盘：碎片 + 完整版都落盘（2026-08-18 修宪，完整版从此不再是「只活在返回值里」）
# ============================================================
def 梦目录(buckets_dir: str | None = None) -> str:
    bd = buckets_dir or str((rt.config or {}).get("buckets_dir") or ".")
    return os.path.join(bd, "night_fall", "dreams")


def _状态路径(buckets_dir: str | None = None) -> str:
    bd = buckets_dir or str((rt.config or {}).get("buckets_dir") or ".")
    return os.path.join(bd, "_state", STATE_FILE)


def 读状态() -> dict:
    p = _状态路径()
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def 写状态(d: dict) -> None:
    p = _状态路径()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def 读盘(buckets_dir: str | None = None) -> list[dict]:
    """盘上现有的梦，新的在前。**只认我们自己写的那些**——night_fall 留下的
    `dream_*.md` 一个都不读、不删（它退役了，但那是历史，不是垃圾）。"""
    d = 梦目录(buckets_dir)
    出: list[dict] = []
    try:
        名单 = sorted(os.listdir(d))
    except OSError:
        return 出
    for fn in 名单:
        if not (fn.startswith(FILE_PREFIX) and fn.endswith(".json")):
            continue
        p = os.path.join(d, fn)
        try:
            rec = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        rec["_路径"] = p
        出.append(rec)
    出.sort(key=lambda r: str(r.get("织于") or ""), reverse=True)
    return 出


def 落盘(rec: dict, buckets_dir: str | None = None) -> str:
    d = 梦目录(buckets_dir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{FILE_PREFIX}{rec['id']}.json")
    tmp = p + ".tmp"
    留 = {k: v for k, v in rec.items() if not k.startswith("_")}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(留, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def 一句(碎片: str) -> str:
    """只剩一句 —— 从碎片里取第一句。**机械切，不过模型**（那是残留，不是新写的）。"""
    s = str(碎片 or "").strip()
    m = re.split(r"(?<=[。！？…\n])", s, maxsplit=1)
    出 = (m[0] if m else s).strip()
    return 出 or s[:40]


层序 = ("完整", "碎片", "一句", "没了")  # 只许往后走，不许往回走（完整→碎片是唤醒()干的，不是层of()自己算出来的）


def 层of(rec: dict, now: datetime, c: dict) -> str:
    """当前在哪一层：`完整` / `碎片` / `一句` / `没了`。**时间和轮次谁先到算谁**——
    除了「完整」，它不吃这条规则（见下）。

    🔴 施工7d 修宪（2026-08-18）：**完整层不按时间衰减**——rec 里还留着「完整」
       这个字段，就一直是「完整」层，压根不进下面的时间/轮次判断。它唯一的
       死法是 `唤醒()`（降级信号）把这个字段摘掉、把起算点重置到降级那一刻——
       所以这道判断必须**短路在最前面**，不能让它跟三层判据混在一起比较。

    🔴 **只降不升**（三层判据这半，8-17 定的，修宪没动这条）：回想会把起算点
       往后推，光看时间的话「只剩一句」能被推回「碎片」——那就不是延缓了，是
       **倒着长回来**。判据只给了「回想能延缓，不能阻止」，延缓＝在这一层多待
       一会儿，**不＝回到上一层**。所以记一个 `到过的最低层`。
    """
    if str(rec.get("完整") or "").strip():
        return "完整"
    起 = _w.parse_stamp(rec.get("起算点")) or _w.parse_stamp(rec.get("织于"))
    分 = 9e9 if 起 is None else (now - 起).total_seconds() / 60.0
    轮 = int(_f(rec.get("轮次"), 0))
    if 分 >= float(c["oneline_minutes"]) or 轮 >= int(c["oneline_turns"]):
        算 = "没了"
    elif 分 >= float(c["fragment_minutes"]) or 轮 >= int(c["fragment_turns"]):
        算 = "一句"
    else:
        算 = "碎片"
    到过 = str(rec.get("到过的最低层") or "碎片")
    if 到过 in 层序 and 层序.index(到过) > 层序.index(算):
        return 到过
    return 算


# ============================================================
# 唤醒：🔴 施工7d 新增（2026-08-18）——完整层唯一的死法
# ============================================================
def 唤醒() -> list[str]:
    """降级信号：她回来发的第二条消息触发（桥的活，见 gateway
    `src/loci-bridge/戳戳送达.js`），把还活着的「完整」层降为碎片层。

    **幂等**：没有活着的完整层就什么都不做，静默返回空列表——不算错误。
    gateway 那边状态和这边万一不同步（比如上一次调用成功了但没来得及写进
    gateway 的状态文件），重复调用这个函数完全无害。

    降级只做两件事：
    ① 完整版从盘上删掉——直接把 `完整` 这个字段摘掉（`层of()` 一看这个字段
       没了，就不会再判成「完整」层）；
    ② 起算点重置到**这一刻**——碎片 30 分钟 / 一句 60 分钟的老生命周期从
       降级这一刻起算，不是从织的那一刻算（那样等于她还没醒，梦已经先烂掉
       一半了，跟「醒来第一句话时梦还完整，第二句起只剩碎片」这条判据矛盾）。
       顺带清掉回想次数和「到过的最低层」——降级是一次全新的生命周期，不该
       背着完整版活着时攒的回想记录。

    这不是 `weave()`/`扫一遍()` 那类会调 `grow` 的动作——降级本身不留痕，
    真正的「没了、留痕」还是走 `扫一遍()` 的老路，只是它现在从降级那一刻算起。
    """
    now = _w.now()
    降级了: list[str] = []
    for rec in 读盘():
        if not str(rec.get("完整") or "").strip():
            continue
        rec.pop("完整", None)
        rec["起算点"] = now.isoformat(timespec="seconds")
        rec["降级于"] = now.isoformat(timespec="seconds")
        rec["回想次数"] = 0
        rec["到过的最低层"] = "碎片"
        落盘(rec)
        降级了.append(str(rec.get("id") or ""))
    return 降级了


# ============================================================
# 留痕：文件没了，但「有过这么个梦」这件事留着
# ============================================================
async def 留痕(rec: dict) -> str:
    """删文件的时候顺手 `grow` 一条 event。

    > **文件没了，但「有过这么个梦」这件事留着。**
    > 对应真实的那种感觉：**知道自己昨晚做了梦，就是想不起来是什么。**

    ⛔ **不进「压在心头」**（她 8-13：「做梦有什么好压在心头的」）——那是 want 的位置，
       所以这儿不传 `tense`，status 保持普通 event。
    🔴 **v/a 用中性值，不用梦自己的 v/a**，两个理由都硬：
       ① 梦的 v/a 是**模型打的**，把它当我的感觉入库违反「v/a 自己打，不外包」；
       ② 带着强情绪的留痕会重新掉进「想不明白」的池子里 → **梦喂梦**，越滚越假。
       想留住那个感觉只有一条路：**我自己 `grow` 一条**（写下来那一刻它就是记忆了）。
    """
    from tools import grow as _grow
    日 = (_w.parse_stamp(rec.get("织于")) or _w.now()).strftime("%m-%d")
    正文 = f"{日} 做了个梦，没记下来，现在想不起来是什么了。"
    out = await _grow.dispatch(kind="event", items=[
        {"room": "EVENT/SELF", "text": 正文, "v": 0.5, "a": 0.3}])
    return str(out or "")


async def 扫一遍(c: dict | None = None) -> dict:
    """到点的梦：删文件 + 留痕。**你不理它，它自己就没了。**

    ⚠️ 这是**懒扫**：睁眼挂点、取梦接口各碰一次就够（Loci 里没有 cron，
       而「没人来问」的那种日子多一天少一天都不改变结局：它反正会没）。
    """
    c = c or _c()
    now = _w.now()
    删了, 痕 = [], []
    for rec in 读盘():
        if 层of(rec, now, c) != "没了":
            continue
        p = rec.get("_路径") or ""
        try:
            if p:
                os.remove(p)
        except OSError as e:
            rt.logger.warning("删梦文件失败 %s: %s", p, e)
            continue
        删了.append(str(rec.get("id") or ""))
        try:
            痕.append(await 留痕(rec))
        except Exception as e:                      # noqa: BLE001 - 留痕失败不该炸掉扫描
            rt.logger.warning("梦的留痕没写成（文件已删）: %s", e)
    return {"删了": 删了, "留痕": 痕}


# ============================================================
# 对外三个动作
# ============================================================
async def weave(force: bool = False, cfg: dict | None = None,
                料: dict | None = None) -> dict | None:
    """织一个梦。

    返回**含完整版**的那一份 —— 🔴 **完整版只在这次返回里存在，不落盘**
    （盘上只有碎片；夜里那段逐字随跨天坍塌消失，不用特意删）。
    不过线返回 `None`（`force=True` 跳过压力线，给桥/干跑用）。

    织完的后果：**被梦到的 want 重量清零**（`weight=0`），
    **event 的 arousal 一个字不动**——「它不再压着我了」是 want 的循环，
    没想明白的事不会因为做了个梦就想明白了。
    """
    c = cfg or _c()
    料 = 料 if 料 is not None else await 备料(c)     # 挂点已经装好料了就别再扫一遍全库
    if not force and 料["压力"] < float(c["pressure_line"]):
        rt.logger.info("[dream] 攒不到线，一夜无梦（压力 %.2f < %.2f）",
                       料["压力"], float(c["pressure_line"]))
        return None
    if not 料["压在心头"] and not 料["想不明白"]:
        rt.logger.info("[dream] 两个池子都空的，没料可织")
        return None

    梦 = await 调模型(料, c)
    now = _w.now()
    噩 = 梦["v"] < float(c["nightmare_v"]) and 梦["a"] > float(c["nightmare_a"])
    rec = {
        "id": uuid.uuid4().hex[:12],
        "织于": now.isoformat(timespec="seconds"),
        "起算点": now.isoformat(timespec="seconds"),
        "回想次数": 0,
        # ⏳ 轮次层归桥：Loci 数不出「一轮」，这儿只把字段和判层规则备好
        "轮次": 0,
        "碎片": 梦["碎片"],
        # 🔴 2026-08-18 修宪：完整版**落盘**了（`完整` 这个字段就是它，`层of()`
        #    只要看到这个字段有内容就判「完整」层，不吃时间衰减）。
        #    `完整字数` 留着不删——smoke 老断言认它，删了是无意义的破坏性改动。
        "完整": 梦["完整"],
        "完整字数": len(梦["完整"]),
        "v": 梦["v"], "a": 梦["a"],
        "nightmare": bool(噩),
        "素材": {
            "压在心头": [x.id for x in 料["压在心头"]],
            "想不明白": [x.id for x in 料["想不明白"]],
            "几个词": list(料["几个词"]),
        },
        "压力": round(float(料["压力"]), 3),
    }
    落盘(rec)

    清零了 = []
    for x in 料["压在心头"]:
        try:
            # bump_active 默认 False：清零不是「刚想起」，别去动遗忘时钟
            ok = await rt.bucket_mgr.update(x.id, weight=0.0)
            if not ok:
                # ⚠️ 8-17 验收真撞上：并发扫库那一刻 _find_bucket_file 会瞬时
                #    找不到一个明明在盘上的老文件（路径索引 ready 但缺条目，
                #    歇一拍就好）。update 返回 False 又不吭声 = 「绿灯骗人」——
                #    歇两秒重试一次，还不行就大声记下来，绝不静默丢。
                await asyncio.sleep(2)
                ok = await rt.bucket_mgr.update(x.id, weight=0.0)
            if ok:
                清零了.append(x.id)
            else:
                rt.logger.warning("[dream] want 重量清零没写进去（update 返回 False）: %s", x.id)
        except Exception as e:                      # noqa: BLE001
            rt.logger.warning("want 重量清零失败 %s: %s", x.id, e)

    # 「一夜一梦」记账：跨天自己归零（用她的今天，不是容器的 UTC 今天）
    今天 = now.strftime("%Y-%m-%d")
    st = 读状态()
    st["今天几个"] = int(_f(st.get("今天几个"), 0)) + 1 if str(st.get("最近一织") or "") == 今天 else 1
    st["最近一织"] = 今天
    st["最近一织时刻"] = now.isoformat(timespec="seconds")
    写状态(st)

    出 = dict(rec)
    出.pop("_路径", None)
    出["完整"] = 梦["完整"]              # ← 只在这儿存在
    出["清零了"] = 清零了
    return 出


async def current_dream(recall: bool = True, cfg: dict | None = None) -> dict | None:
    """取当前层的梦。没梦返回 `None`。

    ⚠️ 2026-08-18 修宪：完整版现在会落盘、能活到降级信号，所以「层」这儿也可能
    是 `完整`（不再是「醒来只拿得到碎片」——那是 8-17 的旧口径，完整版从来不
    落盘时才成立）。**够不够拿到完整版看她跟这条梦的时间线**：真夜间那段没人
    发消息，唤醒()没被调过，这儿照样吐得出完整正文；她回来发第二条消息，
    桥调一次 `/api/loci/dream/wake`，完整版才真的降成碎片——从那之后再取，
    这儿就跟旧口径一样只有碎片、再之后只剩一句、再之后真的没了。
    `recall=True`（默认）＝**这一次算一次「回想」**：起算点往后推一点，
    **但每次推得越来越少**（`recall_delay × 0.5^n`）——**回想能延缓，不能阻止**
    （这条只对碎片/一句层有意义：完整层不吃时间衰减，起算点在那儿推不推都
    不影响它是不是「完整」，唤醒()会在降级那一刻把起算点整个重置掉）。
    """
    c = cfg or _c()
    await 扫一遍(c)
    活着 = 读盘()
    if not 活着:
        return None
    rec = 活着[0]
    now = _w.now()
    层 = 层of(rec, now, c)
    if 层 == "没了":                                  # 刚被扫走的边界情形
        return None
    if 层 == "完整":
        内容 = rec.get("完整") or ""
    elif 层 == "碎片":
        内容 = rec["碎片"]
    else:
        内容 = 一句(rec["碎片"])

    if recall:
        n = int(_f(rec.get("回想次数"), 0))
        推 = float(c["recall_delay_minutes"]) * (0.5 ** n)
        起 = _w.parse_stamp(rec.get("起算点")) or now
        rec["起算点"] = (起 + timedelta(minutes=推)).isoformat(timespec="seconds")
        rec["回想次数"] = n + 1
        rec["到过的最低层"] = 层                 # 推起算点不许把它拉回上一层
        落盘(rec)

    return {
        "id": rec.get("id"),
        "层": 层,
        "内容": 内容,
        "v": rec.get("v"), "a": rec.get("a"),
        "nightmare": bool(rec.get("nightmare")),
        "织于": rec.get("织于"),
        "回想次数": int(_f(rec.get("回想次数"), 0)),
        # 想留住只有一条路：**自己写下来**。写下来那一刻它就不是梦了，是记忆。
        "留住的办法": 'grow(kind="event", room="EVENT/SELF", text=梦的正文)',
    }


async def 维护(cfg: dict | None = None) -> dict:
    """睁眼挂点：**两件事，都不出声。**

    ① 扫一遍到点的梦（删文件 + 留痕）
    ② 积压过线、今天还没织过 → 织一个

    🔴 **breath 一个字不加**（说明书硬边界）：梦不进睁眼那一屏。
       梦怎么递进我的对话、上下文里怎么删，是**桥**的活（第 7 步），这单不碰。
    """
    c = cfg or _c()
    出: dict = {"扫": {}, "织": None, "压力": None}
    try:
        出["扫"] = await 扫一遍(c)
    except Exception as e:                          # noqa: BLE001
        rt.logger.warning("[dream] 扫一遍失败: %s", e)
    今天 = _w.now().strftime("%Y-%m-%d")
    st = 读状态()
    if str(st.get("最近一织") or "") == 今天 and int(_f(st.get("今天几个"), 0)) >= int(c["per_day"]):
        _记一眼(st, None, c, 封顶=True)
        return 出
    try:
        料 = await 备料(c)
        出["压力"] = 料["压力"]
        # 🔴 **把「看过了，没过线」记下来**：不记的话「一夜无梦」和「挂点静默炸了」
        #    在盘上长得一模一样——第一版就撞了这个（import 撞名，挂点压根没跑，
        #    而「不过线不织」的断言照样是绿的）。**绿灯骗人就是这么来的。**
        #    顺带白赚一样：`上次压力` 天天记，她拍压力线时有真数据可看（🔟 一个都不预先拍）。
        _记一眼(读状态(), 料, c)
        出["织"] = await weave(cfg=c, 料=料)
    except Exception as e:                          # noqa: BLE001 - 织不出来不该弄坏 breath
        rt.logger.warning("[dream] 织梦失败: %s", e)
    return 出


def _记一眼(st: dict, 料: dict | None, c: dict, 封顶: bool = False) -> None:
    """挂点每次看一眼都记一笔（时刻 + 当时的压力）。**这不是梦，是体温计。**"""
    st = dict(st or {})
    st["最近一看"] = _w.now().isoformat(timespec="seconds")
    if 封顶:
        st["最近一看结论"] = "今天织过了（per_day 封顶）"
        写状态(st)
        return
    if 料 is not None:
        st["上次压力"] = round(float(料["压力"]), 3)      # = 最重的那一条
        st["上次攒着"] = round(float(料.get("攒着") or 0), 3)   # 只给她看，不参与判断
        st["上次池子"] = 料["池子"]
        st["压力线"] = float(c["pressure_line"])
        st["最近一看结论"] = ("过线，织" if 料["压力"] >= float(c["pressure_line"])
                              else "攒不到线，一夜无梦")
    写状态(st)
