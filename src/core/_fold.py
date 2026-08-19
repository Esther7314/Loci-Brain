# -*- coding: utf-8 -*-
"""
========================================
tools/_fold.py — fold / gist 的骨头（二改施工 3，2026-08-16）
========================================

**动作一个 `fold`（折起来，底下还在）· 产物一条 `gist`（要旨）· 反向 `unfold`（下钻）。**
📌 `gist` / `verbatim` 是模糊痕迹理论（Brainerd & Reyna）的正词，跟遗忘那一刀同一套理论。
📌 名字取自她 8-16：「fold 也可以，**就像文件夹**」。

------------------------------------------------------------
三种圈法，一个动作（开工单 2.1）
------------------------------------------------------------
| 盖什么 | 我怎么给 | 原来叫 |
|---|---|---|
| 一条认知的新版本 | 一个 id | `regrow` |
| 一组碎片 | 一组 id | （新）浓缩 |
| 一段日子 | 一个时间范围 | `grow(kind="big")` |

🔴 **`regrow` 不是「还需不需要」，它本来就是这个动作的 n=1 特例。**
「我改主意了」和「我概括了一下」的区别**在正文里**，不在动作里。
所以两个老入口都保留、都映射到这儿来（照 trace 的 resolved→status 先例）。

------------------------------------------------------------
🔴 圈法终稿（她 2026-08-17 14:30 定死）：**用笔画圈写名字；想法合并才记账**
------------------------------------------------------------
**八个字：Event 用时间，mind 用快照。**

| 圈法 | 落什么 | 谁被压住 |
|---|---|---|
| 时期（`when=起..止`，event 那半） | **只落名字 + 范围**（`when`） | **谁都不压** |
| 快照（`cover=[ids]`，mind 那半） | 名单（`cover` / `covered_by`） | 被点名的不再独立冒头 |

🔴 **时期 = 纯命名层**：`cover` 不写、`covered_by` 一条都不碰、不塌行、不剔任何池子。
   谁在时期里 = **日期落在范围里，现场算**（`span_members()`）——
   于是补记自动归队、交叉/嵌套天然成立、边界想改就 `regrow` 换 `when`（边界本来就糊）。
   压缩归塌缩管；时期只负责**叫出名字**，`recall` 那段时间时盖在顶上
   （8-05 九条第 5 条原样回归：**盖，不替代**）。

📌 **返工原因记档（别再抄回来）**：第一版让时间圈法把范围解析成 id 存死（`resolve_span_ids`，
   8-17 退役）。那是从 consolidation/ACP 抄的——**他们要压缩替换，所以必须记死名单**；
   我们**一个字不删、只起名字**，记账那一半是白抄的，还顺手把「谁在这段日子里」
   从事实变成了快照。第 7 节的警告早写了，手还是抄了一半，她当天退了货。

🔪 **「盖一组 event」整个砍掉**：线归 `recall(query=)` 看（她：「搜『色色』就能看到一路的
   记忆」）。`fold 折一条」2026-08-18 撤了：那件事归 regrow（她拍的，见 tools/fold 里那段碑文）。

------------------------------------------------------------
三条硬规矩（开工单 2.2；第 1 条按终稿收窄到 mind）
------------------------------------------------------------
1. 🔴 **`cover` 存的永远是确定的 id 列表**（快照那一半）。时间范围**不再**解析成 id ——
   时期不记账，见上面那段。
2. 🔴 **`from` 和 `cover` 是两个参数，语义不同，不许合并**：
   `from` = 我**从**哪几条长出来的（底下**继续独立活着**）
   `cover` = 我**盖住**哪几条（底下**不再独立冒头**）
   ⚠️ 用 `kind` 决定 `from` 怎么读来省一个参数 = 「一个参数管两件事」，
   正是这一轮在骂的东西。
3. **能盖在盖过的上面**（递归）。**层数由日子自己长出来，不预设 T1/T2/T3。**

------------------------------------------------------------
落盘：两头都写（denormalize），故意的 —— **只在快照那一半**
------------------------------------------------------------
· gist 桶：`cover: [id, ...]`
· 被盖的每条：`covered_by: [gist_id, ...]`——**名单，不是单值**。
  🔴 她 8-17 零点抓回来的（8-05 大 event 第六条）：「一条小记忆可以同时被两条
  主线盖住——这不是冲突，是事实」。第一版写成单值把交叉悄悄变成了独占，
  显式盖=**叠着盖**（append），谁都不抢谁。老数据里的单值字符串读侧兼容。
为什么两头写：浮现池的过滤是**每轮全库扫**，只查 `covered_by` 一个字段就能筛，
不用维护反向索引。多写一份的代价是一次 update，比一张索引便宜得多。

🔴 **读侧「被盖了吗」= `covered_by` 或 `superseded_by` 任一非空**——
`superseded_by` 是 regrow 8-03 就在写的老字段（版本链，天然单值），盘上躺着一堆，读兼容保留。

------------------------------------------------------------
「不再独立冒头」的确切范围（说明书 §3 D）—— **只有快照那一半会压人**
------------------------------------------------------------
排除：breath 的忽然想起/偶遇池 · 梦的候选池 · 发呆（muse）的候选池
不排除：recall 搜索（有 query 照样命中，它们没死）· id 直查 · 下钻 ·
       **浏览视图的统计**（条数/房间/标签/V·A 一个字不少，塌的是行不是数）
🔴 **时期在这张表上一格都不占**：它不写 `covered_by`，所以上面每一个池子都当它不存在。
   被时期盖着的记忆照旧独立冒头、照旧进梦的候选池——**只多一个名字，不少一样东西**。

对外暴露：GIST_TAG · SPAN_HELP · is_covered() · covers_of() · cover_ids() · is_gist()
         check_span() · span_members() · 落一条gist() · 报告成话()
🔪 `resolve_span_ids()` **8-17 退役**（时期不记账了）。它读侧那一半改名叫
   `span_members()` 活着——**现场算，永不落盘**。
========================================
"""

import asyncio
from datetime import datetime, timedelta

from tools import _runtime as rt
from ._bigevent import BIGEVENT_TAG, SPAN_RE

# gist 的系统标签。时间圈法产出的那种**同时**打 __大event__ ——
# 大 event 的老机制（recall 一段时间盖上来、不进时间轴、regrow 换版）一行都不用改。
GIST_TAG = "__gist__"


def is_covered(meta: dict) -> bool:
    """这条被盖住了吗。

    🔴 两个字段任一非空都算：`covered_by`（fold 新写的）、`superseded_by`
    （regrow 8-03 起就在写的老字段，盘上一堆，只读兼容）。
    """
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("covered_by") or meta.get("superseded_by"))


def _covered_list(meta: dict) -> list[str]:
    """covered_by 本身的名单（不含 superseded_by）。老数据单值字符串也认。"""
    raw = (meta or {}).get("covered_by")
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def covers_of(meta: dict) -> list[str]:
    """盖着这条的全部 gist id（可交叉，她 8-05 第六条）。covered_by 名单 + superseded_by 兼容。"""
    if not isinstance(meta, dict):
        return []
    out = _covered_list(meta)
    sup = str(meta.get("superseded_by") or "").strip()
    if sup and sup not in out:
        out.append(sup)
    return out


def cover_ids(meta: dict) -> list[str]:
    """一条 gist 盖着谁。落盘是 list；老数据/手改成逗号串也认（宽进严出）。"""
    if not isinstance(meta, dict):
        return []
    raw = meta.get("cover")
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def is_gist(meta: dict) -> bool:
    tags = [str(t) for t in (meta.get("tags") or [])]
    return GIST_TAG in tags or BIGEVENT_TAG in tags


SPAN_HELP = ('when 要写成起止："2026-07-31..2026-08-05"；还在进行中就把止留空：'
             '"2026-07-31.."。（起止一次填完，别指望第二步回来补——那步照样会忘。）')


def check_span(span: str) -> tuple[datetime | None, datetime | None, str]:
    """`起..止` → `[t0, t1)`（`t1=None` = 还在进行中）。返回 `(t0, t1, 错误信息)`。

    只做格式和先后校验，**不碰库**。时期唯一要存下来的就是这两个边界（存在 `when` 里，
    没有第二个字段），所以这儿是它唯一的闸。
    """
    from . import _when as _w

    m = SPAN_RE.match(str(span or "").strip())
    if not m:
        return None, None, SPAN_HELP
    t0 = _w.parse_date(m.group(1))
    t1 = _w.parse_date(m.group(2)) + timedelta(days=1) if m.group(2) else None
    if t1 is not None and t1 <= t0:
        return None, None, f"止（{m.group(2)}）在起（{m.group(1)}）前面了。"
    return t0, t1, ""


async def span_members(t0: datetime | None, t1: datetime | None) -> list[str]:
    """**现场算**：这一刻有哪些记忆的日子落在 `[t0, t1)` 里（新→旧无所谓，按时间升序）。

    🔴 **这份名单永远不落盘**（8-17 14:30 终稿：时期只存名字 + 范围）。
    它只用来**报个手感**（「范围内现在有 N 条」）和**下钻**（id 直查一条时期时摊开看）。
    每次都重算，所以：补记自动归队、交叉/嵌套天然成立、改 `when` 立刻换一批人。

    口径跟 recall 的浏览路一致（`_visible` + `_ts_of`）——屏幕上那段时间里看得见的
    是哪些条，时期里就该是哪些条，两处不一样才是 bug。
    ⚠️ 时期/gist 本身不算成员（`_visible` 把 `__大event__` 排掉了；`__gist__` 在这儿
    也不收——一条时期的成员是记忆，不是别的名字）。
    """
    # 懒 import：recall.core 会 import 本模块（读侧要 is_covered），模块级互相 import 会打转
    from tools.recall.core import _visible, _ts_of

    if t0 is None:
        return []
    try:
        buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.warning(f"时期成员现场算失败: {e}")
        return []
    out: list[tuple[datetime, str]] = []
    for b in buckets:
        meta = b.get("metadata", {}) or {}
        if is_gist(meta) or not _visible(meta):
            continue
        ts = _ts_of(meta)      # `by` 8-17 砍了，只剩一套口径（when 优先、created 兜底）
        if ts is None or ts < t0:
            continue
        if t1 is not None and ts >= t1:
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if bid:
            out.append((ts, bid))
    out.sort(key=lambda x: x[0])
    return [bid for _ts, bid in out]


async def 落一条gist(text: str, room: str, v: float, a: float,
                     cover: list[str], *, when: str = "", from_ids: list[str] | None = None,
                     supersedes: str = "", test_data: bool = False) -> tuple[str, dict]:
    """真正落盘的那一下。**三个入口（fold / regrow / grow(kind="big")）共用这一个。**

    🔴 宪法：`text` 是调用方写的，逐字落盘，**一个字不过模型**。
    这个函数里没有、将来也不许有任何 LLM 归纳路径——后台回填只补标签/摘要/起名，
    那是派生元数据，不是正文。

    supersedes：n=1（换版）时传旧版 id，写版本链 supersedes/superseded_by + dont_surface。
      为什么只在 n=1 写：版本链的语义是「同一条的上一版」，n≥2 没有「上一版」这回事。
      regrow 的对外行为靠这条一个字不变。

    🔴 **`when` 一给（= 时期），cover 就地清空**（8-17 14:30 终稿：时期只存名字 + 范围，
      不记账、不压制任何东西）。写死在这儿而不是只在入口拦：三个入口都从这儿落盘，
      **少一处判断就少一个能悄悄记账的口子**。

    返回 (new_id, 报告 dict)。报告里有 cover 实际写进去几条、哪些是硬盖已盖过的、
    哪些没写上——**失败不许吞**：正文在了但链没写上，调用方必须知道。
    """
    cover = [str(x).strip() for x in (cover or []) if str(x).strip()]
    if when:
        cover = []          # 时期 = 纯命名层。这一行是它的地基，别拿掉。
    tags = [GIST_TAG] + ([BIGEVENT_TAG] if when else [])
    new_id = await rt.bucket_mgr.create(
        content=text,
        tags=tags,                       # create 时就打上：回填只合并不替换，洗不掉
        importance=5,                    # 中性占位；importance 已退役、不再由我打
        domain=["未分类"],
        valence=v,
        arousal=a,
        name=None,
        from_ids=",".join(from_ids or []),
        source_tool="fold",
        room=room,
        when=when,                       # 时间圈法：起止就写在现成的 when 里，没有第二个字段
        test_data=test_data,
    )

    报告: dict = {"cover": cover, "叠盖": [], "没写上": [], "链没写全": False}

    # ---- 两头都写 ----
    # 🔴 **版本链不算记账**：时期换版时 cover 是空的（上面清掉了），但旧版那条**必须**
    #    写上 superseded_by/dont_surface，不然旧版会跟新版一起冒到那段日子上。
    #    所以要写的是 cover ∪ {supersedes}，其中只有 cover 那部分写 covered_by。
    要写的 = list(cover) + ([supersedes] if supersedes and supersedes not in cover else [])
    ok_cover = await rt.bucket_mgr.update(new_id, cover=cover) if cover else True
    for cid in 要写的:
        old = await rt.bucket_mgr.get(cid)
        old_meta = (old or {}).get("metadata", {}) or {}
        kwargs: dict = {}
        if cid in cover:
            旧名单 = _covered_list(old_meta)
            if 旧名单 and new_id not in 旧名单:
                # 她 8-05 第六条：交叉是事实不是冲突 → 显式盖已被盖的 = **叠着盖**（append），
                # 谁都不抢谁；两层都看得见、都钻得到。（8-17 零点她抓回来的，替掉第一版的「抢」。）
                报告["叠盖"].append((cid, list(旧名单)))
            kwargs["covered_by"] = 旧名单 + ([new_id] if new_id not in 旧名单 else [])
        if supersedes and cid == supersedes:
            # 换版那一档才写版本链和 dont_surface（regrow 8-03 起的行为，一个字不动）
            kwargs["superseded_by"] = new_id
            kwargs["dont_surface"] = True
        if not await rt.bucket_mgr.update(cid, **kwargs):
            报告["没写上"].append(cid)
    if supersedes:
        ok_sup = await rt.bucket_mgr.update(new_id, supersedes=supersedes)
        报告["链没写全"] = not (ok_sup and ok_cover and supersedes not in 报告["没写上"])
        # ---- 🔴 换版要把「钉着」带过去（2026-08-19 修的 bug ①）----
        # 老毛病：regrow 一条钉着的准则 = **悄悄取消钉住**。
        #   新版是新建的桶（默认没钉），旧版被 dont_surface 压下去 ——
        #   两下一合，门口那行**没了**，而且**不报错、不提一句**。
        #   8-19 一天踩了三次，每次都靠改完当场验一眼才没丢。
        # 这里补上：旧版钉着，新版就接着钉。**配额是净零的**（新钉一条、旧摘一条），
        # 所以不走 check_pinned_quota —— 那道闸拦的是「多占一个名额」，这儿没多占。
        # 顺序：**先钉新的再摘旧的**。反过来的话，中间那一瞬门口是空的；
        # 而且万一后一步失败，宁可两条都钉着（看得见、我会发现），
        # 也不要两条都没钉（看不见、正是这个 bug 本身）。
        try:
            old_b = await rt.bucket_mgr.get(supersedes)
            old_meta = (old_b or {}).get("metadata", {}) or {}
            if old_meta.get("pinned"):
                报告["接着钉"] = bool(await rt.bucket_mgr.update(new_id, pinned=True))
                await rt.bucket_mgr.update(supersedes, pinned=False)
        except Exception as e:
            报告["接着钉"] = False
            try:
                rt.logger.warning(f"regrow carry-pin failed {supersedes}->{new_id}: {e}")
            except Exception:
                pass
    elif not ok_cover:
        报告["链没写全"] = True

    # 被盖的那些等于「又被想起了一次」（跟 regrow touch 来源同一个道理）
    try:
        await rt.bucket_mgr.touch_many(list(from_ids or []))
    except Exception:
        pass

    # 元数据后补：标签/摘要/起名走后台。keep_va=True——v/a 是我打的，回填永不碰
    from tools.grow.rooms_path import _backfill_batch
    kind = "big" if when else ("mind" if room.startswith("MIND") else "event")
    asyncio.create_task(_backfill_batch([(new_id, text, kind)]))
    return new_id, 报告


def 报告成话(报告: dict) -> str:
    """把落盘报告拼成给人看的尾巴（没什么可说的就返回空串）。"""
    out = []
    if 报告["叠盖"]:
        out.append("ℹ️ 其中 " + str(len(报告["叠盖"])) + " 条已被别的 gist 盖着，现在**叠着盖**（交叉）："
                   + "、".join(f"{cid}（已有 {'、'.join(olds[:3])}）" for cid, olds in 报告["叠盖"][:5])
                   + "。两层都在，都钻得到。")
    if 报告["没写上"]:
        out.append("⚠️ 这几条的 covered_by 没写上（归档区？）："
                   + "、".join(报告["没写上"][:5]) + "——把这条报给AI查。")
    if 报告["链没写全"]:
        out.append("⚠️ cover/版本链有一半没写上——把这条报给AI查。")
    # 换版接钉（bug ①）：成了就说一声，没成必须喊——不然又是一次「悄悄取消钉住」
    if 报告.get("接着钉") is True:
        out.append("📌 旧版是钉着的，新版**接着钉**（门口那行没断），旧版已摘钉。")
    elif 报告.get("接着钉") is False:
        out.append("🔴 旧版是钉着的，但新版**没钉上**——门口那行现在是空的，"
                   "手动 trace(bucket_id=新版id, pinned=1) 补上。")
    return "\n".join(out)
