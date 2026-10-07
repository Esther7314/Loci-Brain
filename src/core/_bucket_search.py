"""
========================================
_bucket_search.py — how search scores
========================================

search() ranks the active buckets against a query on two dimensions: the embedding
cosine (semantic, weight 2.5) and BM25 over the text (weight 1.5), normalised to
0~100. A literal hit (the whole query appears in name, tags or body) carries a flag and
no bonus; the recall layer floors it at its relevance line. Closed entries and the
faded / sunk forgetting stages take a discount. The BM25 index is kept level with the
store incrementally, and built the first time in the background.

The scoring numbers are tuned in core/bucket_manager.py ("search scoring"), where the
panel (core/embedding_switch.thresholds) names the vector line.

BucketManager (core/bucket_manager.py) inherits SearchMixin.
========================================
"""

import asyncio
import logging
from typing import Optional

from utils import is_closed

try:
    # ⚠️ A mine caught during acceptance: after the move into core, the old top-level path
    #    `from bm25_index import` failed with a silent ImportError -> the entire BM25
    #    dimension died without a word. That is the trap in a soft dependency: it fails
    #    without erroring, and retrieval quietly goes blind.
    #    This try is only meant to cover rank_bm25/jieba being absent. It must not cover an
    #    import path of our own being wrong.
    from .bm25_index import BM25Index as _BM25Index
except ImportError:
    _BM25Index = None  # type: ignore

logger = logging.getLogger("loci_brain.bucket")


class SearchMixin:
    """The BM25 index and search()."""

    async def search(
        self,
        query: str,
        limit: Optional[int] = None,
        domain_filter: Optional[list[str]] = None,
        query_valence: Optional[float] = None,
        query_arousal: Optional[float] = None,
        vector_scores: Optional[dict[str, float]] = None,
        include_archive: bool = False,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if not query or not query.strip():
            return []

        limit = limit or self.max_results
        # Literal recall: keep the query as it stands (lowercased, trimmed) for substring
        # matching, so a word that was explicitly searched for is always recalled
        q_norm = query.strip().lower()
        # Read before the list: BM25 is synced to the list, and a write that lands after
        # this read has to leave the index dirty (see _sync_bm25).
        generation = self._active_cache_generation
        all_buckets = await self.list_all(include_archive=include_archive)

        if not all_buckets:
            return []

        # --- Layer 0: the direct bucket-id channel (pure lookup, short-circuits) ---
        # A bucket id is random hex and carries **no meaning**, so it has no business in the
        # vector, BM25 or fuzzy channels — putting it there only pollutes the semantic
        # space. This does an exact full-id match on its own: if the query string equals some
        # visible bucket's full id, return that bucket at full marks and bypass semantic
        # ordering. This is the "I know exactly which one I want" lookup.
        # Only a full id counts (no prefix matching), so an ordinary keyword cannot trip it;
        # soft-deleted and archived buckets are not in all_buckets, so a deleted bucket
        # cannot be found by id either, which matches get()'s visibility.
        q_exact = query.strip()
        if q_exact:
            for b in all_buckets:
                if str(b.get("id")) == q_exact:
                    hit = dict(b)
                    hit["score"] = 1.0
                    return [hit]

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            if not candidates:
                candidates = all_buckets
        else:
            candidates = all_buckets

        # --- Layer 1.5: the embedding semantic score (_vector_scores) ---
        vector_scores = await self._vector_scores(query, vector_scores)
        # --- BM25 scoring (_bm25_scores) ---
        bm25_scores = await self._bm25_scores(query, all_buckets, generation, include_archive)
        # --- Layer 2: two-dimension scoring (_score_candidates) ---
        scored = self._score_candidates(candidates, q_norm, vector_scores, bm25_scores)

        # In the ordering, a literal hit is lifted at least to the median of the current
        # pool, so that "the word I searched for is right there, yet it sits behind a pile of
        # vaguely related things" cannot happen. The real max(score, line) is done in the
        # recall layer, since the line arrives with each request and is out of reach here.
        scored.sort(key=lambda x: (x["score"], bool(x.get("literal_hit"))), reverse=True)
        return scored[:limit]

    async def _vector_scores(
        self,
        query: str,
        vector_scores: Optional[dict[str, float]],
    ) -> dict[str, float]:
        """{bucket_id: cosine} for this query: the caller's own when it passed them, else
        one query to the embedding engine; {} when there is no engine or it failed."""
        from .bucket_manager import _VECTOR_TOPK  # lazy: bucket_manager imports this module

        # --- Layer 1.5: the embedding semantic score. It is a scoring dimension only and
        #     never narrows the candidate set. ---
        # Narrowing to "the buckets present in embeddings.db" would filter out wholesale any
        # bucket lacking an embedding (the embed key failed at write time, or an old script
        # bulk-imported without backfilling vectors) as soon as the query matched any vector
        # at all -> breath's retrieval counts would stop agreeing with pulse.
        # So vector_scores feed Layer 2's semantic dimension and `candidates` is left
        # alone. A bucket with no embedding scores semantic_score=0 and can still be hit on
        # topic/emotion/time/importance.
        # ``None`` means this caller wants BucketManager to query the engine.
        # An explicit dict (including {}) lets an orchestration layer perform
        # the query once and reuse the same scores for ranking and recall.
        vector_scores_provided = vector_scores is not None
        if vector_scores is None:
            vector_scores = {}
        else:
            vector_scores = dict(vector_scores)
        if (
            not vector_scores_provided
            and self.embedding_engine
            and self.embedding_engine.enabled
        ):
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=_VECTOR_TOPK)
                if vector_results:
                    vector_scores = {bid: score for bid, score in vector_results}
            except Exception as e:
                logger.warning(f"Embedding score failed, using fuzzy only / embedding 评分失败: {e}")
        return vector_scores

    async def _bm25_scores(
        self,
        query: str,
        all_buckets: list[dict],
        generation: int,
        include_archive: bool,
    ) -> dict[str, float]:
        """{bucket_id: BM25 score} for this query, after bringing the index level with
        `all_buckets` (read at store generation `generation`); {} while it is not built."""
        # --- BM25 scoring. What was just written has to be literally findable on the next
        #     search, while its vector may still be queued. ---
        # Built and dirty -> synced right here before scoring: only the entries that changed
        # are tokenised again (tens of milliseconds), and an index built from another
        # process's writes catches up the same way once list_all has seen them.
        # Never built -> the first build (the whole store through jieba) goes to a background
        # thread, and this query scores without BM25; vectors and whole-query literal hits
        # still carry it. The decay cycle builds it a few seconds after start.
        # The archive is never indexed: a search that includes it scores BM25 as it stands.
        bm25_scores: dict[str, float] = {}
        if self._bm25 is not None:
            if not self._bm25.built:
                if not self._bm25_rebuilding and not include_archive:
                    self._bm25_rebuilding = True
                    asyncio.create_task(self._rebuild_bm25_async(all_buckets, generation))
            elif self._bm25_dirty and not include_archive:
                try:
                    await self._sync_bm25(all_buckets, generation)
                except Exception as e:
                    logger.warning(f"[bm25] 同步失败，本次用旧索引: {e}")
            try:
                bm25_scores = self._bm25.score(query)
            except Exception as e:
                logger.warning(f"[bm25] score 失败，本次跳过 BM25 维度: {e}")
                bm25_scores = {}
        return bm25_scores

    def _score_candidates(
        self,
        candidates: list[dict],
        q_norm: str,
        vector_scores: dict[str, float],
        bm25_scores: dict[str, float],
    ) -> list[dict]:
        """Score each candidate 0~100 and mark how it matched; the ones with no signal and
        no literal hit are left out. Each scored bucket dict is annotated in place."""
        # The numbers are tuned in bucket_manager.py ("search scoring"), which imports
        # this module: read at call time.
        from .bucket_manager import (
            _FADED_SEARCH_DISCOUNT,
            _RESOLVED_RANK_PENALTY,
            _SUNK_SEARCH_DISCOUNT,
        )
        from . import thresholds as _T
        meaning_line = _T.value(_T.RECALL_MEANING)

        # --- Layer 2: two-dimension scoring (semantic 2.5 + bm25 1.5); a literal hit only
        #     sets a flag ---
        # weight_sum is constantly w_semantic + w_bm25: a bucket with no vector scores
        # semantic=0 (a sunk bucket's vector was computed from the original text and is left
        # untouched, so it can still be hit on its semantic fingerprint even with the body
        # gone — that is the design).
        # The score runs 0~100; the relevance line lives over in recall, one number for
        # everything.
        scored = []
        weight_sum = self.w_semantic + self.w_bm25
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # A literal hit: the query string appears as-is in name, tags or body.
                # (domain is a folder name the model invented and was taken out of literal
                # matching; aliases only feed bm25 and take no part in it either — the floor
                # recognises only characters that genuinely appear in this memory.)
                literal_hit = False
                if q_norm:
                    hay = " ".join([
                        str(meta.get("name", "")),
                        " ".join(str(t) for t in (meta.get("tags") or [])),
                        bucket.get("content", "") or "",
                    ]).lower()
                    literal_hit = q_norm in hay

                semantic_score = vector_scores.get(bucket["id"], 0.0) or 0.0
                bm25_score = bm25_scores.get(bucket["id"], 0.0) if bm25_scores else 0.0
                total = semantic_score * self.w_semantic + bm25_score * self.w_bm25
                normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0.0

                # The ticket in: any dimension has signal, or there was a literal hit (a word
                # searched for explicitly has to be reachable).
                # The above-the-line / below-the-line decision is not made here — recall does
                # its own filtering with the relevance line, and floors a literal hit up to
                # that line (max(score, line), never a bonus).
                if normalized > 0 or literal_hit:
                    # Resolved buckets get ranking penalty (but still reachable)
                    if is_closed(meta):  # only `status` marks an ending; the old booleans stay read-only for compatibility
                        normalized *= _RESOLVED_RANK_PENALTY
                    # The three forgetting stages: faded takes a discount, sunk a harsher one.
                    # It happens quietly and is not flagged in the output — a literal hit is
                    # still floored up to the line by the recall layer.
                    _stage = str(meta.get("decay_stage") or "")
                    if _stage == "faded":
                        normalized *= _FADED_SEARCH_DISCOUNT
                    elif _stage == "sunk":
                        normalized *= _SUNK_SEARCH_DISCOUNT
                    bucket["score"] = round(normalized, 2)
                    # How it matched, for the reader to see after the score: the whole query
                    # as written (literal_hit), some of its words (bm25_hit), its meaning
                    # (vector_match: cosine at or above the vector line). Any of them can
                    # hold at once.
                    bucket["literal_hit"] = literal_hit
                    bucket["bm25_hit"] = bm25_score > 0
                    bucket["vector_match"] = semantic_score >= meaning_line
                    scored.append(bucket)
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue
        return scored

    # ---------------------------------------------------------
    # The BM25 index: what it reads, the first build, and the incremental sync
    # ---------------------------------------------------------
    def _bm25_source(self, bucket: dict) -> dict:
        """The bucket whose text BM25 indexes. A sunk bucket has only its summary left in
        the main store, but search still matches against the original text (you simply
        cannot see its details any more), so the original is read back from
        archive/原文/{id}.txt. Called from inside to_thread, never on the event loop."""
        meta = bucket.get("metadata", {}) or {}
        if str(meta.get("decay_stage") or "") != "sunk":
            return bucket
        txt = self._sunk_orig_path(str(meta.get("id") or bucket.get("id") or ""))
        try:
            with open(txt, encoding="utf-8") as f:
                return {**bucket, "content": f.read()}
        except OSError:
            return bucket  # if the original is gone, match on the summary; do not blow up the whole index over it

    def _build_bm25_index(self, buckets: list):
        """Build a **brand new** BM25 index and return it (jieba segmenting the whole store
        is slow: run it in a thread)."""
        idx = _BM25Index()  # type: ignore[operator]
        idx.build(buckets, self._bm25_source)
        return idx

    async def _sync_bm25(self, buckets: list, generation: int | None) -> None:
        """Bring BM25 level with `buckets`, read at store generation `generation`, in a
        thread. Only what changed is tokenised again, so after a write this costs tens of
        milliseconds, not a rebuild. The index stays dirty when a write landed after
        `buckets` was read (or when nobody knows when it was read): the next search
        syncs again."""
        await asyncio.to_thread(self._bm25.sync, buckets, self._bm25_source, generation)
        with self._active_cache_state_guard:
            if generation is not None and generation == self._active_cache_generation:
                self._bm25_dirty = False

    async def _rebuild_bm25_async(self, buckets: list, generation: int | None = None) -> None:
        """The first build, in the background: jieba over the whole store takes about a
        second per ~1700 entries (longer on a slow disk), too long to hold a search for.
        Searches until it lands score without BM25."""
        try:
            await self._sync_bm25(buckets, generation)
        except Exception as e:
            logger.warning(f"[bm25] 后台建索引失败，这次先不用字面分: {e}")
        finally:
            self._bm25_rebuilding = False
