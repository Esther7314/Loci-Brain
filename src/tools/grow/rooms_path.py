"""
========================================
tools/grow/rooms_path.py — 批 1 新 grow：kind=event|mind（2026-08-03）
========================================

设计出处：D:\\lento\\交接\\工单-om接口层-给fable5.md §5。

核心原则一句话：**正文先落盘，元数据后补。**
正文是调用方写的，丢了就没了；元数据（标签/摘要/起名/向量）是派生的，
晚十秒补上没人受伤。

关键行为：
- event：一次多条，逐条直接 bucket_mgr.create()，**不走 merge_or_create**
  （不 search、不 judge_same_event、不 LLM 合并——那一串就是超时的根因，
  也是「把不同事情并进一个桶」的元凶）
- 立刻返回真 bucket_id 列表（目标 < 3 秒），打标/摘要/起名走后台回填
- mind：独立的桶 + triggered_by（结构照抄 feel），v/a 必须调用方传
- 2026-08-06（机制③）：event 的 v/a 也必填了；后台回填**永不碰任何桶的 v/a**；
  importance/meaning 由调用方传（可选）；tags=场景锚点，引申词进 aliases 只喂 bm25
- tense="want" → create 后补 update(status="want", weight=…)
- 施工 6（二改 §6）：tense="want" 时 when 多认一种写法——时长记号
  （3w/10d/2m/1y），跟绝对日期一起构成 want 的"三类钟"；正文是等触发（when 留空，
  条件写在正文里）；怎么读三类归 `core/profile._三类钟`，这儿只管校验存不存得进去
- 校验先行：任何一条不合法 → 整个调用报错，不创建任何桶

不做什么（边界）：
- 不做合并（规格定死：items 每条独立成桶）
- room 永远不由模型生成或修改（tools/_rooms.py 校验，调用方判断）
- 后台回填失败只 logger.warning，不回滚、不重试到死

对外暴露：grow_event(items, tense, weight, test_data) → str
         grow_mind(room, text, from_ids, v, a, tense, weight, test_data) → str
========================================
"""

import asyncio
import uuid

from core import _fold as _F       # 大 event = fold 的时间圈法（施工 3）
from .. import _runtime as rt
from core._bigevent import SPAN_RE
from .._common import check_content_size
from core._rooms import check_room, is_mind_room
from .._subjects import normalize_subjects

# from → triggered_by（上限 64 字符）：12 位 hex id × 5 + 4 个逗号 = 64，正好放下；
# 第 6 个开始会被静默截断成半截 id，指向不存在的桶。所以在这儿挡死。
_FROM_MAX = 5
_TRIGGERED_BY_LIMIT = 64      # 与 bucket_manager._TRIGGERED_BY_MAX 一致

# 单条正文超过这个长度就在返回里提一句「看着不止一件事」。
# 她 8-05 定的触发方式：**提示我、由我判断拆不拆、怎么拆** —— 不自动动手。
# （自动拆会让一次 grow 突然多出几个 id，from 该指哪条得重新看；
#   而且「宁可不拆」是这件事从头到尾的基调。）
_LONG_HINT = 600
import re as _re
_WHEN_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")
# 施工 6 · A 件（二改 §6）：want 的"有量级"时长记号——`<N><单位>`，
# d=天 w=周 m=月(≈30天) y=年(≈365天)。没有前缀符号（她 8-18 裁决：没有语义的符号不留）。
# 只在 tense="want" 时才认——普通 event 的 when 是"这件事发生在哪天"，
# 时长记号在那儿没有意义，照旧只认绝对日期。
_WANT_DURATION_RE = _re.compile(r"^\d+[dwmy]$")

# 后台回填的摘要提示词。EVENT 记「发生了什么」，MIND 记「我认识到什么」。
_SUMMARY_PROMPT = (
    "你是记忆系统的摘要器。给下面这段记忆写一句话摘要，直接输出那一句，"
    "不要引号不要前缀，中文，不超过60字。"
    "如果是一件事，概括发生了什么；如果是一条认知/感受，概括认识到了什么。"
)


def _placeholder_meta() -> dict:
    """create 时的本地中性占位；真值由后台 _backfill 回填。"""
    return {"tags": [], "importance": 5, "domain": ["未分类"],
            "valence": 0.5, "arousal": 0.3}


async def _make_summary(text: str) -> str:
    """调 dehydrator 同一个 LLM 通道写一句摘要。失败返回空串（不阻塞回填其它字段）。"""
    chat = getattr(rt.dehydrator, "_chat", None)
    if not callable(chat):
        return ""
    # max_tokens 给足：deepseek-v4-flash 有推理 token，给 100 会被吃光、content 空
    #（_chat_once 对空响应返回空串不报错）。空结果重试一次。
    for _attempt in range(2):
        try:
            raw = await chat(_SUMMARY_PROMPT, text[:2000], max_tokens=400, temperature=0.3)
        except Exception as e:
            rt.logger.warning(f"summary 生成失败（正文已落盘，不影响）: {e}")
            return ""
        out = (raw or "").strip().strip('"').strip()[:200]
        if out:
            return out
        rt.logger.warning("summary 返回空，重试一次" if _attempt == 0 else
                          "summary 两次为空（疑似内容过滤），降级用正文开头")
    # 降级：正文开头当摘要——是调用方自己写的原文，不是编的。比空着强：
    # 摘要的作用是「知道它存在」的钩子，钩子缺了这条记忆在缩放视图里就是隐形的。
    return text[:60].strip()


# 疑似同件的相似度线。跟 dashboard 相似度页的拐点、G1 pin 提醒是**同一个数**
# （全库两两余弦扫出来的拐点在 80，见 流水/2026-08-03 相似度那节）。
_DUP_COS_THRESHOLD = 0.80


async def _backfill_one(bucket_id: str, text: str, kind: str) -> None:
    """后台给一个桶补元数据：标签 / aliases / 摘要 / 起名。

    kind = "event" | "mind" | "big"。
    🔴 v/a 一律不回填（机制③ 第 3 条，2026-08-06 定死）：event 的 v/a 现在也是
    调用方自己打的——「我当时什么感觉」交给模型猜，那条记忆就不是我的了。
    mind 不抽 scene（认知里没有照片），tags 会全空——正常，她认了。
    """
    update_kwargs: dict = {}
    try:
        meta = await rt.dehydrator.analyze(text, for_mind=(kind == "mind"))
    except Exception as e:
        rt.logger.warning(f"backfill analyze 失败 {bucket_id}（正文已落盘）: {e}")
        meta = None
    if meta:
        if meta.get("tags"):
            # 合并不替换：create 之后、回填之前打上的标签（尤其 __档案事实__ 这类
            # 系统标签）不能被 DeepSeek 的标签洗掉——8-03 真踩过一次
            existing: list = []
            try:
                cur = await rt.bucket_mgr.get(bucket_id)
                if cur:
                    existing = [str(t) for t in (cur.get("metadata", {}).get("tags") or [])]
            except Exception:
                pass
            update_kwargs["tags"] = list(dict.fromkeys(existing + [str(t) for t in meta["tags"]]))
        if meta.get("aliases"):
            # 引申词只喂 bm25（bm25_index.build 会吃它），不进给人看的标签行
            update_kwargs["aliases"] = meta["aliases"]
        if meta.get("subjects"):
            # 二改 B 件：主体（谁）。第三类标签，deepseek 抽 + 别名表归一，
            # 独立字段——不进 tags（会破坏字面校验）、不进 aliases（不该进 BM25 打分）。
            update_kwargs["subjects"] = normalize_subjects(meta["subjects"])
        if meta.get("domain"):
            # domain 只当文件夹用了（机制③ 第 5 条），检索不吃它；编成什么都无所谓
            update_kwargs["domain"] = meta["domain"]
        if meta.get("suggested_name"):
            update_kwargs["name"] = meta["suggested_name"]

    summary = await _make_summary(text)
    if summary:
        update_kwargs["summary"] = summary

    # 疑似同件提示（她 8-03 问的）：不合并、不拦路，只在后台查一次相似度，
    # 超阈值给新桶打个「疑似同件:xx」标签，消化（recall by=touched）时人眼定夺。
    # 阈值宁高勿低（她 8-02 的原话：哪怕贴的少，也比贴的多好）。
    # 2026-08-06 改成直接查**向量余弦**：原来用 search 综合分 ≥80，打分砍成两维后
    # 综合分的刻度整个变了；而「是不是同一件事」本来就该问语义距离，不该问检索排名。
    # mind 的相似另有去处（G1 pin 提醒：认知反复出现不是噪音，是准则在冒头）。
    if kind == "event":
        try:
            ee = getattr(rt.bucket_mgr, "embedding_engine", None)
            if ee and getattr(ee, "enabled", False):
                sims = await ee.search_similar(text, top_k=3)
                _top = next(((sid, s) for sid, s in sims if str(sid) != bucket_id), None)
                if _top:
                    rt.logger.info(f"[近似] {bucket_id} top={str(_top[0])[:6]} cos={float(_top[1]):.2f}")
                for sid, s in sims:
                    sid = str(sid)
                    if sid and sid != bucket_id and float(s) >= _DUP_COS_THRESHOLD:
                        tags_now = update_kwargs.get("tags") or []
                        update_kwargs["tags"] = list(dict.fromkeys(
                            tags_now + [f"疑似同件:{sid[:6]}"]))
                        break
        except Exception:
            pass
    elif kind == "mind":
        # G1（机制④ 第 4 条）：mind 的相似**要提醒**——老注释写「认知相似是常态
        # 不提示」，那是错的：认知反复出现不是噪音，**是准则在冒头**。
        # 阈值 0.80，跟疑似同件同一个数。打「相似认知:」标，breath 睁眼时提醒
        # 「转成朝向再 pin」（不能直接 pin 描述型的——把缺点钉成准则，
        # 语义就成了「我要犯这个错」）。
        try:
            ee = getattr(rt.bucket_mgr, "embedding_engine", None)
            if ee and getattr(ee, "enabled", False):
                sims = await ee.search_similar(text, top_k=6)
                for sid, s in sims:
                    sid = str(sid)
                    if not sid or sid == bucket_id or float(s) < _DUP_COS_THRESHOLD:
                        continue
                    sb = await rt.bucket_mgr.get(sid)
                    smeta = (sb or {}).get("metadata", {}) or {}
                    # 二改 A 件：别写 `"/MIND/" in room`（新房名开头没斜杠会静默不匹配）
                    if not is_mind_room(smeta.get("room")) \
                            and str(smeta.get("type") or "") not in ("feel", "i"):
                        continue
                    if smeta.get("superseded_by"):
                        continue  # 旧版认知不算「又冒头」——它就是同一条的前世
                    tags_now = update_kwargs.get("tags") or []
                    update_kwargs["tags"] = list(dict.fromkeys(
                        tags_now + [f"相似认知:{sid[:6]}"]))
                    rt.logger.info(f"[准则冒头] {bucket_id} ≈ {sid[:6]} cos={float(s):.2f}")
                    break
        except Exception:
            pass

    if not update_kwargs:
        return
    try:
        await rt.bucket_mgr.update(bucket_id, **update_kwargs)
    except Exception as e:
        rt.logger.warning(f"backfill update 失败 {bucket_id}（正文已落盘）: {e}")


async def _backfill_batch(pairs: list[tuple[str, str, str]]) -> None:
    """并发回填（pairs 每条 = (bucket_id, text, kind)）。串行的话 5 条 ×（analyze+summary ≈14s）要 70s+；
    analyze/_chat 只读无副作用可以并发（同 grow_items 的 [LENTO PATCH] 先例），
    update 各写各的桶、有 per-bucket 锁，互不打架。"""
    await asyncio.gather(
        *(_backfill_one(bucket_id, text, kind) for bucket_id, text, kind in pairs),
        return_exceptions=True,
    )
    # 回填写完缓存必失效——趁后台把全库解析缓存预热掉，别让下一次睁眼付 8 秒（codex 三轮 #5）
    try:
        await rt.bucket_mgr.list_all()
    except Exception:
        pass


# ------------------------------------------------------------
# 退役字段的闸（二改 C 件，2026-08-16）
# ------------------------------------------------------------
# 她 8-16 的两句话，就是这两个字段的判决书：
#   importance —— `decay_engine.py` 白纸黑字「importance 不参与」，唯一消费者自己的
#     注释说「它不再决定任何桶的生死」。留着一个我每次都要打、却谁都不读的分，
#     只是在给写入负担加码。
#   meaning    —— 「为什么重要，说白了就是这件事引起了你的思考……说白了重要的还是
#     思考产物」。留着它等于**给 event 开一个偷偷写 mind 的后门**，而写在 meaning
#     里的那句话没有来源链、不能 regrow、不能被发呆整合——是个死字段。
#     实证：`d52a38` 的 meaning 里躺着的那段，本身就是一条完整的 mind。
# 🔴 想说「为什么重要」，就 grow(kind="mind") 正经写一条，让它有来处、能换版。
_RETIRED_MSG = {
    "importance": (
        'importance 已退役（2026-08-16）——它不参与遗忘公式，'
        '「不再决定任何桶的生死」是它自己注释里的原话。别再打这个分。'),
    "meaning": (
        'meaning 已退役（2026-08-16）——想说「为什么重要」就写成一条真的认知：'
        'grow(kind="mind", room="MIND/TRAITS", text="…", from=["这条事件的id"], v=, a=)。'
        '写在 meaning 里的话没有来源链、不能 regrow、不能被发呆整合。'),
    "digested": (
        'digested 已退役（2026-08-16）——我们没有「消化」这个动作，'
        '所有的事件、认知都长新的。'),
}


def _retired_fields_msg(*names: str) -> str:
    """给一组已退役字段拼一条拒绝文案（照 _rooms.py 拒绝的样子：说清 + 给出路）。"""
    hits = [n for n in names if n in _RETIRED_MSG]
    return "\n".join(_RETIRED_MSG[n] for n in hits)


def _retired_item_fields(item: dict) -> str:
    """items[i] 里带了退役字段就返回拒绝文案，没带返回空串。"""
    hit = [n for n in ("importance", "meaning", "digested")
           if item.get(n) not in (None, "")]
    return _retired_fields_msg(*hit)


def _check_tense(tense: str) -> str | None:
    if tense and tense not in ("want",):
        return f'tense 无效：{tense}。可选："want"（想发生的）；已发生就不传。'
    return None


async def _apply_tense(bucket_id: str, tense: str, weight) -> str:
    """tense="want" → status="want"（终点走 resolved/abandoned）；weight=承诺压在心头多重。
    返回警告串（空=成功）。update 失败不能吞：正文在但朝向没写上，调用方得知道。"""
    if tense != "want":
        return ""
    kwargs: dict = {"status": "want"}
    if weight is not None:
        kwargs["weight"] = max(0.0, min(1.0, float(weight)))
    ok = await rt.bucket_mgr.update(bucket_id, **kwargs)
    return "" if ok else f"⚠️{bucket_id} 正文已存但 want 状态没写上，用 trace 补 status"


async def backfill_sweep() -> int:
    """启动自愈（codex 复核第 3 条）：裸 asyncio.create_task 的在飞回填会随重启丢失，
    留下只有正文+占位元数据的桶。开机把它们找回来重新回填。

    不建持久队列——**扫描本身就是队列**：「room 有值（=新路径存的）且 summary 缺失」
    是可靠的未完成标记，成功回填后标记自动消失，天然幂等。
    老 grow 桶（批 1 之前的）没有 room，不会被误扫。

    2026-08-06（B5）：去掉 source_tool 判据。原来只认 grow/regrow → 迁移进来的
    老桶（source_tool=hold/import…）永远补不上，444 条漏了两个月。
    「room 有值但 summary 缺」本身就是完整的判据，来源是谁不重要。
    """
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.warning(f"backfill_sweep 扫描失败: {e}")
        return 0
    pending: list[tuple[str, str, str]] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not meta.get("room") or meta.get("summary"):
            continue
        kind = "mind" if is_mind_room(meta.get("room")) else "event"
        pending.append((str(b.get("id")), str(b.get("content") or ""), kind))
    if pending:
        rt.logger.info(f"backfill_sweep: 补 {len(pending)} 个上次没回填完的桶")
        await _backfill_batch(pending)
    return len(pending)


# ------------------------------------------------------------
# kind="event"
# ------------------------------------------------------------

def _normalize_from(from_ids) -> tuple[list[str] | None, str]:
    """归一化 from 列表并做条数/总长校验。返回 (ids, 错误信息)。ids=None 表示没传。"""
    if from_ids is None:
        return None, ""
    if isinstance(from_ids, str):
        from_ids = [s.strip() for s in from_ids.split(",") if s.strip()]
    if not isinstance(from_ids, list):
        return None, "from 要传 bucket_id 列表。"
    ids = [str(s).strip() for s in from_ids if str(s).strip()]
    if not ids:
        return None, ""
    if len(ids) > _FROM_MAX:
        return None, (f"from 最多 {_FROM_MAX} 条（收到 {len(ids)} 条）。"
                      "底层字段 64 字符上限，多了会被静默截断成半截 id——拆开分别存。")
    joined = ",".join(ids)
    if len(joined) > _TRIGGERED_BY_LIMIT:
        # 条数够但单个 id 太长（历史 feel_… 这类可读 id）也会被截断（codex 第 5 条）
        return None, (f"from 拼起来 {len(joined)} 字符，超过底层 {_TRIGGERED_BY_LIMIT} 上限，"
                      "会被静默截断——减少条数或拆开存。")
    return ids, ""


async def grow_event(items: list, tense: str = "", weight=None,
                     from_ids=None, test_data: bool = False) -> str:
    if not isinstance(items, list) or not items:
        return 'kind="event" 需要 items=[{room, text, when?}, ...]，至少一条。'
    tense_err = _check_tense(tense)
    if tense_err:
        return tense_err
    # event 的 from 可选（她 8-03 拍的：want 尽量带 from，不强制——不然太凭空捏造了）。
    # 传了就校验存在性并写进每条的 triggered_by。
    from_ids, from_err = _normalize_from(from_ids)
    if from_err:
        return from_err
    if from_ids:
        missing = []
        for fid in from_ids:
            if not await rt.bucket_mgr.get_including_archive(fid):
                missing.append(fid)
        if missing:
            return f"from 里这些 id 不存在：{', '.join(missing)}。"

    # --- 校验先行：任何一条不合法 → 全部拒绝，不创建任何桶 ---
    cleaned: list[dict] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return f"items[{idx}] 必须是对象 {{room, text, v, a, when?}}，收到：{type(item).__name__}"
        room = str(item.get("room") or "").strip()
        text = str(item.get("text") or "")  # 逐字落盘：不 strip 正文（codex 复核第 4 条）
        when = str(item.get("when") or "").strip()
        room_err = check_room(room, "event")
        if room_err:
            return f"items[{idx}]: {room_err}"
        if not text.strip():
            return f"items[{idx}]: text 不能为空。"
        # 施工 6 · A 件：tense="want" 时 when 多一种合法写法——时长记号（"有量级"）。
        # 三类怎么读见 core/profile.py._三类钟；这儿只管"存不存得进去"。
        if when:
            _when_ok = bool(_WHEN_RE.match(when))
            if not _when_ok and tense == "want" and _WANT_DURATION_RE.match(when):
                _when_ok = True
            if not _when_ok:
                if tense == "want":
                    return (f"items[{idx}]: when 格式无效：{when}。三种填法：有期限写 "
                            "YYYY-MM-DD；有量级写时长记号（3w=3周/10d=10天/2m=2个月/1y=1年）；"
                            "等触发就不填 when，条件写在正文里。")
                return f"items[{idx}]: when 格式无效：{when}。用 YYYY-MM-DD（可带时间），不填＝存入时间。"
        size_err = check_content_size(text)
        if size_err:
            return f"items[{idx}]: {size_err}"
        # --- v/a 必填（机制③ 第 3 条，2026-08-06 定死）：event 的情绪坐标也是
        # 我自己打的，不留「模型回填」的后门——衰减公式里 valence 决定忘得多快，
        # 模型猜的情绪不能决定我忘什么。她的原话：「要自己打！不要模型打」。
        try:
            iv = float(item.get("v"))
            ia = float(item.get("a"))
        except (TypeError, ValueError):
            return (f"items[{idx}]: v/a 必填——这条事件当时给我的感觉，自己打（0~1），"
                    "不外包给模型。v=效价(0难受~1开心) a=唤醒(0平静~1强烈)。")
        if not (0 <= iv <= 1 and 0 <= ia <= 1):
            return f"items[{idx}]: v/a 必须在 0~1 之间（收到 v={iv}, a={ia}）。"
        # --- importance / meaning：**已退役，当场拒**（二改 C 件，2026-08-16）---
        # 🔴 选「拒」不选「静默忽略」：静默忽略的话我会继续填，填进去的东西
        #    直接掉进虚空，而我永远不会知道——那比报错难查一百倍。
        retired = _retired_item_fields(item)
        if retired:
            return f"items[{idx}]: {retired}"
        cleaned.append({"room": room, "text": text, "when": when,
                        "v": iv, "a": ia})

    # --- 逐条直接 create：不 search、不 judge、不合并 ---
    batch_id = f"g_{uuid.uuid4().hex[:12]}"
    ph = _placeholder_meta()
    results: list[str] = []
    pairs: list[tuple[str, str, str]] = []
    # 同文防重（她 8-03 提的）：一字不差的正文再存 → 还原 id，不建新桶。
    # 措辞不同的重复不拦——那是真的两次记录，看见了想清可以 trace。
    # ⚠️ 用解析缓存整批查一次（find_exact_content 逐条全库扫盘，bind mount 上 ~3s/条，
    # 8-03 把 5 条批量拖到 16 秒——那就是它）。
    existing_by_content: dict[str, str] = {}
    try:
        for _b in await rt.bucket_mgr.list_all(include_archive=False):
            _m = _b.get("metadata", {}) or {}
            if not _m.get("deleted_at"):
                existing_by_content.setdefault(str(_b.get("content") or ""), str(_m.get("id") or ""))
    except Exception:
        pass

    for item in cleaned:
        dup_id = existing_by_content.get(item["text"])
        if dup_id:
            results.append(f"♻️{dup_id} 已存过（同文，未重建）")
            continue
        bucket_id = await rt.bucket_mgr.create(
            content=item["text"],
            tags=ph["tags"],
            importance=ph["importance"],   # 中性占位；importance 已退役、不再由我打
            domain=ph["domain"],
            valence=item["v"],
            arousal=item["a"],
            name=None,
            source_tool="grow",
            grow_batch_id=batch_id,
            room=item["room"],
            when=item["when"],
            from_ids=",".join(from_ids) if from_ids else "",
            test_data=test_data,
        )
        tense_warn = await _apply_tense(bucket_id, tense, weight)
        if tense_warn:
            results.append(tense_warn)
        results.append(f"📝{bucket_id} {item['room']}")
        pairs.append((bucket_id, item["text"], "event"))
        existing_by_content.setdefault(item["text"], bucket_id)

    # --- 元数据后补：打标 / 摘要 / 起名走后台，失败只留警告 ---
    asyncio.create_task(_backfill_batch(pairs))

    dup_n = sum(1 for r in results if r.startswith("♻️"))
    head = f"{len(pairs)}条 event 已落盘 batch:{batch_id}"
    if dup_n:
        head += f"（另 {dup_n} 条同文已存过，未重建）"
    if tense == "want":
        head += " [want]"
    out = head + "（标签/摘要后台回填中，几十秒内可检索）\n" + "\n".join(results)

    # 超长提一句，**拆不拆、怎么拆由我当场判断**（她 8-05 定的触发方式）。
    # 她的理由：一条里挤了好几件事 → tags 里每个主题只占一两个词，哪个都不突出，
    # 场景锚点也混（几件事的画面搅在一起）。现成的反例是 3ce26609 那条，塞了四件事。
    long_ones = [(bid, len(txt)) for bid, txt, _ in pairs if len(txt) >= _LONG_HINT]
    for bid, n in long_ones:
        out += (f"\n📏 {bid} 有 {n} 字——真是好几件事就分成几条重存"
                f"（正文在你手里，逐字贴过去，别改字），一件事就别管这条提示。")
    return out


# ------------------------------------------------------------
# kind="big" —— 大 event / **时期**：盖在一段时间上的一句话（2026-08-05 她定的九条）
# ------------------------------------------------------------

# ⚰️ 2026-08-18：`grow_big` 连同 `kind="big"` 那个入口一起删了。
#    它只是把 fold 的核心（`_F.落一条gist`）包了一层——立一个「时期」有两个入口，
#    而两个入口迟早说两套话。现在只剩 fold(when="起..止") 一条路。



# ------------------------------------------------------------
# kind="mind"
# ------------------------------------------------------------

async def grow_mind(room: str, text: str, from_ids, v, a,
                    tense: str = "", weight=None,
                    importance=None, meaning: str = "",
                    test_data: bool = False) -> str:
    room = str(room or "").strip()
    text = str(text or "")  # 逐字落盘：不 strip 正文
    room_err = check_room(room, "mind")
    if room_err:
        return room_err
    if not text.strip():
        return "text 不能为空。"
    size_err = check_content_size(text)
    if size_err:
        return size_err
    tense_err = _check_tense(tense)
    if tense_err:
        return tense_err
    # importance / meaning 已退役 —— 当场拒，别静默吞（二改 C 件，理由见 _RETIRED_MSG）。
    # 形参留着是为了能报出这条人话；删掉形参的话调用方拿到的是 pydantic 的
    # 「unexpected keyword」，看不出发生了什么、更看不出该改成什么。
    retired = _retired_fields_msg(
        *[n for n, val in (("importance", importance), ("meaning", meaning))
          if val not in (None, "")])
    if retired:
        return retired

    # --- from 必填，一条都不能少；一条没有来处的自我认识跟一条编的读起来一模一样 ---
    from_ids, from_err = _normalize_from(from_ids)
    if from_err:
        return from_err
    if not from_ids:
        return ("from 必填：这条认知是从哪几条记忆看出来的？填真 bucket_id 列表。"
                "确实凭空想的，就在正文里老实标「凭空想的」，并把来源指到相关的事件上。")
    missing = []
    for fid in from_ids:
        found = await rt.bucket_mgr.get_including_archive(fid)
        if not found:
            missing.append(fid)
    if missing:
        return f"from 里这些 id 不存在：{', '.join(missing)}。填 grow(kind=\"event\") 返回的真 id。"

    # --- v/a 必填，调用方自己打，不许外包给模型 ---
    try:
        v = float(v)
        a = float(a)
    except (TypeError, ValueError):
        return "v/a 必填：MIND 的情绪坐标是你自己打的（0~1），不外包给模型。"
    if not (0 <= v <= 1 and 0 <= a <= 1):
        return f"v/a 必须在 0~1 之间（收到 v={v}, a={a}；没传会是 -1）。MIND 的坐标你自己打。"

    ph = _placeholder_meta()
    bucket_id = await rt.bucket_mgr.create(
        content=text,
        tags=ph["tags"],
        importance=ph["importance"],   # 中性占位；importance 已退役
        domain=ph["domain"],
        valence=v,
        arousal=a,
        name=None,
        from_ids=",".join(from_ids),
        source_tool="grow",
        room=room,
        test_data=test_data,
    )
    tense_warn = await _apply_tense(bucket_id, tense, weight)
    try:
        await rt.bucket_mgr.touch_many(from_ids)  # 提炼认知=想起了来源（codex 三轮 #4）
    except Exception:
        pass

    # mind 回填只补 aliases/摘要/起名（不抽 scene、不碰 v/a）
    asyncio.create_task(_backfill_batch([(bucket_id, text, "mind")]))

    head = f"🧠mind→{bucket_id} {room} ←{{{','.join(from_ids)}}} V{v:.2f}/A{a:.2f}"
    if tense == "want":
        head += " [want]"
    if tense_warn:
        head += "\n" + tense_warn
    return head + "（标签/摘要后台回填中）"
