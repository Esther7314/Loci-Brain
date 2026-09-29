"""
========================================
tools/grow/rooms_path.py — the newer grow: kind=event|mind
========================================

The core principle in one line: **the body hits disk first, metadata is filled in
afterwards.**
The body is what the caller wrote; lose it and it is gone. Metadata (tags, gist,
naming, vectors) is derived, and arriving ten seconds late hurts nobody.

Key behaviour:
- event: several at a time, each going straight to bucket_mgr.create(); nothing
  is searched for, judged or merged on the way in (merging was the root cause of
  the timeouts, and of "different things merged into one bucket")
- The real list of bucket_ids comes back immediately (target: under 3 seconds);
  tagging, gist and naming go to background backfill
- mind: its own bucket + from (structure copied from feel), and v/a must
  be supplied by the caller
- event's v/a became mandatory too; background backfill **never touches any
  bucket's v/a**; importance/meaning are passed by the caller (optional);
  tags are scene anchors, while broadenings go into aliases and feed bm25 only
- direction_of_fit / bound / evidential / internally_generated / weight are
  validated here and written by create() itself, in the same write as the body
- With direction_of_fit="telic", when accepts one more form: a duration marker (3w/10d/2m/1y)
  which, together with absolute dates, makes up a want's "three kinds of clock";
  the third is waiting for a trigger (when is left empty and the condition is
  written into the body). How the three are read belongs to
  `core/profile._want_clock`; this file only validates that they can be stored
- Validation comes first: if any single item is invalid the whole call errors and
  no bucket is created

What this file deliberately does not do:
- No merging (fixed by the spec: every item in items becomes its own bucket)
- room is never generated or modified by a model (tools/_rooms.py validates; the
  caller decides)
- A failed background backfill only logger.warning()s — no rollback, no retrying
  to death

Exports: grow_event(items, direction_of_fit, bound, evidential, internally_generated,
                    weight, from_ids, test_data) -> str
         grow_mind(room, text, from_ids, v, a, direction_of_fit, bound, evidential,
                   internally_generated, weight, test_data) -> str
========================================
"""

import asyncio
import uuid
from datetime import timedelta

from core import _fold as _F       # a big event = fold's way of circling time
from .. import _runtime as rt
from core._bigevent import SPAN_RE, first_line as _F_first_line
from .._common import check_content_size
from core._rooms import check_room, is_mind_room
from .._subjects import normalize_bound, normalize_subjects
from utils import parse_bool

# from (a 64-character ceiling): five 12-hex-digit ids plus four
# commas = 64, which fits exactly; from the sixth on it would be silently
# truncated into half an id pointing at a bucket that does not exist. So it is
# stopped dead here.
_FROM_MAX = 5
_TRIGGERED_BY_LIMIT = 64      # matches bucket_manager._TRIGGERED_BY_MAX

# A body longer than this earns a line in the response saying "this looks like
# more than one thing".
# The chosen behaviour: **point it out and let me decide whether and how to
# split** — never split automatically.
# (Splitting automatically would make one grow call suddenly produce several
#  extra ids, and which one `from` should point at would have to be reconsidered;
#  besides, "when in doubt, do not split" is the tone of this whole thing.)
_LONG_HINT = 600
import re as _re
_WHEN_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")
# A want's duration marker, which carries a magnitude — `<N><unit>`, where
# d=day w=week m=month (~30 days) y=year (~365 days). No prefix symbol: a symbol
# that carries no meaning does not stay.
# Recognised only for something wanted (telic) — an ordinary event's when is "the day this
# happened", where a duration marker means nothing, so that path still accepts
# absolute dates only.
_WANT_DURATION_RE = _re.compile(r"^\d+[dwmy]$")

# The summary prompt used by background backfill. EVENT records "what happened",
# MIND records "what I came to see".
_SUMMARY_PROMPT = (
    "你是记忆系统的摘要器。给下面这段记忆写一句话摘要，直接输出那一句，"
    "不要引号不要前缀，中文，不超过60字。"
    "如果是一件事，概括发生了什么；如果是一条认知/感受，概括认识到了什么。"
)


def _placeholder_meta() -> dict:
    """Locally neutral placeholders used at create time; the real values are
    filled in later by the background _backfill."""
    return {"tags": [], "importance": 5, "domain": ["未分类"],
            "valence": 0.5, "arousal": 0.3}


# The stamp. One value, one meaning: **this field was not written by the model,
# it was cut out of the caller's own body text.**
#
# 🔴 It exists for a reader, not for a human eye. Her words on 2026-08-20:
#    「可以兜底 但是能不能做个记号 比如说谁没打标是兜底的 然后不是正好也要做一个
#    一键打标的按钮嘛」 — the second half is the criterion, not an aside. A fallback
#    is by construction invisible: the field stops being empty, so every "this one is
#    unfinished" check stops matching it and **nothing ever comes back to finish the
#    job**. The stamp is what a re-tagging pass can still find them by.
#
# It is written ONLY when a fallback actually fired, and it is cleared the moment the
# model does supply the real thing — so "the field is present" means exactly "this is
# still standing in for an answer that never came", with no third state to interpret.
_SOURCE_FALLBACK = "fallback"

# How much of the opening line a fallback name may use. Not a new number: it is the
# one `_make_summary`'s own degraded path already uses for "the opening of the body".
_FALLBACK_NAME_MAX = 60


def _fallback_name(text: str) -> str:
    """A stand-in title cut from the body's first line.

    The same move `_make_summary` degrades to, and for the same reason: it quotes what
    the caller wrote instead of inventing a line. A bucket with no title is not neutral
    — it is called after the second it was born in (`2026-08-20 01-05-33`), which is a
    row of digits carrying nothing, in a list read by eye.

    Returns "" for a body with nothing in it, and an empty name is never written: there
    is no text to quote, so there is nothing honest to put there.
    """
    return _F_first_line(text).strip()[:_FALLBACK_NAME_MAX].strip()


async def _make_summary(text: str) -> tuple[str, bool]:
    """Write a one-sentence gist through the same LLM channel the dehydrator uses.

    Returns `(summary, came_from_fallback)`. The second half of that pair is the whole
    reason this signature changed: the degraded path below produces a perfectly
    ordinary-looking summary, and the caller could not previously tell it apart from one
    the model wrote — so it could not stamp it either.

    On failure it returns `("", False)` (never blocking the backfill of the other
    fields); an empty summary is the marker `backfill_sweep` finds this bucket by, and
    that is a different state from "a stand-in is in place".
    """
    chat = getattr(rt.dehydrator, "_chat", None)
    if not callable(chat):
        return "", False
    # Give max_tokens plenty of room: a reasoning model spends tokens thinking, so
    # a budget of 100 gets eaten entirely and content comes back empty
    # (_chat_once returns an empty string for an empty response rather than
    # raising). An empty result is retried once.
    for _attempt in range(2):
        try:
            raw = await chat(_SUMMARY_PROMPT, text[:2000], max_tokens=400, temperature=0.3)
        except Exception as e:
            rt.logger.warning(f"summary 生成失败（正文已落盘，不影响）: {e}")
            return "", False
        out = (raw or "").strip().strip('"').strip()[:200]
        if out:
            return out, False
        rt.logger.warning("summary 返回空，重试一次" if _attempt == 0 else
                          "summary 两次为空（疑似内容过滤），降级用正文开头")
    # Degraded path: use the start of the body as the gist — it is the caller's
    # own text, not something invented. Better than leaving it empty: a gist is
    # the hook by which you know an entry exists, and without the hook that
    # memory is invisible in the zoomed-out views.
    # It comes back stamped, because from here on it is indistinguishable from a
    # real one by looking at it.
    return text[:60].strip(), True


# The similarity line for "possibly the same thing". It is **the same number** as
# the elbow on the dashboard's similarity page (a full pairwise cosine scan of the
# library puts the elbow at 80).
_DUP_COS_THRESHOLD = 0.80


async def _merged_tags(bucket_id: str, additions: list[str]) -> list[str] | None:
    """The bucket's current tags with `additions` appended, read right before the write.

    🔴 `bucket_mgr.update(tags=...)` replaces the whole list. System tags (`__gist__`,
       `__档案事实__`, `__大event__`) are put on at creation, before backfill runs, and the
       model knows nothing about them — so every tag backfill writes has to go on top of
       what is already there. That includes a lone similarity hint on a round where the
       model returned no tags: a regrown entry is always close to the version it replaced,
       so that round is the common case, not the rare one.
    Read as late as possible, so tags applied while the model calls were running are kept.
    Returns None when the current tags cannot be read: writing the additions alone would
    be a replacement, and skipping one round of added tags is the cheaper loss.
    """
    try:
        cur = await rt.bucket_mgr.get(bucket_id)
    except Exception as e:
        rt.logger.warning(f"backfill 读不到 {bucket_id} 现有的 tags，这轮不写 tags: {e}")
        return None
    if not cur:
        return None
    existing = [str(t) for t in ((cur.get("metadata") or {}).get("tags") or [])]
    return list(dict.fromkeys(existing + additions))


async def _backfill_one(bucket_id: str, text: str, kind: str) -> None:
    """Fill in one bucket's metadata in the background: tags / aliases / gist /
    name.

    kind = "event" | "mind" | "big".
    🔴 v/a is never backfilled: an event's v/a is now set by the caller too —
    hand "what I felt at the time" to a model to guess and the memory stops being
    mine.
    mind extracts no scene (there are no photographs inside a piece of thinking),
    so its tags come out empty — that is normal and accepted.
    """
    update_kwargs: dict = {}
    # Every tag this backfill wants to add. They are only ever added: the list goes on
    # top of the bucket's current tags right before the write (see _merged_tags).
    tag_additions: list[str] = []
    try:
        meta = await rt.dehydrator.analyze(text, for_mind=(kind == "mind"))
    except Exception as e:
        rt.logger.warning(f"backfill analyze 失败 {bucket_id}（正文已落盘）: {e}")
        meta = None
    if meta:
        if meta.get("tags"):
            tag_additions += [str(t) for t in meta["tags"]]
        if meta.get("aliases"):
            # Broadenings feed bm25 only (bm25_index.build consumes them); they
            # never appear on the tag line a human reads
            update_kwargs["aliases"] = meta["aliases"]
        if meta.get("subjects"):
            # Subjects (who). The third kind of tag: extracted by deepseek and
            # normalised through the alias table, kept as its own field — not in
            # tags (that would break the literal-string guarantee) and not in
            # aliases (it must not enter BM25 scoring).
            update_kwargs["subjects"] = normalize_subjects(meta["subjects"])
        if meta.get("domain"):
            # domain is now used purely as a folder; retrieval does not consume
            # it, so whatever it gets filled with makes no difference
            update_kwargs["domain"] = meta["domain"]
    # --- Naming, and what happens when no name comes back ---
    # 🔴 Deliberately OUTSIDE the `if meta:` block above: the case this exists for is
    #    "the model call did not succeed", and the commonest shape of that is meta being
    #    None altogether. Fold it back inside and the fallback stops firing in exactly
    #    the situation it was written for — silently, since a bucket named after its own
    #    birth-second looks like a bucket, not like a failure.
    # She settled this on 2026-08-20: fall back, **but stamp it**.
    if meta and meta.get("suggested_name"):
        update_kwargs["name"] = meta["suggested_name"]
        # The model named it, so any earlier stand-in is over. None deletes the field.
        # Written only alongside a real name — never on its own, or a bucket that never
        # had a stamp would get a pointless write (and, worse, `if not update_kwargs`
        # below would stop being able to tell "nothing to do" from "something to do").
        update_kwargs["name_source"] = None
    else:
        fallback = _fallback_name(text)
        if fallback:
            update_kwargs["name"] = fallback
            update_kwargs["name_source"] = _SOURCE_FALLBACK

    summary, summary_is_fallback = await _make_summary(text)
    if summary:
        update_kwargs["summary"] = summary
        # Same two-state rule as the name: stamped while standing in, cleared the moment
        # the model supplies a real one.
        update_kwargs["summary_source"] = _SOURCE_FALLBACK if summary_is_fallback else None

    # The "possibly the same thing" hint: nothing is merged and nothing is
    # blocked. Similarity is checked once in the background, and above the
    # threshold the new bucket gets a 「疑似同件:xx」 tag for human eyes to settle
    # later.
    # The threshold errs high rather than low — fewer stickers is better than more.
    # It now queries the **vector cosine** directly: it used to use search's
    # combined score >= 80, but once scoring was cut down to two dimensions the
    # scale of that combined score changed entirely — and "is this the same
    # thing" is a question about semantic distance, not about retrieval ranking.
    # Similarity between minds is handled elsewhere (the pin reminder: a thought
    # recurring is not noise, it is a principle surfacing).
    if kind == "event":
        try:
            ee = getattr(rt.bucket_mgr, "embedding_engine", None)
            if ee and getattr(ee, "enabled", False):
                sims = await ee.search_similar(text, top_k=3)
                _top = next(((sid, s) for sid, s in sims if str(sid) != bucket_id), None)
                if _top:
                    rt.logger.info(f"[近似] {bucket_id} top={str(_top[0])[:6]} cos={float(_top[1]):.2f}")
                for sid, s in sims:
                    sid = str(sid)
                    if sid and sid != bucket_id and float(s) >= _DUP_COS_THRESHOLD:
                        tag_additions.append(f"疑似同件:{sid[:6]}")
                        break
        except Exception:
            pass
    elif kind == "mind":
        # Similarity between minds **does** deserve a reminder — the old comment
        # here said "similar thinking is normal, do not flag it", and that was
        # wrong: a thought recurring is not noise, **it is a principle
        # surfacing**.
        # Threshold 0.80, the same number as for suspected same-thing. It applies
        # a 「相似认知:」 tag, and breath's waking screen suggests turning it into
        # a statement of intent before pinning (a descriptive one must not be
        # pinned as-is — pin a flaw as a principle and it comes to mean "I intend
        # to keep making this mistake").
        try:
            ee = getattr(rt.bucket_mgr, "embedding_engine", None)
            if ee and getattr(ee, "enabled", False):
                sims = await ee.search_similar(text, top_k=6)
                for sid, s in sims:
                    sid = str(sid)
                    if not sid or sid == bucket_id or float(s) < _DUP_COS_THRESHOLD:
                        continue
                    sb = await rt.bucket_mgr.get(sid)
                    smeta = (sb or {}).get("metadata", {}) or {}
                    # Do not write `"/MIND/" in room`: the new room names have no
                    # leading slash and would silently fail to match
                    if not is_mind_room(smeta.get("room")) \
                            and str(smeta.get("type") or "") not in ("feel", "i"):
                        continue
                    if smeta.get("superseded_by"):
                        continue  # a superseded thought is not "surfacing again" — it is the same entry's earlier life
                    tag_additions.append(f"相似认知:{sid[:6]}")
                    rt.logger.info(f"[准则冒头] {bucket_id} ≈ {sid[:6]} cos={float(s):.2f}")
                    break
        except Exception:
            pass

    if tag_additions:
        merged = await _merged_tags(bucket_id, tag_additions)
        if merged is not None:
            update_kwargs["tags"] = merged
    if not update_kwargs:
        return
    try:
        await rt.bucket_mgr.update(bucket_id, **update_kwargs)
    except Exception as e:
        rt.logger.warning(f"backfill update 失败 {bucket_id}（正文已落盘）: {e}")


async def _backfill_batch(pairs: list[tuple[str, str, str]]) -> None:
    """Concurrent backfill (each entry in pairs = (bucket_id, text, kind)). Run
    serially, 5 items x (analyze + summary, about 14s each) would take over 70s.
    analyze/_chat are read-only and side-effect free, so they can run concurrently
    (the same precedent as the [LENTO PATCH] in grow_items), and each update
    writes its own bucket under a per-bucket lock, so they do not collide."""
    await asyncio.gather(
        *(_backfill_one(bucket_id, text, kind) for bucket_id, text, kind in pairs),
        return_exceptions=True,
    )
    # A backfill write always invalidates the cache — while still in the
    # background, warm the whole-library parse cache back up, so the next waking
    # screen does not have to pay 8 seconds for it
    try:
        await rt.bucket_mgr.list_all()
    except Exception:
        pass


# ------------------------------------------------------------
# The gate in front of retired fields
# ------------------------------------------------------------
# Two sentences are the verdict on these two fields:
#   importance —— `decay_engine.py` states in black and white that importance
#     takes no part, and its only consumer's own comment says it no longer
#     decides whether any bucket lives or dies. Keeping a score I have to assign
#     every single time and nobody reads only adds weight to writing.
#   meaning    —— "why it matters" is, put plainly, the thinking this thing
#     provoked; and what matters in the end is that thinking. Keeping the field
#     means **leaving a back door for an event to write a mind on the sly**, and
#     a sentence written into meaning has no source chain, cannot be regrown, and
#     cannot be pulled together while musing — it is a dead field.
#     Evidence: what was sitting in one bucket's meaning was, by itself, a
#     complete piece of thinking.
# 🔴 To say "why it matters", write a proper grow(kind="mind") entry, so that it
#    has a provenance and can be re-versioned.
_RETIRED_MSG = {
    "importance": (
        'importance 已退役（2026-08-16）——它不参与遗忘公式，'
        '「不再决定任何桶的生死」是它自己注释里的原话。别再打这个分。'),
    "meaning": (
        'meaning 已退役（2026-08-16）——想说「为什么重要」就写成一条真的认知：'
        'grow(kind="mind", room="MIND/TRAITS", text="…", from=["这条事件的id"], v=, a=)。'
        '写在 meaning 里的话没有来源链、不能 regrow、不能被发呆整合。'),
    "digested": (
        'digested 已退役（2026-08-16）——我们没有「消化」这个动作，'
        '所有的事件、认知都长新的。'),
}


def _retired_fields_msg(*names: str) -> str:
    """Assemble one rejection message for a set of retired fields (shaped like
    _rooms.py's rejections: say what is wrong and give a way out)."""
    hits = [n for n in names if n in _RETIRED_MSG]
    return "\n".join(_RETIRED_MSG[n] for n in hits)


def _retired_item_fields(item: dict) -> str:
    """If items[i] carries a retired field, return the rejection message;
    otherwise return an empty string."""
    hit = [n for n in ("importance", "meaning", "digested")
           if item.get(n) not in (None, "")]
    return _retired_fields_msg(*hit)


def _check_v2(kind: str, direction_of_fit, bound, evidential,
              internally_generated, weight) -> tuple[dict, str]:
    """The v2 write-layer arguments -> the create() keywords, or an error to hand back.

    Written in the same create() as the body: a follow-up update() that fails would
    leave a body without its orientation."""
    d = str(direction_of_fit or "").strip().lower()
    if d not in ("", "thetic", "telic"):
        return {}, ('direction_of_fit 只有两个值："telic"（想让它发生：答应的、计划的、想要的），'
                    '或者不填（记下已经是这样的）。')
    telic = d == "telic"
    names, err = normalize_bound(bound)
    if err:
        return {}, err
    if names and not telic:
        return {}, 'bound 只跟 direction_of_fit="telic" 一起用：只有想要的事才有谁被绑着。'
    ev = str(evidential or "").strip().lower()
    if ev and kind != "mind":
        return {}, ('evidential 只给 mind。事件是亲历还是听说，写在房间里：'
                    'EVENT/SELF 亲历，EVENT/WORLD 听说、看到。')
    if ev not in ("", "inference", "assumption"):
        return {}, 'evidential 只有两个值："inference"（有看得见的迹象）/ "assumption"（凭推理、常识）。'
    ig = parse_bool(internally_generated, default=False)
    if ig and kind != "event":
        return {}, ("internally_generated 只给事件：梦和想象是一段经历，存成 EVENT/SELF；"
                    "梦醒后想明白的才是 mind，它不带这个记号。")
    if weight is not None and not telic:
        return {}, 'weight 只跟 direction_of_fit="telic" 一起用：压在心上多重，只有想要的事才有。'
    fields = {"direction_of_fit": d if telic else "", "bound": names,
              "evidential": ev, "internally_generated": ig}
    if weight is not None:
        fields["weight"] = max(0.0, min(1.0, float(weight)))
    return fields, ""


def _in_the_future(when: str) -> bool:
    """Is `when` after now? A bare date is its local midnight; ten minutes of slack for
    a clock that runs a little ahead."""
    from core import _when as _w
    t = _w.parse_stamp(when)
    return bool(t) and t > _w.now() + timedelta(minutes=10)


async def backfill_sweep() -> int:
    """Self-healing at startup: a backfill in flight under a bare
    asyncio.create_task is lost across a restart, leaving buckets that have only
    a body and placeholder metadata. On boot they are found again and refilled.

    No persistent queue is built — **the scan is the queue**: "room has a value
    (i.e. it was stored by the new path) and summary is missing" is a reliable
    marker of unfinished work, it disappears by itself once the backfill
    succeeds, and it is naturally idempotent.
    Older grow buckets have no room, so they are never swept by mistake.

    The source_tool criterion was later dropped. It used to accept only
    grow/regrow, which meant migrated older buckets (source_tool=hold/import...)
    could never be repaired — 444 of them were missed for two months.
    "room has a value but summary is missing" is a complete criterion on its own;
    where the bucket came from is irrelevant.
    """
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.warning(f"backfill_sweep 扫描失败: {e}")
        return 0
    pending: list[tuple[str, str, str]] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not meta.get("room") or meta.get("summary"):
            continue
        kind = "mind" if is_mind_room(meta.get("room")) else "event"
        pending.append((str(b.get("id")), str(b.get("content") or ""), kind))
    if pending:
        rt.logger.info(f"backfill_sweep: 补 {len(pending)} 个上次没回填完的桶")
        await _backfill_batch(pending)
    return len(pending)


# ------------------------------------------------------------
# kind="event"
# ------------------------------------------------------------

def _normalize_from(from_ids) -> tuple[list[str] | None, str]:
    """Normalise the from list and check its count and total length. Returns
    (ids, error message); ids=None means nothing was passed."""
    if from_ids is None:
        return None, ""
    if isinstance(from_ids, str):
        from_ids = [s.strip() for s in from_ids.split(",") if s.strip()]
    if not isinstance(from_ids, list):
        return None, "from 要传 bucket_id 列表。"
    ids = [str(s).strip() for s in from_ids if str(s).strip()]
    if not ids:
        return None, ""
    if len(ids) > _FROM_MAX:
        return None, (f"from 最多 {_FROM_MAX} 条（收到 {len(ids)} 条）。"
                      "底层字段 64 字符上限，多了会被静默截断成半截 id——拆开分别存。")
    joined = ",".join(ids)
    if len(joined) > _TRIGGERED_BY_LIMIT:
        # Few enough entries but individually long ids (historical readable ones
        # like feel_...) still get truncated
        return None, (f"from 拼起来 {len(joined)} 字符，超过底层 {_TRIGGERED_BY_LIMIT} 上限，"
                      "会被静默截断——减少条数或拆开存。")
    return ids, ""


async def grow_event(items: list, direction_of_fit: str = "", bound=None,
                     evidential: str = "", internally_generated: bool = False,
                     weight=None, from_ids=None, test_data: bool = False) -> str:
    if not isinstance(items, list) or not items:
        return 'kind="event" 需要 items=[{room, text, when?}, ...]，至少一条。'
    v2, v2_err = _check_v2("event", direction_of_fit, bound, evidential,
                           internally_generated, weight)
    if v2_err:
        return v2_err
    telic = v2["direction_of_fit"] == "telic"
    # from is optional for an event (a want should carry a from where possible,
    # but it is not enforced — otherwise it becomes something invented out of
    # nothing).
    # If passed, it is checked for existence and written into each entry's
    # from.
    from_ids, from_err = _normalize_from(from_ids)
    if from_err:
        return from_err
    if from_ids:
        missing = []
        for fid in from_ids:
            if not await rt.bucket_mgr.get_including_archive(fid):
                missing.append(fid)
        if missing:
            return f"from 里这些 id 不存在：{', '.join(missing)}。"

    # --- Validation first: if any item is invalid, reject everything and create
    # no bucket at all ---
    cleaned: list[dict] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return f"items[{idx}] 必须是对象 {{room, text, v, a, when?}}，收到：{type(item).__name__}"
        room = str(item.get("room") or "").strip()
        text = str(item.get("text") or "")  # stored verbatim: never strip the body
        when = str(item.get("when") or "").strip()
        room_err = check_room(room, "event")
        if room_err:
            return f"items[{idx}]: {room_err}"
        if not text.strip():
            return f"items[{idx}]: text 不能为空。"
        # Something wanted accepts one more legal form of when: a duration marker
        # (one that carries a magnitude).
        # How the three kinds are read lives in core/profile.py._want_clock; this
        # only decides whether the value can be stored.
        if when:
            _when_ok = bool(_WHEN_RE.match(when))
            if not _when_ok and telic and _WANT_DURATION_RE.match(when):
                _when_ok = True
            if not _when_ok:
                if telic:
                    return (f"items[{idx}]: when 格式无效：{when}。三种填法：有期限写 "
                            "YYYY-MM-DD；有量级写时长记号（3w=3周/10d=10天/2m=2个月/1y=1年）；"
                            "等触发就不填 when，条件写在正文里。")
                return f"items[{idx}]: when 格式无效：{when}。用 YYYY-MM-DD（可带时间），不填＝存入时间。"
        # You live things only in the past. A lived event set in the future is either
        # something wanted (telic) or something imagined (internally_generated).
        if (room == "EVENT/SELF" and when and not telic
                and not v2["internally_generated"] and _WHEN_RE.match(when)
                and _in_the_future(when)):
            return (f"items[{idx}]: 亲历只能在过去——{when} 还没到。想让它发生就加 "
                    'direction_of_fit="telic"；是梦或想象就加 internally_generated=true；'
                    "是听别人说的就放 EVENT/WORLD。")
        size_err = check_content_size(text)
        if size_err:
            return f"items[{idx}]: {size_err}"
        # --- v/a is mandatory: an event's emotional coordinates are set by me
        # too, with no "the model will backfill it" back door — in the decay
        # formula valence determines how fast something is forgotten, and an
        # emotion a model guessed must not decide what I forget.
        try:
            iv = float(item.get("v"))
            ia = float(item.get("a"))
        except (TypeError, ValueError):
            return (f"items[{idx}]: v/a 必填——这条事件当时给我的感觉，自己打（0~1），"
                    "不外包给模型。v=效价(0难受~1开心) a=唤醒(0平静~1强烈)。")
        if not (0 <= iv <= 1 and 0 <= ia <= 1):
            return f"items[{idx}]: v/a 必须在 0~1 之间（收到 v={iv}, a={ia}）。"
        # --- importance / meaning: **retired, and rejected on the spot** ---
        # 🔴 Rejecting rather than silently ignoring: if they were silently
        #    ignored I would keep filling them in, and what I filled in would drop
        #    straight into the void without my ever knowing — a hundred times
        #    harder to track down than an error.
        retired = _retired_item_fields(item)
        if retired:
            return f"items[{idx}]: {retired}"
        cleaned.append({"room": room, "text": text, "when": when,
                        "v": iv, "a": ia})

    # --- Straight to create, one by one: no search, no judge, no merging ---
    batch_id = f"g_{uuid.uuid4().hex[:12]}"
    ph = _placeholder_meta()
    results: list[str] = []
    pairs: list[tuple[str, str, str]] = []
    # Identical-text deduplication: storing a body that matches word for word
    # returns the original id instead of creating a new bucket.
    # Duplicates that are worded differently are not blocked — those really are
    # two records, and once seen and thought about they can be handled with trace.
    # ⚠️ Query the whole batch once through the parse cache (find_exact_content
    # scans the entire library per item, roughly 3s each over a bind mount, which
    # is what once dragged a batch of five out to 16 seconds).
    existing_by_content: dict[str, str] = {}
    try:
        for _b in await rt.bucket_mgr.list_all(include_archive=False):
            _m = _b.get("metadata", {}) or {}
            if not _m.get("deleted_at"):
                existing_by_content.setdefault(str(_b.get("content") or ""), str(_m.get("id") or ""))
    except Exception:
        pass

    for item in cleaned:
        dup_id = existing_by_content.get(item["text"])
        if dup_id:
            results.append(f"♻️{dup_id} 已存过（同文，未重建）")
            continue
        bucket_id = await rt.bucket_mgr.create(
            content=item["text"],
            tags=ph["tags"],
            importance=ph["importance"],   # neutral placeholder; importance is retired and no longer set by me
            domain=ph["domain"],
            valence=item["v"],
            arousal=item["a"],
            name=None,
            source_tool="grow",
            grow_batch_id=batch_id,
            room=item["room"],
            when=item["when"],
            from_ids=",".join(from_ids) if from_ids else "",
            test_data=test_data,
            **v2,
        )
        results.append(f"📝{bucket_id} {item['room']}")
        pairs.append((bucket_id, item["text"], "event"))
        existing_by_content.setdefault(item["text"], bucket_id)

    # --- Metadata comes later: tagging / gist / naming run in the background,
    # and a failure only leaves a warning ---
    asyncio.create_task(_backfill_batch(pairs))

    dup_n = sum(1 for r in results if r.startswith("♻️"))
    head = f"{len(pairs)}条 event 已落盘 batch:{batch_id}"
    if dup_n:
        head += f"（另 {dup_n} 条同文已存过，未重建）"
    if telic:
        head += " [telic]"
    out = head + "（标签/摘要后台回填中，几十秒内可检索）\n" + "\n".join(results)

    # Overlong entries get a single remark; **whether and how to split is my
    # judgement, made on the spot**.
    # The reason: several things crammed into one entry means each topic gets only
    # a word or two in tags and none of them stands out, and the scene anchors mix
    # as well (the imagery of several separate things stirred together). There is
    # a ready-made counter-example in the library with four things in one entry.
    long_ones = [(bid, len(txt)) for bid, txt, _ in pairs if len(txt) >= _LONG_HINT]
    for bid, n in long_ones:
        out += (f"\n📏 {bid} 有 {n} 字——真是好几件事就分成几条重存"
                f"（正文在你手里，逐字贴过去，别改字），一件事就别管这条提示。")
    return out


# ------------------------------------------------------------
# kind="big" —— a big event / a **period**: one sentence laid over a stretch of time
# ------------------------------------------------------------

# ⚰️ `grow_big` was deleted along with the `kind="big"` entry point.
#    It was only a wrapper around fold's core (`_F.save_gist`) — naming a period
#    had two entry points, and two entry points sooner or later tell two
#    different stories. The single remaining path is fold(when="起..止").



# ------------------------------------------------------------
# kind="mind"
# ------------------------------------------------------------

async def grow_mind(room: str, text: str, from_ids, v, a,
                    direction_of_fit: str = "", bound=None, evidential: str = "",
                    internally_generated: bool = False, weight=None,
                    importance=None, meaning: str = "",
                    test_data: bool = False) -> str:
    room = str(room or "").strip()
    text = str(text or "")  # stored verbatim: never strip the body
    room_err = check_room(room, "mind")
    if room_err:
        return room_err
    if not text.strip():
        return "text 不能为空。"
    size_err = check_content_size(text)
    if size_err:
        return size_err
    v2, v2_err = _check_v2("mind", direction_of_fit, bound, evidential,
                           internally_generated, weight)
    if v2_err:
        return v2_err
    # importance / meaning are retired — rejected on the spot, never silently
    # swallowed (the reasoning is in _RETIRED_MSG).
    # The parameters are kept so that this human-readable message can be
    # produced; delete them and the caller gets pydantic's "unexpected keyword",
    # which shows neither what happened nor what to write instead.
    retired = _retired_fields_msg(
        *[n for n, val in (("importance", importance), ("meaning", meaning))
          if val not in (None, "")])
    if retired:
        return retired

    # --- from is mandatory, every single one of them: a piece of self-knowledge
    # with no provenance reads exactly like one that was invented ---
    from_ids, from_err = _normalize_from(from_ids)
    if from_err:
        return from_err
    if not from_ids:
        return ("from 必填：这条认知是从哪几条记忆看出来的？填真 bucket_id 列表。"
                "确实凭空想的，就在正文里老实标「凭空想的」，并把来源指到相关的事件上。")
    missing = []
    for fid in from_ids:
        found = await rt.bucket_mgr.get_including_archive(fid)
        if not found:
            missing.append(fid)
    if missing:
        return f"from 里这些 id 不存在：{', '.join(missing)}。填 grow(kind=\"event\") 返回的真 id。"

    # --- v/a is mandatory, set by the caller, never outsourced to a model ---
    try:
        v = float(v)
        a = float(a)
    except (TypeError, ValueError):
        return "v/a 必填：MIND 的情绪坐标是你自己打的（0~1），不外包给模型。"
    if not (0 <= v <= 1 and 0 <= a <= 1):
        return f"v/a 必须在 0~1 之间（收到 v={v}, a={a}；没传会是 -1）。MIND 的坐标你自己打。"

    ph = _placeholder_meta()
    bucket_id = await rt.bucket_mgr.create(
        content=text,
        tags=ph["tags"],
        importance=ph["importance"],   # neutral placeholder; importance is retired
        domain=ph["domain"],
        valence=v,
        arousal=a,
        name=None,
        from_ids=",".join(from_ids),
        source_tool="grow",
        room=room,
        test_data=test_data,
        **v2,
    )
    try:
        await rt.bucket_mgr.touch_many(from_ids)  # distilling a thought = remembering its sources
    except Exception:
        pass

    # A mind backfill only fills in aliases/gist/name (no scene extraction, and
    # v/a is never touched)
    asyncio.create_task(_backfill_batch([(bucket_id, text, "mind")]))

    head = f"🧠mind→{bucket_id} {room} ←{{{','.join(from_ids)}}} V{v:.2f}/A{a:.2f}"
    if v2["direction_of_fit"] == "telic":
        head += " [telic]"
    return head + "（标签/摘要后台回填中）"
