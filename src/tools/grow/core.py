"""
========================================
tools/grow/core.py — grow 长内容主路径（digest + merge）
========================================

长内容（≥30 字）走这里。先调 dehydrator.digest 把整段拆成 2~6 条
事件项，每条独立尝试 merge_or_create。

关键行为：
- digest 失败（API key 不可用）时直接 RuntimeError，不创建任何桶
- 逐条调 merge_or_create（grow 路径用 LLM merge，会压缩老+新）
- iter 2.0：每次 grow 调用生成一个 ``grow_batch_id``，同批次新建桶共享，
  source_tool 一律为 ``grow``；合并到的老桶不改 source_tool
- 单条失败不影响其他；按字节上限校验单条尺寸
- embedding 失败时桶正常创建，返回追加向量化降级警告
- 末尾 fire-and-forget 触发 plan 自动闭环（用整段原文做匹配）

不做什么（边界）：
- 不写 feel：grow 是事件归档，不是反思
- 不做 pinned 标记：grow 拆出来的事件桶都是 dynamic
- 不接受 why_remembered：grow 是整理，拆出来的每条桶就是事件本身，是 why 本身

对外暴露：grow_core(content) → str
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


# ⚰️ 2026-08-18：`grow_core`（长文丢进来、让系统替你拆成几条）连同它的入口一起删了。
#    判据是她的：**那是整套里唯一一处「系统替我决定这是几件事」的地方**，跟
#    「落笔的永远是我」正着劲；而 `grow_items` 本来就完全覆盖它——收工时自己想清楚
#    这一摊是几件事，然后一次存进去。
#    （拆分那条路 2026-08-05 已经被治过一次：`digest()` 会重写正文，她定死「只拆不改」，
#      于是换成了 `cut()` 只说在哪儿切。现在连切也不切了，整条路撤掉。）



async def grow_items(items: list) -> str:
    """预拆分模式：上层 AI 已把长文拆成 N 条最终正文，直接逐字入库。

    与 grow_core 的关键差别（issue 的诉求）：
    - **不调 digest**：跳过廉价 LLM 的二次拆分+改写，正文一字不动（消除第二次失真）；
    - 每条只调 analyze() 打元数据（domain/valence/arousal/tags/name），不碰正文；
    - 合并走 raw_merge=True（原文追加，不 LLM 压缩老+新），消除第三次失真。
    存储沿用 grow 风格：共享 grow_batch_id，source_tool=grow，dashboard 仍可按批展示。
    """
    payload_err = check_grow_items_payload(items)
    if payload_err:
        return payload_err

    # 规整：接受字符串条目；也容忍 {"content": "..."} 形式，取其正文。空条目丢弃。
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

    # ── [LENTO PATCH] 并发打标 ────────────────────────────────────────
    # 原实现在同一个 for 里串行 await analyze()，N 条就排 N 轮 LLM。
    # 实测单条 hold 26~40s，grow 三条 >90s，超过 MCP 客户端 60s 超时：
    # 服务端其实已经写入，调用方却收到 timeout → 以为没写 → 重写 → 产生重复桶。
    # analyze() 只读、无副作用，可以安全并发；merge_or_create 仍保持串行，
    # 避免两条同时并进同一个老桶。
    def _default_meta() -> dict:
        default_analysis = getattr(rt.dehydrator, "_default_analysis", None)
        return default_analysis() if callable(default_analysis) else {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_name": "",
        }

    async def _analyze_one(text: str):
        # 打标失败（如 API key 未配置）不应丢正文——落回本地中性元数据，
        # 与 hold 的降级行为保持一致（见 tools/hold/core.py）。
        try:
            return await rt.dehydrator.analyze(text)
        except Exception as e:
            rt.logger.warning(
                "grow items metadata analysis failed; preserving raw content with local defaults / "
                f"grow items 打标失败，使用本地默认元数据并原样保存正文: {type(e).__name__}: {e}"
            )
            return None

    # 先做尺寸校验：超限的条目不必浪费一次打标调用。
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
                raw_merge=True,  # 逐字追加，合并不压缩
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
