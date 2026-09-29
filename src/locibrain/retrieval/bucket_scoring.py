"""
========================================
locibrain.retrieval.bucket_scoring — the per-dimension retrieval sub-scores
========================================

Split out of bucket_manager.py. BucketManager.search() ranks candidate buckets with a
weighted multi-dimensional score: text relevance (topic) + emotional resonance (emotion)
+ temporal proximity (time) + touch frequency (touch). Those four are pure functions —
they read only their arguments and this module's constants, and never touch the
filesystem or the network — so they live on their own, where they are easy to unit-test
and to reuse.

The importance / semantic (embedding) / bm25 dimensions are short enough to stay inline
in bucket_manager.search(): importance is a one-line normalization, and semantic and
bm25 depend on instance state such as self.embedding_engine and self._bm25, so pulling
them out here would add coupling rather than remove it.

What this does NOT do:
- No weighted sum, no normalization across dimensions. That is search()'s job; each
  function here returns a single 0~1 score for its own dimension.
- No reading of bucket files and no access to self.config. The content_weight that the
  topic score needs is passed in explicitly rather than looked up here.

Public surface: calc_topic_score / calc_emotion_score / calc_time_score /
                calc_touch_score
========================================
"""

import math
from typing import Optional

from rapidfuzz import fuzz
from utils import parse_iso_datetime, utc_now

# --- topic: text-dimension weights ---
TOPIC_NAME_W = 3.0
TOPIC_DOMAIN_W = 2.5
TOPIC_TAG_W = 2.0
TOPIC_BODY_SLICE = 1000   # how much of the body head is fed to the fuzzy match

# --- emotion dimension ---
_DEFAULT_VALENCE = 0.5
_DEFAULT_AROUSAL = 0.3
EMOTION_MAX_DIST = math.sqrt(2)  # the theoretical max Euclidean distance in Russell's space

# --- time dimension ---
TIME_DECAY_LAMBDA = 0.02  # e^(-lambda*days); smaller -> cools off more slowly
TIME_FALLBACK_DAYS = 30   # assumed age when last_active cannot be parsed

# --- touch dimension ---
TOUCH_NORMALIZE_CAP = 10.0   # activation_count divided by this, clipped to 1.0


# ---------------------------------------------------------
# Topic relevance sub-score:
# name(x3) + domain(x2.5) + tags(x2) + body(x1)
# ---------------------------------------------------------
def calc_topic_score(query: str, bucket: dict, content_weight: float = 1.0) -> float:
    """
    Calculate text dimension relevance score (0~1).
    """
    meta = bucket.get("metadata", {})

    name_score = fuzz.partial_ratio(query, meta.get("name", "")) * TOPIC_NAME_W
    domain_score = (
        max(
            (fuzz.partial_ratio(query, d) for d in meta.get("domain", [])),
            default=0,
        )
        * TOPIC_DOMAIN_W
    )
    tag_score = (
        max(
            (fuzz.partial_ratio(query, tag) for tag in meta.get("tags", [])),
            default=0,
        )
        * TOPIC_TAG_W
    )
    content_score = fuzz.partial_ratio(query, bucket.get("content", "")[:TOPIC_BODY_SLICE]) * content_weight

    return (name_score + domain_score + tag_score + content_score) / (
        100 * (TOPIC_NAME_W + TOPIC_DOMAIN_W + TOPIC_TAG_W + content_weight)
    )


# ---------------------------------------------------------
# Emotion resonance sub-score:
# Based on Russell circumplex Euclidean distance
# No emotion in query -> neutral 0.5 (doesn't affect ranking)
# ---------------------------------------------------------
def calc_emotion_score(
    q_valence: Optional[float], q_arousal: Optional[float], meta: dict
) -> float:
    """
    Calculate emotion resonance score (0~1, closer = higher).
    """
    if q_valence is None or q_arousal is None:
        return 0.5  # No emotion coordinates -> neutral score

    try:
        b_valence = float(meta.get("valence", _DEFAULT_VALENCE))
        b_arousal = float(meta.get("arousal", _DEFAULT_AROUSAL))
    except (ValueError, TypeError):
        return 0.5

    # Euclidean distance, max sqrt(2) ≈ 1.414
    dist = math.sqrt((q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2)
    return max(0.0, 1.0 - dist / EMOTION_MAX_DIST)


# ---------------------------------------------------------
# Time proximity sub-score:
# More recent activation -> higher score
# ---------------------------------------------------------
def calc_time_score(meta: dict) -> float:
    """
    Calculate time proximity score (0~1, more recent = higher).
    """
    last_active_str = meta.get("last_active", meta.get("created", ""))
    try:
        last_active = parse_iso_datetime(last_active_str)
        days = max(0.0, (utc_now() - last_active).total_seconds() / 86400)
    except (ValueError, TypeError):
        days = TIME_FALLBACK_DAYS
    return math.exp(-TIME_DECAY_LAMBDA * days)


# ---------------------------------------------------------
# Touch frequency sub-score: the more often a bucket was deliberately recalled,
# the higher it scores.
# ---------------------------------------------------------
def calc_touch_score(meta: dict) -> float:
    """
    Calculate touch frequency score (0~1).
    Normalizes activation_count over 10; capped at 1.0.
    """
    count = float(meta.get("activation_count") or 0)
    return min(count / TOUCH_NORMALIZE_CAP, 1.0)
