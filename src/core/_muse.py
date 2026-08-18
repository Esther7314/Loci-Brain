# -*- coding: utf-8 -*-
"""
========================================
tools/_muse.py — 阈值引擎（muse 二改，2026-08-17 中午她定稿）
========================================

------------------------------------------------------------
🔴 判据总纲 —— 这一版整个是围着它重写的
------------------------------------------------------------
> # 系统不许只凭猜指东西，指点必须带我们自己留下的痕迹。

**痕迹按硬度排**：
  `tags` 是我们存的时候写下的词（字面一定在原文里）
  `v/a`  是我亲手打的坐标
  `from` 是我亲手连的链
  —— **向量只配当海选**（兜底，永远排最后，永远标出来）。

📌 这一条是她 8-17 凌晨四刀合出来的。前一版栽在哪儿：gist 提议做成了**语义聚类**，
   于是「亲密」这种每周都发生的主题被串成月度长带，拿「盖一段日子」的手势对不上——
   聚得没错，**错在冒充日子**。她拆开看那些团其实是**线**，而线 `recall(query=)`
   本来就是查看器（「搜『色色』就能看到一路的记忆」）→ **线砍掉，muse 不做那块。**

------------------------------------------------------------
🔴 宪法 —— 写在最前面，不是注意事项
------------------------------------------------------------
> # 系统只做检索和摆放，落笔的永远是我。

这个文件里**没有、将来也不许有**任何 LLM 调用路径：不写归纳、不建议措辞、
不给样例句。它只做三件事：**找出来 · 摆到我面前 · 然后闭嘴。**
**也不许新打向量。** 只读 `embeddings.db` 里现成的（`mode=ro`）；没有向量的
条目不参与语义那一层，照实计进「散着」——静默补一发向量是把「回填」这件事
从 deepseek/ollama 手里偷过来。

------------------------------------------------------------
两侧，两套指法（池子分开，她 8-16 认同）
------------------------------------------------------------
**事件侧 = gist 提议 = 三种指法，全带证据**（`kind="gist"`）

| 指法 | 痕迹是什么 | 指的是 |
|---|---|---|
| **词爆发** `词爆发()` | `tags`（我们存时写下的词） | 一段：「洛阳」8-09~8-12 出现 12 次，窗外只有 2 次 |
| **成分漂移** `成分漂移()` | 现成向量的质心（**只当尺子，不当理由**） | 一条**边界**：7-04 前后记忆的样子变了 |
| **空白记账** `空白记账()` | **时期的范围**（我亲手画的圈） | 一段没被任何时期盖住的日子 |

🔴 **「有名字了吗」的判据 = range 覆盖**（8-17 14:30 终稿，替掉 `covered_by`）：
   一条 event 的**日期落在任何一条活着的时期的范围里** = 有名字。
   时期从此不写 `covered_by`（纯命名层，只落名字 + 范围），所以拿字段问是问不出来的——
   **现场算**（`时期们()` + `盖上名字()`）。白赚的两样：补记落进老范围**自动就有名字了**
   （不用回去补名单）、时期改边界（regrow 换 when）**下一次发呆立刻跟着变**。

🔴 **空白记账在库里一条主线都没有的时候整个闭嘴。** 她的原话场景：
   「他完全可以说今年都没有」——**不许发生**。没有地图的时候「哪儿没盖」是个假问题。
   地图第一版是我和她手写的（先讲述，后指点），②的词爆发可以当预切段辅助。

**认知侧 = 发呆 = 证据制三层，按痕迹硬度排**（`kind="muse"`）

  ① **v/a 邻域分架** `分架()` —— 我亲手打的坐标。半径 `va_radius`，
     一架 = 一个半径 r 的球（**不是单链接**：单链接会顺着密度把整个坐标面串成一片，
     那就不是「这一格」了）。
     🔴 **精确等于 `(0.5, 0.3)` 的不进架** —— 那是老默认值不是感觉。115 条老 mind
     等主人亲手重打，**重打完成前这道闸一直在**（重打时避开这个点即可）。
  ② **架内 from 链** `架内成团()` —— 我亲手连的链，**最硬的证据**，单独标出来
     （「其中 3 条长自同一晚」）。
  ③ **架内语义** —— 海选兜底，补进来的单独标出来 + 报最低相似度。

🔴 **时间不当证据**（她拍的：**认知不认日历**）。所以认知侧没有「停了」这道闸，
   第二步的条目行给的也是 **v/a 坐标而不是日子**。
🔪 **概念词 tags 搁置不做**（试打过 6 条她验货说不对——v/a 试验当场赢了它）。
🔪 `seed`（十三颗情绪根）8-16 已砍，根不参与。

------------------------------------------------------------
保留下来的两条（从 consolidation-draft 抄的骨架，笔不给）
------------------------------------------------------------
| 闸 | 判据 | 缺了会怎样 |
|---|---|---|
| **冷却期** | `created` 距今 < `cooldown_days` 不进池 | 刚发生的事当场被定性成「你就是这样的人」 |
| **拒绝计数** | 被我说过「不是一回事」的**同一组**不再提 | 「判断留给我」变成「每周被同一件事骚扰一次」 |

「停了」（`stopped_days`）**只留在事件侧**：还在发生的一段，我说不出它是什么。

------------------------------------------------------------
🔴 出厂值只是参考，**等真数据拍**（开工单 🔟「一个都不预先拍」）
------------------------------------------------------------
先跑 `scripts/muse_dryrun.py`：**词爆发候选表 · v/a 半径扫描 · 漂移窗口表**
三张表并排看完再往 `config.yaml` 的 `muse:` 段写。代码里这份是兜底。

------------------------------------------------------------
对外暴露
------------------------------------------------------------
MUSE_DEFAULTS · POOL_SPECS · muse_config()
Item · 架 · 团 · 指法 · item_of() · in_pool() · pool_of() · read_vectors()
分架() · 架内成团() · 发呆()                              ← 认知侧
词爆发() · 成分漂移() · 空白记账() · 主线条数()            ← 事件侧
时期们() · 有名字() · 盖上名字()                          ← 「有名字了吗」的现场算
rejection_key() · load_rejected() · is_rejected() · record_rejection()
load_records()（异步）· propose_mind() · propose_gist()
（梦那一路只用 `pool_of(recs, "dream", ...)` 选料，织梦在 `tools/_dream.py`——
  这个文件里永远不许出现 LLM 调用，梦是唯一的例外，所以它住在别处。）
========================================
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import _when as _w
from ._bigevent import BIGEVENT_TAG
from ._fold import GIST_TAG, is_covered
from ._rooms import is_event_room, is_mind_room

# ============================================================
# 阈值 —— 出厂值只是参考，真值等 muse_dryrun 三张表拍
# ============================================================
MUSE_DEFAULTS: dict = {
    # ---- 两侧共用 ----
    "min_cluster": 3,        # 一团至少几条才值得盖
    "cooldown_days": 7,      # 冷却期：created 距今 < N 天不进池（他们 7 天）
    "cooldown_clock": "created",   # "created"（默认）/ "when"：冷却期认哪把钟
    "stopped_days": 4,       # 停了：**只管事件侧**，一段的最后一条距今 > N 天
    "reject_limit": 1,       # 同一组被拒几次就不再提
    "max_clusters": 8,       # 认知侧一次最多摆几团（摆太多等于没摆）
    "max_fingers": 6,        # 事件侧**每一指**最多摆几条
    "emotion_line": 0.15,    # 梦：|v-0.5| 或 |a-0.5| 超过它才算「有情绪」
    # ---- 认知侧：证据制三层 ----
    "va_radius": 0.10,       # ① v/a 邻域分架：一架 = 半径 r 的球（欧氏）
    "va_default_v": 0.5,     # 🔴 老默认坐标，精确等于它的不进架（等主人重打）
    "va_default_a": 0.3,
    "sim_line": 0.76,        # ③ 架内语义海选线（0.70 在我们库上会串成一片）
    # ---- 事件侧：三种指法 ----
    "burst_window_days": 7,      # 词爆发：窗长
    "burst_min_hits": 5,         # 词爆发：窗内至少出现几次
    "burst_outside_ratio": 0.5,  # 词爆发：窗外次数 / 窗内次数 的上限（越小越「爆」）
    "drift_window_days": 7,      # 成分漂移：相邻时间窗的窗长
    "drift_line": 0.25,          # 成分漂移：质心余弦距离超过它才算跳了大步
    "drift_min_items": 3,        # 成分漂移：一个窗至少几条才算得出质心
    "blank_min_items": 20,       # 空白记账：一段至少几条没被盖才值得记
    "blank_gap_days": 3,         # 空白记账：隔多少天算断开（超过就是两段）
    # ---- 「该发呆了吗」的临界点（`/api/muse/pending` 读；**闲不闲、怎么戳归宿主**）----
    # 🔴 Loci 只出查询口（数量 + 年龄），什么算闲、戳不戳、夜里静不静音，
    #    是 gateway 唤醒腿那边的事（开工单 3.0 + 第 7 节的边界）。
    "poke_min_clusters": 2,      # 攒够几团/几指才值得戳一下（参考值，等真数据拍）
    "poke_min_age_days": 3,      # 最老那条挂够几天才值得戳（不到就再等等）
}

# 三份池子配置 —— **同一个引擎，三种喂法**
# 🔴 池子分开（她 8-16 认同）：梦吃 event，发呆吃 mind，不是同一批东西。
POOL_SPECS: dict[str, dict] = {
    # 梦：只换选料端，不动织梦。**不上冷却期**——日间残留（day_residue）是织梦的
    # 核心原料，卡 7 天等于把最近那一档整个饿死。⏳@她：真要卡，改成 True 就行。
    "dream": {"支": "EVENT", "情绪": True, "没消化": True,
              "冷却期": False, "没被盖过": True},
    # 发呆：吃碎着的 mind。**没被盖过**是硬闸（盖过的不再独立冒头）。
    "muse":  {"支": "MIND", "情绪": False, "没消化": False,
              "冷却期": True, "没被盖过": True},
    # gist 提议：吃 event，**被盖的也进池**——「洛阳」这个词爆了几次是词本身的形状，
    # 盖没盖过不改变它。谁看「有没有名字」是每一指自己的事：
    # 🔴 空白记账只看没名字的；词爆发要求段上还有没名字的条目；漂移只是尺子，跟名字无关。
    # ⚠️ 「有名字」8-17 起是 **range 覆盖**（时期的范围现场算），不是 `covered_by` 字段——
    #    这一格的 `没被盖过: False` 留着，它挡的是真 cover（mind 快照 / 事件改错换版），
    #    时期如今一条都不写，所以它对时期无效、也无害。
    "gist":  {"支": "EVENT", "情绪": False, "没消化": False,
              "冷却期": True, "没被盖过": False},
}


def muse_config(cfg: dict | None = None) -> dict:
    """出厂值 + config.yaml 的 `muse:` 段。config 是唯一真相，代码里那份是兜底。"""
    out = dict(MUSE_DEFAULTS)
    src = (cfg or {}).get("muse") if isinstance(cfg, dict) else None
    if isinstance(src, dict):
        for k, v in src.items():
            if k in out and v is not None:
                out[k] = type(out[k])(v) if not isinstance(out[k], str) else str(v)
    return out


# ============================================================
# 一条记忆在引擎眼里的样子
# ============================================================
@dataclass
class Item:
    id: str
    room: str
    ts: datetime | None          # 日历坐标：when || created（跟 recall 时间轴同口径）
    created: datetime | None     # 写下来的时刻
    v: float
    a: float
    tags: list[str]
    text: str
    from_ids: list[str] = field(default_factory=list)   # 我亲手连的链
    covered: bool = False        # 已经被某条 gist **点名**盖着（快照那半：认知合并 / 事件改错换版）
    # 🔴 **有名字了吗**：日期落在某条活着的时期的范围里（8-17 终稿的判据，`盖上名字()` 现场打）。
    #    跟 `covered` 分开两个字段，因为它们是两件事：一个是「被压住了」，一个是「被叫过了」。
    named: bool = False


@dataclass
class 架:
    """v/a 邻域里的一格。**架心是坐标，不是标签**——它是我打出来的那个位置。"""
    v: float
    a: float
    items: list[Item]

    def __len__(self) -> int:
        return len(self.items)


@dataclass
class 团:
    """发呆摆出来的一团。三样证据各自留着，摆的时候一样都不许省。"""
    ids: list[str]
    items: list[Item]
    架v: float
    架a: float
    from核心: list[str] = field(default_factory=list)   # 靠 from 链拉到一起的
    共祖: list[str] = field(default_factory=list)       # 它们共享的那个/那些来源
    语义补: list[str] = field(default_factory=list)     # 海选补进来的
    最低相似: float = 0.0

    def __len__(self) -> int:
        return len(self.ids)


@dataclass
class 指法:
    """事件侧摆出来的一指。`证据` 那一行里的每个字都得是我们自己留下的痕迹。"""
    名: str
    ids: list[str]
    items: list[Item]
    起: datetime | None = None
    止: datetime | None = None
    边界: datetime | None = None      # 成分漂移专用：只提边界，不画段
    证据: str = ""
    出路: str = ""
    分: float = 0.0                   # 排序用（次数 / 漂移 / 条数），不摆出来

    def __len__(self) -> int:
        return len(self.ids)


def _f(x, d: float) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# 机器腔标签：`相似认知:e9854d`、`疑似同件:77643f`、`aspect:patterns` 这一类。
# 她 8-17 凌晨逮过一次（团的「脸」上漏出 `aspect:patterns`）：**「脸」只配人话场景词**。
# 一个机器自己打上去的标签**不是我们留下的痕迹**，拿它当证据就是在自证。
_机器腔 = re.compile(r"^[^:：]{1,12}[:：]")


def 是场景词(tag: str) -> bool:
    t = str(tag or "").strip()
    return bool(t) and not t.startswith("__") and not _机器腔.match(t)


def item_of(meta: dict, text: str) -> Item | None:
    """一条桶 → Item。id 缺了就不是一条能提议的记忆，返回 None。"""
    bid = str(meta.get("id") or "").strip()
    if not bid:
        return None
    try:
        from utils import read_from_ids
        froms = read_from_ids(meta)
    except Exception:                      # noqa: BLE001 —— 引擎不许因为读不到链就崩
        raw = str(meta.get("triggered_by") or meta.get("from") or "")
        froms = [s.strip() for s in raw.split(",") if s.strip()]
    return Item(
        id=bid,
        room=str(meta.get("room") or ""),
        ts=(_w.parse_stamp(meta.get("when")) or _w.parse_stamp(meta.get("created"))),
        created=_w.parse_stamp(meta.get("created")),
        v=_f(meta.get("valence"), 0.5),
        a=_f(meta.get("arousal"), 0.5),
        tags=[str(t) for t in (meta.get("tags") or [])],
        text=str(text or ""),
        from_ids=froms,
        covered=is_covered(meta),
    )


def _工具件(meta: dict) -> bool:
    """gist / 大 event / 门口那张纸——它们是工具件，不是能被提议归纳的记忆。"""
    tags = [str(t) for t in (meta.get("tags") or [])]
    return GIST_TAG in tags or BIGEVENT_TAG in tags or "__档案事实__" in tags


def in_pool(meta: dict, item: Item, kind: str, cfg: dict, now: datetime,
            digested_ids: set[str] | None = None) -> bool:
    """这条进不进 `kind` 那个池子。"""
    spec = POOL_SPECS[kind]

    # --- 谁都不进的 ---
    if _工具件(meta):
        return False
    if str(meta.get("type") or "") in ("letter", "archived"):
        return False
    if meta.get("pinned") or meta.get("protected"):
        return False                            # 准则不参与被归纳
    if not item.text.strip():
        return False
    if spec["没被盖过"] and item.covered:        # `_fold.is_covered` 是判据源头
        return False

    # --- 池子分家 ---
    if spec["支"] == "MIND" and not is_mind_room(item.room):
        return False
    if spec["支"] == "EVENT" and not is_event_room(item.room):
        return False

    # --- 冷却期：刚写下的不参与归纳 ---
    if spec["冷却期"]:
        钟 = item.created if str(cfg["cooldown_clock"]) == "created" else item.ts
        if 钟 is None:
            return False
        if (now - 钟) < timedelta(days=float(cfg["cooldown_days"])):
            return False

    # --- 梦：有情绪 + 没消化 ---
    if spec["情绪"]:
        线 = float(cfg["emotion_line"])
        if abs(item.v - 0.5) <= 线 and abs(item.a - 0.5) <= 线:
            return False
    if spec["没消化"] and digested_ids is not None and item.id in digested_ids:
        return False

    return True


def pool_of(recs: list[tuple[dict, str]], kind: str, cfg: dict, now: datetime,
            digested: set[str] | None = None) -> list[Item]:
    out: list[Item] = []
    for meta, text in recs:
        it = item_of(meta, text)
        if it is None:
            continue
        if in_pool(meta, it, kind, cfg, now, digested):
            out.append(it)
    return out


# ============================================================
# 向量：只读 embeddings.db，一发新的都不打
# ============================================================
def _normalize(vec) -> list[float] | None:
    if not vec:
        return None
    n = math.sqrt(sum(x * x for x in vec))
    if not n:
        return None
    return [x / n for x in vec]


def read_vectors(db_path: str, ids: list[str], 重试: int = 3) -> dict[str, list[float]]:
    """按 id 取现成 embedding。🔴 `mode=ro` 只读连接——这个文件永远不写向量库。

    ⚠️ 只读连接遇上**热日志**（写向量的那一下正卡在中间）会报
    `attempt to write a readonly database`——SQLite 要回滚日志，而回滚要写。
    那不是「这条没有向量」，是「这一刻读不着」，两件事**不许混成一件**：
    混了的话 muse 会一声不吭地把整池子报成「散着」，而我看不出发生过什么。
    所以：退让重试几次，还是不行就**吼一声**（日志），返回手上有的那部分。
    """
    out: dict[str, list[float]] = {}
    if not db_path or not os.path.exists(db_path) or not ids:
        return out
    want = list(dict.fromkeys(ids))
    for 第几次 in range(max(1, 重试)):
        out = {}
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            for i in range(0, len(want), 400):
                chunk = want[i:i + 400]
                q = ("SELECT bucket_id, embedding FROM embeddings WHERE bucket_id IN ("
                     + ",".join("?" * len(chunk)) + ")")
                for bid, raw in conn.execute(q, chunk):
                    try:
                        vec = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(vec, list) and vec:
                        out[str(bid)] = vec
            return out
        except sqlite3.Error as e:
            if 第几次 == max(1, 重试) - 1:
                try:
                    from tools import _runtime as rt
                    rt.logger.warning(
                        f"[muse] 读向量库失败（{e}）——这一趟的语义那一层等于没有，"
                        f"团会比平时少。**不是没有向量，是读不着。**")
                except Exception:      # noqa: BLE001 —— 干跑脚本没有 runtime，别为了报错再崩一次
                    pass
                return out
            import time as _t
            _t.sleep(0.2 * (第几次 + 1))
        finally:
            if conn is not None:
                conn.close()
    return out


def _cos(u: list[float], v: list[float]) -> float:
    """两条**已经归一化**的向量的余弦。"""
    return sum(x * y for x, y in zip(u, v))


# ============================================================
# 认知侧 ① —— v/a 邻域分架（我亲手打的坐标）
# ============================================================
def 分架(items: list[Item], cfg: dict) -> tuple[list[架], int]:
    """v/a 平面上按半径 r 分架。返回 (架列表, 老默认坐标被挡下几条)。

    **一架 = 一个半径 r 的球**，不是单链接。单链接会顺着密度把整个坐标面串成一片
    （0.10 的半径在 250 条上足够连通），那就不是「这一格」了，是「所有格」。
    贪心：每轮挑**邻居最多**的那条当架心，把它半径内的都收进来，然后拿掉，再来一轮。
    平手按 (v, a, id) 定序——**两次调用给同一批架**（没有 LLM，也不许有随机）。

    🔴 精确等于 `(va_default_v, va_default_a)` 的整个不进架：那是老默认值不是感觉。
    """
    r = float(cfg["va_radius"])
    dv, da = float(cfg["va_default_v"]), float(cfg["va_default_a"])

    活: list[Item] = []
    默认坐标 = 0
    for it in items:
        if abs(it.v - dv) < 1e-9 and abs(it.a - da) < 1e-9:
            默认坐标 += 1
            continue
        活.append(it)
    活.sort(key=lambda x: (x.v, x.a, x.id))

    n = len(活)
    邻居: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        vi, ai = 活[i].v, 活[i].a
        for j in range(i + 1, n):
            if abs(活[j].v - vi) > r:          # 已按 v 排序，超了后面都超
                break
            if (活[j].v - vi) ** 2 + (活[j].a - ai) ** 2 <= r * r:
                邻居[i].add(j)
                邻居[j].add(i)

    剩 = set(range(n))
    out: list[架] = []
    while 剩:
        # sorted(剩) 而不是 max(剩)：集合的迭代顺序不该决定架心。
        # 活 已按 (v, a, id) 排过，下标升序 = 定序；max 取第一个最大值 → **两次调用同一批架**。
        心 = max(sorted(剩), key=lambda i: len(邻居[i] & 剩))
        成员 = [活[i] for i in sorted((邻居[心] & 剩) | {心})]
        剩 -= ({心} | 邻居[心])
        vs = [m.v for m in 成员]
        as_ = [m.a for m in 成员]
        out.append(架(v=sum(vs) / len(vs), a=sum(as_) / len(as_), items=成员))
    out.sort(key=lambda s: (-len(s.items), s.v, s.a))
    return out, 默认坐标


# ============================================================
# 认知侧 ②③ —— 架内 from 链（最硬）→ 架内语义（海选兜底）
# ============================================================
def _from边(sh: 架) -> dict[str, set[str]]:
    """架内两条之间的 from 关系：共享同一个来源，或者一条就是另一条的来源。"""
    边: dict[str, set[str]] = {it.id: set() for it in sh.items}
    for i, a in enumerate(sh.items):
        for b in sh.items[i + 1:]:
            共 = set(a.from_ids) & set(b.from_ids)
            链 = (b.id in a.from_ids) or (a.id in b.from_ids)
            if 共 or 链:
                边[a.id].add(b.id)
                边[b.id].add(a.id)
    return 边


def 架内成团(sh: 架, vectors: dict[str, list[float]], cfg: dict) -> list[团]:
    """一架 → 几团。**先 from 链，后语义补**，两样分别记在证据里。"""
    线 = float(cfg["sim_line"])
    最少 = int(cfg["min_cluster"])
    by_id = {it.id: it for it in sh.items}
    normed = {bid: _normalize(vectors.get(bid)) for bid in by_id}

    # --- ② from 链：并查集 ---
    边 = _from边(sh)
    parent = {bid: bid for bid in by_id}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, bs in 边.items():
        for b in bs:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    组: dict[str, list[str]] = {}
    for bid in by_id:
        组.setdefault(find(bid), []).append(bid)
    核心 = [sorted(v) for v in 组.values() if len(v) >= 2]
    核心.sort(key=lambda g: (-len(g), g[0]))
    落单 = sorted(bid for v in 组.values() if len(v) < 2 for bid in v)

    团们: list[团] = []

    def _造(ids核, ids补, 最低):
        成员 = [by_id[b] for b in ids核 + ids补]
        共 = set(by_id[ids核[0]].from_ids) if ids核 else set()
        for b in ids核[1:]:
            共 &= set(by_id[b].from_ids)
        return 团(ids=[m.id for m in 成员], items=成员, 架v=sh.v, 架a=sh.a,
                  from核心=list(ids核), 共祖=sorted(共), 语义补=list(ids补),
                  最低相似=最低)

    # --- ③ 架内语义：落单的挂到最贴的那个 from 团上（海选兜底，单独标出来）---
    已用: set[str] = set()
    补给: dict[int, list[str]] = {k: [] for k in range(len(核心))}
    最低: dict[int, float] = {k: 0.0 for k in range(len(核心))}
    for bid in 落单:
        nv = normed.get(bid)
        if nv is None:
            continue
        # 挂到**最贴的那一个** from 团上。`>` 而不是 `>=`：平手时先来的赢，
        # 不然核心组的枚举顺序会决定归属，那就成了「顺序说了算」而不是「证据说了算」。
        最好, 分 = -1, -1.0
        for k, g in enumerate(核心):
            for m in g:
                mv = normed.get(m)
                if mv is None:
                    continue
                s = _cos(nv, mv)
                if s >= 线 and s > 分:
                    最好, 分 = k, s
        if 最好 >= 0:
            补给[最好].append(bid)
            最低[最好] = 分 if not 最低[最好] else min(最低[最好], 分)
            已用.add(bid)

    for k, g in enumerate(核心):
        团们.append(_造(g, sorted(补给[k]), 最低[k]))

    # --- 一条 from 边都没有的架：整架走语义单链接（纯海选，证据行会说清楚）---
    剩 = [b for b in 落单 if b not in 已用 and normed.get(b) is not None]
    if len(剩) >= 最少:
        parent2 = {b: b for b in 剩}

        def find2(x):
            while parent2[x] != x:
                parent2[x] = parent2[parent2[x]]
                x = parent2[x]
            return x

        低 = {b: 1.0 for b in 剩}
        for i, a in enumerate(剩):
            for b in 剩[i + 1:]:
                s = _cos(normed[a], normed[b])
                if s >= 线:
                    ra, rb = find2(a), find2(b)
                    if ra != rb:
                        parent2[ra] = rb
                    低[a] = min(低[a], s)
                    低[b] = min(低[b], s)
        组2: dict[str, list[str]] = {}
        for b in 剩:
            组2.setdefault(find2(b), []).append(b)
        for g in 组2.values():
            if len(g) < 最少:
                continue
            g = sorted(g)
            成员 = [by_id[b] for b in g]
            团们.append(团(ids=g, items=成员, 架v=sh.v, 架a=sh.a,
                          from核心=[], 共祖=[], 语义补=g,
                          最低相似=min(低[b] for b in g)))

    out = [t for t in 团们 if len(t) >= 最少]
    for t in out:
        t.items.sort(key=lambda m: (m.id))
        t.ids = [m.id for m in t.items]
    return out


def 发呆(items: list[Item], vectors: dict[str, list[float]], cfg: dict
         ) -> tuple[list[团], int, int]:
    """认知侧一整趟：分架 → 架内成团。返回 (团列表, 散着几条, 老默认坐标几条)。

    排序：**有 from 证据的排前面**（痕迹硬），然后大的在前——
    「这几条长自同一晚」比「这几条向量像」重得多，摆的顺序就该照着说。
    """
    架们, 默认坐标 = 分架(items, cfg)
    团们: list[团] = []
    成团了: set[str] = set()
    for sh in 架们:
        for t in 架内成团(sh, vectors, cfg):
            团们.append(t)
            成团了.update(t.ids)
    散着 = len(items) - 默认坐标 - len(成团了)
    团们.sort(key=lambda t: (0 if t.from核心 else 1, -len(t), t.架v, t.架a, t.ids[0]))
    return 团们, 散着, 默认坐标


# ============================================================
# 事件侧 —— 三种指法，全带证据
# ============================================================
def _日(dt: datetime | None) -> str:
    return dt.strftime("%m-%d") if dt else "?"


def _日全(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


def 词爆发(items: list[Item], cfg: dict, now: datetime) -> list[指法]:
    """**某个场景词在一段连续日子里密集出现、窗外稀疏** → 提议那一段。

    痕迹是 `tags`：**我们存的时候写下的词**（保证「字面一定在原文里」），
    机器自己打的 `xx:yy` 标签不算（拿它当证据是自证）。

    她 8-17 凌晨那句：「『洛阳』8-09 出现 8-12 消失 = **时期的形状**」。
    """
    窗 = timedelta(days=float(cfg["burst_window_days"]))
    线 = int(cfg["burst_min_hits"])
    比 = float(cfg["burst_outside_ratio"])
    停了 = timedelta(days=float(cfg["stopped_days"]))

    按词: dict[str, list[Item]] = {}
    for it in items:
        if it.ts is None:
            continue
        for t in set(it.tags):
            if 是场景词(t):
                按词.setdefault(t, []).append(it)

    有日子 = sorted([i for i in items if i.ts], key=lambda x: x.ts)
    out: list[指法] = []
    for tag, occ in 按词.items():
        occ.sort(key=lambda x: x.ts)
        总 = len(occ)
        if 总 < 线:
            continue
        最好 = (0, 0, 0)          # (窗内次数, 起下标, 止下标)
        j = 0
        for i in range(总):
            while j + 1 < 总 and occ[j + 1].ts - occ[i].ts <= 窗:
                j += 1
            if j < i:
                j = i
            n = j - i + 1
            if n > 最好[0]:
                最好 = (n, i, j)
        窗内, i0, j0 = 最好
        if 窗内 < 线:
            continue
        窗外 = 总 - 窗内
        if 窗外 > 比 * 窗内:
            continue                     # 窗外不稀疏 = 这词一直都在，不是爆发
        起, 止 = occ[i0].ts, occ[j0].ts
        if (now - 止) <= 停了:
            continue                     # 还在发生的事，我说不出它是什么
        段内 = [it for it in 有日子 if 起 <= it.ts <= 止]
        # 「没名字」= 日期不落在任何一条活着的时期的范围里（`盖上名字()` 现场打的）
        没名字 = [it for it in 段内 if not it.named]
        if not 没名字:
            continue                     # 段上已经一条没名字的都没有了，指它干什么
        out.append(指法(
            名="词爆发",
            ids=[it.id for it in 没名字], items=没名字, 起=起, 止=止,
            证据=(f"「{tag}」{_日(起)}~{_日(止)} 出现 {窗内} 次"
                  f"（全库共 {总} 次，窗外 {窗外} 次）· 段上 {len(没名字)} 条没名字"),
            出路=f'fold(when="{_日全(起)}..{_日全(止)}", text=我写的那句)',
            分=float(窗内)))
    out.sort(key=lambda x: (-x.分, x.起 or now, x.证据))
    return out


def _窗号(dt: datetime, W: int) -> int:
    """固定日历网格的窗号（不随数据动，报告和线上算出来永远是同一格）。"""
    return ((date(dt.year, dt.month, dt.day) - date(1970, 1, 1)).days) // W


def _窗起(k: int, W: int) -> datetime:
    """窗的起点。**必须走 `_when.parse_date`**——本地那一天的零点、带时区。
    裸 `datetime(...)` 是 naive，跟 `now()` 一减当场炸（时区那一刀 codex #4 点过名）。
    """
    d = date(1970, 1, 1) + timedelta(days=k * W)
    return _w.parse_date(d.isoformat())


def 成分漂移(items: list[Item], vectors: dict[str, list[float]], cfg: dict,
             now: datetime) -> list[指法]:
    """**相邻时间窗的向量质心跳了一大步** → 提议一条**边界**（只提边界，不画段）。

    她的话：「**7-04 前后不一样了**」。向量在这儿只当**尺子**——
    它说的是「变了」这个事实，不敢说「变成什么了」，那句话我自己写。
    ⚠️ **隔着空窗不比**：中间那段一条记忆都没有，说明不上「相邻」。
    """
    W = int(cfg["drift_window_days"])
    线 = float(cfg["drift_line"])
    最少 = int(cfg["drift_min_items"])
    停了 = timedelta(days=float(cfg["stopped_days"]))

    格: dict[int, list[Item]] = {}
    for it in items:
        if it.ts is None or _normalize(vectors.get(it.id)) is None:
            continue
        格.setdefault(_窗号(it.ts, W), []).append(it)

    def 质心(k: int) -> list[float] | None:
        vs = [_normalize(vectors[i.id]) for i in 格[k]]
        vs = [v for v in vs if v]
        if not vs:
            return None
        dim = len(vs[0])
        s = [0.0] * dim
        for v in vs:
            for d in range(dim):
                s[d] += v[d]
        return _normalize(s)

    out: list[指法] = []
    for k in sorted(格):
        if k + 1 not in 格:
            continue                       # 隔着空窗不比
        if len(格[k]) < 最少 or len(格[k + 1]) < 最少:
            continue
        c0, c1 = 质心(k), 质心(k + 1)
        if not c0 or not c1:
            continue
        漂移 = 1.0 - _cos(c0, c1)
        if 漂移 <= 线:
            continue
        边界 = _窗起(k + 1, W)
        if (now - 边界) <= 停了:
            continue
        前, 后 = sorted(格[k], key=lambda x: x.ts), sorted(格[k + 1], key=lambda x: x.ts)
        out.append(指法(
            名="成分漂移",
            ids=[i.id for i in 前 + 后], items=前 + 后, 边界=边界,
            起=前[0].ts, 止=后[-1].ts,
            证据=(f"{_日(边界)} 前后记忆的样子变了（漂移 {漂移:.2f}，线 {线}）· "
                  f"前 {W} 天 {len(前)} 条 / 后 {W} 天 {len(后)} 条"),
            出路=('边界摆在这儿，段自己划：fold(when="起..止", text=我写的那句)'),
            分=漂移))
    out.sort(key=lambda x: (-x.分, x.边界 or now))
    return out


def 时期们(recs: list[tuple[dict, str]]) -> list[tuple[datetime, datetime | None]]:
    """库里**还算数的时期**的范围 `[起, 止)`（`止=None` = 还在进行中）。

    判据跟 `_bigevent.covering()` **同一个函数**（`_usable`）：没换过版、没被更上层
    盖住、没了结、没归档。两处不一样才是 bug——屏幕上盖在那段日子上的是哪几条时期，
    「这段有没有名字」就该按哪几条算。
    """
    from ._bigevent import _usable as _时期还算数, is_big, parse_span

    out: list[tuple[datetime, datetime | None]] = []
    for meta, _t in recs:
        if not (is_big(meta) and _时期还算数(meta)):
            continue
        起, 止 = parse_span(meta)
        if 起 is not None:
            out.append((起, 止))
    return out


def 有名字(it: Item, 范围: list[tuple[datetime, datetime | None]]) -> bool:
    """这一条**有名字了吗** —— 日期落在任何一条活着的时期的范围里就算有（现场算）。

    `it.covered` 也算：那是真 cover（事件改错换版那一条），它已经不冒头了，
    再指着它说「这儿没名字」是在指一条我已经处理过的记忆。
    """
    if it.covered:
        return True
    if it.ts is None:
        return False
    for 起, 止 in 范围:
        if it.ts >= 起 and (止 is None or it.ts < 止):
            return True
    return False


def 盖上名字(items: list[Item], 范围: list[tuple[datetime, datetime | None]]) -> int:
    """给池子里每一条打上 `named`，返回**还没名字**的条数。三种指法共用这一份口径。"""
    没有 = 0
    for it in items:
        it.named = 有名字(it, 范围)
        if not it.named:
            没有 += 1
    return 没有


def 主线条数(recs: list[tuple[dict, str]]) -> int:
    """库里有几条还算数的时期（空白记账的地图闸拿它当判据）。"""
    return len(时期们(recs))


def 空白记账(items: list[Item], 主线: int, cfg: dict, now: datetime) -> list[指法]:
    """**一段连续的日子，一条都没被时期盖住** → 拍我一下。

    🔴 **库里一条主线都没有的时候，这一指整个闭嘴。** 她的原话场景：
       「他完全可以说今年都没有」——**不许发生**。没有地图的时候「哪儿没盖」
       是个假问题：那不是空白，那是还没开始画。
       （地图第一版是我和她手写的——**先讲述，后指点**。）
    ⚠️ 「没名字」8-17 起是 **range 覆盖**（时期的范围，`盖上名字()` 现场打在 `named` 上），
       不再问 `covered_by`——时期不记账了。补记落进老范围自动就有名字，不会再被记一次空白。
    """
    if int(主线) < 1:
        return []
    最少 = int(cfg["blank_min_items"])
    断 = timedelta(days=float(cfg["blank_gap_days"]))
    停了 = timedelta(days=float(cfg["stopped_days"]))

    没名字 = sorted([i for i in items if not i.named and i.ts], key=lambda x: x.ts)
    out: list[指法] = []
    段: list[Item] = []

    def 收(段):
        if len(段) < 最少:
            return
        起, 止 = 段[0].ts, 段[-1].ts
        if (now - 止) <= 停了:
            return
        out.append(指法(
            名="空白记账",
            ids=[i.id for i in 段], items=list(段), 起=起, 止=止,
            证据=f"{_日(起)}~{_日(止)} 有 {len(段)} 条没落在任何一条时期的范围里",
            出路=f'fold(when="{_日全(起)}..{_日全(止)}", text=我写的那句)',
            分=float(len(段))))

    for it in 没名字:
        if 段 and (it.ts - 段[-1].ts) > 断:
            收(段)
            段 = []
        段.append(it)
    收(段)
    out.sort(key=lambda x: (-x.分, x.起 or now))
    return out


# ============================================================
# 拒绝计数 —— sidecar，**不放 frontmatter**
# ============================================================
# 🔴 拒绝是关于「**这一组**」的，不属于任何单条。写进 frontmatter 等于把一个
#    关于组合的事实拆散塞进成员里，下次组变了还得挨个擦。
REJECT_FILE = "muse_rejected.json"


def state_dir(buckets_dir: str) -> str:
    return os.path.join(str(buckets_dir or "."), "_state")


def rejection_key(ids) -> str:
    """id 集合排序后做 key。**排序**是重点：同一组换个顺序还是同一组。"""
    return ",".join(sorted({str(i).strip() for i in (ids or []) if str(i).strip()}))


def load_rejected(buckets_dir: str) -> dict:
    p = os.path.join(state_dir(buckets_dir), REJECT_FILE)
    if not os.path.exists(p):
        return {"version": 1, "rejected": {}}
    try:
        data = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "rejected": {}}
    if not isinstance(data, dict) or not isinstance(data.get("rejected"), dict):
        return {"version": 1, "rejected": {}}
    return data


def is_rejected(data: dict, ids, limit: int) -> bool:
    ent = (data.get("rejected") or {}).get(rejection_key(ids))
    if not isinstance(ent, dict):
        return False
    return int(ent.get("count") or 0) >= int(limit)


def record_rejection(buckets_dir: str, ids) -> tuple[str, int]:
    """记一笔「这几条不是一回事」。返回 (key, 累计次数)。

    **组变了就是另一个 key**（多一条少一条都算），所以补记进来的那条会让这一组
    重新被提——这是要的：那时候它确实是新的一组。
    """
    key = rejection_key(ids)
    data = load_rejected(buckets_dir)
    ent = data["rejected"].get(key) or {}
    now = _w.now().isoformat(timespec="seconds")
    cnt = int(ent.get("count") or 0) + 1
    data["rejected"][key] = {
        "ids": sorted({str(i).strip() for i in ids if str(i).strip()}),
        "count": cnt,
        "first": ent.get("first") or now,
        "last": now,
    }
    d = state_dir(buckets_dir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, REJECT_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return key, cnt


# ============================================================
# 服务端：装料 → 出团 / 出指
# ============================================================
async def load_records() -> tuple[list[tuple[dict, str]], set[str]]:
    """全库一遍（不含归档）+ 「被认知 from 指向过」的 id 集合。

    `mind_from_ids()` 就是 night_fall 8-06 那条「没消化」的判据，**搬过来用，不重写**。
    """
    from tools import _runtime as rt
    recs: list[tuple[dict, str]] = []
    for b in await rt.bucket_mgr.list_all(include_archive=False):
        meta = b.get("metadata", {}) or {}
        recs.append((meta, str(b.get("content") or "")))
    try:
        digested = await rt.bucket_mgr.mind_from_ids()
    except Exception:
        digested = set()
    return recs, digested


def _db_path() -> str:
    from tools import _runtime as rt
    p = getattr(rt.embedding_engine, "db_path", "") or ""
    if p:
        return p
    return os.path.join(str((rt.config or {}).get("buckets_dir") or ""), "embeddings.db")


def _没被拒过(候选: list, cfg: dict) -> tuple[list, int]:
    from tools import _runtime as rt
    rejected = load_rejected(str((rt.config or {}).get("buckets_dir") or ""))
    limit = int(cfg["reject_limit"])
    keep, 拒过 = [], 0
    for x in 候选:
        if is_rejected(rejected, x.ids, limit):
            拒过 += 1
            continue
        keep.append(x)
    return keep, 拒过


async def propose_mind(cfg: dict | None = None, 料=None
                       ) -> tuple[list[团], int, int, dict]:
    """认知侧一整趟。返回 (团列表, 散着几条, 老默认坐标几条, 统计)。**不写任何东西。**"""
    from tools import _runtime as rt
    c = muse_config(cfg if cfg is not None else rt.config)
    now = _w.now()
    recs, digested = 料 if 料 is not None else await load_records()
    items = pool_of(recs, "muse", c, now, digested)
    vectors = read_vectors(_db_path(), [i.id for i in items])
    团们, 散着, 默认坐标 = 发呆(items, vectors, c)
    # 被拒过的组不算进「散着」——它们没散，只是**我说过不是一回事**，别再提。
    团们, 拒过 = _没被拒过(团们, c)
    stats = {"池子": len(items), "有向量": len(vectors), "团": len(团们),
             "散着": 散着, "老默认坐标": 默认坐标, "被拒过的组": 拒过}
    return 团们, 散着, 默认坐标, stats


async def propose_gist(cfg: dict | None = None, 料=None
                       ) -> tuple[dict[str, list[指法]], dict]:
    """事件侧一整趟：三种指法各走一遍。返回 ({指法名: [指法]}, 统计)。"""
    from tools import _runtime as rt
    c = muse_config(cfg if cfg is not None else rt.config)
    now = _w.now()
    recs, digested = 料 if 料 is not None else await load_records()
    items = pool_of(recs, "gist", c, now, digested)
    vectors = read_vectors(_db_path(), [i.id for i in items])
    # 🔴 「有名字了吗」**现场算**：时期只落名字 + 范围，字段里问不出来（8-17 终稿）。
    范围 = 时期们(recs)
    主线 = len(范围)
    没名字 = 盖上名字(items, 范围)

    出: dict[str, list[指法]] = {
        "词爆发": 词爆发(items, c, now),
        "成分漂移": 成分漂移(items, vectors, c, now),
        "空白记账": 空白记账(items, 主线, c, now),
    }
    拒过 = 0
    for k in 出:
        出[k], n = _没被拒过(出[k], c)
        拒过 += n
    stats = {"池子": len(items), "有向量": len(vectors), "主线": 主线,
             "没名字": 没名字, "被拒过的组": 拒过}
    return 出, stats


# ============================================================
# 视图缓存（施工 5 · H 件，2026-08-17）：一次全库扫，两步走都用它
# ============================================================
# **为什么要有**：`muse()` 摆完团，我接着 `muse(cluster=3)` 看那一批——
# 第二步跟第一步要的是**同一份**结果（[N] 的编号口径必须一致），
# 可它现在会把全库重扫一遍（`load_records` + 向量 + 三种指法），
# 8-17 的流水记着这个坑：一次调用肉眼可感的慢，两步就是两倍。
#
# 🔴 **宁可失效勤一点，绝不给旧数据**（说明书 H 件原话）。
#    钥匙 = `bucket_manager._active_cache_generation` —— 它在**每一次托管写盘**
#    （create/update/archive/delete，`_invalidate_bm25`）和**每一次 touch**
#    （`_cache_bump`）时都 +1，外部改动（Obsidian/git 手编）被轮询发现时也 +1。
#    所以 grow / fold / regrow / trace 一律当场失效，一个都不用自己去挂钩子。
#    ⚠️ 反过来说：**它宁可多失效**（recall 一次 id 直查就 touch 一下）——
#    那正是想要的方向。缓存只保「这一屏和下一屏是同一屏」，不保跨对话。
# ⚠️ `not_same`（拒绝计数）写的是 `_state/` 里的 json，**不动桶**，钥匙不会变
#    → `record_rejection()` 之后必须**手动清一次**（`视图清空()`）。
_视图缓存: dict = {"钥匙": None, "值": None}


def 视图钥匙() -> tuple | None:
    """这一屏该不该重算。拿不到 generation 就返回 None = **不敢缓存**。"""
    from tools import _runtime as rt
    gen = getattr(rt.bucket_mgr, "_active_cache_generation", None)
    if gen is None:
        return None
    # config 也进钥匙：她在面板上改了 muse 段的阈值，下一屏就该按新线算
    c = muse_config(rt.config)
    return (int(gen), tuple(sorted((k, str(v)) for k, v in c.items())))


def 视图清空() -> None:
    _视图缓存["钥匙"] = None
    _视图缓存["值"] = None


async def 两侧一趟(强制: bool = False) -> tuple[list, int, int, dict, dict]:
    """认知侧 + 事件侧一整趟，**带视图缓存**。返回 (团们, 散着, 默认坐标, 指们, 统计)。

    两个调用方共用这一份：`tools/muse/__init__.py`（工具面的两步走）和
    `web/loci.py::build_muse_pending`（「该发呆了吗」只要数量和年龄）。
    **绝不各算各的**——页面说「攒了 3 团」而我 muse() 看到 4 团，那就是两个脑子。
    """
    钥 = 视图钥匙()
    if not 强制 and 钥 is not None and _视图缓存["钥匙"] == 钥:
        return _视图缓存["值"]
    料 = await load_records()
    团们, 散着, 默认坐标, s1 = await propose_mind(料=料)
    指们, s2 = await propose_gist(料=料)
    出 = (团们, 散着, 默认坐标, 指们, {"mind": s1, "event": s2})
    if 钥 is not None:
        _视图缓存["钥匙"] = 钥
        _视图缓存["值"] = 出
    return 出


# ⚰️ `dream_pool()`（返回候选 id 列表）2026-08-17 删掉了。
#    它是给 night_fall 的选料端用的，night_fall 整个退役之后没有第二个调用方；
#    而新引擎（`tools/_dream.py`）要的是 **Item 本身**（正文 + v/a + 日子），
#    不是一串 id —— 留着它等于把「梦吃哪个池子」这件事摆两份。
#    🔴 **选料统一走这儿**：`POOL_SPECS["dream"]` + `pool_of(recs, "dream", ...)` 是唯一入口。
