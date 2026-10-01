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
  tagging, gist and naming go to background backfill: one side-model call that only
  fills blanks — a slot the caller filled is never changed, every slot it fills is
  listed in `backfilled` — and also reads bound, a date, dreamt/imagined, evidential,
  cue phrasings and "looks like a promise" off the sentence (the date arithmetic is
  core/_dates.py's), and may add a name or a missing kind to the names table
- mind: its own bucket + from (structure copied from feel), and v/a must
  be supplied by the caller
- event's v/a became mandatory too; background backfill **never touches any
  bucket's v/a**; importance/meaning are passed by the caller (optional);
  tags are scene anchors, while broadenings go into aliases and feed bm25 only
- direction_of_fit / bound / evidential / internally_generated / weight are
  validated here and written by create() itself, in the same write as the body
- With direction_of_fit="telic", when accepts one more form: a duration marker (3w/10d/2m/1y)
  which, together with absolute dates, makes up a want's "three kinds of clock";
  the third is waiting for something to happen (when is left empty and the event
  goes in `cue`). How the three are read belongs to `core/profile._want_clock`;
  this file only validates that they can be stored
- cue ({condition, phrasings}) may ride on any entry; writing one is the
  declaration that it waits on something, so a cue without a condition is refused.
  A telic entry with neither when nor cue is a plain want, and is accepted
- A hold (core/_holds.py) is a one-item telic event with exception_of + hold:
  checked here (the target live and current, the level known, when a day or a
  closed span), bound defaulting to the AI, review_after set from the host's
  config for a defer without a date
- card_of makes a mind the card of a name (check_card): the name goes through the
  names table, a person's card sits in MIND/TRAITS and a thing's in MIND/VIEWS when
  the table knows which it is, and a name has one live card at a time. trace reads
  cards through here too
- sources (the host's material an entry was formed from, core/_sources.py) go through
  check_sources: each record gets a quoted prov line, a bare host id in `from` is linked
  to the record with that id, the registry refuses withdrawn and deleted sources, and the
  receipt says when the same delivery is already on another memory. fold, regrow and
  trace take sources through here too
- Validation comes first: if any single item is invalid the whole call errors and
  no bucket is created

What this file deliberately does not do:
- No merging (fixed by the spec: every item in items becomes its own bucket)
- room is never generated or modified by a model (tools/_rooms.py validates; the
  caller decides)
- A failed background backfill only logger.warning()s — no rollback, no retrying
  to death

Exports: grow_event(items, direction_of_fit, bound, evidential, internally_generated,
                    weight, from_ids, test_data, cue, exception_of, hold, sources) -> str
         grow_mind(room, text, from_ids, v, a, direction_of_fit, bound, evidential,
                   internally_generated, weight, test_data, cue, card_of, sources) -> str
         check_card(card_of, room, exclude) · card_room_rule(name, room) · live_card(name)
         check_sources(sources, prov, exclude)
========================================
"""

import asyncio
import uuid
from datetime import date, timedelta

from core import _dates
from core import _fold as _F       # a big event = fold's way of circling time
from core import _holds as _H
from core.dehydrator import (BACKFILL_MAX_TOKENS, BackfillAnswer, backfill_kinds,
                             backfill_request, parse_backfill)
from .. import _runtime as rt
from core._bigevent import SPAN_RE, first_line as _F_first_line
from .._common import check_content_size, read_scope, resolve_bucket_id, resolve_bucket_ids
from core._rooms import check_room, is_mind_room
from core import _sources as _src
from .. import _subjects as _S
from .._subjects import normalize_bound, normalize_subjects
from utils import (PROV_MAX_LINES, PROV_TARGET_MAX, WAS_DERIVED_FROM, WAS_QUOTED_FROM,
                   is_bucket_id, is_telic, parse_bool, prov_targets)

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

# How much of the body a stand-in may quote: the opening line for a name, the opening
# characters for a summary.
_FALLBACK_MAX = 60


def _fallback_name(text: str) -> str:
    """A stand-in title cut from the body's first line.

    It quotes what the caller wrote instead of inventing a line. A bucket with no title
    is not neutral — it is called after the second it was born in (`2026-08-20
    01-05-33`), which is a row of digits carrying nothing, in a list read by eye.

    Returns "" for a body with nothing in it, and an empty name is never written: there
    is no text to quote, so there is nothing honest to put there.
    """
    return _F_first_line(text).strip()[:_FALLBACK_MAX].strip()


def _fallback_summary(text: str) -> str:
    """A stand-in gist: the start of the body, the caller's own text. Better than empty
    — a gist is the hook by which you know an entry exists, and without it that memory
    is invisible in the zoomed-out views. It is always written stamped, because from
    then on it is indistinguishable from a real one by looking at it."""
    return text[:_FALLBACK_MAX].strip()


# ------------------------------------------------------------
# The backfill: one side-model call, and it only fills blanks
# ------------------------------------------------------------
# Who fills which slot: the main model writes what cannot be read off the sentence
# (wanted or not, how it felt, the room, the cue's condition); the side model reads what
# can (core/dehydrator.BACKFILL_PROMPT); code works out what has a known origin (a date
# from the phrase the model copied, core/_dates.py). A slot the main model filled is never
# changed here, and every slot this fills is listed in `backfilled`, so the panel can show
# it and the owner or the main model can change it.
#
# The names table may be written in exactly two ways: a name it does not have is added
# with the kind the side model gave, and a name it has without a kind gets that kind. A
# kind already there is never changed (the owner fixes kinds on the panel), and when the
# names part of an answer is malformed the table is not touched at all — a different model
# returning a bad shape must not pollute it.

# A bucket is born named after the second it was written in (bucket_manager.create): a
# name like that is a blank, not a title.
_BIRTH_NAME_RE = _re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}$")
_PLACEHOLDER_DOMAIN = "未分类"
_YEARLY = "FREQ=YEARLY"
_BACKFILL_TEMPERATURE = 0.1


def _name_is_blank(meta: dict) -> bool:
    """No name yet: absent, the birth-second name, or a stamped stand-in."""
    name = str(meta.get("name") or "").strip()
    return (not name or bool(_BIRTH_NAME_RE.match(name))
            or meta.get("name_source") == _SOURCE_FALLBACK)


def _summary_is_blank(meta: dict) -> bool:
    return (not str(meta.get("summary") or "").strip()
            or meta.get("summary_source") == _SOURCE_FALLBACK)


async def _current_meta(bucket_id: str) -> dict:
    """The entry's frontmatter as it is now. Unreadable reads as a fresh entry (nothing
    filled yet), which is what the backfill finds right after a write."""
    try:
        cur = await rt.bucket_mgr.get(bucket_id)
    except Exception as e:
        rt.logger.warning(f"backfill 读不到 {bucket_id} 现在的样子，按刚存下的算: {e}")
        return {}
    return dict((cur or {}).get("metadata") or {})


def _backfill_context(meta: dict, mind: bool) -> dict:
    """What the side model is told about the entry: its room, whether it is wanted, the
    day it was written, what it waits on, and the slots already filled, so it does not
    fill them again (code would not take them anyway)."""
    cue = meta.get("cue") if isinstance(meta.get("cue"), dict) else {}
    existing = {
        "name": "" if _name_is_blank(meta) else meta.get("name"),
        "summary": "" if _summary_is_blank(meta) else meta.get("summary"),
        "subjects": list(meta.get("subjects") or []),
        "bound": list(meta.get("bound") or []),
        "when": str(meta.get("when") or ""),
        "evidential": str(meta.get("evidential") or ""),
        "internally_generated": "true" if meta.get("internally_generated") else "",
        "cue_phrasings": list(cue.get("phrasings") or []),
    }
    return {"room": str(meta.get("room") or ("MIND" if mind else "EVENT")),
            "telic": is_telic(meta),
            "created_day": _dates.local_day(meta.get("created")),
            "cue_condition": str(cue.get("condition") or ""),
            "existing": existing}


async def _ask_backfill(bucket_id: str, text: str, context: dict,
                        kinds) -> tuple[BackfillAnswer | None, bool]:
    """One side-model call; an answer with nothing usable in it is asked once more.

    Returns (answer, came_back_empty):
      (answer, False)  the model answered
      (None, False)    it never answered — no channel, or the call raised. The record of
                       that is "not done yet": no stand-in, so the sweep comes back.
      (None, True)     it answered twice with nothing usable (empty, not JSON, or no
                       part holding up): stand-ins quoted from the body are used.
    """
    chat = getattr(rt.dehydrator, "_chat", None)
    if not callable(chat):
        return None, False
    system, user = backfill_request(text, context, kinds)
    for attempt in range(2):
        try:
            raw = await chat(system, user, max_tokens=BACKFILL_MAX_TOKENS,
                             temperature=_BACKFILL_TEMPERATURE)
        except Exception as e:
            rt.logger.warning(f"backfill 回填调用失败 {bucket_id}（正文已落盘）: {e}")
            return None, False
        answer = parse_backfill(raw, text, kinds)
        if answer is not None:
            return answer, False
        rt.logger.warning(f"backfill {bucket_id} 回填答案是空的或读不懂，重试一次" if attempt == 0
                          else f"backfill {bucket_id} 两次都没有能用的答案，名字和摘要用正文开头顶上")
    return None, True


def _time_fill(ans: BackfillAnswer, meta: dict, *, mind: bool, telic: bool,
               today) -> dict:
    """`when` (and `recurrence`) from the copied time phrase, read against the day the
    memory was written. Only into an empty `when`; a phrase core/_dates cannot read falls
    back to the model's own date only when that is a real YYYY-MM-DD. A wanted thing's date
    is not before that day and a lived event's is not after it — either would be a misread
    — and a yearly date (a birthday) is this year's occurrence or the date as stated."""
    if today is None or (mind and not telic):
        return {}
    resolved = (_dates.resolve_phrase(ans.time_phrase, today, yearly=ans.time_yearly)
                if ans.time_phrase else None)
    if resolved is None and ans.time_absolute:
        resolved = _dates.ResolvedTime(date.fromisoformat(ans.time_absolute),
                                       yearly=ans.time_yearly)
    if resolved is None:
        return {}
    when = str(meta.get("when") or "").strip()
    if resolved.yearly:
        if meta.get("recurrence"):
            return {}
        if not when:
            return {"when": resolved.stamp(), "recurrence": _YEARLY}
        stored = _re.match(r"^\d{4}-(\d{2})-(\d{2})", when)
        same_day = stored and (int(stored.group(1)), int(stored.group(2))) == \
            (resolved.day.month, resolved.day.day)
        return {"recurrence": _YEARLY} if same_day else {}
    if when or (telic and resolved.day < today) or (not telic and resolved.day > today):
        return {}
    return {"when": resolved.stamp()}


def _fills_from(ans: BackfillAnswer, meta: dict, *, mind: bool) -> dict:
    """The update() keywords for every blank this answer can fill, name and summary
    aside. Never a slot that already holds something; subjects only gain names."""
    telic = is_telic(meta)
    out: dict = {}
    if ans.aliases and not meta.get("aliases"):
        # Broadenings feed bm25 only (bm25_index.build consumes them); they never
        # appear on the tag line a human reads
        out["aliases"] = ans.aliases
    if ans.subjects:
        # Subjects (who or what), through the names table; kept as their own field — not
        # in tags (that would break the literal-string guarantee) and not in aliases (who
        # a memory is about must not move BM25 relevance). The main model's stay.
        have = [str(s) for s in meta.get("subjects") or []]
        added = [n for n in normalize_subjects([n for n, _ in ans.subjects]) if n not in have]
        if added:
            out["subjects"] = have + added
    domain = [str(d) for d in meta.get("domain") or []]
    if ans.domain and (not domain or domain == [_PLACEHOLDER_DOMAIN]):
        # domain is used purely as a folder; retrieval does not consume it
        out["domain"] = ans.domain
    if telic and ans.bound and not meta.get("bound"):
        names, err = normalize_bound(ans.bound)
        if names and not err:
            out["bound"] = names
    out.update(_time_fill(ans, meta, mind=mind, telic=telic,
                          today=_dates.local_day(meta.get("created"))))
    if not mind and ans.internally_generated and not meta.get("internally_generated"):
        out["internally_generated"] = True
    if mind and ans.evidential and not meta.get("evidential"):
        out["evidential"] = ans.evidential
    cue = meta.get("cue") if isinstance(meta.get("cue"), dict) else {}
    if cue.get("condition") and not cue.get("phrasings") and ans.cue_phrasings:
        out["cue"] = {"condition": cue["condition"], "phrasings": ans.cue_phrasings}
    if not telic and ans.looks_like_promise and not meta.get("looks_like_promise"):
        # Telic is the main model's switch and stays as it is; this only leaves the mark
        # the read side turns into a question.
        out["looks_like_promise"] = True
    return out


def _backfilled_names(fills: dict) -> list[str]:
    """The slot names `backfilled` lists for these update() keywords."""
    names = []
    for k in fills:
        if k in ("name_source", "summary_source"):
            continue
        names.append("cue.phrasings" if k == "cue" else k)
    return names


def _record_kinds(bucket_id: str, pairs: list) -> None:
    """Write what the side model said each name is into the names table, in the two ways
    the backfill may: a name the table does not have is added with its kind, a name it
    has without a kind gets one. A kind already there, a name the table lists under
    several entries, and a name that is no subject at all (a pronoun, a word marked
    "not a person") are left alone."""
    for name, kind in pairs:
        if not kind or not _S.canonical(name) or len(_S.candidates(name)) > 1:
            continue
        rec = _S.record_of(name)
        if rec is not None and rec.instance_of:
            continue
        try:
            _S.set_kind(rec.name if rec is not None else name, kind)
        except (ValueError, OSError) as e:
            rt.logger.warning(f"backfill {bucket_id}: 「{name}」的种类没写进人名表: {e}")


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
    """Fill in one bucket's blanks in the background, from one side-model call: name,
    summary, tags, aliases, domain, subjects, and the slots read off the sentence.

    kind = "event" | "mind" | "big".
    🔴 v/a is never backfilled: an event's v/a is now set by the caller too —
    hand "what I felt at the time" to a model to guess and the memory stops being
    mine.
    mind extracts no scene (there are no photographs inside a piece of thinking),
    so its tags come out empty — that is normal and accepted.
    """
    meta = await _current_meta(bucket_id)
    mind = kind == "mind" or is_mind_room(meta.get("room"))
    kinds = backfill_kinds()
    answer, came_back_empty = await _ask_backfill(
        bucket_id, text, _backfill_context(meta, mind), kinds)

    update_kwargs: dict = {}
    # Every tag this backfill wants to add. They are only ever added: the list goes on
    # top of the bucket's current tags right before the write (see _merged_tags).
    tag_additions: list[str] = []
    if answer is not None:
        if answer.problems:
            rt.logger.warning(
                f"backfill {bucket_id}: 回填答案里这几块形状不对，没用上：{', '.join(answer.problems)}"
                + ("（人名表这次一个字没写）" if "subjects" in answer.problems else ""))
        if answer.tags and not mind:
            tag_additions += answer.tags
        update_kwargs.update(_fills_from(answer, meta, mind=mind))
    # --- Naming, and what happens when no name comes back ---
    # 🔴 Deliberately OUTSIDE the `if answer` block above: the case this exists for is
    #    "the model call did not succeed", and the commonest shape of that is no answer
    #    at all. Fold it back inside and the fallback stops firing in exactly the
    #    situation it was written for — silently, since a bucket named after its own
    #    birth-second looks like a bucket, not like a failure.
    # She settled this on 2026-08-20: fall back, **but stamp it**.
    if _name_is_blank(meta):
        if answer is not None and answer.name:
            update_kwargs["name"] = answer.name
            # The model named it, so any earlier stand-in is over. None deletes the field.
            update_kwargs["name_source"] = None
        else:
            fallback = _fallback_name(text)
            if fallback:
                update_kwargs["name"] = fallback
                update_kwargs["name_source"] = _SOURCE_FALLBACK
    # Same two-state rule for the summary — with one difference: a model that never
    # answered leaves it absent, the marker `backfill_sweep` finds this bucket by.
    if _summary_is_blank(meta):
        if answer is not None and answer.summary:
            update_kwargs["summary"] = answer.summary
            update_kwargs["summary_source"] = None
        elif came_back_empty or answer is not None:
            fallback = _fallback_summary(text)
            if fallback:
                update_kwargs["summary"] = fallback
                update_kwargs["summary_source"] = _SOURCE_FALLBACK

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

    filled = _backfilled_names(update_kwargs)
    if tag_additions:
        merged = await _merged_tags(bucket_id, tag_additions)
        if merged is not None:
            update_kwargs["tags"] = merged
            if answer is not None and answer.tags and not mind:
                filled.append("tags")
    if filled:
        have = [str(f) for f in meta.get("backfilled") or []]
        update_kwargs["backfilled"] = have + [f for f in filled if f not in have]
    if update_kwargs:
        try:
            await rt.bucket_mgr.update(bucket_id, **update_kwargs)
        except Exception as e:
            rt.logger.warning(f"backfill update 失败 {bucket_id}（正文已落盘）: {e}")
    if answer is not None and answer.subjects:
        _record_kinds(bucket_id, answer.subjects)


async def _backfill_batch(pairs: list[tuple[str, str, str]]) -> None:
    """Concurrent backfill (each entry in pairs = (bucket_id, text, kind)). Run
    serially, 5 items x one side-model call (about 14s each) would take over 70s.
    _chat is read-only and side-effect free, so the calls can run concurrently
    (the same precedent as the [LENTO PATCH] in grow_items), each update writes its
    own bucket under a per-bucket lock, and the names table is written under its
    own lock, so they do not collide."""
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


_CUE_KEYS = ("condition", "phrasings")
_CUE_CONDITION_MAX = 200


def check_cue(cue) -> tuple[dict | None, str]:
    """The `cue` argument -> {condition, phrasings} to store, None when none was given,
    or a refusal. Writing a cue is the declaration that the entry waits on something,
    so the condition (what it waits on) is what cannot be missing. phrasings belong to
    the backfill and are taken as given. trace reads cues through here too."""
    if cue is None or cue == "":
        return None, ""
    if isinstance(cue, str):
        cue = {"condition": cue}
    if not isinstance(cue, dict):
        return None, 'cue 写成 {"condition": "等的那件事，一句话"}。'
    extra = [k for k in cue if k not in _CUE_KEYS]
    if extra:
        return None, (f"cue 不认识 {', '.join(map(str, extra))}：只有 condition（等的那件事）"
                      "和 phrasings（它可能被怎么说出来，可以不填）。")
    condition = str(cue.get("condition") or "").strip()
    if not condition:
        return None, ('cue 要有 condition：等的是哪件事，一句话（例如 "考完试"）。'
                      "写了 cue 就是在等，等什么得说出来；不是在等就别写 cue。")
    if len(condition) > _CUE_CONDITION_MAX:
        return None, f"cue 的 condition 是一句话，最多 {_CUE_CONDITION_MAX} 字（收到 {len(condition)}）。"
    phrasings = cue.get("phrasings") or []
    if isinstance(phrasings, str):
        phrasings = [phrasings]
    if not isinstance(phrasings, list) or not all(isinstance(p, str) for p in phrasings):
        return None, "cue 的 phrasings 是几句话的列表。"
    return {"condition": condition, "phrasings": [p.strip() for p in phrasings if p.strip()]}, ""


async def _check_hold(exception_of, hold) -> tuple[str, str, str]:
    """The hold arguments -> (target id, level, refusal). Both empty is no hold. The
    target goes through the same short-id resolver as every write tool, and has to be
    a live, current entry that is not itself a hold."""
    target = str(exception_of or "").strip()
    level = str(hold or "").strip().lower()
    if not target and not level:
        return "", "", ""
    if not target:
        return "", "", "有 hold 就要有 exception_of：这张条子挂在哪条约定上，填它的 id。"
    if not level:
        return "", "", ('挂条子要说是哪种：hold="defer"（先别催 / 先别提，事还算数），'
                        'hold="avoid"（别碰，连想都别想）。')
    if level not in _H.HOLD_LEVELS:
        return "", "", (f'hold 只有两个值："defer"（先别催 / 先别提）/ "avoid"（别碰）'
                        f"，收到 {hold}。")
    target, id_err = await resolve_bucket_id(target)
    if id_err:
        return "", "", f"exception_of —— {id_err}"
    found = await rt.bucket_mgr.get_including_archive(target)
    if not found:
        return "", "", f"exception_of 指的 {target} 不存在。填那条约定的真 id。"
    meta = found.get("metadata") or {}
    if meta.get("deleted_at") or not rt.bucket_mgr.is_live(target):
        return "", "", (f"{target} 在归档区，条子挂不上去。"
                        f'先 trace(bucket_id="{target}", restore=True) 捞回来。')
    newer = str(meta.get("superseded_by") or "").strip()
    if newer:
        return "", "", f"{target} 已经有新版 {newer}——条子挂到新版上。"
    if _H.is_hold(meta):
        return "", "", (f"{target} 本身是一张条子。条子挂在约定上："
                        f"exception_of 填它挂着的 {meta.get('exception_of')}。")
    return target, level, ""


def _hold_receipt(bid: str, target: str, level: str, when: str, review_after: str) -> str:
    """One line saying how this hold will end, so the model hears it once at writing."""
    head = f"📎 条子 {bid} 挂在 {target} 上（{level}）"
    close = f'trace(bucket_id="{bid}", status="resolved")'
    if when:
        return f"{head}：{when} 过完自己放下；提前放下就 {close}。"
    if review_after:
        return f"{head}：没写日子，不会自己放下，{review_after} 是回头再看它的日子；放下就 {close}。"
    return f"{head}：不会自己放下，要放下就 {close}。"


# ------------------------------------------------------------
# Name cards: card_of
# ------------------------------------------------------------
# A card is one MIND entry filed as the card of a name: how I see that person, or that
# game, book, group. What the name is (person or thing) is the names table's call; the
# card only follows it, and says nothing when the table does not know yet.

_CARD_NAME_MAX = 64      # the store's cap on one name (bucket_manager._MAX_SUBJECT_CHARS)


def card_room_rule(name: str, room: str) -> tuple[str, str]:
    """Which room a card of `name` may sit in. Returns (refusal, note).

    A card is a mind. A person's card is MIND/TRAITS and a thing's MIND/VIEWS, once
    the names table says which the name is; while it does not, either MIND room is
    taken and the note says the table does not know yet — nothing is guessed from
    the card itself."""
    if not is_mind_room(room):
        return ("名字卡是一条 mind：人的卡放 MIND/TRAITS（这个人是什么样的），"
                "东西的卡放 MIND/VIEWS（我怎么看它）。"), ""
    person = _S.is_person(name)
    if person is None:
        return "", f"人名表里还不知道「{name}」是什么（人还是东西），先按 {room} 收下。"
    if person and room != "MIND/TRAITS":
        return f"「{name}」在人名表里是人，人的卡放 MIND/TRAITS。", ""
    if not person and room != "MIND/VIEWS":
        kind = _S.kind_of(name)
        what = f"是{kind}" if kind else "记着不是人"
        return f"「{name}」在人名表里{what}，东西的卡放 MIND/VIEWS（我怎么看它）。", ""
    return "", ""


async def live_card(name: str, exclude: str = "") -> str:
    """The id of `name`'s live card, or "". Live = in the active store and not replaced
    by a live newer version; a stored card_of is read through the table as it is now,
    so a card filed under a spelling that has since become an alias still counts."""
    want = name.lower()
    for b in await rt.bucket_mgr.list_all(include_archive=False):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        card = str(meta.get("card_of") or "").strip()
        if not card or bid == exclude or meta.get("deleted_at"):
            continue
        newer = str(meta.get("superseded_by") or "").strip()
        if newer and rt.bucket_mgr.is_live(newer):
            continue
        if (_S.name_key(card) or card).lower() == want:
            return bid
    return ""


async def check_card(card_of, room: str, exclude: str = "") -> tuple[str, str, str]:
    """The card_of argument -> (the name to store, a note for the reply, refusal). An
    empty argument is no card. `exclude` is the entry being changed, so trace can set
    the card an entry already is.

    The name is stored as the table's key for it (an alias spelling files under the
    entry it stands for). One live card per name: a second is refused, naming the one
    there is."""
    raw = str(card_of or "").strip()
    if not raw:
        return "", "", ""
    if "\n" in raw or len(raw) > _CARD_NAME_MAX:
        return "", "", f"card_of 是一个名字（最多 {_CARD_NAME_MAX} 字，不换行）。"
    name = _S.name_key(raw)
    if not name:
        return "", "", f"card_of 写名字，不写「{raw}」——卡是某个名字的卡。"
    owners = _S.candidates(raw)
    if len(owners) > 1 and name not in owners:
        return "", "", (f"「{raw}」在人名表里挂在好几个名字底下（{'、'.join(owners)}）"
                        "——card_of 写其中那一个的规范名。")
    room_err, note = card_room_rule(name, room)
    if room_err:
        return "", "", room_err
    existing = await live_card(name, exclude=exclude)
    if existing:
        return "", "", (f"「{name}」已经有名字卡了：{existing}。一个名字一张卡——"
                        f'改写它用 regrow(bucket_id="{existing}", ...)；真要换一张，'
                        f'先 trace(bucket_id="{existing}", card_of="") 把那张摘掉。')
    return name, note, ""


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

async def _check_quoted_source(target: str) -> str:
    """Whether a target from outside the library may be quoted as a source; returns the
    refusal, or "" to accept. Every write tool that takes `from` comes through
    _normalize_from, so this is the one place that decides it. A target is either a
    source's string form (`lento:home/private:U#m_0142`, or `…#m_0142..m_0160` for a run
    of lines; `#` marks it), which has to parse and must not be withdrawn or deleted in
    the registry (a run is read by its own identity, else its first line's), or the
    host's bare id for a line (`m_0931`), whose identity is not known yet: shape only —
    one token, short enough to store whole."""
    if _re.search(r"\s", target):
        return (f"from 里「{target[:40]}」不像 id（带空格）——填 bucket_id，"
                "或宿主那句话自己的 id（如 m_0931）。")
    if len(target) > PROV_TARGET_MAX:
        return (f"from 里 {target[:40]}… 有 {len(target)} 字符，超过 {PROV_TARGET_MAX} 上限"
                "——不截断，换成它的短 id。")
    if "#" in target:
        try:
            sid, _revision = _src.SourceId.parse(target)
        except _src.SourceRecordError as e:
            return (f"from 里「{target[:40]}」像来源的全称，但{e.zh}。全称写成 "
                    "system:instance/container#id（有版本号再加 @版本）。")
        registry = _registry()
        if registry is not None:
            state = registry.state_of(sid)
            if state in (_src.WITHDRAWN, _src.DELETED):
                word = "撤回" if state == _src.WITHDRAWN else "删除"
                return f"来源 {sid} 已经被{word}了，不能再拿它写记忆。本次什么都没写。"
    return ""


# ------------------------------------------------------------
# Outside material: `sources`
# ------------------------------------------------------------
# A record names one piece of the host's material (core/_sources.py). Each record on an
# entry has a wasQuotedFrom line naming it by its string form, so prov and sources say the
# same thing. A bare host id in `from` (m_0142) is that line before its record is known:
# when the same call carries a record with that id the line is linked to it; when none
# does, the line stays as it is and no record is made up. A full string form in `from`
# with no record of its own gets the record it spells out. Whether a source may be used at
# all is the registry's call (check_writable): withdrawn and deleted are refused, unreadable
# is taken with a note, and a grant set by the request layer limits a turn to its own.

def _registry():
    return getattr(rt.bucket_mgr, "sources", None)


def _same(stored, given: dict) -> bool:
    try:
        return isinstance(stored, dict) and _src.same_delivery(stored, given)
    except (KeyError, TypeError):
        return False


async def _already_recorded(records: list[dict], exclude: set) -> list[str]:
    """One hint per live, current memory that already carries the same delivery (same
    identity and fingerprint) as one of `records`. A hint only: two memories from one
    message are often two different things, so nothing is blocked. Under a read scope a
    memory the request may not read is not named."""
    if not records:
        return []
    view = await read_scope()
    hits: dict[str, str] = {}
    for b in await rt.bucket_mgr.list_all(include_archive=False):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or bid in exclude or meta.get("deleted_at") or meta.get("superseded_by"):
            continue
        if view is not None and not view.permits(meta):
            continue
        for stored in meta.get(_src.SOURCES_FIELD) or []:
            match = next((r for r in records if _same(stored, r)), None)
            if match is not None:
                hits.setdefault(bid, _src.record_string(match))
                break
    return [f"这条来源已经记过 {bid}，看一眼是不是同一件（{text}）。"
            for bid, text in hits.items()]


async def check_sources(raw, prov: list[dict] | None, exclude=(), *,
                        from_call: bool = True,
                        ) -> tuple[list[dict], list[dict] | None, list[str], str]:
    """The `sources` argument and the provenance lines `from` produced -> (records to
    store, provenance lines with every record's quoted line, notes for the receipt,
    refusal). `exclude` are the entries this write replaces or edits, left out of the
    already-recorded hint. Nothing given and nothing to link: (``[]``, prov, [], "").

    `from_call` says `prov` is this call's own `from`. False (trace appending to an
    entry) means it is what the entry already has: a string form there is not made into
    a record again, and a bare id that names several new records stays as it is."""
    try:
        records = _src.normalize_sources(_src.coerce_sources_arg(raw))
    except _src.SourceRecordError as e:
        return [], prov, [], (f"sources 不对：{e.zh}。每条写 {{system, instance, container, "
                              "id}，可选 through / revision / fingerprint / fingerprint_by / "
                              "span / use。")
    lines = list(prov or [])
    strings = [_src.record_string(r) for r in records]
    for i, line in enumerate(lines):
        if line["rel"] != WAS_QUOTED_FROM:
            continue
        target = line["target"]
        if "#" in target:
            if from_call and target not in strings:
                try:
                    sid, revision = _src.SourceId.parse(target)
                except _src.SourceRecordError as e:
                    return [], prov, [], f"from 里「{target[:40]}」像来源的全称，但{e.zh}。"
                records.append({"system": sid.system, "instance": sid.instance,
                                "container": sid.container, "id": sid.id,
                                **({"through": sid.through} if sid.through else {}),
                                "revision": revision, "fingerprint": None,
                                "fingerprint_by": None, "use": None})
                strings.append(target)
            continue
        named = list(dict.fromkeys(s for r, s in zip(records, strings) if r["id"] == target))
        if len(named) > 1 and from_call:
            return [], prov, [], (f"from 里的 {target} 对得上好几条来源（{'、'.join(named)}）"
                                  "——from 里写那一条的全称。")
        if len(named) == 1:
            lines[i] = {"rel": WAS_QUOTED_FROM, "target": named[0]}
    if not records:
        return [], prov, [], ""
    if len(records) > _src.SOURCES_MAX:
        return [], prov, [], (f"sources 最多 {_src.SOURCES_MAX} 条（收到 {len(records)} 条）"
                              "——拆开分别存。")
    registry = _registry()
    notes: list[str] = []
    if registry is not None:
        refusal, notes = registry.check_writable(records, _src.current_grant())
        if refusal:
            return [], prov, [], refusal
    lines += [{"rel": WAS_QUOTED_FROM, "target": s} for s in strings]
    lines = list({(ln["rel"], ln["target"]): ln for ln in lines}.values())
    if len(lines) > PROV_MAX_LINES:
        return [], prov, [], (f"from 加 sources 一共 {len(lines)} 条来源，超过 {PROV_MAX_LINES}"
                              "——一条记忆挂不了这么多来源，拆开分别存。")
    notes += await _already_recorded(records, set(exclude))
    return records, lines, notes, ""


async def _normalize_from(from_ids, missing_hint: str = "") -> tuple[list[dict] | None, str]:
    """Turn the `from` argument into provenance lines. Returns (lines, error
    message); lines=None means nothing was passed. grow, regrow and fold all take
    `from` through here, so this is the one place that decides what a source is:

    - a short handle resolves to the full id it names (the same resolver every
      write tool uses), so what lands on disk is always the full id;
    - a memory's id (12 hex, or a `feel_…` id) has to exist, archive included, and
      becomes a wasDerivedFrom line;
    - anything else is the host's own id for a line of the conversation (`m_0931`):
      it is not in the library, so nothing is looked up, and it becomes a
      wasQuotedFrom line once _check_quoted_source lets it through.

    More than PROV_MAX_LINES distinct sources is refused outright, never cut.
    `missing_hint` is appended to the "these ids do not exist" refusal."""
    if from_ids is None:
        return None, ""
    if isinstance(from_ids, str):
        from_ids = [s.strip() for s in from_ids.split(",") if s.strip()]
    if not isinstance(from_ids, list):
        return None, "from 要传 id 列表。"
    ids = list(dict.fromkeys(str(s).strip() for s in from_ids if str(s).strip()))
    if not ids:
        return None, ""
    if len(ids) > PROV_MAX_LINES:
        return None, (f"from 最多 {PROV_MAX_LINES} 条（收到 {len(ids)} 条）"
                      "——一条记忆挂不了这么多来源，拆开分别存。")
    ids, id_err = await resolve_bucket_ids(ids, "from")
    if id_err:
        return None, id_err
    lines: list[dict] = []
    missing: list[str] = []
    for fid in dict.fromkeys(ids):
        if is_bucket_id(fid):
            if not await rt.bucket_mgr.get_including_archive(fid):
                missing.append(fid)
            lines.append({"rel": WAS_DERIVED_FROM, "target": fid})
            continue
        quote_err = await _check_quoted_source(fid)
        if quote_err:
            return None, quote_err
        lines.append({"rel": WAS_QUOTED_FROM, "target": fid})
    if missing:
        return None, f"from 里这些 id 不存在：{', '.join(missing)}。{missing_hint}"
    return lines, ""


async def grow_event(items: list, direction_of_fit: str = "", bound=None,
                     evidential: str = "", internally_generated: bool = False,
                     weight=None, from_ids=None, test_data: bool = False,
                     cue=None, exception_of: str = "", hold: str = "",
                     sources=None) -> str:
    if not isinstance(items, list) or not items:
        return 'kind="event" 需要 items=[{room, text, when?}, ...]，至少一条。'
    cue_v, cue_err = check_cue(cue)
    if cue_err:
        return cue_err
    hold_target, hold_level, hold_err = await _check_hold(exception_of, hold)
    if hold_err:
        return hold_err
    if hold_target:
        # A hold is about what I do next: always wanted, and mine unless named otherwise.
        if len(items) != 1:
            return f"一张条子一条：items 只放这一条（收到 {len(items)} 条）。"
        if str(direction_of_fit or "").strip().lower() == "thetic":
            return '条子总是 telic（它管的是我接下来怎么做），direction_of_fit 不填或填 "telic"。'
        direction_of_fit = "telic"
        if not bound:
            bound = ["我"]
    v2, v2_err = _check_v2("event", direction_of_fit, bound, evidential,
                           internally_generated, weight)
    if v2_err:
        return v2_err
    telic = v2["direction_of_fit"] == "telic"
    # from is optional for an event (a want should carry a from where possible,
    # but it is not enforced — otherwise it becomes something invented out of
    # nothing).
    # If passed, it is checked (_normalize_from) and written into each entry's
    # prov.
    prov, from_err = await _normalize_from(from_ids)
    if from_err:
        return from_err
    # The host's material this came from, checked against the registry; every item of
    # the call carries the same records, as it carries the same prov.
    source_records, prov, source_notes, sources_err = await check_sources(sources, prov)
    if sources_err:
        return sources_err

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
        # A hold's when is the day it ends, or the span it covers (core/_holds.py).
        if when and hold_target:
            hold_when_err = _H.check_hold_when(when)
            if hold_when_err:
                return f"items[{idx}]: {hold_when_err}"
        elif when:
            _when_ok = bool(_WHEN_RE.match(when))
            if not _when_ok and telic and _WANT_DURATION_RE.match(when):
                _when_ok = True
            if not _when_ok:
                if telic:
                    return (f"items[{idx}]: when 格式无效：{when}。三种填法：有期限写 "
                            "YYYY-MM-DD；有量级写时长记号（3w=3周/10d=10天/2m=2个月/1y=1年）；"
                            "等某件事发生就不填 when，写 cue={\"condition\": \"等的那件事\"}。")
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
    # Under a read scope an entry the request may not read is not a duplicate it can be
    # told about: its id would be named, so the write goes ahead.
    existing_by_content: dict[str, str] = {}
    _view = await read_scope()
    try:
        for _b in await rt.bucket_mgr.list_all(include_archive=False):
            _m = _b.get("metadata", {}) or {}
            if not _m.get("deleted_at") and (_view is None or _view.permits(_m)):
                existing_by_content.setdefault(str(_b.get("content") or ""), str(_m.get("id") or ""))
    except Exception:
        pass

    # cue, sources, and the hold's three fields, in the same create() as the body.
    extra: dict = {"cue": cue_v, "sources": source_records}
    if hold_target:
        from core import _when as _w
        extra.update(exception_of=hold_target, hold=hold_level,
                     review_after=_H.review_after_for(hold_level, cleaned[0]["when"],
                                                      _w.now(), rt.config))
    hold_ids: list[str] = []
    for item in cleaned:
        # A hold is written even when its words match an older one: the same "not this
        # week" a month later is a new hold, not a duplicate.
        dup_id = None if hold_target else existing_by_content.get(item["text"])
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
            prov=prov,
            test_data=test_data,
            **v2,
            **extra,
        )
        results.append(f"📝{bucket_id} {item['room']}")
        pairs.append((bucket_id, item["text"], "event"))
        existing_by_content.setdefault(item["text"], bucket_id)
        if hold_target:
            hold_ids.append(bucket_id)

    # --- Metadata comes later: tagging / gist / naming run in the background,
    # and a failure only leaves a warning ---
    asyncio.create_task(_backfill_batch(pairs))

    dup_n = sum(1 for r in results if r.startswith("♻️"))
    head = f"{len(pairs)}条 event 已落盘 batch:{batch_id}"
    if dup_n:
        head += f"（另 {dup_n} 条同文已存过，未重建）"
    if telic:
        head += " [telic]"
    if cue_v:
        head += " [cue]"
    out = head + "（标签/摘要后台回填中，几十秒内可检索）\n" + "\n".join(results)
    for bid in hold_ids:
        out += "\n" + _hold_receipt(bid, hold_target, hold_level, cleaned[0]["when"],
                                    extra.get("review_after") or "")
    if pairs:
        out += "".join("\n" + note for note in source_notes)

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
                    test_data: bool = False, cue=None, card_of: str = "",
                    sources=None) -> str:
    room = str(room or "").strip()
    text = str(text or "")  # stored verbatim: never strip the body
    room_err = check_room(room, "mind")
    if room_err:
        return room_err
    card, card_note, card_err = await check_card(card_of, room)
    if card_err:
        return card_err
    if not text.strip():
        return "text 不能为空。"
    size_err = check_content_size(text)
    if size_err:
        return size_err
    v2, v2_err = _check_v2("mind", direction_of_fit, bound, evidential,
                           internally_generated, weight)
    if v2_err:
        return v2_err
    cue_v, cue_err = check_cue(cue)
    if cue_err:
        return cue_err
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
    prov, from_err = await _normalize_from(
        from_ids, missing_hint="填 grow(kind=\"event\") 返回的真 id。")
    if from_err:
        return from_err
    # A record in `sources` is a source as much as a line in `from` is: a thought formed
    # straight from what was said names that piece, and its quoted line counts.
    source_records, prov, source_notes, sources_err = await check_sources(sources, prov)
    if sources_err:
        return sources_err
    if not prov:
        return ("from 必填：这条认知是从哪几条记忆看出来的？填真 bucket_id 列表。"
                "确实凭空想的，就在正文里老实标「凭空想的」，并把来源指到相关的事件上。")
    source_ids = prov_targets(prov)

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
        prov=prov,
        source_tool="grow",
        room=room,
        test_data=test_data,
        cue=cue_v,
        card_of=card,
        sources=source_records,
        **v2,
    )
    try:
        await rt.bucket_mgr.touch_many(source_ids)  # distilling a thought = remembering its sources
    except Exception:
        pass

    # A mind backfill only fills in aliases/gist/name (no scene extraction, and
    # v/a is never touched)
    asyncio.create_task(_backfill_batch([(bucket_id, text, "mind")]))

    head = (f"🧠mind→{bucket_id} {room} ←{{{','.join(ln['target'] for ln in prov)}}}"
            f" V{v:.2f}/A{a:.2f}")
    if v2["direction_of_fit"] == "telic":
        head += " [telic]"
    if cue_v:
        head += " [cue]"
    if card:
        head += f" [名字卡:{card}]"
    out = head + "（标签/摘要后台回填中）"
    out += "".join("\n" + note for note in source_notes)
    return out + ("\n" + card_note if card_note else "")
