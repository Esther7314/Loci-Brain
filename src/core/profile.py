"""
========================================
core/profile.py — 睁眼/档案页共用的合同源（脱壳 C，从 tools/breath/awaken.py 搬来）
========================================

施工 5（2026-08-17）把「门口那张纸」和「忽然想起」的判据从两份平行实现
（`tools/breath/awaken.py` 的睁眼 + `web/loci.py` 的档案页各写一遍）合成了一份，
写在 `awaken.py` 里、两边各自 import。她 8-17 指出这份合同源本身不属于
breath 这一个工具——`web/loci.py` 的档案页跟睁眼要读的是同一份判据，放在
`tools/breath/` 下面容易让人误以为它是 breath 专属的。按层重码这一步把它
挪来 `core/`：跟 `_fold`/`_rooms`/`_when` 这些引擎件放一起，`tools/breath/awaken.py`
和 `web/loci.py` 都改成从这儿 import，判据只有一处没变。

⚠️ **只挪家不改逻辑**：`门口那张纸()` / `事件池()` 两个函数、连同它们各自的
判据注释，逐字照搬，一个字没改；只有 import 路径跟着新家变了。

施工 6（2026-08-18，二改 §6+§8）在这份合同源上加了三块，都只在**读侧**判断，
不新增落盘字段（除了下面这处 tag 约定）：
① **want 三类钟**（§6）：类型不加字段，从 `when` 的填法本身推断——
   空＝等触发、`<N>[dwmy]` 时长记号＝有量级、`YYYY-MM-DD`＝有期限。
   方案全文见 `D:\\lento\\交接\\2-记忆系统\\开工单-Loci二改-2026-08-12.md` §6，
   第一阶段的方案报告（含真库实证 `ffe707f`/`d7cf87`/`1e12906`/`87f84e`/`d52a38`）
   在流水里能找到，这里只落地。
② **问句只问最久那条**（§6.1）：`heavy` 列表照旧全给，另外多给一个
   `heavy_question_id`——**挂得最久**（不是最重）的那条 id，渲染层拿它去
   决定哪条该问句、哪条照旧陈述。
③ **「她改过」的通知**（§8）：`事件池()` 之外新增 `她改过()`，扫「她改的」
   标签 + 没被我 fold 掉的（`_F.is_covered()`）——这就是"给我留一条通知"的
   全部机制：标签本身既是标记也是通知，没有另开一张单独的通知表。

对外暴露：门口那张纸(all_buckets, now) / 事件池(all_buckets) / 她改过(all_buckets)
========================================
"""

import re
from datetime import datetime

from utils import is_closed

from . import _fold as _F         # 被盖的不再独立冒头（施工 3）
from . import _when as _w          # 「她的今天」（本地时区）
# is_mind_room 2026-08-19 起不再进来：门口那道「准则得住在 MIND」的二次筛子拆了
from ._rooms import is_event_room
from tools.recall.core import _visible  # core→tools 反向依赖：见 core/__init__.py 顶部说明

_PROFILE_TAG = "__档案事实__"
_BIGEVENT_TAG = "__大event__"
_REMIND_DAYS = 30

# ------------------------------------------------------------
# 施工 6 · A 件：want 三类钟（二改 §6）
# ------------------------------------------------------------
# 她改事实用的这个 tag（§8）单独摆一处，跟 _PROFILE_TAG/_BIGEVENT_TAG 放一起，
# 免得两边（web/loci.py 的写口 + 这儿的读口）各写一份字符串走漏。
_EDITED_BY_HER_TAG = "她改的"

# 时长记号：`<N><单位>`，没有前缀符号（她 8-18 裁决砍掉了 `~`——没有语义的符号不留）。
# 不能跟日期格式 `\d{4}-\d{2}-\d{2}` 混：时长记号里没有横杠，天然不歧义。
_DURATION_RE = re.compile(r"^(\d+)([dwmy])$")
_DURATION_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def _量级天数(w: str) -> float | None:
    """时长记号 → 约等于多少天。认不出返回 None（不是"有量级"这一类）。"""
    m = _DURATION_RE.match(w)
    if not m:
        return None
    n = int(m.group(1))
    return n * _DURATION_UNIT_DAYS[m.group(2)]


def _量级多大声(ratio: float) -> str:
    """有量级这一类专属的曲线：挂的天数 ÷ 量级天数。

    两个校准点（她 8-18 给的，写死在 smoke 里）：
    「这周内」（7天）挂 10 天 → ratio=1.43 → 该催；
    「今年内」（365天）挂 10 天 → ratio=0.027 → 不该催。
    阈值本身是我按这两点反推的提案，不是开工单钦定的数字，
    她的实际感受说要调就调，不是这单的卡点（统筹 8-18 裁决）。
    """
    if ratio < 0.7:
        return "far"
    if ratio < 1.0:
        return "near"
    if ratio < 1.75:
        return "soon"
    return "now"


def _三类钟(meta: dict, created, now: datetime) -> tuple[str, str, str]:
    """给一条 want 判「有期限 / 有量级 / 等触发 / 旧数据待复核」，算出这一类该多大声。

    返回 (clock, loud, note)：clock 只给内部/调试用，note 是给人看的一句解释
    （非空时该在展示层露出来——目前只有"旧数据待复核"这类会有）。

    🔴 这函数只处理**已经落进"压在心头"池子**的 want（=已过期或没有未来
    when 的那些）——还没到期的有期限项走的是既有的 `reminders` 分支
    （下面 `门口那张纸()` 里那段一个字没动），这儿不重复判。
    """
    w = str(meta.get("when") or "").strip()
    if not w:
        return "等触发", "far", ""

    mag = _量级天数(w)
    if mag is not None:
        held = (now.date() - created.date()).days if created else 0
        ratio = (held / mag) if mag else 0.0
        return "有量级", _量级多大声(ratio), ""

    m = re.match(r"(\d{4}-\d{2}-\d{2})", w)
    if m:
        try:
            when_date = _w.parse_date(m.group(1))
        except ValueError:
            return "旧数据待复核", "far", "没定期限 —— when 的格式认不出来，它不会自己催你"
        # 老数据雷区（真库实证 1e12906/87f84e）：when 跟 created 是同一天，
        # 十有八九是历史上随手把"今天"填进 when 的占位，不是真期限。
        # 结构上判不清"真的当天到期"和"误填占位"，保守一侧：
        # 宁漏催不误催（她 8-13「不想把记忆丢给系统去操作」的精神，统筹 8-18 裁决接受）。
        if created and when_date.date() == created.date():
            return "旧数据待复核", "far", "没定期限 —— 存的那天顺手填成了 when，它不会自己催你"
        # 到这里说明这条 want 已经过期还没了结（没过期的会被 reminders 分支截走，
        # 不会进 heavy 池）。过期锚点换成"过期了多少天"，曲线复用现成的
        # `_压得多大声`——最小改动，她觉得"迟到"该有独立曲线再拆（统筹 8-18 裁决）。
        overdue = max(0, (now.date() - when_date.date()).days)
        return "有期限", _压得多大声(overdue), ""

    return "旧数据待复核", "far", "没定期限 —— when 的格式认不出来，它不会自己催你"


def _f_weight(x) -> float:
    """weight 读成 float，读不动就当 0.5。**0 必须活下来**（见「压在心头」那段）。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.5


def _提醒多大声(days: int) -> str:
    """⏰ 越近越大声（她 8-03 的原话）。**门槛只有这一处**，两张皮都读它。"""
    return "now" if days == 0 else "soon" if days <= 3 else "near" if days <= 14 else "far"


def _压得多大声(held: int) -> str:
    """🫀 越挂越大声（催的是「到底做不做」，不是「快到日子了」）。"""
    return "now" if held >= 60 else "soon" if held >= 30 else "near" if held >= 7 else "far"


def 事件池(all_buckets: list) -> list[dict]:
    """「忽然想起」的池子：可见的、EVENT 房间里的、**没被盖过**的事件。

    🔴 三道闸，缺一道都会静默出错：
    ① `is_event_room()` 新旧房名都认——原来两边各写 `.find("/EVENT/") > 0`，
       新房名 `EVENT/SELF` 里压根没有 `/EVENT/`，池子会**静默变空**（不报错）。
    ② 工具件（名字页 / 时期）不算记忆。
    ③ **被盖住的不进这个池子**（施工 3）：「忽然想起」是偶遇，而被我 fold 过的
       东西已经有了名字，它该以那句话的形式出现在 recall 里，不该再当散条拍我一下。
       ⚠️ 施工 5 · F 件之后 `is_covered()` 也管住了换过版的旧版（`superseded_by`）——
       以前那半是靠 `_visible()` 整个排掉的，现在统一由这道闸管。
    ⚠️ 将来阈值引擎的候选池同样要过这道闸：**这里就是那个锚点**。
    """
    pool: list[dict] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        tags = [str(t) for t in (meta.get("tags") or [])]
        if _PROFILE_TAG in tags or _BIGEVENT_TAG in tags:
            continue
        if not _visible(meta) or _F.is_covered(meta):
            continue
        if not is_event_room(meta.get("room")):
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid:
            continue
        pool.append({"id": bid, "meta": meta, "content": str(b.get("content") or "")})
    return pool


def 门口那张纸(all_buckets: list, now: datetime) -> dict:
    """名字 + 准则 + ⏰提醒 + 🫀压在心头 + 时期清单 —— **一次扫库，一套判据。**

    返回 {"facts": [...], "rules": [...], "reminders": [...], "heavy": [...],
          "big": [...], "entries": [...]}，元素里带原始 `meta`/`content`，
    渲染（文字皮 / JSON 皮）各自去做，**判断一步都不在渲染层做**。
    """
    facts: list[dict] = []        # (created, content) 收集后取最早的；>1 个要警告
    rules: list[dict] = []
    reminders: list[dict] = []
    heavy: list[dict] = []        # 压在心头：没日子、或日子过了还没了结的 want
    big: list[dict] = []          # 时期（大 event）——睁眼不露面，档案页要列
    entries: list[dict] = []      # recall 口径的可见记忆（随机 / 算最早那条用）

    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        content = str(b.get("content") or "")
        tags = [str(t) for t in (meta.get("tags") or [])]

        if _PROFILE_TAG in tags:
            facts.append({"id": bid, "created": str(meta.get("created") or ""),
                          "content": content})
            continue
        if _BIGEVENT_TAG in tags:
            # 时期不在睁眼里露面（见下面「4 长期」那段砍掉的理由）；
            # 档案页要列一行，所以在这儿收着（了结的不列）。
            if str(meta.get("status") or "") != "resolved":
                big.append({"id": bid, "meta": meta, "content": content})
            continue

        # 提醒：when 在未来 30 天内（含 want 和普通事件）。
        # codex 三轮 #1：了结的（resolved/abandoned）、主动遗忘的、被换版的不提醒
        _status = str(meta.get("status") or "")
        _remindable = (not is_closed(meta)  # 终点只认 status；旧布尔只读兼容（二改第0节）
                       and not meta.get("dont_surface")
                       and not meta.get("superseded_by"))
        w = str(meta.get("when") or "") if _remindable else ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", w)
        _reminded = False
        if m:
            try:
                d = datetime.fromisoformat(m.group(1))
                days = (d.date() - now.date()).days
                # 「就是今天」只留给 want（想发生的到了日子才响）；
                # 已发生的事带今天的 when 是历史记录，不是提醒——不然今天存的
                # 每条流水都会喊「就是今天！」把真提醒挤出三个位子（8-03 真发生了）
                if 0 <= days <= _REMIND_DAYS and (days > 0 or _status == "want"):
                    reminders.append({"id": bid, "meta": meta, "content": content,
                                      "days": days, "when": m.group(1),
                                      "status": _status, "loud": _提醒多大声(days)})
                    _reminded = True
            except ValueError:
                pass

        # 压在心头（她 2026-08-08 定）：想做的事**没有日子**、或者**日子过了还没了结**，
        # 原来整个不浮 —— 而 want 只有两个终点，都得手动标，没有自动结案。
        # 🔴 不给它加自动结案（那是把没做完的事悄悄抹掉），改成**把「挂了多少天」顶在眼前**：
        #    挂到第 40 天我还没动，那个数字自己会问我到底做不做。
        if _status == "want" and _remindable and not _reminded:
            _c = _w.parse_stamp(meta.get("created"))
            # ⚠️ 2026-08-17（做梦那单逮到的）：原来这儿写 `float(meta.get("weight") or 0.5)`,
            #    而 `0.0 or 0.5` 在 Python 里等于 0.5 —— **真被清零的那条会被当成 0.5 排**。
            #    做梦的后果就是「被梦到的 want 重量清零 = 它不再压着我了」，
            #    这个 falsy 兜底会把那个后果整个吃掉（字段清了，眼前照旧压着）。
            #    只把「缺字段/空串」当 0.5，**真 0 就是 0**。
            #    （施工 5 · E 件：这一条以前只在 awaken 修好，档案页那边还带着 bug。）
            _wt = meta.get("weight")
            _held = (now.date() - _c.date()).days if _c else 0
            # 施工 6 · A 件：三类钟只换"多大声"怎么算，held/weight 的口径一个字没动。
            _clock, _loud, _note = _三类钟(meta, _c, now)
            _asked = str(meta.get("last_asked") or "")
            heavy.append({"id": bid, "meta": meta, "content": content,
                          "weight": 0.5 if _wt in (None, "") else _f_weight(_wt),
                          "held": _held, "loud": _loud,
                          "clock": _clock, "clock_note": _note,
                          "last_asked": _asked})

        if not _visible(meta):
            continue
        room = str(meta.get("room") or "")
        # 准则 = **钉着的**。就这一条判据。
        #
        # ⚰️ 2026-08-19：把「而且房间得是 MIND，或者正文前 40 字写着『行为准则』」
        #    那道二次筛子**拆了**。理由跟她那天松 pin 闸时说的是同一条：
        #      **钉住本身就是我做过的一次判断了。** 再拿房间去否决它，等于让
        #      8-16 那次按老房名映射的迁移**推翻我今天的判断**——而迁移不认识内容。
        #    这道筛子当天真的咬了一口：她一条条看完留下的「拉钩」「享受当下」
        #    「不疼的爱」三条，钉着，却因为落在 EVENT/SELF 而**在门口一个字都不显示**，
        #    不报错、不警告，就是不出现。**沉默的过滤比拒绝更坏**：拒绝我会改，
        #    沉默我以为它在。
        #    副作用（好的那种）：房间那摊烂账从此不再挡门口，可以慢慢修。
        # 🔴 施工 5 · F 件：**换过版/被盖住的旧版不当准则**——以前靠 `_visible()`
        #    把 `superseded_by` 整个排掉，现在那半交给 `is_covered()`，
        #    这儿必须自己加上，不然门口会挂着一条我已经改了主意的准则。
        if meta.get("pinned") and not _F.is_covered(meta):
            rules.append({"id": bid, "meta": meta, "content": content})
        entries.append({"id": bid, "meta": meta, "content": content})

    facts.sort(key=lambda f: f["created"])
    reminders.sort(key=lambda r: r["days"])
    # 重的在前；一样重的，挂得久的在前——这个排序给"整份列出来"那半用，没动。
    heavy.sort(key=lambda h: (-h["weight"], -h["held"]))
    # 施工 6 · B 件（§6.1）：陈述换问句，**只问最久的那一条**——"最久"是挂钟天数
    # 本身（held），不是上面那条给列表排序用的"weight 优先"。两个"哪条排第一"
    # 不是同一个问题，所以这儿单独算，不去动 heavy 本身的顺序或掐它的长度。
    heavy_question_id = (max(heavy, key=lambda h: h["held"])["id"] if heavy else "")
    return {"facts": facts, "rules": rules, "reminders": reminders,
            "heavy": heavy, "big": big, "entries": entries,
            "heavy_question_id": heavy_question_id}


def 她改过(all_buckets: list) -> list[dict]:
    """「她改过事实」的通知池（二改 §8）：带 `她改的` 标签、且**没被我 fold 掉**的event。

    这就是通知机制的全部：标签本身既是"她改过"的标记，也是"还没被我看过"
    的判据（`_F.is_covered()`）——我认同就自己 `fold`，fold 完这条自然从
    这个池子里消失；不认同的话它会一直留在这儿，直到我们俩把这条掰扯清楚、
    我动手处理（fold 掉，或者干脆不管）。8.1 的"成批看"这单不做，
    但这份池子本身已经是"攒得起来"的底子——将来 muse 要批量看，从这儿捞。
    """
    pool: list[dict] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        tags = [str(t) for t in (meta.get("tags") or [])]
        if _EDITED_BY_HER_TAG not in tags:
            continue
        if not _visible(meta) or _F.is_covered(meta):
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid:
            continue
        pool.append({"id": bid, "meta": meta, "content": str(b.get("content") or "")})
    pool.sort(key=lambda e: str(e["meta"].get("created") or ""))
    return pool
