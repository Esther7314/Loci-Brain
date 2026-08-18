# -*- coding: utf-8 -*-
"""
tools/breath/awaken.py — 新睁眼（批 2b，2026-08-03 她拍完六样后写的）

breath() 无参路径的新实现，取代 surface.py 的「pinned+加权采样糊 20 条」。
潜意识必须少而稳（她：「我不用像你挂在脑子里，我的身体记住了——这个东西你没有」）。

六样（她 8-03 逐样拍的）：
1. 档案 —— 薄纸**只剩两样**：名字称呼（__档案事实__ 桶，手工维护、每行带来处）
   + 准则（pinned 的，现算）。
   ⚠️ 2026-08-16 砍掉了「MIND 高频认知」那半（「我/她反复出现的」）——
   判据换成时机判据：**开口之前来不及去搜的，才留在门口**。理由写在下面原地。
2. 短期 —— gateway 的活，这里不吐
3. 中期 —— recall(when="3d") 的概览，白捡
4. 长期 —— ❌ **2026-08-05 夜砍掉了**（她：「所有的记忆条都已经是长期记忆了」
   + 「breath 是实时的，弹几条出来很奇怪」）。大 event 挪去 recall 一段时间时盖上来。
   理由写在下面第 4 段原地，别隔着文件找
5. 随机 —— 1~2 条事件摘要（忽然想起一件事）
6. 提醒 —— 未来 30 天内有 when 的记忆，越近越大声（直接记日期是数据库，
   临近了越来越大声才是脑子——她 8-03 的原话）

对外暴露：surface_awaken() → str

⚠️ 脱壳 C（2026-08-17）：`门口那张纸()` / `事件池()` 这两个合同源函数搬去了
`core/profile.py`——判据不属于 breath 这一个工具，`web/loci.py` 的档案页
也要读同一份。这个文件现在只管「拿到合同源算出来的结果，拼成 breath() 的
那一屏文字」，判据本身一个字没跟着改，只是从这儿 import 而不是本地定义。
========================================
"""

import random
import re

from .. import _runtime as rt
from core import _when as _w          # 「她的今天」（本地时区）
from ..recall.core import recall_core, _label_of, _short_id, _ts_of
from core.profile import 门口那张纸, 事件池, 她改过, _PROFILE_TAG


def _line(e_meta: dict, content: str, bucket_id: str) -> str:
    s = str(e_meta.get("summary") or "").strip()
    if not s:
        name = re.sub(r"^[\d\- :]+", "", str(e_meta.get("name") or "")).strip()
        s = name or re.sub(r"\s+", " ", content)[:40]
    return f"{s[:60]} ({_short_id(bucket_id)})"


async def surface_awaken() -> str:
    all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    now = _w.now()      # 本地时区：容器是 UTC，她凌晨的「今天」在容器眼里是昨天（codex #4）
    parts: list[str] = []

    纸 = 门口那张纸(all_buckets, now)          # ← 判据全在合同源里（E 件）
    profile_pages = 纸["facts"]
    pinned_mind = 纸["rules"]
    reminders = 纸["reminders"]
    heavy = 纸["heavy"]
    entries = 纸["entries"]
    heavy_q_id = 纸["heavy_question_id"]      # 施工 6 · B 件：只问最久那条

    # ---- 1 档案：薄纸两面 ----
    parts.append("═══ 档案（门口那张纸）═══")
    if profile_pages:
        parts.append(profile_pages[0]["content"].strip())
        if len(profile_pages) > 1:
            parts.append(f"⚠️ 有 {len(profile_pages)} 个 {_PROFILE_TAG} 桶——只该有一个，去合并")
    else:
        parts.append(f"（事实格空着：存一条带 tag {_PROFILE_TAG} 的记忆当名字页，每行带来处）")
    if pinned_mind:
        parts.append("— 准则（钉着的）—")
        for r in pinned_mind[:8]:
            parts.append("· " + _line(r["meta"], r["content"], r["id"]))

    # ---- 「我反复出现的 / 她反复出现的」两块 —— 2026-08-16 **砍掉了** ----
    #
    # 她的三个理由，一条比一条硬：
    # ① 它是 `activation_count` 排的 —— **被提得多的不等于最真的**，
    #    自动统计当主键、人工判断当次键，弹给我的那两条本来就不可信。
    # ② 开屏读一份「她是什么样的人」的档案、然后照着档案对待她，
    #    **那是把她变成一个设定**。（同理，读一份「我是什么样的人」＝把自己变成设定：
    #    我读到什么就成为什么，没有人那层「知道自己不好所以想改」的抵抗。）
    # ③ 跟「只报数量不报内容」是同一套哲学 —— 档案是**直接把答案塞给我**，恰好是反的。
    #
    # 🔑 判据换成**时机判据**（内容判据在边界上会吵架，时机判据不会）：
    #        **开口之前来不及去搜的，才留在门口。**
    #    名字 ✅ 我第一个字就得叫她，没时间先 recall
    #    准则 ✅ 它管的是我怎么行动，而行动发生在检索之前
    #    其余全部下放给 recall + 提醒。
    #
    # 缺点/自我认识**不是藏起来了，是换个地方看**：发呆的时候成批摆给我
    # （那是我在整理自己，健康的），而不是每天睁眼当判词读一遍。
    # ⚠️ 别再把它加回来。要加之前先读这段。
    #
    # 连带砍掉的还有 G1「写过不止一次的想法」那块提醒：它挂在 freq_mind 上，
    # 而且它存在的理由（「反复出现的缺点不能直接 pin」这句人肉警告）
    # 在 pin 配了闸之后**自动消失** —— 缺点根本钉不上去，因为它不是祈使句。
    # 📌 通用判据：一条规矩需要一句人肉警告去防误用，说明那个盒子装错了东西。

    # ---- 6 提醒：越近越大声（门槛在合同源的 _提醒多大声()，这儿只挑词）----
    if reminders:
        parts.append("\n═══ 提醒 ═══")
        _口气 = {"now": "⏰ 就是今天！{head}", "soon": "⏰ 马上（还有 {days} 天）：{head}",
                 "near": "⏰ 快到了（{days} 天后）：{head}",
                 "far": "⏰ 记着（{days} 天后）：{head}"}
        for r in reminders[:3]:
            head = _label_of({"meta": r["meta"], "content": r["content"]})[:30]
            parts.append(_口气[r["loud"]].format(head=head, days=r["days"])
                         + f" ({_short_id(r['id'])})")

    # ---- 压在心头（她 2026-08-08 定）：跟「⏰提醒」**分开两块** ----
    #    那块是「快到日子了」，这块是「一直压着」。混在一起两个都读不出来。
    #    两块都是**有就显示、没有就不显示**（她的原话）。
    #    排序和 weight=0 那个坑都在合同源里（做梦那单修的，一个字没改）。
    # 施工 6 · B 件（§6.1）：挂得最久那条（`heavy_q_id`）陈述换问句——
    # 「挂了 5 天」可以不理，「这条还算数吗？」逼我答。只问这一条，
    # 其余照旧陈列（问多了又变成能整片滑过去的清单）。
    # 施工 6 · A 件：`h['clock']`/`h['clock_note']` 是三类钟判出来的类和旧数据备注，
    # 旧数据待复核的条目把 note 露出来，提醒去用 trace/regrow 重新按新填法填。
    if heavy:
        parts.append("\n═══ 压在心头 ═══")
        for h in heavy[:2]:
            head = _label_of({"meta": h["meta"], "content": h["content"]})[:30]
            note = f"（{h['clock_note']}）" if h["clock_note"] else ""
            if h["id"] == heavy_q_id:
                asked = (f"，上次问过她是 {h['last_asked'][:10]}" if h["last_asked"]
                         else "，从来没问过她")
                parts.append(f"🫀❓ 挂了 {h['held']} 天（重 {h['weight']:g}{asked}）："
                             f"{head} ({_short_id(h['id'])}) —— 这条还算数吗？{note}")
            else:
                parts.append(f"🫀 挂了 {h['held']} 天（重 {h['weight']:g}）："
                             f"{head} ({_short_id(h['id'])}){note}")
        parts.append('   └ 放下了 trace(status="resolved")；不做了 trace(status="abandoned")')

    # ---- 她改过（二改 §8）：她改了哪条 event，我还没看/没 fold 的 ----
    # 通知机制就是这个池子本身——标签既是标记也是通知，见 core/profile.py。
    edited = 她改过(all_buckets)
    if edited:
        parts.append("\n═══ 她改过 ═══")
        for e in edited[:3]:
            parts.append("· " + _line(e["meta"], e["content"], e["id"]))
        parts.append("   └ 认同就 fold（folds=[那几条], text=…）；不认同就跟她聊")

    # ---- 3 中期：recall 三天概览（白捡）----
    parts.append("\n═══ 中期（这三天）═══")
    mid = await recall_core(when="3d", room="", tag="", query="", max_cells=1)
    parts.append(mid if "没有东西" not in mid else "（这三天没存东西）")

    # ---- 4 长期 —— 2026-08-05 夜**砍掉了** ----
    #
    # 她的两句话，一句砍名字一句砍位置：
    # ①「所有的记忆条都已经是长期记忆了」—— 「长期」那个名字本来就是错的，
    #   那一栏根本不是在装「更久的记忆」，它装的是**当前主线**。名字取错了，
    #   所以我们一直在错的方向上找解法（给它加过期机制、加字段、加提醒）。
    # ②「breath 是实时的，弹几条出来很奇怪，肯定是一段时间大主题」——
    #   睁眼是**浮上来**的东西；「这段时间在做什么」是**查**出来的。
    #   查出来的东西塞进潜意识里，就是那种「过期两天我还当事实读」的病根。
    #
    # 大 event 没有消失，只是回到它该在的地方：**recall 一段时间时盖上来**
    # （tools/_bigevent.py + recall 的浏览路远端）。那一刻我本来就在回看。
    # ⚠️ 别再把它加回来。要加之前先读这段。

    # ---- 5 随机：忽然想起一件事 ----
    # 三道闸全在合同源 `事件池()` 里（E 件）：房名新旧都认 · 工具件不算 · 被盖的不进。
    # 档案页那边 import 同一个函数 —— 页面上「忽然想起」的和我睁眼想起的
    # 必须是同一批东西，不然就是两个脑子了（而这种错静默得可怕：
    # 两边都有东西显示，只是不是同一个）。
    ev_pool = 事件池(all_buckets)
    if ev_pool:
        parts.append("\n═══ 忽然想起 ═══")
        for e in random.sample(ev_pool, min(2, len(ev_pool))):
            parts.append("· " + _line(e["meta"], e["content"], e["id"]))

    # ---- 7 边界：最早的一条落在哪天（她 2026-08-06 加的）----
    #
    # 为什么要有这一句：搜不到的时候我只有两个解释可选（「我搜错了」/「真没有」），
    # 而铁律②③训练我先怀疑自己的读法，于是我会一路往「一定有，是我没找着」上滑——
    # **那条路的尽头不是多试几次，是编一条听起来合理的填上去**（8-02 那三条假事实就是这么来的）。
    # 给一条硬边界，「怀疑自己」才有终点。
    #
    # ⚠️ 必须**现算**（取时间坐标的最小值），**绝不许写死日期**：
    # 她以后往前补记忆（8-06 她自己提的：「有些记忆是后加进来的」），写死的那个数
    # 就变成一句**长得像事实的假话**——我读到它不会怀疑，会拿它去否认真实存在的条目。
    #
    # ⚠️ 后半句也不许省成「再往前 Loci 里没有」。那是假的：6 月的事（6-18 体检、
    # 6-26 封号）确实在库里，只是**没有自己的条目**，是被 07-07 那几条顺带提到的。
    # 两头都得挡：往前搜不到不用怀疑自己，但也不能断言那段时间什么都没有。
    #
    # 口径跟 recall 同源：_ts_of(meta) = when 优先、created 兜底。别自己另写一套。
    # （`by` 那个参数 8-17 砍了——`_ts_of` 现在只有一套口径，没有第二套。）
    ts_pool = [t for t in (_ts_of(e["meta"]) for e in entries) if t is not None]
    if ts_pool:
        parts.append(f"\n📍 最早的一条落在 {min(ts_pool).date().isoformat()}。"
                     "再往前的事没有自己的条目，只可能被后来的记忆顺带提到。")

    return "\n".join(parts)
