"""
========================================
tools/letter/core.py — letter_write / letter_read implementation
========================================

A letter bucket holds a long letter between the two sides of this conversation.
It is its own type: kept forever, never decayed, and never surfacing in an
ordinary breath.

Key behaviour:
- letter_write: the text is kept forever; author accepts any string as a
  signature ("ai", or a value equal to ai_name, is stored as ai_name's value,
  any other string is stored verbatim as the signature, and "user" means the
  user's side); writes type=letter plus author/title/letter_date metadata
- letter_read: newest first by default; with a query it goes through vector
  neighbours; supports author / date_from / date_to filtering, and the author
  field is returned exactly as stored, never converted

What this file deliberately does not do:
- letters are never merged, never compressed, never archived by decay

Exports: letter_write / letter_read
========================================
"""

import math
from typing import Optional

from core import visibility as _V
from .. import _runtime as rt
from .._common import check_content_size, check_metadata_size, check_query_size
from utils import strip_wikilinks, get_ai_name




async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
    ai_name: Optional[str] = "",
) -> str:
    if user_name is None:
        user_name = ""
    if title is None:
        title = ""
    if date is None:
        date = ""
    # ai_name: an explicit argument wins, otherwise the AI_NAME environment
    # variable (falling back to "AI").
    ai = (ai_name or "").strip() or get_ai_name()
    if not author or not author.strip():
        return "author 不能为空。"
    if not content or not content.strip():
        return "信件内容不能为空。"
    size_err = check_content_size(content)
    if size_err:
        return size_err
    metadata_err = check_metadata_size(
        author=author,
        user_name=user_name,
        title=title,
        date=date,
        ai_name=ai_name,
    )
    if metadata_err:
        return metadata_err

    # Signature normalisation:
    #   - "user" -> the user's side, stored as "user" (the user name is stored
    #     separately in user_name; that logic is unchanged)
    #   - "ai" / a value equal to ai_name / the legacy value "claude" (kept for
    #     compatibility) -> all stored as ai_name's value
    #   - any other string -> kept verbatim as the signature
    raw = author.strip()
    low = raw.lower()
    if low == "user":
        a = "user"
    elif low in ("ai", "claude") or raw == ai:
        a = ai
    else:
        a = raw

    extra_meta = {"author": a}
    if user_name.strip():
        extra_meta["user_name"] = user_name.strip()
    if title.strip():
        extra_meta["title"] = title.strip()[:120]
    if date.strip():
        extra_meta["letter_date"] = date.strip()

    bucket_id = await rt.bucket_mgr.create(
        content=content.strip(),
        tags=["__letter__"],
        importance=10,
        domain=["letter"],
        valence=0.5,
        arousal=0.3,
        name=(title.strip()[:60] or f"{a}_{date.strip() or 'letter'}"),
        bucket_type="letter",
        source_tool="letter",
    )
    try:
        await rt.bucket_mgr.update(bucket_id, **extra_meta)
    except Exception as e:
        rt.logger.warning(f"letter_write update meta failed: {e}")
    # Note: bucket_mgr.create() already posted the vector to the embedding
    # outbox once content hit disk. Calling generate_and_store again here is
    # unnecessary and would be wrong.
    return f"💌letter→{bucket_id} [{a}]"


async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    if query is None:
        query = ""
    if limit is None:
        limit = 10
    if author is None:
        author = ""
    if date_from is None:
        date_from = ""
    if date_to is None:
        date_to = ""
    query_err = check_query_size(query)
    if query_err:
        return query_err
    metadata_err = check_metadata_size(
        author=author,
        date_from=date_from,
        date_to=date_to,
    )
    if metadata_err:
        return metadata_err
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError, OverflowError):
        limit = 10
    try:
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        return f"读取信件失败: {e}"
    # Reading letters is a lookup (core/visibility.py, the `letter` road): dont_surface
    # hides nothing, and a letter that is not live is never read out as if it were.
    letters = [b for b in all_b if b["metadata"].get("type") == "letter"
               and _V.visible_for(b, road=_V.LETTER)]
    af = author.strip()
    if af:
        ai = get_ai_name()
        af_low = af.lower()
        if af_low == "user":
            letters = [b for b in letters if b["metadata"].get("author") == "user"]
        elif af_low in ("ai", "claude") or af == ai:
            # The AI side: match the current ai_name signature plus the legacy "claude"
            ai_aliases = {ai, "claude"}
            letters = [b for b in letters if b["metadata"].get("author") in ai_aliases]
        else:
            # Any custom signature: match the stored value exactly
            letters = [b for b in letters if b["metadata"].get("author") == af]

    def _within(b):
        d = b["metadata"].get("letter_date") or b["metadata"].get("created", "")
        if date_from and d and d < date_from:
            return False
        if date_to and d and d > date_to:
            return False
        return True

    letters = [b for b in letters if _within(b)]

    query_text = query.strip()

    def _matches_query(b):
        if not query_text:
            return True
        meta = b.get("metadata", {})
        parts = [
            b.get("content", ""),
            str(meta.get("name") or ""),
            str(meta.get("title") or ""),
            str(meta.get("author") or ""),
        ]
        parts.extend(str(t) for t in (meta.get("tags") or []))
        return query_text.lower() in "\n".join(parts).lower()

    if query_text and rt.embedding_engine and getattr(rt.embedding_engine, "enabled", False):
        try:
            sims = await rt.embedding_engine.search_similar(query_text, top_k=limit * 3)
            id_score = {bid: sc for bid, sc in sims}
            vector_matches = [b for b in letters if b["id"] in id_score]
            if vector_matches:
                letters = vector_matches
                letters.sort(key=lambda b: id_score.get(b["id"], 0.0), reverse=True)
            else:
                letters = [b for b in letters if _matches_query(b)]
                letters.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)
        except Exception as e:
            rt.logger.warning(f"letter_read vector search failed: {e}")
            letters = [b for b in letters if _matches_query(b)]
            letters.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    else:
        if query_text:
            letters = [b for b in letters if _matches_query(b)]
        letters.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)

    letters = letters[:limit]
    if not letters:
        return "没有找到匹配的信件。"
    parts = []
    for b in letters:
        m = b["metadata"]
        a = m.get("author", "?")
        d = (m.get("letter_date") or m.get("created", ""))[:10]
        title = m.get("title") or m.get("name", "")
        parts.append(
            f"[{b['id']}] {a} · {d}{(' · ' + title) if title else ''}\n"
            + strip_wikilinks(b["content"])
        )
    return "=== 信件 ===\n" + "\n\n---\n\n".join(parts)
