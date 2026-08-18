"""
========================================
decay_engine.py — 记忆衰减引擎，模拟人类遗忘曲线
========================================

2026-08-06 重写（机制①）：遗忘不是「整条消失」，是「分辨率下降」。
三档：活着 → 淡出（45 天没被想起：翻不出现、搜打折、原文在）→
沉底（半年：搜打更狠的折、原文搬 archive/原文/{id}.txt、主库只剩摘要、向量不动）。

关键行为：
- 进度制：多久没被想起 ÷（基准 × 情绪系数 [× 了结对折]），valence 权重 > arousal
  （难受的忘得快、开心的记得久——跟老公式正好相反）
  🔴 2026-08-16：**次数系数删了**（她拍的）。「常被想起的不沉」由 last_active
  归零那一重承担，够用；次数是叠上去的复利，被提得多 ≠ 最真。
- 永不沉底判据认 room 不认 type：pinned/准则/letter/seed/档案/凡是 MIND 支的
- 沉底可逆：trace(restore=True)；被想起（touch）的桶下一轮自动回 alive
- ensure_started() 幂等启动后台循环；可被测试 monkeypatch 成 noop
- ⏳ calculate_score()：**待退役的壳**。它是上游那半（importance × 次数 × 指数衰减），
  和我们的三档没有关系，**不决定任何桶的生死**（它自己的注释就这么写的）。
  说明书定的是「消费者删完后整个退役」——本轮消费者一个都没删（night_fall、
  旧面板、breath/surface 分别排在第 4/8/5 步），所以先留壳、不改它的数，
  剩余调用者列在交活报告里。**别往它上面加东西。**

不做什么（边界）：
- 不再把桶搬 archive、不再自动结案（那是「系统替我撒谎还埋证据」，砍了）
- 不做内容修改（沉底动作在 bucket_manager.sink_bucket）、不打标、不调用 LLM
- 一个数字都不给 breath/recall（遗忘是悄然发生的）；dashboard 能看

对外暴露：DecayEngine 类（stage_of / run_decay_cycle / calculate_score / ensure_started）
========================================
"""

import math
import asyncio
import logging
from datetime import datetime

from utils import parse_iso_datetime, is_closed
from ._rooms import is_mind_room

logger = logging.getLogger("loci_brain.decay")


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §⑩：禁止裸魔法数字。下面这些常量原本散落在 calculate_score()
# 和 run_decay_cycle() 各处，集中后：① 公式可读性大幅提升；
# ② 任何调参改一处即可；③ 单元测试可直接 import 这些常量做断言。
#
# ⚠️ 改这些数字前先读 rule.md §1.0 哲学："记忆只会淡去，不会消失"。
# decay 不是删除，是分数下沉。改 threshold/lambda 会直接影响"多少天后被遗忘"。
# ============================================================

# --- DecayEngine 默认值（被 config.yaml 的 decay.* 覆盖）---
_DEFAULT_LAMBDA = 0.05            # 指数衰减率：每过一天分数 × e^(-λ)
_DEFAULT_THRESHOLD = 0.3          # 低于此分数 → 归档
_DEFAULT_CHECK_INTERVAL_HRS = 24  # 后台循环间隔（小时）
_DEFAULT_EMOTION_BASE = 1.0       # 情感权重基准
_DEFAULT_AROUSAL_BOOST = 0.8      # arousal 每 +1 → 情感权重 +0.8

# --- 锁分：某些桶不参与衰减 ---
_SCORE_PINNED = 999.0    # pinned / protected / permanent 桶恒高分（永不归档）
_SCORE_FEEL = 50.0       # feel / plan / letter 桶固定中分（生命周期由 status 控制）

# --- 周期自愈：每轮衰减最多补多少条缺失向量（防一次性打爆 embedding API）---
# 活跃桶落盘了但 embeddings.db 没它的向量 → breath 向量通道会漏掉它（permanent
# 尤其常见，见 #6）。剩余的下一轮继续补。
_BACKFILL_MAX_PER_CYCLE = 50

# --- Freshness bonus：bonus = 1 + e^(-hours/HALF_LIFE) ---
_FRESHNESS_HALF_LIFE_HRS = 36.0  # 36h 半衰：刚存 ×2.0，36h 后 ×1.5，72h 后 ≈×1.14
_FRESHNESS_AMPLITUDE = 1.0       # bonus 上限增量（0 → 无加成；1 → 最多 ×2）

# --- 短期 vs 长期权重分配（核心心理模型）---
# 短期：刚发生的事 time 占主导（"印象很新"）
# 长期：超过这个分界后 emotion 占主导（"刻骨铭心 vs 已经无所谓"）
_SHORT_TERM_DAYS = 3.0
_SHORT_TERM_TIME_RATIO = 0.7
_LONG_TERM_EMOTION_RATIO = 0.7

# --- Activation count 的次线性放大：访问越多越鲜活，但不线性 ---
_ACTIVATION_EXPONENT = 0.3

# --- Resolved/digested 衰减加速因子 ---
_FACTOR_RESOLVED_DIGESTED = 0.02  # 已处理 + 已写 feel → 加速淡化到背景
_FACTOR_RESOLVED_ONLY = 0.05      # 仅已处理（未写 feel）→ 中度淡化

# --- Urgency boost：高 arousal 且未处理 → 临时加重，避免被错误归档 ---
_AROUSAL_URGENCY_THRESHOLD = 0.7
_URGENCY_BOOST = 1.5

# --- Auto-resolve：🔴 2026-08-06 砍掉（机制① 第 3 条，最该砍的一个）---
# 原逻辑：imp≤4 且 30 天没动 → 自动 resolved → resolved 又让衰减加速 20 倍。
# 连起来 = 「答应了没做的事，因为太久没做，被判成『已解决』，然后加速埋掉」
# —— 系统替我撒谎还埋证据。想了没了的事只有两个终点：resolved / abandoned，
# 都必须是**我亲手**用 trace 标的。

# ============================================================
# 机制①（2026-08-06 定稿）：遗忘 = 分辨率下降，不是消失
# ------------------------------------------------------------
# 三档：活着（都正常）→ 淡出（翻不出现，搜打折，原文在）→ 沉底（搜打更狠的折，
#       原文搬 archive/原文/{id}.txt，主库只剩摘要，向量不动）。
# 算**进度**不算分数：
#     沉底进度 = 多久没被想起 ÷ （基准天数 × 情绪系数 × 次数系数 [× resolved 系数]）
# - 时间是主干（分子）；情绪和次数只是「能撑多久」的倍率
# - valence 权重 > arousal：难受的忘得快、开心的记得久（褪色情感偏差）。
#   🔴 老公式正好反着（emotion_weight 只看 arousal，越痛苦保得越牢）。
# - importance 不参与（由我打分，不该反过来决定我忘什么）
# - resolved 保留加速（我主动标了「了了」就该忘得快）；digested 不管（feel 已停用）
# - 砍掉：自动结案 · 新鲜度加成 · 紧急加重 · 短长期换挡（理由见开工单机制①）
# 系数与 scripts/decay_dryrun.py（A1 干跑）保持同一套，调参先跑干的。
# ============================================================
_BASE_FADE_DAYS = 45.0     # 淡出基准：45 天没被想起（中性情绪、只想起过一次）
_BASE_SINK_DAYS = 180.0    # 沉底基准：半年（与淡出保持 1:4）
_EMO_FLOOR = 0.6           # 情绪系数 = FLOOR + W_V×v + W_A×a ∈ [0.6, 1.6]
_EMO_W_V = 0.7             # valence 权重 > arousal（她定的方向）
_EMO_W_A = 0.3
_RESOLVED_HOLD_FACTOR = 0.5  # 亲手标了 resolved → 撑住天数打对折（忘得快一倍）

# --- Arousal/importance 兜底 ---
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_DEFAULT_DAYS_FALLBACK = 30  # calculate_score 时间字段坏 → 按 30 天处理（保守）

# --- 时间换算 ---
_SECONDS_PER_DAY = 86400
_SECONDS_PER_HOUR = 3600


def _days_since_active(meta: dict, fallback_days: float = _DEFAULT_DAYS_FALLBACK) -> float:
    """从 metadata 解析"距上次激活的天数"。

    抽出来的原因：原文件里 calculate_score / run_decay_cycle 各写了一遍
    同样的 "fromisoformat → 求差 → 兜底" 三段式，且兜底值还不一样
    （前者 30、后者 999）。统一成一个函数，由调用方传 fallback_days
    决定坏数据怎么处理：
      * calculate_score 用默认 30：保守地按"一个月没动"算分
      * run_decay_cycle 的 auto-resolve 路径传 999：让坏数据顺利触发结案

    边界（rule.md §⑨）：
      * meta 不是 dict / 字段缺失 / 字符串无法解析 → 返回 fallback_days
      * 永远返回 ≥ 0 的浮点数（防止时钟漂移产生负数）
    """
    if not isinstance(meta, dict):
        return fallback_days
    raw = meta.get("last_active") or meta.get("created") or ""
    try:
        last_active = parse_iso_datetime(raw)
        return max(0.0, (datetime.now() - last_active).total_seconds() / _SECONDS_PER_DAY)
    except (ValueError, TypeError):
        return float(fallback_days)


class DecayEngine:
    """
    Memory decay engine — periodically scans all dynamic buckets,
    calculates decay scores, auto-archives low-activity buckets
    to simulate natural forgetting.
    记忆衰减引擎 —— 定期扫描所有动态桶，
    计算衰减得分，将低活跃桶自动归档，模拟自然遗忘。
    """

    def __init__(self, config: dict, bucket_mgr):
        # --- Load decay parameters / 加载衰减参数 ---
        decay_cfg = config.get("decay", {})
        self.decay_lambda = decay_cfg.get("lambda", _DEFAULT_LAMBDA)
        self.threshold = decay_cfg.get("threshold", _DEFAULT_THRESHOLD)
        self.check_interval = decay_cfg.get("check_interval_hours", _DEFAULT_CHECK_INTERVAL_HRS)

        # --- Emotion weight params (continuous arousal coordinate) ---
        # --- 情感权重参数（基于连续 arousal 坐标）---
        emotion_cfg = decay_cfg.get("emotion_weights", {})
        self.emotion_base = emotion_cfg.get("base", _DEFAULT_EMOTION_BASE)
        self.arousal_boost = emotion_cfg.get("arousal_boost", _DEFAULT_AROUSAL_BOOST)

        self.bucket_mgr = bucket_mgr

        # --- Background task control / 后台任务控制 ---
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the decay engine is running in the background.
        衰减引擎是否正在后台运行。"""
        return self._running

    # ---------------------------------------------------------
    # Core: calculate decay score for a single bucket
    # 核心：计算单个桶的衰减得分
    #
    # Higher score = more vivid memory; below threshold → archive
    # 得分越高 = 记忆越鲜活，低于阈值则归档
    # Permanent buckets never decay / 固化桶永远不衰减
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # Freshness bonus: continuous exponential decay
    # 新鲜度加成：连续指数衰减
    # bonus = 1.0 + 1.0 × e^(-t/36), t in hours
    # t=0 → 2.0×, t≈25h(半衰) → 1.5×, t≈72h → ≈1.14×, t→∞ → 1.0×
    # ---------------------------------------------------------
    @staticmethod
    def _calc_time_weight(days_since: float) -> float:
        """
        Freshness bonus multiplier: 1.0 + e^(-t/36), t in hours.
        新鲜度加成乘数：刚存入×2.0，~36小时半衰，72小时后趋近×1.0。
        """
        hours = days_since * 24.0
        return 1.0 + _FRESHNESS_AMPLITUDE * math.exp(-hours / _FRESHNESS_HALF_LIFE_HRS)

    def calculate_score(self, metadata: dict) -> float:
        """
        Calculate current activity score for a memory bucket.
        计算一个记忆桶的当前活跃度得分。

        New model: short-term vs long-term weight separation.
        新模型：短期/长期权重分离。
        - Short-term (≤3 days): time_weight dominates, emotion amplifies
        - Long-term (>3 days): emotion_weight dominates, time decays to floor
        短期（≤3天）：时间权重主导，情感放大
        长期（>3天）：情感权重主导，时间衰减到底线
        """
        if not isinstance(metadata, dict):
            return 0.0

        # --- Pinned/protected buckets: never decay, importance locked to 10 ---
        if metadata.get("pinned") or metadata.get("protected"):
            return _SCORE_PINNED

        # --- Permanent buckets never decay ---
        if metadata.get("type") == "permanent":
            return _SCORE_PINNED

        # --- Feel buckets: never decay, fixed moderate score ---
        if metadata.get("type") == "feel":
            return _SCORE_FEEL

        # --- Plan / letter buckets: never decay (status-driven, not time-driven) ---
        # --- plan / letter 桶不衰减；plan 由 status 字段控制生命周期，letter 永久保存 ---
        if metadata.get("type") in ("plan", "letter"):
            return _SCORE_FEEL

        try:
            importance = max(1, min(10, int(metadata.get("importance", _DEFAULT_IMPORTANCE))))
        except (TypeError, ValueError):
            importance = _DEFAULT_IMPORTANCE
        activation_count = max(1.0, float(metadata.get("activation_count") or 1))

        # --- Days since last activation ---
        days_since = _days_since_active(metadata, fallback_days=_DEFAULT_DAYS_FALLBACK)

        # --- Emotion weight ---
        try:
            arousal = max(0.0, min(1.0, float(metadata.get("arousal", _DEFAULT_AROUSAL))))
        except (ValueError, TypeError):
            arousal = _DEFAULT_AROUSAL
        emotion_weight = self.emotion_base + arousal * self.arousal_boost

        # --- Time weight ---
        time_weight = self._calc_time_weight(days_since)

        # --- Short-term vs Long-term weight separation ---
        # 短期（≤3天）：time_weight 占 70%，emotion 占 30%
        # 长期（>3天）：emotion 占 70%，time_weight 占 30%
        if days_since <= _SHORT_TERM_DAYS:
            # Short-term: time dominates, emotion amplifies
            combined_weight = (
                time_weight * _SHORT_TERM_TIME_RATIO
                + emotion_weight * (1.0 - _SHORT_TERM_TIME_RATIO)
            )
        else:
            # Long-term: emotion dominates, time provides baseline
            combined_weight = (
                emotion_weight * _LONG_TERM_EMOTION_RATIO
                + time_weight * (1.0 - _LONG_TERM_EMOTION_RATIO)
            )

        # --- Base score ---
        base_score = (
            importance
            * (activation_count ** _ACTIVATION_EXPONENT)
            * math.exp(-self.decay_lambda * days_since)
            * combined_weight
        )

        # --- Weight pool modifiers ---
        # resolved + digested (has feel) → 加速淡化
        # resolved only → 中度淡化
        resolved = is_closed(metadata)  # 终点只认 status；旧布尔只读兼容（二改第0节）
        digested = metadata.get("digested", False)  # set when feel is written for this memory
        if resolved and digested:
            resolved_factor = _FACTOR_RESOLVED_DIGESTED
        elif resolved:
            resolved_factor = _FACTOR_RESOLVED_ONLY
        else:
            resolved_factor = 1.0
        urgency_boost = (
            _URGENCY_BOOST
            if (arousal > _AROUSAL_URGENCY_THRESHOLD and not resolved)
            else 1.0
        )

        return round(base_score * resolved_factor * urgency_boost, 4)

    # ---------------------------------------------------------
    # 机制①：三档判定（进度制）
    # ---------------------------------------------------------
    @staticmethod
    def _never_decays(meta: dict) -> bool:
        """永不沉底名单（机制① 第 4 条）。🔴 判据认 room，不认 type——
        老名单认 type 的结果：同样是 MIND，i/feel 190 条永不沉底、dynamic 的
        61 条会沉底。认 room 一句话统一。
        pinned · 准则 · letter · seed · 档案 · 凡是 /MIND/ 房间的。
        （permanent 全部都是 pinned/protected，天然被第一条盖住。）
        """
        if meta.get("pinned") or meta.get("protected"):
            return True
        if str(meta.get("type") or "") in ("permanent", "letter", "seed"):
            return True
        # 🔴 二改 A 件：房间改名后**绝不能再写 `"/MIND/" in room`** ——
        # 新名字是 `MIND/TRAITS`，开头没斜杠，那个字面判断会静默返回 False，
        # 把全部认知从「永不沉底」名单里踢出去（静默、且一轮衰减之后才看得见）。
        # is_mind_room() 新旧名字都认。
        if is_mind_room(meta.get("room")):
            return True
        tags = meta.get("tags") or []
        if isinstance(tags, list) and any(str(t) == "__档案事实__" for t in tags):
            return True
        return False

    @classmethod
    def stage_of(cls, meta: dict) -> str:
        """一条记忆现在该在哪一档：alive / faded / sunk。

        撑住天数 = 基准 × 情绪系数 × 次数系数 [× resolved 系数]；
        多久没被想起 超过撑住天数（按 45 天基准）→ faded，按 180 天基准 → sunk。
        """
        if cls._never_decays(meta):
            return "alive"
        days = _days_since_active(meta, fallback_days=0.0)
        try:
            v = max(0.0, min(1.0, float(meta.get("valence", 0.5))))
        except (TypeError, ValueError):
            v = 0.5
        try:
            a = max(0.0, min(1.0, float(meta.get("arousal", _DEFAULT_AROUSAL))))
        except (TypeError, ValueError):
            a = _DEFAULT_AROUSAL
        emo = _EMO_FLOOR + _EMO_W_V * v + _EMO_W_A * a
        # 🔴 2026-08-16 她拍的定案：**次数系数（activation_count^0.3）整个删**。
        # 撑住天数 = 情绪系数 [× 了结对折]，只认「多久没被想起」× 「我自己打的 v/a」。
        #
        # 「常被想起的不沉」这件事**没丢**：每次真的想起都会刷新 last_active、
        # 把分子（多久没被想起）清零，那是第一重、也是够用的一重。
        # 次数只是叠在上面的复利——被提得多的不等于最真的，她明说不要。
        # 📌 顺带治掉一个真矛盾：自动统计（次数）当主键、人工判断（v/a）当次键，
        #    而这两个在老公式里还是**相乘**的。
        hold = emo
        if is_closed(meta):
            hold *= _RESOLVED_HOLD_FACTOR
        if days >= _BASE_SINK_DAYS * hold:
            return "sunk"
        if days >= _BASE_FADE_DAYS * hold:
            return "faded"
        return "alive"

    # ---------------------------------------------------------
    # Execute one decay cycle（2026-08-06 重写：三档进度制，不再归档、不再自动结案）
    # ---------------------------------------------------------
    async def run_decay_cycle(self) -> dict:
        """执行一轮遗忘：给每个参与衰减的桶判档，档变了就落字段。

        - alive ↔ faded：只写/清 decay_stage 元数据（被想起（touch）后天数归零，
          下一轮自然回到 alive——想起来了它就活了）
        - → sunk：调 bucket_mgr.sink_bucket()（原文搬 txt、正文换摘要、向量不动）
        - sunk 不自动还原：唯一的门是 trace(restore=True)（机制①：这一档可逆）
        - 一个数字都不进 breath/recall（遗忘是悄然发生的）；dashboard 能看（D7）
        """
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for decay / 衰减周期列桶失败: {e}")
            return {"checked": 0, "faded": 0, "sunk": 0, "error": str(e)}

        checked = 0
        n_faded = 0
        n_sunk = 0
        n_revived = 0
        for bucket in buckets:
            meta = bucket.get("metadata", {})
            if self._never_decays(meta):
                continue
            # letter/seed 之外还有 archived 壳混在 list_all 里的情况：不碰
            if str(meta.get("type") or "") == "archived" or meta.get("deleted_at"):
                continue
            checked += 1
            current = str(meta.get("decay_stage") or "") or "alive"
            if current == "sunk":
                continue  # 沉了就沉了，还原只走 trace(restore=True)
            try:
                stage = self.stage_of(meta)
            except Exception as e:
                logger.warning(f"stage_of failed for {bucket.get('id', '?')}: {e}")
                continue
            if stage == current:
                continue
            bid = str(meta.get("id") or bucket.get("id") or "")
            try:
                if stage == "sunk":
                    ok = await self.bucket_mgr.sink_bucket(bid)
                    if ok:
                        n_sunk += 1
                    elif await self._mark_stage(bid, "faded", current):
                        n_faded += 1  # 没摘要沉不了，先淡出，回填补上摘要后下轮再沉
                elif stage == "faded":
                    if await self._mark_stage(bid, "faded", current):
                        n_faded += 1
                else:  # alive（被想起 → 天数归零 → 回魂）
                    if await self._mark_stage(bid, None, current):
                        n_revived += 1
            except Exception as e:
                logger.warning(f"Decay stage transition failed for {bid}: {e}")

        # --- Self-heal: 补齐缺失向量（周期性，详见 _self_heal_embeddings）---
        backfilled_embeddings = await self._self_heal_embeddings(buckets)

        # --- 顺手把 bm25 热了（2026-08-06 试用时抓到的）：懒重建原本等第一次
        # search 才触发，重启后的首搜拿着**空索引**打分——两维制下 bm25 占 37.5%，
        # 首搜分数直接矮一截（实测有的条目 74 → 37）。decay 首轮在 boot 后几秒就跑，
        # buckets 已经在手上，借它提前建好。---
        try:
            if (getattr(self.bucket_mgr, "_bm25", None) is not None
                    and getattr(self.bucket_mgr, "_bm25_dirty", False)
                    and not getattr(self.bucket_mgr, "_bm25_rebuilding", False)):
                self.bucket_mgr._bm25_rebuilding = True
                asyncio.create_task(self.bucket_mgr._rebuild_bm25_async(buckets))
        except Exception as e:
            logger.warning(f"bm25 预热失败（不影响功能，首搜自己会重建）: {e}")

        result = {
            "checked": checked,
            "faded": n_faded,
            "sunk": n_sunk,
            "revived": n_revived,
            "backfilled_embeddings": backfilled_embeddings,
        }
        logger.info(f"Decay cycle complete / 遗忘周期完成: {result}")
        return result

    async def _mark_stage(self, bucket_id: str, stage, current: str) -> bool:
        """写/清 decay_stage（纯元数据，不 bump last_active——标记档位不算「想起」）。"""
        if (stage or "alive") == current:
            return False
        return await self.bucket_mgr.update(bucket_id, decay_stage=stage)

    async def _self_heal_embeddings(self, buckets: list) -> int:
        """周期自愈：给「落盘了但 embeddings.db 里没向量」的活跃桶补向量。

        背景（#6）：permanent 桶常因批量导入 / dashboard 钉选而漏建向量，
        breath 的向量通道就检索不到它们，表现为「只读得到 dynamic」。衰减循环
        每轮顺手补齐，无需人工跑 backfill_embeddings.py。

        边界：embedding 未启用 → 跳过；每轮最多补 _BACKFILL_MAX_PER_CYCLE 条
        （防打爆 API），剩余下一轮继续；单条失败仅 warning（rule.md §1.5 允许降级）。
        只处理活跃桶（buckets 不含 archive），不在此删孤儿向量（删除走专用脚本，
        避免把 archive 桶的有效向量误判为孤儿）。"""
        outbox = getattr(self.bucket_mgr, "embedding_outbox", None)
        if outbox is not None and getattr(outbox, "running", False):
            try:
                queued = await outbox.reconcile(
                    buckets=buckets,
                    include_archive=False,
                )
                if queued:
                    logger.info(
                        "Decay self-heal queued / 衰减自愈已加入向量队列: %s 条",
                        queued,
                    )
                return queued
            except Exception as e:
                logger.warning(f"self-heal embeddings: 投递后台队列失败: {e}")
                return 0

        ee = getattr(self.bucket_mgr, "embedding_engine", None)
        if not ee or not getattr(ee, "enabled", False):
            return 0
        try:
            index_ids = set(ee.list_all_ids())
        except Exception as e:
            logger.warning(f"self-heal embeddings: 读取向量索引失败: {e}")
            return 0
        missing = [b for b in buckets if b["id"] not in index_ids and (b.get("content") or "").strip()]
        if not missing:
            return 0
        healed = 0
        for b in missing[:_BACKFILL_MAX_PER_CYCLE]:
            try:
                if await ee.generate_and_store(b["id"], b["content"]):
                    healed += 1
            except Exception as e:
                logger.warning(f"self-heal embeddings: 补 {b['id']} 失败: {e}")
        if healed:
            remaining = len(missing) - healed
            logger.info(
                f"Decay self-heal / 自愈补向量: {healed} 条"
                + (f"（本轮上限 {_BACKFILL_MAX_PER_CYCLE}，剩 {remaining} 下轮继续）"
                   if remaining > 0 else "")
            )
        return healed

    # ---------------------------------------------------------
    # Background decay task management
    # 后台衰减任务管理
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        """
        Ensure the decay engine is started (lazy init on first call).
        确保衰减引擎已启动（懒加载，首次调用时启动）。
        """
        if not self._running:
            await self.start()

    async def start(self) -> None:
        """Start the background decay loop.
        启动后台衰减循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Decay engine started, interval: {self.check_interval}h / "
            f"衰减引擎已启动，检查间隔: {self.check_interval} 小时"
        )

    async def stop(self) -> None:
        """Stop the background decay loop.
        停止后台衰减循环。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Decay engine stopped / 衰减引擎已停止")

    async def _background_loop(self) -> None:
        """Background loop: run decay → sleep → repeat.
        后台循环体：执行衰减 → 睡眠 → 重复。"""
        while self._running:
            try:
                await self.run_decay_cycle()
            except Exception as e:
                logger.error(f"Decay cycle error / 衰减周期出错: {e}")
            # --- Wait for next cycle / 等待下一个周期 ---
            try:
                await asyncio.sleep(self.check_interval * _SECONDS_PER_HOUR)
            except asyncio.CancelledError:
                break
