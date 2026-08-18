"""
========================================
tools/breath/search.py — 有 query 的检索模式
========================================

走 breath(query=...) 时进入这里。一次向量查询与 bucket_manager 的
关键词/BM25 检索融合，命中后逐字返回桶正文并套 token 预算。

关键行为：
- domain/valence/arousal 作为过滤参数传给 bucket_mgr.search
- embedding 未配置/未启用/调用失败时明确提示并继续关键词/BM25 检索
- 向量通道阈值 sim>=0.65；domain/tags/type 过滤与关键词通道完全一致
- 命中正文不经过 LLM 摘要、改写或压缩，直接返回当前存储的 content
- 命中后调 touch()，但不修改本次返回的正文或元数据
- 检索结果不足时，从低权重旧桶里随机漂出 3-5 条「忽然想起来」
- 命中 0 条时回 webhook 报空，并给出可操作的引导文案

不做什么（边界）：
- 不返回 feel/plan/letter（专用通道有自己的入口）
- pinned/protected/permanent 仍可被检索（也是记忆，只是同时在浮现模式置顶）
- dont_surface/digested 在真实检索命中中保留；只限制无参浮现和非命中随机漂浮

对外暴露：surface_search(query, max_results, max_tokens, domain, valence,
                          arousal, tag_filter) → str
========================================
"""

import asyncio
import hashlib
import random
from datetime import datetime, time

from locibrain.policy.surfacing import SurfacePolicyVM
from .. import _runtime as rt
from ._verbatim import render_stored_bucket
from utils import parse_iso_datetime

_SURFACE_POLICY = SurfacePolicyVM.default()

_VECTOR_QUERY_TOPK = 50

_SEMANTIC_DISABLED_NOTE = "[检索降级：语义索引暂不可用，本次仅使用关键词/BM25。]"
_BUDGET_NOTICE = "[token 预算不足：命中的下一条记忆未被截断或摘要，请提高 max_tokens 后重试。]"


def _bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def _can_surface_search(bucket: dict) -> bool:
    return _SURFACE_POLICY.evaluate_bucket(bucket, mode="search").allowed


def _is_archived(bucket: dict) -> bool:
    meta = bucket.get("metadata", {}) or {}
    return (
        str(meta.get("type") or "").strip().lower() == "archived"
        or bool(meta.get("deleted_at"))
        or bool(meta.get("tombstone"))
    )


def _render_archived_hit(bucket: dict, footprint: str) -> tuple[str, int]:
    bucket_id = str(bucket.get("id") or "")
    header = (
        f"[query 命中·已删除到档案] [bucket_id:{bucket_id}] "
        "[状态:已退出日常记忆，原文仍保留]"
    )
    rendered, _ = render_stored_bucket(bucket, header, footprint)
    rendered += (
        "\n[反思：这条记忆对当下的我有帮助吗？它值得被再次回忆吗？]"
        f'\n[若决定恢复：trace(bucket_id="{bucket_id}", restore=True)]'
    )
    from utils import count_tokens_approx
    return rendered, count_tokens_approx(rendered)


def _parse_date_bound(value: str, *, upper: bool) -> datetime | None:
    """解析创建时间边界；YYYY-MM-DD 的上界包含当天全日。"""
    raw = value.strip()
    if not raw:
        return None
    parsed = parse_iso_datetime(raw)
    if len(raw) == 10:
        day = parsed.date()
        return datetime.combine(day, time.max if upper else time.min)
    return parsed


def _bucket_in_created_range(
    bucket: dict,
    created_from: datetime | None,
    created_to: datetime | None,
) -> bool:
    if created_from is None and created_to is None:
        return True
    raw_created = str(bucket.get("metadata", {}).get("created") or "").strip()
    if not raw_created:
        return False
    try:
        created = parse_iso_datetime(raw_created)
    except (TypeError, ValueError):
        return False
    if created_from is not None and created < created_from:
        return False
    if created_to is not None and created > created_to:
        return False
    return True


async def _semantic_scores(query: str, top_k: int) -> tuple[dict[str, float], str]:
    """Run the vector query once and return scores plus an optional notice."""
    engine = rt.embedding_engine
    if not engine or not getattr(engine, "enabled", False):
        rt.logger.warning("breath semantic search unavailable; using keyword/BM25 only")
        return {}, _SEMANTIC_DISABLED_NOTE

    try:
        strict_search = getattr(engine, "search_similar_strict", None)
        if callable(strict_search):
            pairs = await strict_search(query, top_k=top_k)
        else:
            pairs = await engine.search_similar(query, top_k=top_k)
        return {bucket_id: float(score) for bucket_id, score in pairs}, ""
    except Exception as exc:
        rt.logger.warning(
            f"breath semantic search failed; using keyword/BM25 only: "
            f"{type(exc).__name__}: {exc}"
        )
        return {}, _SEMANTIC_DISABLED_NOTE


def _semantic_diagnostics(
    query: str,
    vector_scores: dict[str, float],
    semantic_notice: str,
) -> dict:
    """收集本次检索的可重建索引状态；不记录查询原文。"""
    engine_status: dict = {}
    status_reader = getattr(rt.embedding_engine, "status", None)
    if callable(status_reader):
        try:
            raw_status = status_reader()
            if isinstance(raw_status, dict):
                engine_status = dict(raw_status)
        except Exception as exc:
            engine_status = {"status_error": f"{type(exc).__name__}: {exc}"}

    outbox_status: dict = {}
    status_reader = getattr(getattr(rt, "embedding_outbox", None), "status", None)
    if callable(status_reader):
        try:
            raw_status = status_reader()
            if isinstance(raw_status, dict):
                outbox_status = dict(raw_status)
        except Exception as exc:
            outbox_status = {"status_error": f"{type(exc).__name__}: {exc}"}

    ranked = sorted(vector_scores.items(), key=lambda item: item[1], reverse=True)
    return {
        "query_hash": hashlib.sha256(
            query.encode("utf-8", errors="replace")
        ).hexdigest()[:12],
        "semantic_available": not bool(semantic_notice),
        "vector_candidates": len(vector_scores),
        "vector_top": [
            {"bucket_id": bucket_id, "score": round(score, 6)}
            for bucket_id, score in ranked[:5]
        ],
        "engine": {
            key: engine_status.get(key)
            for key in (
                "enabled", "backend", "model", "vector_dim",
                "embedding_count", "status_error",
            )
            if key in engine_status
        },
        "outbox": {
            key: outbox_status.get(key)
            for key in (
                "running", "provider_ready", "pending", "retrying",
                "last_success", "last_error", "status_error",
            )
            if key in outbox_status
        },
    }


async def surface_search(
    query: str,
    max_results: int,
    max_tokens: int,
    domain: str,
    valence: float,
    arousal: float,
    tag_filter: list,
    date_from: str = "",
    date_to: str = "",
) -> str:
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None
    try:
        created_from = _parse_date_bound(date_from, upper=False)
        created_to = _parse_date_bound(date_to, upper=True)
    except (TypeError, ValueError):
        return "日期格式无效，请使用 YYYY-MM-DD 或 ISO 8601 时间。"
    if created_from and created_to and created_from > created_to:
        return "date_from 不能晚于 date_to。"

    try:
        footprint_snapshot = rt.bucket_mgr.footprint_snapshot()
    except Exception as exc:
        rt.logger.warning(f"Footprint snapshot unavailable / 足迹读取失败: {exc}")
        footprint_snapshot = None

    def _footprint(bucket: dict) -> str:
        if footprint_snapshot is None:
            return "👣 Footprint：暂时无法读取"
        return footprint_snapshot.summary(
            str(bucket.get("id") or ""), bucket.get("metadata", {})
        )

    # A full bucket id is an address, not a semantic query.  Resolve it before
    # embedding/BM25 work so callers can reliably read the on-disk source text
    # immediately before trace(content=...) without an LLM or derived index in
    # the path.  Archived/deleted and dedicated bucket types keep the same
    # visibility boundary as ordinary search.
    exact_id = query.strip()
    try:
        exact_reader = getattr(rt.bucket_mgr, "get_including_archive", None)
        exact_bucket = (
            await exact_reader(exact_id)
            if callable(exact_reader)
            else await rt.bucket_mgr.get(exact_id)
        )
    except Exception as exc:
        rt.logger.warning(
            f"breath exact bucket lookup failed; continuing with search: "
            f"{type(exc).__name__}: {exc}"
        )
        exact_bucket = None
    if exact_bucket:
        meta = exact_bucket.get("metadata", {}) or {}
        is_archived = _is_archived(exact_bucket)
        archived_original_kind = (
            footprint_snapshot.original_kind(exact_id, meta)
            if is_archived and footprint_snapshot is not None
            else "dynamic"
        )
        if (
            is_archived
            and archived_original_kind not in ("feel", "plan", "letter")
            and _bucket_has_tags(meta, tag_filter)
            and _bucket_in_created_range(exact_bucket, created_from, created_to)
        ):
            rendered, entry_tokens = _render_archived_hit(
                exact_bucket, _footprint(exact_bucket)
            )
            return rendered if entry_tokens <= max_tokens else _BUDGET_NOTICE
        if (
            not is_archived
            and meta.get("type") not in ("feel", "plan", "letter")
            and _can_surface_search(exact_bucket)
            and _bucket_has_tags(meta, tag_filter)
            and _bucket_in_created_range(exact_bucket, created_from, created_to)
        ):
            rendered, entry_tokens = render_stored_bucket(
                exact_bucket,
                f"[exact_bucket_id:true] [bucket_id:{exact_bucket['id']}]",
                _footprint(exact_bucket),
            )
            if entry_tokens > max_tokens:
                return _BUDGET_NOTICE
            asyncio.create_task(
                rt.bucket_mgr.touch_many([exact_bucket["id"]], ripple=False)
            )
            if rt.fire_webhook:
                await rt.fire_webhook(
                    "breath",
                    {"mode": "exact_id", "matches": 1, "chars": len(rendered)},
                )
            return rendered

    vector_scores, semantic_notice = await _semantic_scores(
        query, top_k=max(max_results, _VECTOR_QUERY_TOPK)
    )
    semantic_diag = _semantic_diagnostics(query, vector_scores, semantic_notice)
    rt.logger.info("op=breath_search phase=semantic diagnostics=%s", semantic_diag)

    search_kwargs = {
        "limit": max(max_results, 20),
        "domain_filter": domain_filter,
        "query_valence": q_valence,
        "query_arousal": q_arousal,
        "vector_scores": vector_scores,
    }
    try:
        try:
            matches = await rt.bucket_mgr.search(
                query, include_archive=True, **search_kwargs
            )
        except TypeError as exc:
            # Lightweight third-party/test managers may predate the archive
            # option.  Preserve active search there; production supports it.
            if "include_archive" not in str(exc):
                raise
            matches = await rt.bucket_mgr.search(query, **search_kwargs)
    except Exception as e:
        rt.logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    eligible_matches = []
    for bucket in matches:
        meta = bucket.get("metadata", {}) or {}
        if _is_archived(bucket):
            original_kind = (
                footprint_snapshot.original_kind(str(bucket.get("id") or ""), meta)
                if footprint_snapshot is not None
                else "dynamic"
            )
            if original_kind in ("feel", "plan", "letter"):
                continue
        elif not _can_surface_search(bucket) or meta.get("type") in ("feel", "plan", "letter"):
            continue
        eligible_matches.append(bucket)
    matches = eligible_matches
    matches = [b for b in matches if _bucket_has_tags(b["metadata"], tag_filter)]
    matches = [
        b for b in matches
        if _bucket_in_created_range(b, created_from, created_to)
    ]
    matches = matches[:max_results]
    rt.logger.info(
        "op=breath_search phase=ranking query_hash=%s matches=%s ids=%s",
        semantic_diag["query_hash"],
        len(matches),
        [bucket.get("id") for bucket in matches],
    )

    results = []
    token_used = 0
    budget_blocked = False
    touched_ids: list = []   # 性能 P2：浮现后统一在后台 touch，不在响应路径逐条 await
    for bucket in matches:
        meta = bucket["metadata"]
        bucket_id = bucket["id"]
        if _is_archived(bucket):
            rendered, entry_tokens = _render_archived_hit(bucket, _footprint(bucket))
        elif meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent":
            header = f"📌 [核心准则] [bucket_id:{bucket_id}]"
            rendered, entry_tokens = render_stored_bucket(
                bucket, header, _footprint(bucket)
            )
        elif bucket.get("vector_match"):
            header = f"[语义关联] [bucket_id:{bucket_id}]"
            rendered, entry_tokens = render_stored_bucket(
                bucket, header, _footprint(bucket)
            )
        else:
            header = f"[bucket_id:{bucket_id}]"
            rendered, entry_tokens = render_stored_bucket(
                bucket, header, _footprint(bucket)
            )
        if token_used + entry_tokens > max_tokens:
            budget_blocked = True
            break
        results.append(rendered)
        token_used += entry_tokens
        if not _is_archived(bucket):
            touched_ids.append(bucket_id)

    # 性能 P2：把 touch 移出响应路径 —— 浮现完的桶在后台一次性更新激活，
    # ripple=False 跳过读全库的时间涟漪。响应不再等这些写盘/涟漪。
    if touched_ids:
        asyncio.create_task(rt.bucket_mgr.touch_many(touched_ids, ripple=False))

    # 检索命中不足时保留设计上的自由联想；用独立分区明确标记，
    # 避免调用方把随机旧桶误当成查询命中。
    if not budget_blocked and len(matches) < min(3, max_results):
        try:
            all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and _SURFACE_POLICY.evaluate_bucket(
                    b, mode="spontaneous"
                ).allowed
                and b["metadata"].get("type") not in ("feel", "plan", "letter")
                and rt.decay_engine.calculate_score(b["metadata"]) < 2.0
                and _bucket_in_created_range(b, created_from, created_to)
            ]
            remaining_slots = max(0, max_results - len(matches))
            if low_weight and remaining_slots:
                drifted = random.sample(
                    low_weight,
                    min(random.randint(3, 5), len(low_weight), remaining_slots),
                )
                drift_results = []
                for b in drifted:
                    rendered, entry_tokens = render_stored_bucket(
                        b,
                        f"[联想浮现·非检索命中] [bucket_id:{b['id']}]",
                        _footprint(b),
                    )
                    if token_used + entry_tokens > max_tokens:
                        budget_blocked = True
                        break
                    drift_results.append(rendered)
                    token_used += entry_tokens
                if drift_results:
                    results.append("=== 忽然想起来（非检索命中） ===\n" + "\n---\n".join(drift_results))
        except Exception as e:
            rt.logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        if budget_blocked:
            return f"{semantic_notice}\n{_BUDGET_NOTICE}" if semantic_notice else _BUDGET_NOTICE
        if rt.fire_webhook:
            await rt.fire_webhook("breath", {"mode": "empty", "matches": 0})
        empty_text = (
            f"没有匹配到「{query}」相关的记忆。\n"
            "可以换个关键词试试，或用 breath() 看当下权重池；feel 用 breath_advanced(domain=\"feel\")，信件用 letter_read。"
        )
        return f"{semantic_notice}\n{empty_text}" if semantic_notice else empty_text

    final_text = "\n---\n".join(results)
    notices = []
    if semantic_notice:
        notices.append(semantic_notice)
    if budget_blocked:
        notices.append(_BUDGET_NOTICE)
    if notices:
        final_text = "\n".join(notices + [final_text])
    if rt.fire_webhook:
        await rt.fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return final_text
