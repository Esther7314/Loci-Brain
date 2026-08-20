"""
========================================
tools/grow/core.py — grow's long-content main path (digest + merge)
========================================

Long content (>=30 chars) came through here. dehydrator.digest split the whole
passage into 2-6 event items, and each one tried merge_or_create on its own.

Key behaviour:
- If digest fails (API key unavailable) it raises RuntimeError and creates no bucket
- merge_or_create is called per item (the grow path uses LLM merge, which
  compresses old + new together)
- Every grow call generates a ``grow_batch_id`` shared by all buckets created in
  that batch, with source_tool always ``grow``; an existing bucket that gets
  merged into keeps its own source_tool
- One item failing does not affect the others; each item's size is checked
  against the byte ceiling
- If embedding fails the bucket is still created and a vectorisation-degraded
  warning is appended to the result
- At the end, plan auto-closing is triggered fire-and-forget (matched against
  the whole original passage)

What this file deliberately does not do:
- Never writes feel: grow files events, it does not reflect
- Never sets pinned: every event bucket grow produces is dynamic
- Never accepts why_remembered: grow is filing, and each bucket it produces is
  the event itself, which is the why

Exports: grow_core(content) -> str
========================================
"""

import asyncio
import uuid

from .. import _runtime as rt
from .._common import (
    merge_or_create,
    check_content_size,
    check_grow_items_payload,
    check_duplicate_for,
    check_plan_resolution,
)


# ⚰️ `grow_core` (throw in long prose, let the system split it into several) was
#    deleted along with its entry point.
#    The rule: **that was the one place in the whole system where the system
#    decided for me how many things this was**, which runs against "the one who
#    writes it down is always me"; and `grow_items` already covers the case —
#    work out for yourself, at the end of a stretch, how many things happened,
#    then store them in one call.
#    (This path had already been treated once before: `digest()` rewrote the
#     body, the rule was fixed at "split only, never edit", and it was replaced
#     by `cut()`, which only says where to cut. Now it does not even cut — the
#     whole path is gone.)



async def grow_items(items: list) -> str:
    """Pre-split mode: the calling model has already split the prose into N final
    bodies, which are stored verbatim.

    The key differences from grow_core:
    - **digest is never called**: the cheap LLM's second round of splitting and
      rewriting is skipped and the body is untouched (removing the second
      distortion);
    - each item only calls analyze() for metadata (domain/valence/arousal/tags/
      name); the body is never touched;
    - merging uses raw_merge=True (append the original text, no LLM compression
      of old + new), removing the third distortion.
    Storage keeps grow's shape: a shared grow_batch_id and source_tool=grow, so
    the dashboard can still display by batch.
    """
    payload_err = check_grow_items_payload(items)
    if payload_err:
        return payload_err

    # Normalisation: accept plain string items, and tolerate the
    # {"content": "..."} form by taking its body. Empty items are dropped.
    clean: list[str] = []
    for it in items:
        if isinstance(it, str):
            s = it.strip()
        elif isinstance(it, dict):
            s = str(it.get("content", "")).strip()
        else:
            s = ""
        if s:
            clean.append(s)
    if not clean:
        return "items 为空或都不合法，未创建任何桶。"

    batch_id = f"g_{uuid.uuid4().hex[:12]}"
    results = []
    created = 0
    merged = 0
    embed_warnings = []

    metadata_fallback = False

    # ── [LENTO PATCH] concurrent tagging ─────────────────────────────
    # The original implementation awaited analyze() serially inside the same for
    # loop, so N items queued N rounds of LLM calls.
    # Measured: a single hold takes 26-40s, three grow items >90s — past the MCP
    # client's 60s timeout. The server had in fact already written, but the caller
    # saw a timeout -> assumed nothing was written -> wrote again -> duplicate
    # buckets.
    # analyze() is read-only and side-effect free, so it is safe to run
    # concurrently; merge_or_create stays serial so two items cannot merge into
    # the same existing bucket at once.
    def _default_meta() -> dict:
        default_analysis = getattr(rt.dehydrator, "_default_analysis", None)
        return default_analysis() if callable(default_analysis) else {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_name": "",
        }

    async def _analyze_one(text: str):
        # A tagging failure (an unconfigured API key, say) must never cost the
        # body — fall back to local neutral metadata, matching hold's degraded
        # behaviour (see tools/hold/core.py).
        try:
            return await rt.dehydrator.analyze(text)
        except Exception as e:
            rt.logger.warning(
                "grow items metadata analysis failed; preserving raw content with local defaults / "
                f"grow items 打标失败，使用本地默认元数据并原样保存正文: {type(e).__name__}: {e}"
            )
            return None

    # Size check first: an oversized item should not waste a tagging call.
    size_errs: dict[int, str] = {}
    sized: list[tuple[int, str]] = []
    for idx, content_str in enumerate(clean):
        size_err = check_content_size(content_str)
        if size_err:
            size_errs[idx] = size_err
        else:
            sized.append((idx, content_str))

    metas_by_idx: dict[int, dict] = {}
    if sized:
        gathered = await asyncio.gather(*(_analyze_one(text) for _, text in sized))
        for (idx, _), meta in zip(sized, gathered):
            if meta is None:
                metadata_fallback = True
                meta = _default_meta()
            metas_by_idx[idx] = meta
    # ── [/LENTO PATCH] ───────────────────────────────────────────────

    for idx, content_str in enumerate(clean):
        if idx in size_errs:
            results.append(f"⚠️（{size_errs[idx]}）")
            continue
        try:
            meta = metas_by_idx[idx]
            result_name, is_merged, embed_warn = await merge_or_create(
                content=content_str,
                tags=meta.get("tags") or [],
                importance=5,
                domain=meta.get("domain") or ["未分类"],
                valence=meta.get("valence", 0.5),
                arousal=meta.get("arousal", 0.3),
                name=meta.get("suggested_name", ""),
                source_tool="grow",
                grow_batch_id=batch_id,
                raw_merge=True,  # append verbatim; merging never compresses
            )
            if embed_warn and embed_warn not in embed_warnings:
                embed_warnings.append(embed_warn)
            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{result_name}")
                created += 1
                asyncio.create_task(check_duplicate_for(result_name, content_str))
        except Exception as e:
            rt.logger.warning(f"grow items 条目处理失败 / verbatim item failed: {e}")
            results.append("⚠️")

    asyncio.create_task(check_plan_resolution("\n".join(clean)))
    summary = f"{len(clean)}条(预拆分·逐字)|新{created}合{merged} batch:{batch_id}\n" + "\n".join(results)
    if embed_warnings:
        summary += f"\n⚠️ {embed_warnings[0]}"
    if metadata_fallback:
        summary += "\n⚠️ 打标 API 暂不可用：正文已逐字保存，未做任何压缩；元数据暂用本地中性值。"
    return summary
