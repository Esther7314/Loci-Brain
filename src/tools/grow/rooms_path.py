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
  to the record with that id (or completed from Loci's registered line orders or the
  host's one container, or refused),
  the registry refuses withdrawn and deleted sources, and the receipt says when the same
  delivery is already on another memory. fold, regrow and trace take sources through here
  too
- Validation comes first: if any single item is invalid the whole call errors and
  no bucket is created
- Before the return is handed back it says which old views the new bodies run into
  (回望) and, for an event that happened, asks about a scene the last two weeks keep
  coming back to (场景常来) — tools/_write_returns.py, from the listing taken before the
  write; a vector backend that is down or slow only drops the meaning hits

What this file deliberately does not do:
- No merging (fixed by the spec: every item in items becomes its own bucket)
- room is never generated or modified by a model (tools/_rooms.py validates; the
  caller decides)
- A failed background backfill only logger.warning()s — no rollback, no retrying
  to death

Where the parts live: the arguments every write checks (when, retired fields, the v2
arguments, cue, hold) in _checks.py; name cards in _cards.py; `from` and `sources` in
_sources_check.py; what the backfill fills and how it decides in _backfill.py. This file
keeps the two write paths, the backfill runner they start, and the startup sweep; every
name the rest of Loci reaches through tools/grow/rooms_path is importable from here.

Exports: grow_event(items, direction_of_fit, bound, evidential, internally_generated,
                    weight, from_ids, test_data, cue, exception_of, hold, sources) -> str
         grow_mind(room, text, from_ids, v, a, direction_of_fit, bound, evidential,
                   internally_generated, weight, test_data, cue, card_of, sources,
                   when) -> str
         clock_when(when) · backfill_sweep(before)
         check_card(card_of, room, exclude) · card_room_rule(name, room) · live_card(name)
         check_sources(sources, prov, exclude)
========================================
"""

import asyncio
import uuid

from core import _holds as _H
from core import _reconsolidation as _R
from core.dehydrator import backfill_kinds
from .. import _runtime as rt
from .._common import check_content_size, read_scope
from .._write_returns import noticed
from core._rooms import check_room, is_mind_room
from core import _sources as _src
from utils import prov_targets

from ._backfill import (_ask_backfill, _backfill_context, _backfill_updates, _current_meta,
                        _placeholder_meta, _possibly_same, _record_kinds)
from ._cards import card_room_rule, check_card, live_card  # re-exported: see Exports
from ._checks import (_WANT_DURATION_RE, _WHEN_RE, _check_hold, _check_v2, _hold_receipt,
                      _in_the_future, _retired_fields_msg, _retired_item_fields, check_cue,
                      clock_when)
from ._sources_check import _normalize_from, _same_entry, check_sources

# A body longer than this earns a line in the response saying "this looks like
# more than one thing".
# The chosen behaviour: **point it out and let me decide whether and how to
# split** — never split automatically.
# (Splitting automatically would make one grow call suddenly produce several
#  extra ids, and which one `from` should point at would have to be reconsidered;
#  besides, "when in doubt, do not split" is the tone of this whole thing.)
_LONG_HINT = 600


# ------------------------------------------------------------
# Running the backfill
# ------------------------------------------------------------
# What a backfill fills and how it decides is _backfill.py's; this runs it. grow_event,
# grow_mind and backfill_sweep start _backfill_batch by this module's name for it, and
# core/_fold.save_gist imports it from here when it runs, so replacing it here (tests do)
# replaces it for all of them.


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
    if meta is None:
        return
    mind = kind == "mind" or is_mind_room(meta.get("room"))
    kinds = backfill_kinds()
    answer, came_back_empty = await _ask_backfill(
        bucket_id, text, _backfill_context(meta, mind), kinds)
    if answer is not None and answer.problems:
        rt.logger.warning(
            f"backfill {bucket_id}: 回填答案里这几块形状不对，没用上：{', '.join(answer.problems)}"
            + ("（人名表这次一个字没写）" if "subjects" in answer.problems else ""))
    # The "possibly the same thing" hint is an event's only (_backfill._possibly_same).
    similar = await _possibly_same(bucket_id, text) if kind == "event" else []

    try:
        written = await rt.bucket_mgr.update(
            bucket_id, revise=lambda now: _backfill_updates(
                now, answer, came_back_empty, text, mind=mind, similar=similar))
        if not written:
            rt.logger.warning(f"backfill {bucket_id} 没写上（读不到或被拒），下一轮再补")
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
    # A backfill write updates its entry in the parse cache in place; this read only
    # pays when the cache was dropped meanwhile (an external edit, an archive), and then
    # pays it here in the background rather than in the next waking screen
    try:
        await rt.bucket_mgr.list_all()
    except Exception:
        pass


async def backfill_sweep(before: str = "") -> int:
    """Self-healing at startup: a backfill in flight under a bare
    asyncio.create_task is lost across a restart, leaving buckets that have only
    a body and placeholder metadata. On boot they are found again and refilled.

    No persistent queue is built — **the scan is the queue**: "room has a value
    (i.e. it was stored by the new path) and summary is missing" is a reliable
    marker of unfinished work, it disappears by itself once the backfill
    succeeds, and it is naturally idempotent.
    Older grow buckets have no room, so they are never swept by mistake.

    The criterion does not look at source_tool: migrated older buckets
    (source_tool=hold/import...) need repairing too, and limiting it to grow/regrow
    would leave them unrepaired forever. "room has a value but summary is missing"
    is a complete criterion on its own; where the bucket came from is irrelevant.

    `before` (a stored `created` stamp, the moment the sweep was started): only entries
    written before it are taken. The sweep starts on the first grow after a restart, in
    the background while that grow writes, and what this run writes has its own backfill
    in flight — taken again here it would be backfilled twice.
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
        if before and str(meta.get("created") or "") >= before:
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
            when, _clock = clock_when(when)
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
    dreamt: set[str] = set()
    # Identical-text deduplication: storing a body that matches word for word
    # returns the original id instead of creating a new bucket.
    # Duplicates that are worded differently are not blocked — those really are
    # two records, and once seen and thought about they can be handled with trace.
    # ⚠️ Query the whole batch once through the parse cache (find_exact_content
    # scans the entire library per item, roughly 3s each over a bind mount, which
    # is what once dragged a batch of five out to 16 seconds).
    # Under a read scope an entry the request may not read is not a duplicate it can be
    # told about: its id would be named, so the write goes ahead.
    # The same listing is the library the return's look-back and scene question read
    # (tools/_write_returns.py): taken before the write, while the parse cache is warm.
    # A stored body is a duplicate only when nothing this call brings would be lost by
    # pointing at it (_same_entry): live and current, wanted the same way, still open,
    # on the same day, and already carrying every source, line and cue of this call.
    existing_by_content: dict[str, list[dict]] = {}
    library: list | None = None
    _view = await read_scope()
    try:
        library = await rt.bucket_mgr.list_all(include_archive=False)
        for _b in library:
            _m = _b.get("metadata", {}) or {}
            if not _m.get("deleted_at") and (_view is None or _view.permits(_m)):
                existing_by_content.setdefault(str(_b.get("content") or ""), []).append(_m)
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
        dup_id = None if hold_target else next(
            (str(m.get("id") or "") for m in existing_by_content.get(item["text"], [])
             if _same_entry(m, item, telic=telic, prov=prov, records=source_records,
                            cue=cue_v)), None)
        if dup_id:
            results.append(f"♻️{dup_id} 已存过（同文，未重建）")
            continue
        # Retelling a dream, or grown from one, is marked without being asked (core/_dream).
        from core._dream import from_a_dream
        ig = v2["internally_generated"] or await from_a_dream(item["text"], prov)
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
            **{**v2, "internally_generated": ig},
            **extra,
        )
        results.append(f"📝{bucket_id} {item['room']}")
        pairs.append((bucket_id, item["text"], "event"))
        if ig:
            dreamt.add(bucket_id)
        existing_by_content.setdefault(item["text"], []).append({
            "id": bucket_id, "room": item["room"], "when": item["when"],
            "direction_of_fit": v2["direction_of_fit"], "cue": cue_v,
            _src.SOURCES_FIELD: source_records, "prov": prov or []})
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

    # Old views these bodies run into, and a scene that keeps coming back. Only an event
    # that happened earns the scene question: a want, a dream or a hold is not the scene
    # occurring again.
    if pairs:
        happened = not telic and not hold_target
        notes = await noticed(library, [(bid, txt) for bid, txt, _ in pairs],
                              skip=_R.lineage(prov_targets(prov or []), library or []),
                              scene_texts=[txt for bid, txt, _ in pairs
                                           if bid not in dreamt] if happened else [])
        out += "".join("\n" + line for line in notes)
    return out


# ------------------------------------------------------------
# kind="mind"
# ------------------------------------------------------------

async def grow_mind(room: str, text: str, from_ids, v, a,
                    direction_of_fit: str = "", bound=None, evidential: str = "",
                    internally_generated: bool = False, weight=None,
                    importance=None, meaning: str = "",
                    test_data: bool = False, cue=None, card_of: str = "",
                    sources=None, when: str = "") -> str:
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
    # `when` on a thought: only a wanted one has a day (its deadline, a length, a clock
    # time), read like an event's. A thought that is not wanted has no day of its own;
    # refused rather than dropped, so the caller knows where the date went.
    when = str(when or "").strip()
    if when:
        if v2["direction_of_fit"] != "telic":
            return ('想法没有「发生在哪天」：when 只给想让它发生的（direction_of_fit="telic"）。'
                    '这条想法跟某天的事有关，就把那天的事存成 event（带 when），从它长出这条。')
        when, _clock = clock_when(when)
        if not (_WHEN_RE.match(when) or _WANT_DURATION_RE.match(when)):
            return (f"when 格式无效：{when}。有期限写 YYYY-MM-DD（可带钟点）；"
                    "有量级写时长记号（3w/10d/2m/1y）；等某件事发生就不填 when，写 cue。")
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

    # The library as it stood before this write, for the return's look-back: the old
    # views the new body may run into, without the new entry itself.
    try:
        library = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:  # noqa: BLE001 - without it the return only skips the look-back
        rt.logger.warning(f"grow mind: library not listed, no look-back this time: {e}")
        library = None

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
        when=when,
        **v2,
    )
    try:
        await rt.bucket_mgr.touch_many(source_ids, road="grow")  # distilling a thought = remembering its sources
    except Exception as e:  # noqa: BLE001 - the thought is written; a failed touch only logs
        rt.logger.warning(f"grow mind: touching its sources failed: {e}")

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
    if card_note:
        out += "\n" + card_note
    # Old views this thought runs into; the ones it stands on are its own lineage.
    notes = await noticed(library, [(bucket_id, text)],
                          skip=_R.lineage(source_ids, library or []))
    return out + "".join("\n" + line for line in notes)
