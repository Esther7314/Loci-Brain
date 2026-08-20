"""
bm25_index.py — sparse BM25 retrieval, with jieba for Chinese segmentation.

Supplies bucket_manager.search() with TF-IDF-weighted keyword recall (Dim 7).

rank_bm25 and jieba are both soft dependencies: when either is missing every method
becomes a silent no-op and the other retrieval dimensions carry on unaffected.
BM25Index is owned by BucketManager, marked dirty after any write, and rebuilt lazily
on the next search().
"""
from __future__ import annotations

import logging

logger = logging.getLogger("loci_brain.bm25")

try:
    from rank_bm25 import BM25Okapi as _BM25Okapi
    _BM25_AVAILABLE = True
except ImportError:
    _BM25Okapi = None  # type: ignore
    _BM25_AVAILABLE = False
    logger.info("[bm25] rank_bm25 未安装 — BM25 关键词检索已禁用（pip install rank-bm25 启用）")

try:
    import jieba as _jieba
    _jieba.setLogLevel(logging.WARNING)
    _JIEBA_AVAILABLE = True
except ImportError:
    _jieba = None  # type: ignore
    _JIEBA_AVAILABLE = False
    logger.info("[bm25] jieba 未安装 — 回退空格分词（pip install jieba 启用中文分词）")


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


class BM25Index:
    """Facade over an in-memory BM25 inverted index.

    lifecycle:
        build(buckets)  — rebuild the index (BucketManager marks dirty on write and
                          calls this lazily from search)
        score(query)    — returns {bucket_id: normalized_score}, scores in [0, 1]
    """

    def __init__(self):
        self._index = None          # BM25Okapi instance or None
        self._ids: list[str] = []

    @property
    def available(self) -> bool:
        return _BM25_AVAILABLE

    def build(self, buckets: list[dict]) -> None:
        """Rebuild the index. A document = name + content[:1200] + tags + aliases,
        concatenated and tokenised.

        `domain` was pulled back out of retrieval: it is a folder name the model made
        up, and indexing it only lets broad words like "programming" or "AI" match a
        pile of unrelated buckets out of nowhere.
        `aliases` were let in, and they have exactly one job: find it even when it was
        phrased differently.
        """
        if not _BM25_AVAILABLE:
            return
        corpus: list[list[str]] = []
        ids: list[str] = []
        for b in buckets:
            meta = b.get("metadata", {})
            text = " ".join([
                meta.get("name") or "",
                b.get("content", "")[:1200],
                " ".join(meta.get("tags", []) or []),
                " ".join(meta.get("aliases", []) or []),
            ])
            tokens = _tokenize(text)
            if tokens:
                corpus.append(tokens)
                ids.append(b["id"])
        if corpus:
            self._index = _BM25Okapi(corpus)
        else:
            self._index = None
        self._ids = ids

    def score(self, query: str) -> dict[str, float]:
        """Returns {bucket_id: normalized_bm25_score}; top score = 1.0, {} if nothing hit."""
        if not _BM25_AVAILABLE or self._index is None:
            return {}
        tokens = _tokenize(query)
        if not tokens:
            return {}
        raw = self._index.get_scores(tokens)  # numpy ndarray
        max_s = float(raw.max()) if raw.size > 0 else 0.0
        if max_s <= 0:
            return {}
        return {bid: float(s) / max_s for bid, s in zip(self._ids, raw) if s > 0}
