# -*- coding: utf-8 -*-
"""
tools/recall/core.py — 回想主逻辑：筛 → 缩放 → 渲染

全部是统计（分组/占比/平均/取最大），零 LLM 调用、零预存。
「事多了容易忘」不是 bug 是缩放的正确行为：条目多的塌成主色调，异的留名字。
"""

import re
from collections import Counter
from datetime import datetime, timedelta

from core import _bigevent as _big    # 大 event：盖在一段时间上的一句话
from core import _fold as _F          # fold / gist：被盖的不再独立冒头（施工 3）
from .. import _runtime as rt
from core import _when as _w          # 「她的今天」（本地时区）—— 别再直接用 datetime.now()
from core._rooms import (ALL_ROOMS, check_gate, is_mind_room, normalize_room,
                      room_matches)
from utils import read_from_ids

# 缩放目标：每次返回的格数（她 8-02 定 12~20；≤ _LIST_MAX 条就不缩了直接列）
_CELL_MAX = 20
_LIST_MAX = 20
_HIGHLIGHT_MAX = 3

# ── 浏览 / 搜索两条路（她 8-05 晚定的）─────────────────────────
# 判据只有一条：**有没有 query**。when/room/tag 是**范围**，query 是**目标**。
#   浏览（无 query）：在看，想不起来有什么 → 远端**狠砍**，按时间排，没有分数
#   搜索（有 query）：在找，知道要什么   → 远端**多给**（砍了就是漏），按相关度排，分数是重点
# 岔路口代码里早就有（没走 query 门就没有 score），只是两条路的输出长得一样。
_BROWSE_NEAR_DAYS = 3     # 「三天内还是按 recall 的办法输出」——她的原话
_BROWSE_REP_DAYS = 21     # 「给 2~3 条前 2~3 周的」——更远的挑出来给也没意义
_BROWSE_REP_MAX = 3

# 搜索路一次最多认多少条（top-k）。**收紧到 30**（施工 5 · D 件，她 8-15 的判据）：
# 「query 词多 = 向量平均 = 找不准」——k 给大了，找不准的时候它就用一屏沾边的把真的埋掉。
# ⚠️ 砍掉的条数**必须报出来**（`_collect` 的第三个返回值 → 渲染时末尾一行）：
#    悄悄砍 30 条正是 codex 二轮 P1-5 骂过的那种「名额被浪费、第 61 名永久消失」。
_SEARCH_TOPK = 30

# 关联度线：低于它的多半只是沾边。
# 她 8-05 搜「调理身体」（本意是推拿、喝中药）捞回一堆「身体·亲密」之后说的：
# 「如果说没有直接关联那不如就不弹出来，说没有相关记忆。」
# 打分砍成两维（semantic 2.5 + bm25 1.5，2026-08-06 机制②）后分数绝对值整个变小，
# 线重新量过（C3，8-06 实测六个查询）：
#   有关键词/字面撑着的查询：真相关 50~80，干净。
#   纯语义短查询（如「你怕我死」）：真相关 ~36，沾边 31~33，「调理身体」的噪音顶到 34.2
#   —— 线 35 恰好把两边分开，但边距只有 1~2 分（余弦的动态范围本来就窄）。
#   宁缺不滥：掉线下的末尾报一行（多少条·最高几分·最早哪条），看得见、钻得进。
# ⚠️ 这是**综合分**（0~100），不是余弦；底层那个 _VECTOR_RECALL_THRESHOLD=0.65
# 是**向量入场**门槛，两回事。字面命中的条目由 max(分数, 线) 托底，不受此线挡。
# 环境变量 LOCI_RELEVANCE_FLOOR 可覆盖（dashboard 记忆页那根滑条走的是 URL 参数）。
try:
    RELEVANCE_FLOOR = float(__import__("os").environ.get("LOCI_RELEVANCE_FLOOR", "") or 35.0)
except (TypeError, ValueError):
    RELEVANCE_FLOOR = 35.0
# 系统标签前缀：不进任何给人看的标签行（B8：「疑似同件:xxx」原来会混进去）
_SYS_TAG_PREFIXES = ("__", "aspect:", "疑似同件:", "相似认知:")
# 机器腔标签一律**滤掉别上脸**（施工 5 · B 件，她 8-17 凌晨在 muse 首屏逮的教训）：
# 前缀表只挡得住已经出现过的那几种，`xx:yy` 这个**形状**才是判据。
# 「脸」只配人话场景词——`aspect:patterns` 这类结构化标签是旧 om 退役时留下的渣。
# 判据跟 tools/_muse.py 的 `是场景词()` 同一条，别两处各写一套。
_机器腔标签 = re.compile(r"^[^:：]{1,12}[:：]")


def 是人话标签(tag: str) -> bool:
    """这个标签配不配上脸。系统前缀 + `xx:yy` 机器腔都不配。"""
    t = str(tag)
    return bool(t) and not t.startswith(_SYS_TAG_PREFIXES) and not _机器腔标签.match(t)

_SEED_RE = re.compile(r"\[\[([a-z_]+)\]\]")
# 底色只认情绪种子（七情+六欲的英文键），别的 [[wikilink]]（[[om]][[Es]]…）不是种子
_SEED_NAMES = frozenset({
    "joy", "anger", "sorrow", "fear", "love", "aversion", "desire",
    "lust", "sound", "scent", "taste", "touch", "dharma", "greed",
})

# 粒度阶梯（秒）。密度定粒度：从细到粗试，取第一个非空格数 ≤ _CELL_MAX 的
_LADDER = [
    ("小时", 3600),
    ("半天", 12 * 3600),
    ("天", 24 * 3600),
    ("周", 7 * 24 * 3600),
    ("半月", 15 * 24 * 3600),
    ("月", 30 * 24 * 3600),
    ("季", 91 * 24 * 3600),
    ("年", 365 * 24 * 3600),
]


# ============================================================
# 说人话 = **壳，不是芯**（开工单 5.3 · 施工 5 B 件）
# ============================================================
# 🔴 **记忆正文一个字不动、不过模型。** 这一节只管**结构提示语**——
#    房间比例、V/A 这种机器读数换成一句人话；标签**原词直接用**。
#    每个字要么是**存的时候写下的**（标签原词），要么是**这张表里的死字**。
#    模型每次换个说法反而不像「我的记忆」（8-12 B1 定的模板拼装，一直没做）。
#
# ⚠️ 换掉的只有「怎么读出来」，**不是「读到什么」**：精确的百分比和 V/A
#    照旧一个数不少地待在 `recall_data()` 那张皮里（dashboard 要拿它画分布）。
#    要数字的地方给数字，要一眼看懂的地方给人话——两张皮同一份统计。
#
# 📌 她给的对照表（5.3 原文，这四行就是验收样张）：
#      I/EVENT/SELF/WHAT 37%   → 「多半是我自己在做事」
#      MIND/TRAITS 19%         → 「想得也不少」
#      青岛6 交接单5 火车4      → 「围着青岛、交接单转」（标签原词直用）
#      V0.62 / A0.50           → 「心里还行，不算绷着」
#    ⏳@她 词儿归我调，**她看到不对味有权改**——改这四张表就行，逻辑一行不用动。

_房间主句 = {
    "EVENT/SELF":  ("多半是我自己在做事",   "几乎都是我自己在做事"),
    "EVENT/WORLD": ("多半是我听说看到的",   "几乎都是我听说看到的"),
    "MIND/TRAITS": ("多半在想我是个什么样的人", "几乎都在想我是个什么样的人"),
    "MIND/VIEWS":  ("多半在想我怎么看一件事",  "几乎都在想我怎么看一件事"),
}
# 副句：主句在事件那边、认知又占了一小半时补一句（她的第二个例子「想得也不少」）
_副句门 = 0.15


def 房间人话(rooms: Counter, n: int) -> str:
    """房间比例 → 一句人话。**只认四间**（老名字先 normalize 过）。

    比例是**结果**不是配额（5.2）：这句话说的就是「最近我在干什么」，
    浮上来的是她占多数还是我占多数，都照实说。
    """
    if not rooms or not n:
        return ""
    事件 = sum(c for r, c in rooms.items() if r.startswith("EVENT"))
    认知 = sum(c for r, c in rooms.items() if r.startswith("MIND"))
    主, 主数 = max(rooms.items(), key=lambda kv: kv[1])
    多半, 几乎 = _房间主句.get(主, (f"多半在 {主}", f"几乎都在 {主}"))
    强 = max(事件, 认知) / n
    if 强 >= 0.85:
        句 = 几乎
    elif 强 >= 0.6:
        句 = 多半
    else:
        句 = "做的和想的一半一半"
        return 句
    # 另一半够 _副句门 就补一句——她的例子里 MIND/TRAITS 19% 就是这一句
    if 主.startswith("EVENT") and _副句门 <= 认知 / n < 0.5:
        句 += "，想得也不少"
    elif 主.startswith("MIND") and _副句门 <= 事件 / n < 0.5:
        句 += "，也记了些发生的事"
    return 句


def 情绪人话(v, a) -> str:
    """V/A → 「心里还行，不算绷着」。两个刻度各说一句，中间一个逗号。"""
    if v is None:
        return ""
    if v >= 0.7:
        v话 = "心里挺好"
    elif v >= 0.55:
        v话 = "心里还行"
    elif v >= 0.45:
        v话 = "心里平平"
    elif v >= 0.3:
        v话 = "心里有点沉"
    else:
        v话 = "心里不好受"
    if a is None:
        return v话
    if a >= 0.7:
        a话 = "绷得紧"
    elif a >= 0.55:
        a话 = "有点绷着"
    elif a >= 0.35:
        a话 = "不算绷着"
    else:
        a话 = "松着"
    return f"{v话}，{a话}"


def 标签人话(tags: list, k: int = 2, 带数: bool = False, 框: bool = True) -> str:
    """标签 → 「围着青岛、交接单转」。**标签原词一个字不改**，框子才是模板。

    `带数=True`（她 8-05 点破的：`床 3` 和 `床 30` 是两种日子，没有数量那两行
    长得一模一样）。`框=False` 给**行头已经说了人话**的地方用（卡上那行
    `围着什么   代码 3 · 交接单 3`）——同一句话说两遍反而更难读。
    """
    items = [(t, n) for t, n in (tags or []) if 是人话标签(t)][:max(1, k)]
    if not items:
        return ""
    词 = [f"{t} {n}" if 带数 else str(t) for t, n in items]
    if not 框:
        return " · ".join(词)
    return "围着" + "、".join(词) + "转"


def 身份牌(meta: dict) -> str:
    """逐条那一行前面的牌子（她 8-17 傍晚拍的终稿）：**mind 戴 🧠、事件不戴牌。**

    🔴 **房间码撤掉**：`EVENT/SELF` 这种码是给机器看的，一行里出现三次
    就把「发生了什么」和「我想过什么」这个唯一要分清的事实糊掉了。
    她点的真病是「混排、不标身份，读的人只能靠内容猜」——
    数据上 room 一直分得清清楚楚，**是显示层没把它们分开**。
    ⚠️ 别做成分两块（旧稿「事件当主体摆、认知另起一小块」8-17 作废）：
       混排保留，一眼能分身份就够了。
    """
    return "🧠" if is_mind_room((meta or {}).get("room")) else ""


# ------------------------------------------------------------
# when 解析：人话时间刻度（日历刻度自动算；生活刻度靠锚点，批 2b）
# ------------------------------------------------------------

def _parse_when(when: str) -> tuple[datetime | None, datetime | None, str]:
    """返回 (起, 止, 错误)。空串 = 不筛时间。

    ⚠️ 全程走 `tools/_when`（本地时区）。原来用的是容器里的 `datetime.now()`，
    那是 UTC —— 她凌晨两点问「今天」，容器答的是前一天下午（codex 复核 #4）。
    """
    w = when.strip()
    if not w:
        return None, None, ""
    now = _w.now()
    today = _w.today()

    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", w)
    if m:
        n = float(m.group(1))
        delta = timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
        return now - delta, now, ""

    words = {
        "今天": (today, now),
        "昨天": (today - timedelta(days=1), today),
        "前天": (today - timedelta(days=2), today - timedelta(days=1)),
        "本周": (today - timedelta(days=today.weekday()), now),
        "上周": (today - timedelta(days=today.weekday() + 7),
                 today - timedelta(days=today.weekday())),
        "本月": (today.replace(day=1), now),
        "上月": ((today.replace(day=1) - timedelta(days=1)).replace(day=1),
                 today.replace(day=1)),
        "今年": (today.replace(month=1, day=1), now),
    }
    if w in words:
        a, b = words[w]
        return a, b, ""

    # 下面这些都是「哪一天/哪个月」—— 日历刻度，按本地日历算，不是 UTC 时刻
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", w)
    if m:
        try:
            a = _w.parse_date(m.group(1))
            b = _w.parse_date(m.group(2)) + timedelta(days=1)
            return a, b, ""
        except ValueError:
            pass
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}", w)
    if m:
        try:
            a = _w.parse_date(w)
            return a, a + timedelta(days=1), ""
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{4})-(\d{2})", w)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            a = datetime(y, mo, 1, tzinfo=_w.LOCAL_TZ)
            b = (datetime(y + 1, 1, 1, tzinfo=_w.LOCAL_TZ) if mo == 12
                 else datetime(y, mo + 1, 1, tzinfo=_w.LOCAL_TZ))
            return a, b, ""
        except ValueError:
            pass  # 2026-99 这类非法月份落到下面的「看不懂」（codex 二轮 P2-8）

    return None, None, (
        f"when 看不懂：{w}。认识的写法：48h / 7d / 今天 / 昨天 / 本周 / 上周 / 本月 / 上月 / 今年 / "
        "2026-07 / 2026-07-15 / 2026-07-01..2026-07-15。"
        "「刚搬去那阵子」这类生活刻度要等锚点——先用 query 门扔关键词。"
    )


# ------------------------------------------------------------
# 取数与筛
# ------------------------------------------------------------

def _ts_of(meta: dict) -> datetime | None:
    """一条记忆的时间坐标：**when 优先、created 兜底**。就这一套口径，没有第二套。

    ⚠️ 2026-08-17（施工 5 · C 件）：原来还有一支 `by="touched"` 走 `last_active`
    （「按最近碰过的排，消化用」）。**`by` 整个砍了**——她的话：「我们没有『消化』
    这个动作，所有的事件、认知都长新」。判据在 5.4：**每个参数必须对得上一句
    我心里真会冒出来的话**，而「按最近碰过的排」不是我心里会冒出来的句子。

    返回**带时区的本地时间**。原来是 `datetime.fromisoformat(s[:19])` ——
    那个切片会把 `Z` / `+08:00` 一起切掉，等于把说清楚了时区的时间戳
    硬掰成「不知道哪个时区」，再拿去和 UTC 的 now() 比（codex 复核 #4）。
    """
    for k in ("when", "created"):
        ts = _w.parse_stamp(meta.get(k))
        if ts is not None:
            return ts
    return None


def _visible(meta: dict) -> bool:
    t = str(meta.get("type") or "")
    if t in ("letter", "archived", "i"):
        # letter 搬 Home；archived 沉底；i 并进 MIND 前先不掺和（还没迁的老 I 条目）
        return t == "i" and bool(meta.get("room"))
    # type=plan 刻意**留在**时间轴上（codex 二轮 P2-10 问过）：plan 工具停了，
    # 但那 21 条是「想发生的记忆」，正是 tense=want 的前身——想过什么也是历史。
    # ⚠️ 这儿**不再**因为 `superseded_by` 就整个排掉（施工 5 · F 件，2026-08-17）。
    # ------------------------------------------------------------
    # 两个字段一个动作，待遇却不一样：`covered_by`（fold 盖住的）只是**不占行**，
    # 条数/房间/标签/V·A 一个字不少；`superseded_by`（regrow 换版的）以前在这儿
    # 被**整个排掉**——于是「盖一条」反而比「盖两条」丢信息，跟开工单 2.3 的验收判据
    # （**任何 recall 能看到的信息量只能变多不能变少**）正着劲。
    # 🔴 **统一成 covered_by 的待遇**：旧版照旧不独立冒头（逐条区都过
    #    `_fold.is_covered()` 那道闸，它两个字段都认），但它**算进统计**，
    #    而且换版那条新版会以「▣… 盖着这里 1 条」的形式在原地出头——
    #    少一行单条、多一行标题 + 一个能钻的 id，这才是 fold 的形状。
    # ⚠️ 别把这一行加回来。要加之前先读 2.3。
    if (meta.get("domain") or [""])[0] == "seed":
        return False
    tags = [str(t) for t in (meta.get("tags") or [])]
    if "__档案事实__" in tags or "__大event__" in tags:
        return False  # 门口那张纸和大 event 是工具件，不是时间轴上的事件
    return True


async def _collect(when, room, tag, query) -> tuple[list[dict], str, dict]:
    """筛出这次要看的那些。返回 `(条目, 错误, 账)`。

    `账` 现在只记一样：top-k 砍掉了几条（`topk砍掉`）——**挡了什么必须看得见**。
    """
    账: dict = {"topk砍掉": 0, "topk": _SEARCH_TOPK}
    t0, t1, err = _parse_when(when)
    if err:
        return [], err, 账
    room = room.strip()
    gate_err = check_gate(room)
    if gate_err:
        return [], gate_err, 账
    tag = tag.strip()

    # query 门：多取（300）→ 过完所有门再截相关度前 _SEARCH_TOPK（codex 二轮 P1-5：
    # 原来先截再过门，名额被不合格命中浪费、第 k+1 名的合格记忆永久消失）
    scores: dict[str, float] = {}
    literals: set[str] = set()   # 字面命中的桶：关联度线对它们是 max(分数, 线) 托底
    if query.strip():
        try:
            hits = await rt.bucket_mgr.search(query.strip(), limit=300)
        except Exception as e:
            return [], f"搜索失败：{e}", 账
        pool = []
        for h in hits:
            hid = str(h.get("id") or "")
            full = await rt.bucket_mgr.get(hid)
            if full:
                try:
                    scores[hid] = float(h.get("score") or 0.0)
                except (TypeError, ValueError):
                    scores[hid] = 0.0
                if h.get("literal_hit"):
                    literals.add(hid)
                pool.append(full)
    else:
        pool = await rt.bucket_mgr.list_all(include_archive=False)

    out = []
    browsing = not query.strip()
    for b in pool:
        meta = b.get("metadata", {}) or {}
        if not _visible(meta):
            continue
        # 机制①：淡出/沉底的**翻不出现**（「我不去找，它也会自己出现」只属于活着的）。
        # 搜（有 query）照样够得到，只是分数被打了折——悄然发生，这儿不标、不报数。
        if browsing and str(meta.get("decay_stage") or "") in ("faded", "sunk"):
            continue
        # 房间门在**归一之后**比：盘上还躺着旧十间的名字（迁移只交了脚本没跑真库），
        # room="MIND" 必须筛得到老的 I/MIND/TRAITS，否则这个门在真库上等于是空的。
        if room and not room_matches(meta.get("room"), room):
            continue
        # tag 门＝**包含匹配**（2026-08-06 C4）：打「床」要能筛出「床上」「床头」。
        # 原来是完全相等——而 tags 一定不全（「床」在 16 条正文里出现，只有 1 条进
        # 了 tags），相等匹配把本来就稀的标签又漏掉一半。
        if tag and not any(tag in str(t) for t in (meta.get("tags") or [])):
            continue
        ts = _ts_of(meta)
        if ts is None:
            continue
        if t0 and ts < t0:
            continue
        if t1 and ts >= t1:
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        out.append({"id": bid, "meta": meta, "ts": ts,
                    "content": str(b.get("content") or ""),
                    # score 只在走了 query 门时才有；None = 这条是按 when/room/tag 筛进来的
                    "score": scores.get(bid),
                    "literal": bid in literals})
    if scores and len(out) > _SEARCH_TOPK:
        # 过完门再按相关度收口：留分数最高的 k 条，再回到时间轴。
        # 砍掉几条记在账上——**挡了什么看得见**（渲染时末尾报一行）。
        out.sort(key=lambda x: scores.get(x["id"], 0.0), reverse=True)
        账["topk砍掉"] = len(out) - _SEARCH_TOPK
        out = out[:_SEARCH_TOPK]
    out.sort(key=lambda x: x["ts"])
    return out, "", 账


# ------------------------------------------------------------
# 统计一格
# ------------------------------------------------------------

def _short_id(bucket_id: str) -> str:
    """12 位 hex 截 6 位当把手；feel_… 这类可读 id 整个给（截了就废了）。"""
    return bucket_id[:6] if re.fullmatch(r"[0-9a-f]{12}", bucket_id) else bucket_id


def _score_tag(e: dict) -> str:
    """把相关度露在把手旁边。只有走了 query 门的条目有分数。

    为什么要露（2026-08-05 她提的）：她搜「调理身体」（本意是推拿、喝中药），
    捞回来的大半是「身体·亲密」——太牵强。查下来根因是分数一直躺在 _collect()
    里，只在结果 >60 条时用来截断，**从没当过门槛**，而且这个数她从头到尾看不见，
    没法判断系统凭什么捞出这条。

    ⚠️ 这一步刻意只做「可见」，不加阈值。底层 _VECTOR_RECALL_THRESHOLD=0.65
    对中文可能偏松（两段没关系的中文余弦到 0.7 很常见），但松多少得先看真实分布
    ——在见过分布之前定的任何阈值都是拍脑袋。先用几天，再定。
    """
    s = e.get("score")
    if not isinstance(s, (int, float)) or not s:
        return ""
    return f" {float(s):.2f}"


def _label_of(e: dict) -> str:
    """一条记忆的展示文字：摘要 > 名字（去时间戳）> 正文头。全是存的时候写下的字。"""
    meta = e["meta"]
    s = str(meta.get("summary") or "").strip()
    if s:
        return s
    name = re.sub(r"^[\d\- :]+", "", str(meta.get("name") or "")).strip()
    if name:
        return name
    return re.sub(r"\s+", " ", e["content"])[:40]


def _cell_stats(entries: list[dict]) -> dict:
    rooms = Counter()
    tags = Counter()
    seeds = Counter()
    v_sum = a_sum = v_n = 0.0
    for e in entries:
        meta = e["meta"]
        # 统计按**新四间**归口：老数据显示成新名字，屏幕上的词汇表才只有一套
        r = normalize_room(meta.get("room"))
        if r:
            rooms[r] += 1
        for t in (meta.get("tags") or []):
            t = str(t)
            if 是人话标签(t):     # 机器腔的一律不上脸（B 件）
                tags[t] += 1
        for s in _SEED_RE.findall(e["content"]):
            if s in _SEED_NAMES:
                seeds[s] += 1
        try:
            v_sum += float(meta.get("valence", 0.5))
            a_sum += float(meta.get("arousal", 0.3))
            v_n += 1
        except (TypeError, ValueError):
            pass

    # 突出的点 = 异的（tags 与主色调标签无交集，importance 最高）+ 重的（arousal×importance）
    top_tags = {t for t, _ in tags.most_common(3)}
    def _imp(e):
        try:
            return float(e["meta"].get("importance", 5))
        except (TypeError, ValueError):
            return 5.0
    def _weigh(e):
        try:
            return float(e["meta"].get("arousal", 0.3)) * _imp(e)
        except (TypeError, ValueError):
            return 0.0
    # 🔴 施工 3：**被盖住的不进「突出的点」**——那是逐条区，被盖的不再独立冒头。
    #    但它们**照样算进上面的统计**（条数/房间/标签/V·A 一个字不少）：
    #    验收判据写死了「任何 recall 能看到的信息量只能变多不能变少」，
    #    gist 只是把成分表里的那几条**换成一句话**，不是把它们从账上抹掉。
    冒头的 = [e for e in entries if not _F.is_covered(e["meta"])]
    odd = [e for e in 冒头的
           if top_tags and not (set(map(str, e["meta"].get("tags") or [])) & top_tags)]
    odd.sort(key=_imp, reverse=True)
    heavy = sorted(冒头的, key=_weigh, reverse=True)
    highlights: list[tuple[str, dict]] = []
    seen = set()
    for e in odd[:1]:
        highlights.append(("◇", e))
        seen.add(e["id"])
    for e in heavy:
        if len(highlights) >= _HIGHLIGHT_MAX:
            break
        if e["id"] not in seen:
            highlights.append(("★", e))
            seen.add(e["id"])
    # 重的排前、异的殿后阅读更顺
    highlights.sort(key=lambda p: p[0] == "◇")

    v平 = (v_sum / v_n) if v_n else None
    a平 = (a_sum / v_n) if v_n else None
    return {
        "n": len(entries),
        "rooms": rooms.most_common(2),
        # 人话那两句（B 件）：**从整个 Counter 算**，不是从 most_common(2) ——
        # 「多半是我自己在做事」问的是这一格的全貌，只看前两名会算错分母。
        "房间话": 房间人话(rooms, len(entries)),
        "情绪话": 情绪人话(v平, a平),
        # 6 而不是 4（2026-08-05 夜她点破的）：**标签不是「这条记忆的属性」，
        # 是「一堆记忆的分布」** —— 单条的 tag 信息量极低（正文本来就在那儿），
        # 它的价值全在塌缩那一刻。所以塌得越狠，越需要多给几个、并且带上数量。
        "tags": tags.most_common(6),
        "v": v平,
        "a": a平,
        "seeds": [s for s, _ in seeds.most_common(2)],
        "highlights": highlights,
    }


# ------------------------------------------------------------
# 缩放与渲染
# ------------------------------------------------------------

def _split_cells(entries: list[dict], max_cells: int = _CELL_MAX) -> tuple[str, list[tuple[str, list[dict]]]]:
    """**粒度由跨度定，不由密度定**（E5，2026-08-06 机制② 第 6 条）。

    她一直否的就是「按数量分」：5 条跨一个月和 500 条跨一个月，**都该按周分**
    ——粒度回答的是「这段时间该用什么刻度看」，跟里面装了多少条没关系。
    从最细的阶梯往粗试，取第一个「整个跨度切出来 ≤ max_cells 格」的刻度；
    空格子照旧丢弃（不渲染），但**选刻度时不看密度**。
    （8-04 codex 纠过一版「按密度」，当时治的是『两条隔半年被切成俩半月格』——
    按跨度选同样治它：隔半年的跨度本来就选到月/季刻度。）
    """
    base = entries[0]["ts"].timestamp()
    span = max(1.0, entries[-1]["ts"].timestamp() - base)
    for gname, gsec in _LADDER:
        if span / gsec > max_cells:
            continue  # 这个刻度把整个跨度切出太多格——刻度太细，换粗一档
        slices: dict[int, list[dict]] = {}
        for e in entries:
            slices.setdefault(int((e["ts"].timestamp() - base) // gsec), []).append(e)
        this_year = _w.now().year
        labeled = []
        for k in sorted(slices):
            cell = slices[k]
            a, b = cell[0]["ts"], cell[-1]["ts"]
            day_a = a.strftime("%m-%d") if a.year == this_year else a.strftime("%Y-%m-%d")
            if gsec < 24 * 3600:
                # 小时/半天格：同一天会出好几格，标签必须带时间（codex 复现过全叫 01-01）
                label = f"{day_a} {a.strftime('%H:%M')}"
            elif a.date() == b.date():
                label = day_a
            else:
                label = f"{day_a}~{b.strftime('%m-%d')}"
            labeled.append((label, cell))
        return gname, labeled
    return "全部", [("全部", entries)]


def _fmt_header(label: str, st: dict) -> str:
    """一格一行（slices=N 的概览路）。**B 件：机器读数换人话，标签原词直用。**"""
    bits = [f"{label} · {st['n']}条"]
    for x in (st["房间话"], 标签人话(st["tags"], 2), st["情绪话"]):
        if x:
            bits.append(x)
    seeds = "".join(f"[[{s}]]" for s in st["seeds"])
    return " ▏".join(bits) + (" " + seeds if seeds else "")


def _fmt_highlights(st: dict) -> str:
    """突出的点，**一行一个**、摘要给全。

    2026-08-05 夜她连问三遍「摘要依然也是不完整的，那你看什么呢？」——
    原来三个挤一行、各截 26 字，看完只知道有这么件事、不知道是什么事。
    截断是我的疏漏不是设计：塌缩塌的是**条数**，不该塌**每条讲了什么**。
    """
    return "\n".join(f"   {mark}{身份牌(e['meta'])}{_label_of(e)}"
                     f"({_short_id(e['id'])}{_score_tag(e)})"
                     for mark, e in st["highlights"])


def _split_calendar(entries: list[dict], unit: str) -> list[tuple[str, list[dict]]]:
    """按**自然周 / 自然月**分段（时间梯度视图的周段、月段用）。

    原来是 `_split_cells_fixed(gsec)`：从最老那条起算的 7 天块 / 30 天块。
    结果是 1 月 31 日和 2 月 1 日可能落进同一个「月」，
    而 1 月 1 日和 1 月 31 日反倒分成两个（codex 复核 #8）。
    人说「按周」「按月」指的是日历上的周和月，不是「从某条记忆起算的 168 小时」。
    """
    keyf = _w.year_week if unit == "week" else _w.year_month
    slices: dict[tuple, list[dict]] = {}
    for e in entries:
        slices.setdefault(keyf(e["ts"]), []).append(e)
    this_year = _w.now().year
    labeled = []
    for k in sorted(slices):
        cell = slices[k]
        a, b = cell[0]["ts"], cell[-1]["ts"]
        if unit == "month":
            label = f"{a.year}-{a.month:02d}" if a.year != this_year else f"{a.month} 月"
        else:
            day_a = a.strftime("%m-%d") if a.year == this_year else a.strftime("%Y-%m-%d")
            label = day_a if a.date() == b.date() else f"{day_a}~{b.strftime('%m-%d')}"
        labeled.append((label, cell))
    return labeled


# ⚰️ 2026-08-18（E3）：这儿原来硬写着两个人的名字（_ME_NAMES / _HER_NAMES）。
#    房间砍成四间之后 room_implied_tags() 已经恒返回空集，这两个集合**一处没人用**
#    ——连着名字一起删了。要挡名字的那条路在 dehydrator._person_tags()，读配置。


def room_implied_tags(room: str) -> set[str]:
    """筛了房间之后，**房间定义里已经包含的人**，标签里再出现就是零信息。

    2026-08-05 她一句话点破：「我们之间的事不就是AI和主人。**room 里面就有，
    又算在 tag 里面了**」。所以那三行 75/46/31 条的标签全是「主人·AI·爱」——
    不是它们高频，是它们在**重复房间已经说过的东西**。

    这跟她 8-04 纠我的是同一条（「筛过的维度不再重复说」），当时纠的是每行后面
    重复几十遍的 `I/EVENT/SELF/WHO 100%`；这次是从房间名延伸到**房间隐含的人**。

    ⚠️ 没筛房间时一个都不去 —— 那时候「主人」是真有信息的（它在区分这条是关于谁）。

    ------------------------------------------------------------
    🔴 二改 A 件之后这个函数**暂时退化成空集**，别当成它坏了：
    房间砍成四间以后，房间名里**再也不隐含任何人**（`EVENT/SELF` 只说「我在场」，
    没说跟谁）。「关于谁」整个搬去了 `subjects` 字段。
    所以这条去重的正确落点也跟着搬家：等第 5 步 subjects 接上检索之后，
    改成「筛了 subjects=主人，标签里的『主人』就是零信息」——**同一条判据，换个字段**。
    在那之前返回空集是对的：现在去掉名字反而会**误删真信息**
    （房间已经不保证那个人在场了）。
    """
    return set()


def common_tags(entries: list[dict], ratio: float = 0.55) -> set[str]:
    """这批结果里**几乎人人都有**的标签 —— 它们是这批的定义，不是某一格的特征。

    2026-08-05 她指出来的：筛 room=I/EVENT/SELF/WHO（我们之间）之后，每一格的
    标签都是「主人·AI·爱」——那是这个房间的定义，零信息。75 条压成的那一行，
    看完等于没看。

    这跟她 8-04 纠过的是同一个毛病：当时纠的是 room（每行后面跟一个
    `I/EVENT/SELF/WHO 100%`，重复几十遍），我们把 room 去重了，**tag 漏了**。
    ⚠️ 这一支只当兜底（阈值保守）。真正管用的是 room_implied_tags()——
    她 8-05 一句话点破：按频率永远抓不准（「主人」只在 50% 的记忆里，
    却在几乎每一格都排第一），因为问题根本不是"高频"。
    纯统计，不过模型。
    """
    cnt = Counter()
    for e in entries:
        for t in {str(x) for x in (e["meta"].get("tags") or [])}:
            if not t.startswith(_SYS_TAG_PREFIXES):
                cnt[t] += 1
    return {t for t, n in cnt.items() if n >= max(2, len(entries) * ratio)}


def _pick_tags(st: dict, drop: set[str], k: int = 2) -> list[str]:
    """挑 k 个有区分度的标签；全被 drop 掉就退回原样（宁可重复也别空着）。"""
    kept = [t for t, _ in st["tags"] if t not in drop]
    return (kept or [t for t, _ in st["tags"]])[:k]


def _pick_tags_n(st: dict, drop: set[str], k: int = 2) -> list[tuple[str, int]]:
    """同 _pick_tags 但带数量（E3：`床 3` 和 `床 30` 是两种日子，没有数量看不出来）。"""
    kept = [(t, n) for t, n in st["tags"] if t not in drop]
    return (kept or list(st["tags"]))[:k]


def _far_line(label: str, st: dict, fixed_room: bool = False,
              drop: set[str] | None = None) -> str:
    """一段塌成一句。她筛过的维度、以及这批共有的标签，都不再重复说。"""
    drop = drop or set()
    bits = [f"{label} · {st['n']}条"]
    if not fixed_room and st["房间话"]:
        bits.append(st["房间话"])
    tags = _pick_tags_n(st, drop)
    if tags:
        bits.append(标签人话(tags, 2))
    if st["情绪话"]:
        bits.append(st["情绪话"])
    line = " ▏".join(bits)
    if st["highlights"]:
        mark, e = st["highlights"][0]
        # 22 → 40：这一行确实要塞统计+标签+情绪+一个代表，不能完全不截；
        # 但 22 字等于没给内容（8-06 傍晚一起放宽的）
        line += (f" {mark}{身份牌(e['meta'])}{_label_of(e)[:40]}"
                 f"({_short_id(e['id'])}{_score_tag(e)})")
    return line


def _fmt_far_line(label: str, st: dict) -> str:
    """远处一段一句（她 8-03 定的梯度：近处逐条清晰，远处塌成一句印象）。

    ⚠️ 现在没有调用方（浏览路的远端 8-05 改成整段不分格了）——留着当那一档的形状，
    B 件顺手把它的机器读数也换成人话，别让死代码把旧词汇表带回来。
    """
    bits = [f"{label} · {st['n']}条"]
    for x in (st["房间话"], 标签人话(st["tags"], 2), st["情绪话"]):
        if x:
            bits.append(x)
    line = " ▏".join(bits)
    if st["highlights"]:
        mark, e = st["highlights"][0]
        line += (f" {mark}{身份牌(e['meta'])}{_label_of(e)[:20]}"
                 f"({_short_id(e['id'])}{_score_tag(e)})")
    return line


def _fmt_card(label: str, st: dict) -> str:
    """一张卡（1~3 格那一档，也是 breath 中期那一块）。**B 件：三行全说人话。**

    行头的词儿也跟着换：`房间/标签/底色` 是数据库的分栏名，
    「在做什么 / 围着什么转 / 心里」是人在说的话。
    """
    lines = [f"{label} · {st['n']}条 " + "─" * 24]
    lines.append("在做什么   " + (st["房间话"] or "-"))
    # 标签这一行**带数量**（她 8-05：`床 3` 和 `床 30` 是两种日子）；
    # 行头已经说了「围着什么」，值里就不再套一遍「围着…转」
    lines.append("围着什么   " + (标签人话(st["tags"], 6, 带数=True, 框=False) or "-"))
    lines.append("心里       " + (st["情绪话"] or "-")
                 + ("  " + " ".join(f"[[{s}]]" for s in st["seeds"]) if st["seeds"] else ""))
    if st["highlights"]:
        first = True
        for mark, e in st["highlights"]:
            prefix = "扎眼的     " if first else "           "
            # 摘要**不截**（她 2026-08-06 傍晚在手机上抓到的）：这张卡就是 breath 的
            # 「中期」那一块（走 slices=1），原来截在 46 字，三条里两条断在半截
            # （「…她验收提五刀全对，并」）。
            # 📌 判据：**塌缩塌的是条数，不该塌「每条讲了什么」**。摘要本来就只有 60 字上下。
            lines.append(f"{prefix}{mark} {身份牌(e['meta'])}{_label_of(e)} "
                         f"({_short_id(e['id'])}{_score_tag(e)})")
            first = False
    return "\n".join(lines)


def _fmt_list(entries: list[dict]) -> str:
    """逐条列（C 档）。**房间码撤掉、mind 戴 🧠**（D 件的显示形态终稿）。"""
    lines = []
    for e in entries:
        # 同上：逐条列就是给内容的地方，不截
        lines.append(f"{_short_id(e['id'])}{_score_tag(e)}  {身份牌(e['meta'])}{_label_of(e)}  "
                     f"{e['ts'].strftime('%m-%d')}")
    return "\n".join(lines)


# 四间房的中文名。查之前先 normalize_room()——老数据的十间名字翻成新的再查，
# 屏幕上就只有一套词汇表（用 _room_cn() 而不是直接 .get()）。
ROOM_CN: dict[str, str] = {
    "EVENT/SELF":  "我亲历的",
    "EVENT/WORLD": "我听说看到的",
    "MIND/TRAITS": "我是什么样",
    "MIND/VIEWS":  "我怎么看",
}


def _room_cn(room) -> str:
    """房间的中文名，新旧名字都认；不认识的原样回显（别把它变成空白）。"""
    r = normalize_room(room)
    if r:
        return ROOM_CN.get(r, r)
    return str(room or "") or "没房间"


def entry_json(e: dict) -> dict:
    """一条记忆的前端形状。字全是存的时候写下的，这里只搬不改。"""
    meta = e["meta"]
    def _f(key, default):
        try:
            return float(meta.get(key, default))
        except (TypeError, ValueError):
            return default
    tags = [str(t) for t in (meta.get("tags") or [])]
    # 前端一律拿到**新四间**的名字（老数据在这儿归一），否则面板上会同时出现两套房名
    room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
    return {
        "id": e["id"],
        "short": _short_id(e["id"]),
        "label": _label_of(e),
        "room": room,
        "room_cn": _room_cn(meta.get("room")),
        "ts": e["ts"].isoformat(timespec="seconds"),
        "date": e["ts"].strftime("%Y-%m-%d"),
        "importance": _f("importance", 5.0),
        "v": _f("valence", 0.5),
        "a": _f("arousal", 0.3),
        "pinned": bool(meta.get("pinned")),
        # 遗忘三档（D7）：只给 dashboard 那张皮看（她得判断引擎干得对不对）；
        # 文字皮（breath/recall 输出）一个字都不提——遗忘是悄然发生的
        "decay": str(meta.get("decay_stage") or "") or "alive",
        "kind": "mind" if is_mind_room(meta.get("room")) else "event",
        "status": str(meta.get("status") or ""),
        "when": str(meta.get("when") or ""),
        "tags": [t for t in tags if not t.startswith(_SYS_TAG_PREFIXES)],
        # 相关度：只有走 query 门时才有；None = 按 when/room/tag 筛进来的（见 _score_tag）
        "score": e.get("score"),
        "dup_of": [t.split(":", 1)[1] for t in tags if t.startswith("疑似同件:")],
        "seeds": sorted({s for s in _SEED_RE.findall(e["content"]) if s in _SEED_NAMES}),
    }


def _stats_json(st: dict) -> dict:
    """_cell_stats 的 JSON 形状（highlights 里的 entry 换成 id+文字）。"""
    return {
        "n": st["n"],
        # 人话两句也给前端（B 件）：文字皮和面板说的是**同一句话**，
        # 但面板照旧拿到精确的百分比和 V/A —— 要数字的地方数字一个不少。
        "房间话": st["房间话"],
        "情绪话": st["情绪话"],
        "rooms": [{"room": r, "room_cn": _room_cn(r), "n": n,
                   "pct": round(100 * n / st["n"]) if st["n"] else 0}
                  for r, n in st["rooms"]],
        "tags": [{"tag": t, "n": n} for t, n in st["tags"]],
        "v": st["v"], "a": st["a"], "seeds": st["seeds"],
        "highlights": [{"mark": mark, "id": e["id"], "short": _short_id(e["id"]),
                        "label": _label_of(e), "score": e.get("score")}
                       for mark, e in st["highlights"]],
    }


# ============================================================
# 两条路：浏览（无 query）· 搜索（有 query）
# ============================================================

def _pick_reps(far: list[dict], k: int = _BROWSE_REP_MAX) -> list[tuple[str, dict]]:
    """远端整段挑 k 条代表。

    她 8-05 的原话：「更远的就写**前段时间**，给 2~3 条前 2~3 周的。」
    所以优先在最近三周里挑；三周内一条都没有（查的是老早以前）才退回整段。
    挑法沿用现成的「异的 + 重的」（_cell_stats 的 highlights），不另起一套。
    """
    cutoff = _w.today() - timedelta(days=_BROWSE_REP_DAYS)
    pool = [e for e in far if e["ts"] >= cutoff] or far
    return _cell_stats(pool)["highlights"][:k]


def _rep_line(mark: str, e: dict) -> str:
    # 60 而不是 30：她 8-05 夜说「recall 返回的摘要不是完整的」——代表条目是那段时间
    # 唯一给出内容的地方，截一半等于没给。日期留着当把手（钻进去用），不是分类。
    return (f"  {mark}{身份牌(e['meta'])}{_label_of(e)[:60]}({_short_id(e['id'])}) "
            f"{e['ts'].strftime('%m-%d')}")


def _big_line(meta: dict, content: str, bid: str) -> str:
    span = _big.fmt_span(meta)
    return f"  ◈{_big.first_line(content)[:38]}({_short_id(bid)})" + (f" {span}" if span else "")


def _cell_span(cell: list[dict]) -> tuple[datetime, datetime]:
    """一格的时间范围，**半开区间**：右边界推到最后那条的第二天。

    🔴 两个都是坑，都踩过：
    ① `entries` 是**新→旧**排的，所以 `cell[0]` 是最新那条、`cell[-1]` 是最旧那条——
       直接当 `(t0, t1)` 传给 `covering()` 就是把起止**倒过来**给，结果只有
       「完整包住整段」的时期才露头（8-17 修：这是 8-05 起就在的静默漏显示）。
    ② 一条 `when=2026-12-25` 的记忆，`ts` 是那天**零点**。右边界取 `max(ts)` 的话
       区间退化成一个点，而 `covering()` 判的是重叠（`s >= t1` 就跳），
       起点正好在那天零点的时期会被自己盖着的那天挡在外面。
    """
    ts = [e["ts"] for e in cell]
    return min(ts), max(ts) + timedelta(days=1)


async def _big_lines(t0, t1, seen: set[str]) -> list[str]:
    """跟这一格有重叠的**时期**，每条一行标题（`◈那阵子在做什么 (id) 8-13~8-16`）。

    🔴 8-17 14:30 终稿之后时期是**纯命名层**：它不写 `covered_by`，所以走不了
    `_gist_lines` 那条「谁被盖了」的路——它的成员是**现场按日期算**的，
    显示层也就该现场算：`_bigevent.covering()`（老机制，一行没改）。
    ⚠️ 一条时期在一次渲染里只出头一次（`seen`）：它盖着三天不等于该说三遍。
    ⚠️ 这是**只多一行**：底下逐条区和统计一个字不少（时期不塌任何行）——
       开工单 2.3「信息量只能变多不能变少」的落点。
    """
    out: list[str] = []
    for meta, content, bid in await _big.covering(t0, t1):
        if bid in seen:
            continue
        seen.add(bid)
        out.append(_big_line(meta, content, bid))
    return out


async def _gist_lines(entries: list[dict], skip: set[str] | None = None) -> list[str]:
    """这一格里被盖住的那些，是被哪几条 gist 盖的 → 每条 gist 一行标题。

    **这就是「多一行」那一行**（开工单 2.3）：
        08-13~08-16  「那几天在青岛做讲义」   ← gist（新增）
          56条 · 房间… · 标签… · 突出…        ← 原来有什么，一个字不少

    🔴 判据：被盖的单条不在逐条区单独出现，但**它们去哪儿了必须看得见** ——
    所以这一行带着 gist 的 id（下钻的把手）和「盖着这格里的几条」。
    信息量只能变多不能变少：少了 N 行单条，多了一行标题 + 一个能钻的 id。
    """
    covered: dict[str, int] = {}
    for e in entries:
        # 交叉（她 8-05 第六条）：一条可以同时被两条主线盖着 → 两条 gist 标题都数它
        for gid in _F.covers_of(e["meta"]):
            if gid and gid not in (skip or set()):
                covered[gid] = covered.get(gid, 0) + 1
    out: list[str] = []
    for gid, n in sorted(covered.items(), key=lambda kv: -kv[1]):
        b = await rt.bucket_mgr.get_including_archive(gid)
        if not b:
            out.append(f"  ▣（盖着这里 {n} 条的 gist {_short_id(gid)} 查无此桶——链断了，报给AI）")
            continue
        meta = b.get("metadata", {}) or {}
        head = _big.first_line(str(b.get("content") or ""))[:38]
        mark = "◈" if _big.is_big(meta) else "▣"
        span = _big.fmt_span(meta) if _big.is_big(meta) else ""
        out.append(f"  {mark}{head}({_short_id(gid)})"
                   + (f" {span}" if span else "") + f" ▏盖着这里 {n} 条")
    return out


async def _render_browse(entries, gates, room, tag) -> str:
    """浏览：在看，想不起来有什么。**远端狠砍**——三天内照旧，更远的整段一句「前段时间」。

    她 8-05 晚指出 `07-13~07-19` 这种精确日期段别扭：**那是机器的分法，人只会想
    「前段时间」**。所以远端**直接取消分格**，整段挑 2~3 条代表。
    顺带治了另一个毛病：改之前 **1 条和 75 条占同样大的位置**。
    """
    now = _w.now()
    today = _w.today()
    dn = today - timedelta(days=_BROWSE_NEAR_DAYS - 1)   # 今天/昨天/前天
    tomorrow = today + timedelta(days=1)

    future = [e for e in entries if e["ts"] >= tomorrow]
    near = [e for e in entries if dn <= e["ts"] < tomorrow]
    far = [e for e in entries if e["ts"] < dn]

    # 她筛掉的维度就是常量，别再说一遍（8-04 纠的）；这批共有的标签同理（8-05 补的）。
    fixed_room = bool(room.strip())
    drop = (room_implied_tags(room)
            | common_tags(entries)
            | ({tag.strip()} if tag.strip() else set()))

    def _head(label: str, st: dict) -> str:
        bits = [f"── {label} · {st['n']}条"]
        if not fixed_room and st["房间话"]:
            bits.append(st["房间话"])
        # E3：近端标签也带数量（`床 3` 和 `床 30` 是两种日子）。
        # 带了数量就**不套「围着…转」那个框**——「围着烟 1、尖塔 2转」读起来是坏的，
        # 词和数字本来就是原样给的，框子只在不带数的地方帮忙。
        tags = _pick_tags_n(st, drop)
        if tags:
            bits.append(标签人话(tags, 2, 带数=True, 框=False))
        if st["情绪话"]:
            bits.append(st["情绪话"])
        seeds = "".join(f"[[{x}]]" for x in st["seeds"])
        return " ▏".join(bits) + (" " + seeds if seeds else "")

    lines = [f"〔{gates}〕{len(entries)} 条 · 新→旧"]
    # 一次渲染里每条时期只出头一次（近端出过了，远端那段就不再重复）
    时期出过: set[str] = set()

    # 还没到的日子：按自然月塌，多远都只占几行
    if future:
        lines.append("— 还没到的 —")
        for label, cell in reversed(_split_calendar(future, "month")):
            lines.append(_far_line(label, _cell_stats(cell), fixed_room, drop))
            lines.extend(await _big_lines(*_cell_span(cell), 时期出过))
            lines.extend(await _gist_lines(cell))

    # 三天内：照旧（一天一行 + 突出的点另起一行）
    if near:
        days: dict[str, list] = {}
        for e in near:
            days.setdefault(e["ts"].strftime("%m-%d"), []).append(e)
        for label in sorted(days, reverse=True):
            st = _cell_stats(days[label])
            lines.append(_head(label, st))
            # 时期/gist 标题在突出的点**上面**：先说这几天叫什么，再说里面哪条扎眼
            lines.extend(await _big_lines(*_cell_span(days[label]), 时期出过))
            lines.extend(await _gist_lines(days[label]))
            hl = _fmt_highlights(st)
            if hl:
                lines.append(hl)

    # 更远的：**不分格**，整段一句「前段时间」+ 2~3 条代表（有大 event 就换成大 event）
    if far:
        st = _cell_stats(far)
        a = far[0]["ts"]
        # 标题里**不再报精确日期段**（她 8-05 夜：说了「前段时间」还挂个 `08-01~08-02`，
        # 自相矛盾——那还是机器的分法）。日期只留在每条代表后面，那是把手不是分类。
        # ⚠️ 例外（E4，机制② 第 6 条）：筛了 room/tag = 在**追一件事**——
        # 「这件事从什么时候到什么时候」正是要问的东西，跨度要给。
        if fixed_room or tag.strip():
            span_txt = f"{a.strftime('%m-%d')} ~ {far[-1]['ts'].strftime('%m-%d')}"
            bits = [f"— 前段时间（{span_txt}）· {len(far)}条"]
        else:
            bits = [f"— 前段时间 · {len(far)}条"]
        # 🔴 **带数量的标签分布**（她 8-05 夜点破的，这一行是远端唯一给「那阵子在过什么日子」
        # 的地方）：`亲密关系 30 · 接纳 12` 和 `亲密关系 3 · 接纳 2` 意思完全相反，
        # 而没有数量时这两行长得一模一样。数量 breath 一直有，是这儿把它扔了。
        #
        # ⚠️ 这儿**只 drop 房间隐含的人**，不 drop common_tags（高频词）——
        # 高频词在别处是噪音（每格都是「主人·AI·爱」），但在这一行**它就是答案**。
        # 她自己说过「按频率永远抓不准」：带上数量之后，频率不再是抓手，是内容。
        drop_lite = room_implied_tags(room) | ({tag.strip()} if tag.strip() else set())
        dist = 标签人话([(t, n) for t, n in st["tags"] if t not in drop_lite],
                        5, 带数=True, 框=False)
        if dist:
            bits.append(dist)
        if st["情绪话"]:
            bits.append(st["情绪话"])
        lines.append(" ▏".join(bits) + " —")
        # 她的原话：「给 2~3 条前 2~3 周的，**如果有大事件就换成大事件**。」
        # 大 event 先占位，剩下的位置才用代表条目补 —— 少于 3 条时不空着。
        # （第 5 条：盖，不替代 —— 上面那行统计和突出的点一个都没少，只是多一句话。）
        盖这段的 = await _big.covering(*_cell_span(far))
        covers = [x for x in 盖这段的 if x[2] not in 时期出过]
        for meta, content, bid in covers[:_BROWSE_REP_MAX]:
            时期出过.add(bid)
            lines.append(_big_line(meta, content, bid))
        # 施工 3：按时间盖上来的（上面那几行）之外，**按 cover 名单**盖住这段里某几条的
        # gist 也要出头——它可能是「一组 id」圈出来的，跟时间范围对不上，
        # 光靠 covering() 那条路永远看不见它。已经出现过的不重复。
        lines.extend(await _gist_lines(far, skip={bid for _m, _c, bid in 盖这段的}))
        for mark, e in _pick_reps(far, _BROWSE_REP_MAX - len(covers[:_BROWSE_REP_MAX])):
            lines.append(_rep_line(mark, e))
        # 第 9 条：触发点挂在「recall 一段时间」上 —— 这一刻我本来就在回看，
        # 材料摊在眼前，「这阵子好像在做一件什么事」是自然浮上来的，
        # 不需要我刻意记得去想。所以提示只在**真没人盖着**的时候出现一次。
        # ⚠️ 判据是 `盖这段的`（真的有没有时期），不是 `covers`（这一格还没出头的那些）——
        #    近端已经把那条时期说过了不等于「这段没人盖」。
        if not 盖这段的 and (now - a).days >= 7:
            lines.append("  （这段时间上没有时期盖着。真觉得是在做一件什么事就写下来："
                         'grow(kind="big", room=…, text=…, when="起..止")）')

    lines.append("（钻：缩小 when / 加 room·tag / slices=N 控格数；看原文：拿 id 搜）")
    return chr(10).join(lines)


def _topk行(账: dict | None) -> str:
    """top-k 砍掉了几条 —— **挡了什么看得见**（D 件收紧 top-k 的配套）。

    她 8-15 的判据：query 词多 = 向量平均 = 找不准。所以这一行不光报数，
    还把出路说清楚：**用一两个核心词、她当时的原话**。
    """
    n = int((账 or {}).get("topk砍掉") or 0)
    if n <= 0:
        return ""
    k = int((账 or {}).get("topk") or _SEARCH_TOPK)
    return (f"── 还有 {n} 条命中被 top-{k} 挡在外面（按相关度截的）——"
            "词多了向量就取平均，换一两个核心词、用她当时的原话再搜一次")


def _eff_score(e: dict, floor: float) -> float:
    """有效分：字面命中 → max(分数, 线)。保底不是加分——低的托上来、高的不动。

    它在家底表里的名字就叫「字面命中保底」，要的是**别漏掉**不是排第一（机制② C2）。
    """
    s = e.get("score") or 0.0
    return max(s, floor) if e.get("literal") else s


def _render_search(entries, gates, floor: float = None, 账: dict | None = None) -> str:
    """搜索：在找，知道要什么。**这是有 query 时的默认视图**（施工 5 · D 件）。

    过线的按时间排（新→旧）、每条带分数（E1）。
    🔴 **8-17 默认视图翻回这一条**：query 单独原来会切到画面簇，
    「找那件事」反而要多绕一道。她 5.5 定的：**默认按时间＋分数排，场景簇显式要**
    （`view="scene"`）。顺带治死了「`when` 一给就换一种视图形态」那个
    「一个参数管两件事」——现在 `when` 只管范围，形态只由 `view` 说。

    2026-08-06 改：分数只管过滤不管顺序——「找一件事」的结果摊开在时间轴上
    才看得出它是怎么一路过来的；相关度排序把 7 月和 8 月的搅在一起。
    时间是打折不是门（机制② 第 5 条）：老的默认不出来靠遗忘打折实现，
    真在找它、撞得准，它扛得住打折冲上来。

    线以下的不列——她 8-05 的原话：「如果说没有直接关联那不如就不弹出来」。
    但它们**不是消失**，末尾一行带上「还有多少 · 最高几分 · 最早哪条讲什么」（E2）
    ——顺带回答了「这件事什么时候开始」。
    """
    floor = RELEVANCE_FLOOR if floor is None else float(floor)
    hit = [e for e in entries if _eff_score(e, floor) >= floor]
    below = [e for e in entries if _eff_score(e, floor) < floor]
    top_below = max(((e.get("score") or 0.0) for e in below), default=0.0)

    if not hit:
        return (f"〔{gates}〕**没有相关的记忆。**\n"
                f"够到 {len(below)} 条，但最高才 {top_below:.1f} 分（线在 {floor:.0f}）——"
                "都只是沾边，不弹出来。\n"
                "真觉得该有：换她说过的原话当 query（别造词），或者用 when/room 直接翻。")

    lines = [f"〔{gates}〕{len(hit)} 条 · 按时间 新→旧（线 {floor:.0f}，分数只管过滤）"]
    for e in sorted(hit, key=lambda x: x["ts"], reverse=True):
        # 搜索路的摘要**不截**（她 8-05 夜指出来的：截了就判断不出这条是不是要找的，
        # 而搜索的整个意义就是判断）。浏览路继续截——那儿要的是印象不是内容。
        # 🧠 = 认知；不戴牌的就是发生的事（房间码撤掉，D 件终稿）。
        lines.append(f"{_eff_score(e, floor):5.1f}  {身份牌(e['meta'])}{_label_of(e)}"
                     f"  ({_short_id(e['id'])})  {e['ts'].strftime('%m-%d')}")
    if below:
        earliest = min(below, key=lambda x: x["ts"])
        lines.append(f"── 另有 {len(below)} 条在线下（最高 {top_below:.1f}，"
                     f"最早 {earliest['ts'].strftime('%m-%d')}：「{_label_of(earliest)[:40]}」）——"
                     "多半只是沾边，没列")
    lines.append(_topk行(账))
    lines.append("（看原文：拿 id 搜；换个说法再搜：用她的原话，别造词）")
    return chr(10).join(x for x in lines if x)


def _render_scene_clusters(entries, gates, floor: float = None, 账: dict | None = None) -> str:
    """画面式回忆（G2，机制② 第 7 条）：这件事是怎么一路过来的。

    🔴 **2026-08-17 起要显式要**（施工 5 · D 件）：`recall(query=…, view="scene")`。
    原来它是「query 单独」的默认视图，于是同一个 query 加不加 `when` 会换一种
    视图形态——**一个参数管两件事**，正是 5.5 点的那个乱源。
    默认翻回「按时间＋分数」（找那件事），画面簇留给「这件事怎么一路过来的」。

    她脑子里的结构不是平铺列表，是**有主有次的簇**——代表底下还能挂从属画面
    （她的例子：学代码 → ①做表格那天讲代码 ②机场和 GPT 聊怎么系统学
    （副画面：飞机座位上让 GPT 出文档）③电脑桌前拿 Mac 看讲义）。
    簇的抓手就是场景锚点（tags 现在只装它）：同一簇 = 命中的记忆里共享画面词的。
    簇按最早那条的时间排（「一路过来」是从头讲起）；簇里代表 = 分数最高的。
    """
    floor = RELEVANCE_FLOOR if floor is None else float(floor)
    hit = [e for e in entries if _eff_score(e, floor) >= floor]
    below = [e for e in entries if _eff_score(e, floor) < floor]
    top_below = max(((e.get("score") or 0.0) for e in below), default=0.0)
    if not hit:
        return (f"〔{gates}〕**没有相关的记忆。**\n"
                f"够到 {len(below)} 条，但最高才 {top_below:.1f} 分（线在 {floor:.0f}）——"
                "都只是沾边，不弹出来。\n"
                "真觉得该有：换她说过的原话当 query（别造词），或者用 when/room 直接翻。")

    def _vis_tags(e) -> set[str]:
        # 簇的抓手也只认人话场景词：机器腔标签（`aspect:patterns` 那类）
        # 会把毫不相干的记忆硬串成一个「画面」（8-17 凌晨的教训）
        return {str(t) for t in (e["meta"].get("tags") or []) if 是人话标签(t)}

    # 贪心成簇：按分数从高到低认主画面，把跟它共享场景词的收作从属画面
    ranked = sorted(hit, key=lambda e: _eff_score(e, floor), reverse=True)
    unassigned = list(ranked)
    clusters: list[list[dict]] = []
    while unassigned:
        head = unassigned.pop(0)
        ht = _vis_tags(head)
        members = [head]
        if ht:
            rest = []
            for e in unassigned:
                if ht & _vis_tags(e):
                    members.append(e)
                else:
                    rest.append(e)
            unassigned = rest
        clusters.append(members)

    clusters.sort(key=lambda c: min(e["ts"] for e in c))  # 一路过来：从头讲起
    lines = [f"〔{gates}〕{len(hit)} 条 · {len(clusters)} 个画面 · 一路过来（线 {floor:.0f}）"]
    for c in clusters[:8]:
        rep = max(c, key=lambda e: _eff_score(e, floor))
        kids = sorted((e for e in c if e is not rep), key=lambda e: e["ts"])
        shared = set.intersection(*(_vis_tags(e) for e in c)) if len(c) > 1 else set()
        label = ("·".join(sorted(shared)[:2]) + " ") if shared else ""
        lines.append(f"■ {rep['ts'].strftime('%m-%d')} {label}"
                     f"{_eff_score(rep, floor):5.1f}  {身份牌(rep['meta'])}{_label_of(rep)}"
                     f"  ({_short_id(rep['id'])})")
        for e in kids[:3]:
            lines.append(f"   └ {e['ts'].strftime('%m-%d')}  {身份牌(e['meta'])}"
                         f"{_label_of(e)[:56]}  ({_short_id(e['id'])})")
        if len(kids) > 3:
            lines.append(f"   └ …还有 {len(kids) - 3} 条同画面的")
    if len(clusters) > 8:
        n_rest = sum(len(c) for c in clusters[8:])
        lines.append(f"…还有 {len(clusters) - 8} 个画面（{n_rest} 条）——加 when 缩小段落再看")
    if below:
        earliest = min(below, key=lambda x: x["ts"])
        lines.append(f"── 另有 {len(below)} 条在线下（最高 {top_below:.1f}，"
                     f"最早 {earliest['ts'].strftime('%m-%d')}：「{_label_of(earliest)[:40]}」）")
    lines.append(_topk行(账))
    lines.append("（要平铺的时间轴：去掉 view；看原文：拿 id 搜）")
    return chr(10).join(x for x in lines if x)


async def recall_data(when: str, room: str, tag: str, query: str,
                      floor=None, view: str = "") -> dict:
    """recall 的**另一张皮**：同样的四个门、同样的 _collect/_cell_stats，吐 dict 给前端。

    文字那张皮是 recall_core()。两张皮共用底下同一份收集+统计，绝不各算各的——
    页面上看到的「主色调」和AI睁眼看到的必须是同一个数，不然就是两个系统了。
    ⚠️ `by` 2026-08-17 砍了（C 件）；`view` 只影响文字皮的形态，这张皮照旧给全量。
    """
    entries, err, 账 = await _collect(when, room, tag, query)
    if err:
        return {"ok": False, "error": err, "entries": [], "total": 0}
    if not entries:
        return {"ok": True, "entries": [], "total": 0, "stats": None,
                "gates": {"when": when, "room": room, "tag": tag, "query": query,
                          "view": view}}
    st = _cell_stats(entries)
    # 关联度线：搜索时才有意义（没走 query 门就没有分数）。
    # 她要在 dashboard 上自己拖着看 —— 所以线是**每次请求带进来的**，
    # 不写死也不落库。在见过真实分布之前定的任何阈值都是拍脑袋（8-05 的教训）。
    fl = RELEVANCE_FLOOR if floor is None else float(floor)
    payload = []
    for e in reversed(entries):  # 新→旧，跟文字皮同一个方向
        j = entry_json(e)
        if e.get("score") is not None:
            # 字面命中保底（C2）：max(分数, 线)，前端看到的就是生效的分
            j["score"] = round(_eff_score(e, fl), 2)
            j["literal"] = bool(e.get("literal"))
        payload.append(j)
    return {
        "ok": True,
        "total": len(entries),
        "stats": _stats_json(st),
        "entries": payload,
        "gates": {"when": when, "room": room, "tag": tag, "query": query, "view": view},
        "floor": fl,
        "floor_default": RELEVANCE_FLOOR,
        # top-k 砍掉几条：面板上也得看得见（文字皮末尾那行的同一个数）
        "topk": 账.get("topk"),
        "topk_dropped": 账.get("topk砍掉", 0),
        "below": sum(1 for e in entries
                     if e.get("score") is not None and _eff_score(e, fl) < fl),
    }


async def recall_core(when: str, room: str, tag: str, query: str,
                      max_cells: int = _CELL_MAX, floor=None, view: str = "") -> str:
    """文字那张皮。**参数账（5.4）**：when / room / tag / query / slices / view，就这六个。

    🔪 **`by` 2026-08-17 整个砍了**（C 件，代码和工具描述一起）：
       `by="touched"` —— 她：「我们没有『消化』这个动作」；
       `by="回看"`   —— `slices` 覆盖（slices=N 就是「那段我要看多粗／多细」）。
       判据：**每个参数必须对得上一句我心里真会冒出来的话。**
    🆕 `view="scene"`（D 件）：场景簇从默认变成**显式要**。
    """
    view = str(view or "").strip()
    if view and view != "scene":
        return (f'view 无效：{view}。现在只有一种："scene"'
                "（按共享场景词聚成簇，看这件事怎么一路过来的）。"
                "不给 view = 默认按时间＋分数排，找那件事。")
    if view and not query.strip():
        return ('view="scene" 要跟 query 一起用——簇是按**命中的记忆**共享的场景词聚的，'
                "没有 query 就没有命中，也就没有画面。"
                "只想翻一段时间：recall(when=…)（要更粗/更细加 slices=N）。")

    # --- id 直查：query 就是一个完整 bucket_id → 返回这一条的逐字原文 + 全部元数据 ---
    # 这就是「点进去看原文」那扇门（C 档列表给摘要，拿 id 从这儿进）。
    q = query.strip()
    if re.fullmatch(r"[0-9a-f]{6,11}", q):
        # 半截 id：唯一前缀匹配；多个候选就列出来；没有就明说（不落语义搜索）
        allb = await rt.bucket_mgr.list_all(include_archive=True)
        cand = [str((bb.get("metadata") or {}).get("id") or "") for bb in allb]
        cand = sorted({cid for cid in cand if cid.startswith(q)})
        if len(cand) == 1:
            q = cand[0]
        elif len(cand) > 1:
            return "半截 id 撞了 " + str(len(cand)) + " 个：" + " / ".join(cand[:8]) + "。给完整的。"
        else:
            return f"查无此桶：{q}（id 形状但没匹配——可能已物理删除或打错）。"
    if re.fullmatch(r"[0-9a-f]{12}", q) or re.fullmatch(r"feel_\d{12}_V\d{3}(_\d+)?", q):
        b = await rt.bucket_mgr.get_including_archive(q)
        if not b:
            return f"查无此桶：{q}（id 形状但不存在——可能已物理删除或打错，不做语义联想）。"
        if b:
            meta = b.get("metadata", {}) or {}
            lines = [f"═ {q} · {str(meta.get('name') or '')}"]
            info = []
            for k, label in (("room", "房间"), ("when", "when"), ("valence", "V"),
                             ("arousal", "A"), ("importance", "重"), ("status", "状态")):
                v = meta.get(k)
                if v not in (None, ""):
                    info.append(f"{label}:{v}")
            tags_ = [str(t) for t in (meta.get("tags") or []) if not str(t).startswith("__")]
            if tags_:
                info.append("标签:" + ",".join(tags_[:6]))
            # E7 / 机制④ 第 2 条：from 不能只给 id——mind 只留思考产物，事件在
            # from 里，读的时候要把来源的摘要一并带出来，认知才有脚可站。
            src_lines: list[str] = []
            for fid in read_from_ids(meta):   # from 优先、triggered_by 兼容（E 件）
                src = await rt.bucket_mgr.get_including_archive(fid)
                if src:
                    smeta = src.get("metadata", {}) or {}
                    hint = str(smeta.get("summary") or smeta.get("name") or "").strip()
                    hint = re.sub(r"^[\d\- :]+", "", hint)[:60]
                    src_lines.append(f"  ← {fid}  {hint}")
                else:
                    src_lines.append(f"  ← {fid}  （查无此桶——源可能被硬删过）")
            if meta.get("supersedes"):
                info.append(f"换掉了:{meta['supersedes']}")
            if meta.get("superseded_by"):
                info.append(f"⚠️已被换版:{meta['superseded_by']}（这是旧版）")
            # 施工 3 · 下钻的另一头：**被谁盖着**（可以是好几条——交叉，她 8-05 第六条）。
            # 换版（n=1）那一档 superseded_by 上面那行已经说清楚了，不再重复。
            _sup = str(meta.get("superseded_by") or "")
            _cbs = [c for c in _F.covers_of(meta) if c != _sup]
            if _cbs:
                info.append(f"⚠️被 {'、'.join(_cbs)} 盖着（不再独立冒头；搜索和这儿照样看得见）")
            if (str(meta.get("type") or "") == "archived" or meta.get("tombstone")
                    or meta.get("deleted_at")):
                info.append("⚠️在归档区")
            lines.append(" · ".join(info))
            if src_lines:
                lines.append("来源:")
                lines.extend(src_lines)
            # 施工 3 · **这就是 unfold，不做单独的工具**（说明书 §3 D）：
            # 一条 gist 盖着谁，在这儿一条一行（id + 摘要）摊开。
            # 为什么不另起一个工具：下钻的动作已经有了（拿 id 搜），
            # 再加一个 unfold 等于给同一件事两个入口，而我只会记住其中一个。
            # 时期（时间圈法）没有名单可查——它只落名字 + 范围，成员**现场算**
            # （8-17 14:30 终稿）。下钻照样有：这儿把此刻落在范围里的那些摊开。
            if _big.is_big(meta) and str(meta.get("when") or ""):
                _t0, _t1, _serr = _F.check_span(str(meta.get("when")))
                if not _serr:
                    mem = await _F.span_members(_t0, _t1)
                    lines.append(f"范围内现在有 {len(mem)} 条（**现场算的**，没记账；"
                                 f"下钻：拿下面的 id 再搜）:")
                    for mid in mem[:30]:
                        mb = await rt.bucket_mgr.get_including_archive(mid)
                        mmeta = (mb or {}).get("metadata", {}) or {}
                        mhint = re.sub(r"^[\d\- :]+", "",
                                       str(mmeta.get("summary") or mmeta.get("name")
                                           or "").strip())[:60]
                        lines.append(f"  ◈ {mid}  {mhint}")
                    if len(mem) > 30:
                        lines.append(f"  …… 还有 {len(mem) - 30} 条")
            cov = _F.cover_ids(meta)
            if cov:
                lines.append(f"盖着 {len(cov)} 条（下钻：拿下面的 id 再搜）:")
                for cid in cov[:30]:
                    cb = await rt.bucket_mgr.get_including_archive(cid)
                    cmeta = (cb or {}).get("metadata", {}) or {}
                    chint = re.sub(r"^[\d\- :]+", "",
                                   str(cmeta.get("summary") or cmeta.get("name") or "").strip())[:60]
                    if not cb:
                        chint = "（查无此桶——可能被硬删过）"
                    # 交叉之后 covered_by 是名单：还挂着这条 gist 就不用标；
                    # 名单里没有它（被人工改动过）才标出来现在归谁
                    now_by = _F.covers_of(cmeta)
                    mark = ("" if (not cb or q in now_by)
                            else f"  ↑现在归 {'、'.join(now_by) or '（没人盖）'}")
                    lines.append(f"  ▣ {cid}  {chint}{mark}")
                if len(cov) > 30:
                    lines.append(f"  …… 还有 {len(cov) - 30} 条")
            # G3 反向链：谁从这条长出过认知/想法——「被 from」是消化程度的直接证据
            try:
                refs = await rt.bucket_mgr.referenced_by(q)
            except Exception:
                refs = []
            if refs:
                lines.append("被引用（有东西从这条长出来过）:")
                for rid in refs[:6]:
                    rb = await rt.bucket_mgr.get_including_archive(rid)
                    rmeta = (rb or {}).get("metadata", {}) or {}
                    rhint = re.sub(r"^[\d\- :]+", "",
                                   str(rmeta.get("summary") or rmeta.get("name") or "").strip())[:60]
                    lines.append(f"  → {rid}  {rhint}")
            lines.append("─" * 30)
            lines.append(str(b.get("content") or ""))  # 逐字，不截
            return "\n".join(lines)
        # id 形状但查无此桶 → 落回普通搜索（可能是半截 id 或已物理删除）
    if not (when.strip() or room.strip() or tag.strip() or query.strip()):
        return ("recall 至少给一个门：when（时间）/ room（房间）/ tag（标签）/ query（扔词搜）。"
                "例：recall(when=\"上周\") · recall(room=\"MIND\") · recall(when=\"本月\", tag=\"Home\")")

    entries, err, 账 = await _collect(when, room, tag, query)
    if err:
        return err
    if not entries:
        gates = "，".join(x for x in [when and f"when={when}", room and f"room={room}",
                                     tag and f"tag={tag}", query and f"query={query}"] if x)
        return f"这儿没有东西（{gates}）。门再开大一点试试。"

    gates = " ".join(x for x in [when and f"when={when}", room and f"room={room}",
                                 tag and f"tag={tag}", query and f"query={query}",
                                 view and f"view={view}"] if x)

    # ── 「今天」逐条（她 2026-08-08 定）：今天的事我人还在里面，不塌缩。
    #    ⚠️ 8-17 砍掉的 `by="回看"` 跟它不是一回事：那个是旧→新读一段历史
    #    （被 slices=N 覆盖了），这个是新→旧看刚发生的。
    async def _render_today(es: list[dict], g: str) -> str:
        # 🔴 时刻用 `created`（真落盘那一刻），不用 `ts`：ts 在有 `when` 时是**那天零点**，
        #    而今天存的大多带 when=今天，全列成 00:00 等于没显示。
        #    created 无后缀=UTC，parse_stamp 会转本地。
        # 🔴 **排序必须跟显示同一个口径**：先按 ts 排、再按 created 显示，
        #    今天的 event 全挤在 00:00，列出来时刻就是乱的（8-08 当场踩到）。
        def _hm(e: dict):
            return _w.parse_stamp(e["meta"].get("created")) or e["ts"]

        lines = [f"〔{g}〕{len(es)} 条 · 今天（全列，不塌缩）· 新→旧"]
        # 施工 3：被盖的今天也不单列，换成上面那一行 gist 标题。
        # **条数还是 len(es)**（一条没少），少的只是行——这是 2.3 判据的落点。
        lines.extend(await _gist_lines(es))
        for e in sorted(es, key=_hm, reverse=True):
            if _F.is_covered(e["meta"]):
                continue
            lines.append(f"{_hm(e).strftime('%H:%M')}  {身份牌(e['meta'])}{_label_of(e)}  "
                         f"({_short_id(e['id'])})")
        lines.append("（看原文：拿 id 搜；要昨天/上周那种概览就换 when）")
        return chr(10).join(lines)

    # ── 岔路口（她 8-05 晚定的）：**判据只有一条，有没有 query。**
    #    when/room/tag 是**范围**（在看），query 是**目标**（在找）。
    #    分数早就拿到了（没走 query 门就没有 score），缺的只是两条路输出长得不一样。
    #    显式 slices=N 是「我要按格数看」，两条路都不走，落到下面的老缩放。
    if max_cells == _CELL_MAX:
        if query.strip():
            # 🔴 D 件（她 8-17 定）：**默认按时间＋分数排**（找那件事）。
            #    画面式要显式要 `view="scene"` —— 而且 `when` 从此只管范围，
            #    加不加 when 视图形态一个字不变（「一个参数管两件事」那条修掉了）。
            if view == "scene":
                return _render_scene_clusters(entries, gates, floor, 账)
            return _render_search(entries, gates, floor, 账)
        # 「今天」不塌缩（她 2026-08-08 定）：今天的事我人还在里面，
        # 塌成「前段时间 + 2~3 条代表」等于把刚发生的推远。
        # 🔴 **只认「今天」**——昨天、前天照旧塌缩，那些已经是历史了。
        if when.strip() == "今天":
            return await _render_today(entries, gates)
        return await _render_browse(entries, gates, room, tag)

    gname, slices = _split_cells(entries, max_cells)

    # B · 1~3 格：完整卡
    if len(slices) <= 3:
        blocks = [_fmt_card(label, _cell_stats(cell)) for label, cell in reversed(slices)]
        return f"〔{gates}〕{len(entries)} 条 · 粒度:{gname}\n\n" + "\n\n".join(blocks) + \
            "\n\n（钻：缩小 when / 加 room·tag；看原文：拿 id 搜）"

    # A · 概览：每格两行
    lines = [f"〔{gates}〕{len(entries)} 条 · {len(slices)} 格 · 粒度:{gname} · 新→旧"]
    for label, cell in reversed(slices):
        st = _cell_stats(cell)
        lines.append(_fmt_header(label, st))
        hl = _fmt_highlights(st)
        if hl:
            lines.append(hl)
    lines.append("（钻：缩小 when / 加 room·tag；看原文：拿 id 搜）")
    return "\n".join(lines)
