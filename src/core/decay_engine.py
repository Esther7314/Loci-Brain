"""
========================================
decay_engine.py — the decay engine, modelling the human forgetting curve
========================================

Rewritten around one idea: forgetting is not "the whole entry disappears", it is
"resolution drops".
Three stages: alive -> faded (45 days without being recalled: it stops turning up while
browsing, takes a penalty in search, and the full text is still there) -> sunk (half a
year: a harsher search penalty, the full text moves to archive/原文/{id}.txt, only the
summary is left in the main store, and the vector is untouched).

Key behaviours:
- Progress, not score: time since last recalled ÷ (baseline × emotion factor
  [× half for closed]), with valence weighted above arousal — painful things fade
  faster, happy ones last longer, which is the exact opposite of the old formula.
  🔴 **The activation-count factor was deleted.** "Things recalled often do not sink" is
  already carried by last_active resetting to zero, and that is enough; a count on top
  of that is compound interest, and being brought up often is not the same as being
  truest.
- The never-sink test looks at `room`, not `type`: pinned / rules / letter / seed /
  profile facts / periods (`__大event__`) / anything in the MIND branch.
- Sinking is reversible: trace(restore=True); a bucket that gets touched returns to
  alive on the next cycle.
- ensure_started() starts the background loop idempotently, and can be monkeypatched to
  a no-op in tests.
- ⏳ calculate_score(): **a shell awaiting retirement.** It belongs to the upstream half
  (importance × count × exponential decay), has nothing to do with the three stages here
  and **decides the fate of no bucket at all** (its own comments say as much). The plan
  is to retire it wholesale once its consumers are gone; none of them have been removed
  yet, so the shell stays and its numbers are left alone. **Do not build anything new on
  top of it.**

What it deliberately does not do:
- No moving buckets into archive, and no automatic closing (that is the system lying on
  your behalf and burying the evidence — it was cut). The one thing it does close is a
  hold whose own date has passed (core/_holds.py): the end was set when the hold was
  written, so closing it records what was said rather than deciding anything, and it
  says so (`closed_by: "expired"`).
- No content changes (sinking itself lives in bucket_manager.sink_bucket), no tagging,
  no LLM calls.
- Not one number reaches breath or recall (forgetting happens quietly); the dashboard
  can see everything.

Exports: the DecayEngine class
         (stage_of / run_decay_cycle / calculate_score / ensure_started)
========================================
"""

import math
import asyncio
import logging

from utils import parse_iso_datetime, is_closed, is_telic, utc_now
from . import _holds as _H
from . import _when as _w
from ._bigevent import BIGEVENT_TAG
from ._rooms import is_mind_room

logger = logging.getLogger("loci_brain.decay")


# ============================================================
# Tunable constants
# ------------------------------------------------------------
# rule.md §⑩: no bare magic numbers. These constants used to be scattered through
# calculate_score() and run_decay_cycle(); gathering them here means ① the formulas
# became far easier to read, ② any tuning happens in one place, and ③ unit tests can
# import the constants directly and assert against them.
#
# ⚠️ Before changing any of these numbers, read the philosophy in rule.md §1.0: "memory
# only fades, it never disappears". Decay is not deletion, it is a score sinking.
# Changing threshold/lambda directly changes "how many days until this is forgotten".
# ============================================================

# --- DecayEngine defaults (overridden by decay.* in config.yaml) ---
_DEFAULT_LAMBDA = 0.05            # exponential decay rate: score × e^(-λ) per day
_DEFAULT_THRESHOLD = 0.3          # below this score -> archive
_DEFAULT_CHECK_INTERVAL_HRS = 24  # background loop interval, in hours
_DEFAULT_EMOTION_BASE = 1.0       # emotion weight baseline
_DEFAULT_AROUSAL_BOOST = 0.8      # each +1 of arousal -> +0.8 emotion weight

# --- Locked scores: some buckets do not take part in decay at all ---
_SCORE_PINNED = 999.0    # pinned / protected / permanent: permanently high (never archived)
_SCORE_FEEL = 50.0       # feel / letter: fixed middling score

# --- Periodic self-heal: how many missing vectors one cycle may backfill at most,
#     so the embedding API is not blown out in a single burst ---
# An active bucket may be on disk while embeddings.db has no vector for it, in which case
# breath's vector channel silently misses it (particularly common for permanent buckets).
# Whatever is left over is picked up on the following cycle.
_BACKFILL_MAX_PER_CYCLE = 50

# --- Freshness bonus：bonus = 1 + e^(-hours/HALF_LIFE) ---
_FRESHNESS_HALF_LIFE_HRS = 36.0  # 36h half-life: ×2.0 fresh, ×1.5 at 36h, ≈×1.14 at 72h
_FRESHNESS_AMPLITUDE = 1.0       # the ceiling of the bonus (0 -> none; 1 -> at most ×2)

# --- Short-term vs long-term weighting (the core psychological model) ---
# Short term: for something that just happened, time dominates ("the impression is fresh")
# Long term: past this boundary, emotion dominates ("unforgettable vs. no longer matters")
_SHORT_TERM_DAYS = 3.0
_SHORT_TERM_TIME_RATIO = 0.7
_LONG_TERM_EMOTION_RATIO = 0.7

# --- Sub-linear amplification of activation count: more visits = more vivid, but not linearly ---
_ACTIVATION_EXPONENT = 0.3

# --- Resolved/digested decay acceleration factors ---
_FACTOR_RESOLVED_DIGESTED = 0.02  # dealt with + a feel written -> fade into the background quickly
_FACTOR_RESOLVED_ONLY = 0.05      # dealt with only (no feel written) -> fade moderately

# --- Urgency boost: high arousal and not yet dealt with -> temporarily heavier, so it does not get archived by mistake ---
_AROUSAL_URGENCY_THRESHOLD = 0.7
_URGENCY_BOOST = 1.5

# --- Auto-resolve: 🔴 CUT. Of everything that was cut, this one deserved it most. ---
# The old logic: importance<=4 and untouched for 30 days -> automatically resolved ->
# and being resolved then accelerated decay twentyfold.
# Chain those together and you get: "something promised but never done gets ruled
# 'already handled' precisely because it went undone for too long, and is then buried
# faster" — the system lying on your behalf and burying the evidence.
# Something wanted but not finished has exactly two endings, resolved and abandoned, and
# both must be marked **by hand** with trace.

# ============================================================
# The mechanism: forgetting = resolution dropping, not disappearing
# ------------------------------------------------------------
# Three stages: alive (everything normal) -> faded (does not turn up while browsing,
#       penalised in search, full text still present) -> sunk (a harsher search penalty,
#       the full text moves to archive/原文/{id}.txt, only the summary stays in the main
#       store, the vector is untouched).
# It computes **progress**, not a score:
#     sink progress = time since last recalled ÷ (baseline days × emotion factor
#                     × count factor [× resolved factor])
# - Time is the trunk (the numerator); emotion and count are only multipliers on "how
#   long it can hold out".
# - valence outweighs arousal: painful things fade faster, happy ones last longer (the
#   fading-affect bias).
#   🔴 The old formula had this exactly backwards (emotion_weight looked only at arousal,
#   so the more painful something was the more firmly it was preserved).
# - importance takes no part (it is assigned by hand, and should not turn around and
#   decide what gets forgotten).
# - resolved keeps its acceleration (deliberately marking something done should make it
#   fade faster); digested is ignored (feel is no longer in use).
# - Cut: automatic closing · the freshness bonus · the urgency boost · the short/long
#   term gear change.
# The coefficients are kept identical to scripts/decay_dryrun.py — tune against the dry
# run first.
# ============================================================
_BASE_FADE_DAYS = 45.0     # fade baseline: 45 days unrecalled (neutral emotion, recalled once)
_BASE_SINK_DAYS = 180.0    # sink baseline: half a year (holding a 1:4 ratio with fading)
_EMO_FLOOR = 0.6           # emotion factor = FLOOR + W_V×v + W_A×a ∈ [0.6, 1.6]
_EMO_W_V = 0.7             # valence outweighs arousal
_EMO_W_A = 0.3
_RESOLVED_HOLD_FACTOR = 0.5  # marked resolved by hand -> halve the holding days (forgotten twice as fast)

# --- Arousal/importance fallbacks ---
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_DEFAULT_DAYS_FALLBACK = 30  # a broken time field in calculate_score -> treat as 30 days (conservative)

# --- Time conversions ---
_SECONDS_PER_DAY = 86400
_SECONDS_PER_HOUR = 3600


def _days_since_active(meta: dict, fallback_days: float = _DEFAULT_DAYS_FALLBACK) -> float:
    """Parse "days since last activation" out of metadata.

    Why it was extracted: calculate_score and run_decay_cycle each wrote out the same
    "fromisoformat -> difference -> fallback" three-step, and their fallback values did
    not even agree (30 in one, 999 in the other). One function now, with the caller
    passing fallback_days to say how bad data should be treated:
      * calculate_score uses the default 30: conservatively score it as "untouched for a month"
      * run_decay_cycle's auto-resolve path passed 999, so bad data would readily trigger closing

    Edges (rule.md §⑨):
      * meta is not a dict / the field is missing / the string will not parse -> return fallback_days
      * always returns a float >= 0 (so clock drift cannot produce a negative)
    """
    if not isinstance(meta, dict):
        return fallback_days
    raw = meta.get("last_active") or meta.get("created") or ""
    try:
        last_active = parse_iso_datetime(raw)
        return max(0.0, (utc_now() - last_active).total_seconds() / _SECONDS_PER_DAY)
    except (ValueError, TypeError):
        return float(fallback_days)


class DecayEngine:
    """
    Memory decay engine — periodically scans all dynamic buckets,
    calculates decay scores, auto-archives low-activity buckets
    to simulate natural forgetting.
    """

    def __init__(self, config: dict, bucket_mgr):
        # --- Load decay parameters ---
        decay_cfg = config.get("decay", {})
        self.decay_lambda = decay_cfg.get("lambda", _DEFAULT_LAMBDA)
        self.threshold = decay_cfg.get("threshold", _DEFAULT_THRESHOLD)
        self.check_interval = decay_cfg.get("check_interval_hours", _DEFAULT_CHECK_INTERVAL_HRS)

        # --- Emotion weight params (continuous arousal coordinate) ---
        emotion_cfg = decay_cfg.get("emotion_weights", {})
        self.emotion_base = emotion_cfg.get("base", _DEFAULT_EMOTION_BASE)
        self.arousal_boost = emotion_cfg.get("arousal_boost", _DEFAULT_AROUSAL_BOOST)

        self.bucket_mgr = bucket_mgr

        # --- Background task control ---
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the decay engine is running in the background."""
        return self._running

    # ---------------------------------------------------------
    # Core: calculate decay score for a single bucket
    #
    # Higher score = more vivid memory; below threshold → archive
    # Permanent buckets never decay
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # Freshness bonus: continuous exponential decay
    # bonus = 1.0 + 1.0 × e^(-t/36), t in hours
    # t=0 → 2.0×, t≈25h (half-life) → 1.5×, t≈72h → ≈1.14×, t→∞ → 1.0×
    # ---------------------------------------------------------
    @staticmethod
    def _calc_time_weight(days_since: float) -> float:
        """
        Freshness bonus multiplier: 1.0 + e^(-t/36), t in hours.
        ×2.0 the moment it is stored, half-life around 36 hours, approaching ×1.0 after 72.
        """
        hours = days_since * 24.0
        return 1.0 + _FRESHNESS_AMPLITUDE * math.exp(-hours / _FRESHNESS_HALF_LIFE_HRS)

    def calculate_score(self, metadata: dict) -> float:
        """
        Calculate current activity score for a memory bucket.

        New model: short-term vs long-term weight separation.
        - Short-term (≤3 days): time_weight dominates, emotion amplifies
        - Long-term (>3 days): emotion_weight dominates, time decays to floor
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

        # --- Letters never decay: a letter is kept forever ---
        if metadata.get("type") == "letter":
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
        # Short term (≤3 days): time_weight 70%, emotion 30%
        # Long term  (>3 days): emotion 70%, time_weight 30%
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
        # resolved + digested (a feel exists) -> fade quickly
        # resolved only -> fade moderately
        resolved = is_closed(metadata)  # only `status` marks an ending; the old booleans stay read-only for compatibility
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
    # The three-stage decision (progress-based)
    # ---------------------------------------------------------
    @staticmethod
    def _never_decays(meta: dict) -> bool:
        """The never-sink list. 🔴 The test looks at `room`, not `type` — what testing
        `type` produced was this: among entries that were all equally MIND, the 190 held
        as i/feel never sank while the 61 held as dynamic did. Testing `room` says it in
        one line instead.
        pinned · rules · letter · seed · profile facts · periods · anything in a /MIND/
        room · something wanted that binds someone and is still open · anything that
        comes back every year.
        (Every permanent bucket is pinned or protected anyway, so the first test already
        covers them.)

        🔴 Periods (`__大event__`) were missing from this list until 2026-08-26, and the
        omission is worth keeping in view because of its shape: a period is the name a
        person gives to a stretch of days, i.e. about as deliberate a gesture as this
        store has — and it was ageing like an ordinary event and sinking. Nothing threw.
        The symptom was a name that stopped laying itself over a recalled stretch of
        time, months later, in a browse nobody was auditing.
        (Gists born in the MIND rooms were already covered by is_mind_room; a period
        lives in the EVENT branch, which is why it fell through.)
        """
        if meta.get("pinned") or meta.get("protected"):
            return True
        if str(meta.get("type") or "") in ("permanent", "letter", "seed"):
            return True
        # 🔴 After the room rename, **never write `"/MIND/" in room` again**: the new
        # names look like `MIND/TRAITS` with no leading slash, so that literal test
        # silently returns False and kicks every insight off the never-sink list —
        # silently, and only visible one decay cycle later.
        # is_mind_room() accepts both old and new names.
        if is_mind_room(meta.get("room")):
            return True
        tags = meta.get("tags") or []
        if isinstance(tags, list) and any(
                str(t) in ("__档案事实__", BIGEVENT_TAG) for t in tags):
            return True
        # Owed and not yet settled: it must not sink while someone is bound by it
        # (plan part 1, rule 6). Closing it lets it age like anything else. It still
        # does not have to be in front of you every day; that is breath's business.
        if is_telic(meta) and meta.get("bound") and not is_closed(meta):
            return True
        # A birthday or an anniversary rings again next year: it never sinks.
        if meta.get("recurrence"):
            return True
        return False

    @classmethod
    def stage_of(cls, meta: dict) -> str:
        """Which stage a memory belongs in right now: alive / faded / sunk.

        Holding days = baseline × emotion factor [× resolved factor]; once the time since
        last recalled exceeds the holding days computed from the 45-day baseline it is
        faded, and from the 180-day baseline, sunk.
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
        # 🔴 Settled: **the count factor (activation_count^0.3) is deleted entirely.**
        # Holding days = the emotion factor [× half if closed], i.e. nothing but "how long
        # since it was recalled" against "the v/a I assigned myself".
        #
        # "Things recalled often do not sink" was **not lost**: every genuine recall
        # refreshes last_active and zeroes the numerator (time since last recalled). That
        # is the first line of defence and it is sufficient on its own.
        # A count layered on top is just compound interest — something brought up often is
        # not thereby the truest thing, and it was deliberately dropped.
        # 📌 It also cured a real contradiction along the way: an automatic statistic (the
        #    count) was acting as the primary key and a human judgement (v/a) as the
        #    secondary one, and in the old formula the two were **multiplied** together.
        hold = emo
        if is_closed(meta):
            hold *= _RESOLVED_HOLD_FACTOR
        if days >= _BASE_SINK_DAYS * hold:
            return "sunk"
        if days >= _BASE_FADE_DAYS * hold:
            return "faded"
        return "alive"

    # ---------------------------------------------------------
    # Execute one decay cycle (rewritten to the three-stage progress model: no more
    # archiving, no more automatic closing)
    # ---------------------------------------------------------
    async def run_decay_cycle(self) -> dict:
        """Run one round of forgetting: decide the stage of every bucket that takes part
        in decay, and persist the field when the stage changed.

        - alive <-> faded: only writes or clears the decay_stage metadata. A bucket that
          gets touched has its day count reset to zero and returns to alive on the next
          cycle — recall it and it lives again.
        - -> sunk: calls bucket_mgr.sink_bucket() (full text moved to a txt file, the body
          replaced by the summary, the vector untouched).
        - sunk never restores itself: the one and only door is trace(restore=True), which
          is what makes this stage reversible.
        - Not one number reaches breath or recall (forgetting happens quietly); the
          dashboard can see it.
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
        n_expired = 0
        today = _w.now()
        for bucket in buckets:
            meta = bucket.get("metadata", {})
            # A dated hold past its last day stops holding on read already; closing it
            # here is what lets it age (open and bound, it would never sink). It ages
            # from the next cycle on.
            if _H.hold_expired(meta, today):
                bid = str(meta.get("id") or bucket.get("id") or "")
                try:
                    if await self.bucket_mgr.update(bid, status="resolved",
                                                    closed_by=_H.CLOSED_BY_EXPIRY):
                        n_expired += 1
                except Exception as e:
                    logger.warning(f"Closing expired hold {bid} failed: {e}")
                continue
            if self._never_decays(meta):
                continue
            # Besides letter/seed there can be archived shells mixed into list_all: leave them alone
            if str(meta.get("type") or "") == "archived" or meta.get("deleted_at"):
                continue
            checked += 1
            current = str(meta.get("decay_stage") or "") or "alive"
            if current == "sunk":
                continue  # once sunk, it stays sunk; the only way back is trace(restore=True)
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
                        n_faded += 1  # without a summary it cannot sink: fade it now, sink next cycle once backfill supplies one
                elif stage == "faded":
                    if await self._mark_stage(bid, "faded", current):
                        n_faded += 1
                else:  # alive (recalled -> day count zeroed -> back from the dead)
                    if await self._mark_stage(bid, None, current):
                        n_revived += 1
            except Exception as e:
                logger.warning(f"Decay stage transition failed for {bid}: {e}")

        # --- Self-heal: backfill missing vectors (periodic; see _self_heal_embeddings) ---
        backfilled_embeddings = await self._self_heal_embeddings(buckets)

        # --- Warm bm25 up while we are here (found in use): the lazy rebuild used to wait
        # for the first search, so the first search after a restart scored against an
        # **empty index** — bm25 is 37.5% of the two-dimension scheme, and that first
        # search came out visibly lower (measured: some entries 74 -> 37). The first decay
        # cycle runs a few seconds after boot and already holds `buckets`, so it builds the
        # index ahead of time. Once built, a search keeps it level itself (search()). ---
        try:
            bm25 = getattr(self.bucket_mgr, "_bm25", None)
            if (bm25 is not None and not bm25.built
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
            "holds_expired": n_expired,
            "backfilled_embeddings": backfilled_embeddings,
        }
        logger.info(f"Decay cycle complete / 遗忘周期完成: {result}")
        return result

    async def _mark_stage(self, bucket_id: str, stage, current: str) -> bool:
        """Write or clear decay_stage. Pure metadata: last_active is deliberately not
        bumped, because marking a stage is not the same as recalling something."""
        if (stage or "alive") == current:
            return False
        return await self.bucket_mgr.update(bucket_id, decay_stage=stage)

    async def _self_heal_embeddings(self, buckets: list) -> int:
        """Periodic self-heal: generate vectors for active buckets that are on disk but
        have no vector in embeddings.db.

        Background: permanent buckets often end up without a vector because they arrived
        through a bulk import or were pinned from the dashboard, and breath's vector
        channel then cannot retrieve them — which shows up as "only dynamic buckets come
        back". The decay loop repairs this as it goes, so nobody has to run
        backfill_embeddings.py by hand.

        Edges: embedding disabled -> skip; at most _BACKFILL_MAX_PER_CYCLE per cycle so
        the API is not blown out, with the remainder continuing next cycle; a single
        failure is only a warning (rule.md §1.5 permits degrading).
        Only active buckets are handled (`buckets` excludes the archive), and orphaned
        vectors are NOT deleted here — deletion goes through a dedicated script, so that a
        valid vector belonging to an archived bucket is never mistaken for an orphan."""
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
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        """
        Ensure the decay engine is started (lazy init on first call).
        """
        if not self._running:
            await self.start()

    async def start(self) -> None:
        """Start the background decay loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Decay engine started, interval: {self.check_interval}h / "
            f"衰减引擎已启动，检查间隔: {self.check_interval} 小时"
        )

    async def stop(self) -> None:
        """Stop the background decay loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Decay engine stopped / 衰减引擎已停止")

    async def _background_loop(self) -> None:
        """Background loop: run decay → sleep → repeat."""
        while self._running:
            try:
                await self.run_decay_cycle()
            except Exception as e:
                logger.error(f"Decay cycle error / 衰减周期出错: {e}")
            # --- Wait for next cycle ---
            try:
                await asyncio.sleep(self.check_interval * _SECONDS_PER_HOUR)
            except asyncio.CancelledError:
                break
