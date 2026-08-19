"""
========================================
tools/regrow/ — 认知换版本（批 2a，2026-08-03；codex 二轮复核后加固）
========================================

同一条认知有了新版（A → A'）：新版上位，旧版留档、不跟新版一起浮现。

🔴 **2026-08-16（施工 3）起，这个入口是 `fold` 的 n=1 特例**：底下走 `tools/_fold.py`
的同一段代码（新桶 `cover=[旧 id]`、旧桶 `covered_by`），版本链和对外的话一个字没变。
「我改主意了」和「我概括了一下」的区别**在正文里**，不在动作里。

跟另外两个动作的边界（规格 13.4，一个都不能混）：
- grow(kind="mind")：从几条 event/mind **提炼出一条新的** → 池子里多一条
- **regrow**：同一条认知**长出新版** → 新版上位，旧版沉下去但还在
- trace：修正/删除。⚠️ 它的 content 参数会**直接覆盖原文**——换版本绝不许用它

codex 二轮修的四处：
- 整个流程包在旧 id 的 _keyed_turn 里（跨进程锁）：并发 regrow 同一条不再分叉
- 归档桶直接拒绝（update() 不写归档桶，硬做必留半条链——先 trace restore 再来）
- from 追加的来源逐个验存在性；链满时逐个塞，塞得下几个算几个
- 回填自愈：backfill_sweep 同时认 source_tool=regrow（改在 rooms_path 那边）

对外暴露：dispatch(bucket_id, text, v, a, from_) → str
========================================
"""

from core import _fold as _F           # fold 的骨头：regrow 是它的 n=1 特例
from .. import _runtime as rt
from core._bigevent import is_big as _is_big
from .._common import _keyed_turn
# is_mind_room 2026-08-18 起不再用于挡 event（那道闸拆了，见下面 regrow 里那段碑文）
# from core._rooms import is_mind_room
from utils import read_from_ids
from ..grow.rooms_path import _normalize_from

_CHAIN_LIMIT = 64  # triggered_by 底层上限


async def dispatch(bucket_id: str = "", text: str = "", v=-1, a=-1, from_=None) -> str:
    bucket_id = str(bucket_id or "").strip()
    text = str(text or "")  # 逐字落盘：不 strip 正文
    if not bucket_id:
        return "regrow 要换谁的版本？传旧版的 bucket_id。"
    if not text.strip():
        return "text 不能为空——新版的完整正文（不是补丁，是整条重写后的样子）。"

    # v/a 跟 mind 一条规矩：我自己打，不外包
    try:
        v = float(v)
        a = float(a)
    except (TypeError, ValueError):
        return "v/a 必填：新版此刻的坐标是你自己打的（0~1）。"
    if not (0 <= v <= 1 and 0 <= a <= 1):
        return f"v/a 必须在 0~1 之间（收到 v={v}, a={a}）。"

    # 追加来源：先归一化，再逐个验存在（codex P2-6）
    extra, from_err = _normalize_from(from_)
    if from_err:
        return from_err
    if extra:
        missing = []
        for fid in extra:
            if not await rt.bucket_mgr.get_including_archive(fid):
                missing.append(fid)
        if missing:
            return f"from 里这些 id 不存在：{', '.join(missing)}。"

    # ---- 检查→建新→写双向链 全部在旧 id 的跨进程锁里做（codex P1-1）----
    async with _keyed_turn(f"regrow-{bucket_id}"):
        old = await rt.bucket_mgr.get(bucket_id)
        if not old:
            arch = await rt.bucket_mgr.get_including_archive(bucket_id)
            if arch:
                # 归档桶 update() 不给写，硬做必留半条链（codex P1-2）
                return (f"{bucket_id} 在归档区。先 trace(bucket_id=\"{bucket_id}\", restore=True) "
                        "把它捞回来，再 regrow。")
            return f"找不到 {bucket_id}。"
        old_meta = old.get("metadata", {}) or {}
        # 房间从旧版继承，**这儿没有改它的口子**（2026-08-19 她定的轴）：
        # 「regrow 改内容本身，trace 改元数据」。房间是元数据 —— 它不影响这条记忆说了什么，
        # 改它像用修正带，不该产生一个新版本。搬家走 trace(bucket_id=…, room=…)。
        # ⚰️ 8-19 白天这里短暂收过 `room`，理由是「搬家是换版的一部分」；
        #    当晚她一句话点破：那是一个字段两个入口，正是 8-18 亲手杀掉的病。
        room = str(old_meta.get("room") or "")
        is_big = _is_big(old_meta)
        # 🔴 2026-08-18：这儿原来有一道闸，只放行认知（MIND 两间）和时期，
        #    普通 event 被挡在外面，理由是「发生过的事不该被改写」。**她拍板拆了。**
        #
        #    拆的理由不是那条原则不对，是**它没被这道闸守住**：regrow 本来就不改原文，
        #    它是新版上位、旧版留档（superseded_by 连着，id 直查逐字还在）。
        #    真正的代价是「同一句心里话有三种手势」——
        #      认知变了 → regrow · 事件记错 → 另存一条修正 · 我记错了 → fold 盖一条
        #    ——讲不清楚，写工具描述的时候当场卡住。
        #
        #    拆完是：**regrow = 这一条有了新版**（认知/事件/时期都用它），
        #    **fold = 折起来**（几条收成一句 / 一段日子起个名）。两个工具两句话，
        #    再没有重叠——她下午问的「regrow 和 fold 是不是重了」也就此消失。
        #
        #    「我记错过」这件事本身要是重要，它该是一条真的认知（「我累的时候会把
        #    日期记混」），而不是靠两条并列的事件去暗示。洞察归洞察，事实归事实。
        if old_meta.get("superseded_by"):
            return (f"{bucket_id} 已经被 {old_meta.get('superseded_by')} 换过版了——"
                    "在最新版上 regrow，别从旧版分叉。")

        # 来源链：继承旧链 + 逐个追加新来源，塞得下几个算几个（codex P2-7）
        inherited = read_from_ids(old_meta)   # from 优先、triggered_by 兼容
        sources = list(dict.fromkeys(inherited))
        dropped: list[str] = []
        for fid in (extra or []):
            if fid in sources:
                continue
            if len(",".join(sources + [fid])) <= _CHAIN_LIMIT:
                sources.append(fid)
            else:
                dropped.append(fid)

        # 时期换版：标签和起止都得跟过来，不然新版就不是时期了 —— **起止照抄旧版**。
        # 时间从旧版继承，同样没有口子（2026-08-19）：`when` 是「这条挂在时间的哪儿」，
        # 是元数据不是内容 —— 时期改起止、事件补发生日，都走 trace(bucket_id=…, when=…)。
        # 📌 时期这条以前是特例（「范围是它的一半身体，所以算内容」），她当晚把这个特例也拆了：
        #    点和段是同一种东西，都是「挂在哪儿」。时期真正的内容是**那个名字**。
        new_when = str(old_meta.get("when") or "") if is_big else ""

        is_test = bool(old_meta.get("provenance", {}).get("kind") == "test"
                       if isinstance(old_meta.get("provenance"), dict) else False)
        # ---- 二改施工 3：落盘这一下交给 fold 的骨头（tools/_fold.py）----
        # 🔴 regrow 从此是 fold 的 **n=1 特例**：新桶 cover=[旧 id]、旧桶 covered_by=新 id，
        #    版本链（supersedes/superseded_by/dont_surface）照旧一个字不变。
        #    「我改主意了」和「我概括了一下」的区别在正文里，不在动作里——
        #    所以底下是同一段代码，上面这个入口只是把老签名和老话保住。
        # 时期换版（8-17 14:30 终稿后）**只换 text/v/a/when**：时期是纯命名层，
        # 没有名单要继承、没有范围要重新解析——`cover` 里只有旧版那一条（版本链）。
        # ⚠️ `落一条gist` 见到 when 会把 cover 清空，所以版本链靠 `supersedes=` 那条路写，
        #    不靠 cover（下面 报告["cover"] 对时期必然是空的，别拿它报数）。
        cover = [bucket_id]
        if is_big and new_when:
            _t0, _t1, span_err = _F.check_span(new_when)
            if span_err:
                return span_err
        new_id, 报告 = await _F.落一条gist(
            text, room, v, a, cover, when=new_when if is_big else "",
            from_ids=sources, supersedes=bucket_id, test_data=is_test)

    try:
        await rt.bucket_mgr.touch_many(sources)  # 换版=重新想起来源（codex 三轮 #4）
    except Exception:
        pass

    mark = "◈时期" if is_big else "🌱regrow"
    span = f" {new_when}" if is_big and new_when else ""
    out = f"{mark} {bucket_id} → {new_id}  {room}{span}（旧版留档不浮现，id 直查仍能看）"
    if is_big and new_when:
        # 时期只换名字和边界——没有名单跟着换。数一下这一刻范围里有谁，只给个手感。
        _t0, _t1, _e = _F.check_span(new_when)
        if not _e:
            out += (f"\n（范围内现在有 {len(await _F.span_members(_t0, _t1))} 条——"
                    "**现场数的**，不落盘；时期只起名字，一条都没被压住。）")
    if dropped:
        out += f"\n⚠️ 来源链满：这几个没挂上 {', '.join(dropped)}（继承链优先）"
    if 报告["链没写全"]:
        out += "\n⚠️ 版本链没写全（supersedes/superseded_by 有一半失败）——把这条报给AI查"
    tail = _F.报告成话(报告)
    if tail:
        out += "\n" + tail
    return out
