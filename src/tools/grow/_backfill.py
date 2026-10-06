"""
========================================
tools/grow/_backfill.py — what the background backfill fills, and how it decides
========================================

A grow write stores the body with placeholder metadata (_placeholder_meta) and hands the
id back at once; the backfill then reads the entry, asks the side model once, and fills
only the slots still blank. This file is that decision: what the side model is told, the
answer turned into update() keywords against the entry as it is on disk, the stamped
stand-ins when no answer comes, the "possibly the same thing" tag and its similarity
line, and the names table's two permitted writes. Running it (_backfill_one,
_backfill_batch, backfill_sweep) is tools/grow/rooms_path's.

Exports: _placeholder_meta() · _current_meta(bucket_id) · _backfill_context(meta, mind)
         _ask_backfill(bucket_id, text, context, kinds) · _backfill_updates(...)
         _possibly_same(bucket_id, text) · _DUP_COS_THRESHOLD
         _record_kinds(bucket_id, pairs)
========================================
"""

import re as _re
from datetime import date

from core import _dates
from core.dehydrator import (BACKFILL_MAX_TOKENS, BackfillAnswer, backfill_request,
                             parse_backfill)
from core import runtime as rt
from core._bigevent import first_line as _F_first_line
from core import names as _S
from core.names import normalize_bound, normalize_subjects
from utils import is_telic


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


async def _current_meta(bucket_id: str) -> dict | None:
    """The entry's frontmatter as it is now, for what the side model is told; None when
    it cannot be read. An unreadable entry is not a blank one: reading it as blank would
    let the answer overwrite everything on it, so that round is skipped."""
    try:
        cur = await rt.bucket_mgr.get(bucket_id)
    except Exception as e:
        rt.logger.warning(f"backfill 读不到 {bucket_id} 现在的样子，这轮不补: {e}")
        return None
    if not cur:
        rt.logger.warning(f"backfill 读不到 {bucket_id}（不在了或读坏了），这轮不补")
        return None
    return dict(cur.get("metadata") or {})


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


# The similarity line for "possibly the same thing". It is **the same number** as
# the elbow on the dashboard's similarity page (a full pairwise cosine scan of the
# library puts the elbow at 80). The panel's thresholds page reads it from here by this
# path (core/embedding_switch.thresholds).
_DUP_COS_THRESHOLD = 0.80


async def _possibly_same(bucket_id: str, text: str) -> list[str]:
    """The 「疑似同件:xx」 tag for a new event whose body sits at or above the line from an
    entry already in the library, or [] (no vectors, nothing close, or the lookup failed).

    Nothing is merged and nothing is blocked: similarity is checked once in the
    background, and the tag is for human eyes to settle later. The threshold errs high
    rather than low — fewer stickers is better than more. It queries the **vector
    cosine** directly: "is this the same thing" is a question about semantic distance,
    not about retrieval ranking. A new body running into an old view is not tagged here:
    the write's own return says it (core/_reconsolidation.py, before the return is
    handed back).
    """
    similar: list[str] = []
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
                    similar.append(f"疑似同件:{sid[:6]}")
                    break
    except Exception:
        pass
    return similar


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


def _backfill_updates(meta: dict, answer: BackfillAnswer | None, came_back_empty: bool,
                      text: str, *, mind: bool, similar: list[str]) -> dict:
    """The update() keywords for this answer against `meta`, the entry as it is on disk
    at the moment of writing (read under the bucket's lock, BucketManager `revise`). Every
    blank-or-not decision is made here, so a trace or a panel edit that landed while the
    side model was thinking is never overwritten nor listed in `backfilled`.

    🔴 `update(tags=...)` replaces the whole list. System tags (`__gist__`, `__档案事实__`,
       `__大event__`) are put on at creation, before backfill runs, and the model knows
       nothing about them — so every tag the backfill adds goes on top of the tags on disk.
       That includes a lone similarity hint (`similar`) on a round where the model returned
       no tags: a regrown entry is always close to the version it replaced, so that round
       is the common case, not the rare one.
    """
    out: dict = {}
    tag_additions: list[str] = []
    if answer is not None:
        if answer.tags and not mind:
            tag_additions += answer.tags
        out.update(_fills_from(answer, meta, mind=mind))
    # --- Naming, and what happens when no name comes back ---
    # 🔴 Deliberately OUTSIDE the `if answer` block above: the case this exists for is
    #    "the model call did not succeed", and the commonest shape of that is no answer
    #    at all. Fold it back inside and the fallback stops firing in exactly the
    #    situation it was written for — silently, since a bucket named after its own
    #    birth-second looks like a bucket, not like a failure.
    # She settled this on 2026-08-20: fall back, **but stamp it**.
    if _name_is_blank(meta):
        if answer is not None and answer.name:
            out["name"] = answer.name
            # The model named it, so any earlier stand-in is over. None deletes the field.
            out["name_source"] = None
        else:
            fallback = _fallback_name(text)
            if fallback:
                out["name"] = fallback
                out["name_source"] = _SOURCE_FALLBACK
    # Same two-state rule for the summary — with one difference: a model that never
    # answered leaves it absent, the marker `backfill_sweep` finds this bucket by.
    if _summary_is_blank(meta):
        if answer is not None and answer.summary:
            out["summary"] = answer.summary
            out["summary_source"] = None
        elif came_back_empty or answer is not None:
            fallback = _fallback_summary(text)
            if fallback:
                out["summary"] = fallback
                out["summary_source"] = _SOURCE_FALLBACK
    filled = _backfilled_names(out)
    tag_additions += similar
    if tag_additions:
        existing = [str(t) for t in (meta.get("tags") or [])]
        out["tags"] = list(dict.fromkeys(existing + tag_additions))
        if answer is not None and answer.tags and not mind:
            filled.append("tags")
    if filled:
        have = [str(f) for f in meta.get("backfilled") or []]
        out["backfilled"] = have + [f for f in filled if f not in have]
    return out
