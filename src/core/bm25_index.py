"""
bm25_index.py — sparse BM25 retrieval, with jieba for Chinese segmentation.

Supplies bucket_manager.search() with TF-IDF-weighted keyword recall (Dim 7).

rank_bm25 and jieba are soft dependencies: without rank_bm25 every method is a no-op and
the other retrieval dimensions carry on; without jieba Chinese is split on whitespace,
which for Chinese text means whole sentences. Either one missing is said loudly: a
WARNING at import and a red row on the health page (`dependency_status()`), because
literal search degrading to whole-sentence substring matching looks like "search got
worse" and nothing else.
BM25Index is owned by BucketManager, which marks it dirty after any write; the next
search() brings it level with the store before scoring (BucketManager.search).
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("loci_brain.bm25")

try:
    from rank_bm25 import BM25Okapi as _BM25Okapi
    _BM25_AVAILABLE = True
except ImportError:
    _BM25Okapi = None  # type: ignore
    _BM25_AVAILABLE = False
    logger.warning("[bm25] rank_bm25 is not installed: literal (BM25) search is off. "
                   "pip install rank-bm25")

try:
    import jieba as _jieba
    _jieba.setLogLevel(logging.WARNING)
    _JIEBA_AVAILABLE = True
except ImportError:
    _jieba = None  # type: ignore
    _JIEBA_AVAILABLE = False
    logger.warning("[bm25] jieba is not installed: Chinese is split on whitespace, i.e. "
                   "into whole sentences. pip install jieba")


def dependency_status() -> dict:
    """For the health page: which of the two literal-search dependencies are importable."""
    return {"rank_bm25": _BM25_AVAILABLE, "jieba": _JIEBA_AVAILABLE}


def _tokenize(text: str) -> list[str]:
    """jieba for Chinese, whitespace for English; lowercased, empty tokens dropped."""
    if not text:
        return []
    text = text.lower()
    if _JIEBA_AVAILABLE:
        tokens = list(_jieba.cut_for_search(text))
    else:
        tokens = text.split()
    return [t for t in tokens if t.strip()]


def _doc_text(bucket: dict) -> str:
    """What one entry contributes to the index: name + content[:1200] + tags + aliases.

    `domain` stays out of retrieval: it is a folder name the model made up, and indexing
    it only lets broad words like "programming" or "AI" match a pile of unrelated buckets
    out of nowhere. `aliases` are in, and they have exactly one job: find it even when it
    was phrased differently.
    """
    meta = bucket.get("metadata", {}) or {}
    return " ".join([
        meta.get("name") or "",
        (bucket.get("content") or "")[:1200],
        " ".join(meta.get("tags", []) or []),
        " ".join(meta.get("aliases", []) or []),
    ])


def _doc_key(bucket: dict) -> tuple[str, str]:
    """What has to change for an entry to be tokenised again: its indexed text, and its
    decay stage (a sunk entry is indexed from its original, which `source` reads)."""
    meta = bucket.get("metadata", {}) or {}
    return str(meta.get("decay_stage") or ""), _doc_text(bucket)


class BM25Index:
    """Facade over an in-memory BM25 inverted index.

    lifecycle:
        sync(buckets)   — bring the index level with the store. Only entries whose
                          indexed text changed, or that are new, are tokenised (jieba is
                          the slow part: the whole store costs about a second per ~1700
                          entries); entries no longer there drop out; then the BM25 table
                          is recomputed from the kept tokens (tens of milliseconds). That
                          is what lets a write be searchable on the very next call.
        build(buckets)  — sync from empty
        score(query)    — returns {bucket_id: normalized_score}, scores in [0, 1]

    sync may run in a worker thread while score runs on the event loop: score reads one
    immutable (table, ids) snapshot that sync swaps in with a single assignment, and syncs
    are serialised by a lock.
    """

    def __init__(self):
        self._state: tuple = (None, [])     # (BM25Okapi or None, ids in corpus order)
        self._docs: dict[str, tuple[tuple[str, str], list[str]]] = {}   # id -> (key, tokens)
        self._stamp = None                  # the store generation the index was last synced to
        self._built = False
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return _BM25_AVAILABLE

    @property
    def built(self) -> bool:
        """Whether it has been synced at least once (before that every score is empty)."""
        return self._built

    def build(self, buckets: list[dict], source=None) -> None:
        with self._lock:
            self._docs = {}
            self._stamp = None
        self.sync(buckets, source)

    def sync(self, buckets: list[dict], source=None, stamp=None) -> int:
        """Bring the index level with `buckets`; returns how many entries were tokenised
        again or dropped.

        `source(bucket)` gives the bucket whose text is indexed when it differs from the
        one listed (a sunk entry's original); it is called only for entries that changed.
        `stamp` is the store generation `buckets` was read at: a sync carrying an older
        stamp than the one already applied does nothing, so a slow sync over an old list
        can never take back an entry a newer one put in. None always applies.
        """
        if not _BM25_AVAILABLE:
            return 0
        with self._lock:
            if stamp is not None and self._stamp is not None and stamp < self._stamp:
                return 0
            docs: dict[str, tuple[tuple[str, str], list[str]]] = {}
            changed = 0
            for b in buckets:
                bid = str(b.get("id") or "")
                if not bid:
                    continue
                key = _doc_key(b)
                old = self._docs.get(bid)
                if old is not None and old[0] == key:
                    docs[bid] = old
                    continue
                docs[bid] = (key, _tokenize(_doc_text(source(b) if source else b)))
                changed += 1
            changed += len(self._docs.keys() - docs.keys())
            if changed or not self._built:
                ids = [bid for bid, (_key, tokens) in docs.items() if tokens]
                corpus = [docs[bid][1] for bid in ids]
                self._state = (_BM25Okapi(corpus) if corpus else None, ids)
            self._docs = docs
            if stamp is not None:
                self._stamp = stamp
            self._built = True
            return changed

    def score(self, query: str) -> dict[str, float]:
        """Returns {bucket_id: normalized_bm25_score}; top score = 1.0, {} if nothing hit."""
        index, ids = self._state
        if not _BM25_AVAILABLE or index is None:
            return {}
        tokens = _tokenize(query)
        if not tokens:
            return {}
        raw = index.get_scores(tokens)  # numpy ndarray
        max_s = float(raw.max()) if raw.size > 0 else 0.0
        if max_s <= 0:
            return {}
        return {bid: float(s) / max_s for bid, s in zip(ids, raw) if s > 0}
