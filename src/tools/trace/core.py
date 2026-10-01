"""
========================================
tools/trace/core.py — trace's main path (edit / delete / regenerate embedding)
========================================

trace is the single entry point for writing metadata, handling every bucket field
update and deletion. Whatever field the model passes is the field that changes;
-1 or an empty string means "leave it alone".

Key behaviour:
- delete=True -> the Markdown moves into archive/ and the rebuildable embedding
  is cleaned up
- hard_delete=True -> only clears test buckets explicitly marked test_data=True at
  creation time; a non-empty delete_reason must be supplied as well, and ordinary
  memories are refused and left where they are
- The passed fields are collected into an updates dict (status/weight/
  dont_surface/pinned/tags/domain/name/valence/arousal/media and so on)
- pinned=1 forces importance=10 and runs the quota check; pinned=0 only clears
  the flag
  (⚠️ importance is an **internal field**: only pin touches it, and there is no
  entry point from outside)
- An old_str/new_str partial replacement rebuilds the embedding in step
- Switching status to resolved/abandoned appends a short note about what that means

What this file deliberately does not do:
- Never creates a bucket (that is grow/letter's job)
- Never converts an ordinary memory into erasable test data, and never physically
  deletes an ordinary memory
- Returns no structured data; always one short sentence

Exports: trace_core(bucket_id, name, domain, valence, arousal, tags, pinned,
                    delete, status, weight, dont_surface, media_append,
                    media_replace, hard_delete, delete_reason, restore,
                    old_str, new_str, closed_by, mark_asked) -> str
⚰️ Seven dead parameters were removed: importance / resolved / digested /
   content / why_remembered / meaning_append / meaning_replace (see the epitaph
   at _retired below)
========================================
"""

import math
from contextlib import AsyncExitStack
from typing import Optional

from locibrain.domain.memory_messages import resolved_hint
from utils import is_telic, parse_bool
from .. import _runtime as rt
from .._pin import pin_note
from core._rooms import check_room
from core._bigevent import SPAN_RE, is_big as _is_big
from core import _fold as _F
from .._common import (
    _HIGH_IMP_THRESHOLD,
    _quota_turn,
    check_metadata_size,
    check_pinned_quota,
    enforce_high_importance_quota,
    occupies_high_importance_quota_slot,
    resolve_bucket_id,
    resolve_bucket_ids,
)


# ⚰️ `_retired_trace_fields()` was deleted.
# Its job was to produce a human-readable complaint when a retired field was
# passed, but trace's arg model is `extra="forbid"`: **those names cannot get in
# through the tool face at all**, so the function body was unreachable.
# The seven parameters below it (importance/resolved/digested/content/
# why_remembered/meaning_append/meaning_replace) went with it — both of the only
# two callers were checked: server.py passes only the live ones, and the panel in
# `web/loci.py` uses only delete/status/closed_by/mark_asked.
# ⚠️ **`importance` as an internal field was left alone**: pinning a principle
#    still locks it to 10, and the quota still reads it.
#    What was removed is the entry point for changing it from outside, not the
#    field.


_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
_DUR_RE = __import__("re").compile(r"^\d+[dwmy]$")


def _check_real_dates(*dates: str) -> str | None:
    """The right shape does not mean the calendar has that day. `2026-09-31` and
    `2026-13-45` look perfectly legal.

    🔴 The write path used to check shape only, accept the value and **write it
       into the library**, while the read path (`parse_span`) called through
       bare — **one mistyped date could make that whole stretch of memory
       permanently unreadable**.
       The read half now falls back instead of throwing, but the place that
       really has to stop it is here:
       **do not let a day that does not exist into the library**; once it is in,
       any amount of leniency is just covering it up.
    """
    from core._when import parse_date_or_none
    for d in dates:
        if d and parse_date_or_none(d) is None:
            return f"日历上没有 {d} 这一天。"
    return None


def _check_when(when: str, meta: dict) -> str | None:
    """Whether `when` is filled in correctly — three bucket kinds, three shapes,
    matching the definition used by grow.

    ⚠️ This one parameter carries three meanings (an event = which day / a period
       = which stretch / something wanted (telic) = a deadline or a duration). If a parameter
       raises that question at all, the design is the problem, and whether to
       split it is still open.
       Until it is split, at least **reject the wrong shape on the spot**: never
       let a "3w" land quietly on an event.
    """
    if _is_big(meta):
        m = SPAN_RE.match(when)
        if not m:
            return ('时期的 when 要写成起止："2026-07-31..2026-08-05"，'
                    '进行中就把止留空："2026-07-31.."。')
        return _check_real_dates(m.group(1), m.group(2) or "")
    if is_telic(meta):
        if not (_DATE_RE.match(when) or _DUR_RE.match(when)):
            return ('想发生的事，when 要么是个日子（"2026-09-01"），'
                    '要么是段时长（"3w" / "10d" / "2m" / "1y"）。')
        return _check_real_dates(when) if _DATE_RE.match(when) else None
    if not _DATE_RE.match(when):
        return ('普通记忆的 when 是**它发生的那一天**："2026-07-06"。\n'
                '（"3w" 这种时长只对想发生的事有意义；起止范围只对时期有意义。）')
    return _check_real_dates(when)


async def _append_folds(gist_id: str, meta: dict, add: list) -> tuple[str | None, list]:
    """Push **a few more entries** under an existing gist. Returns (error, the new
    cover list).

    🔴 **Append only, never replace.** Passing a fresh list to replace the old one
       means that leaving one id out **quietly releases it** — another silent
       change of behaviour, and this codebase has already been bitten twice by
       that same shape (the silent filter and the silent truncation).
    """
    if not _F.is_gist(meta):
        return (f"{gist_id} 不是 gist（它没盖着任何东西）。"
                "要把几条收成一句，用 fold；这个参数只往已有的 gist 底下加。", [])
    old_cover = list(_F._covered_list(meta) if hasattr(_F, "_covered_list") else [])
    old_cover = [c for c in (meta.get("cover") or [])] or old_cover
    cover = list(dict.fromkeys([*old_cover, *add]))
    covered_rooms = []
    for cid in add:
        if cid == gist_id:
            return ("一条 gist 盖不了自己。", [])
        live = await rt.bucket_mgr.get(cid)
        if not live:
            arch = await rt.bucket_mgr.get_including_archive(cid)
            if arch:
                return (f'{cid} 在归档区，盖不上（盖上了只会留半条链）。'
                        f'先 trace(bucket_id="{cid}", restore=True) 捞回来。', [])
            return (f"这些 id 不存在：{cid}。填真 bucket_id。", [])
        covered_rooms.append(str((live.get("metadata", {}) or {}).get("room") or ""))
    from core._rooms import is_event_room
    if len(cover) >= 2 and any(is_event_room(r) for r in covered_rooms):
        return ("盖一组事件不存在（跟 fold 同一条闸）：日子用时期画圈，"
                "看一条线用 recall(query)。", [])
    # Write both directions: the covered entries have to acknowledge this gist
    for cid in add:
        old = await rt.bucket_mgr.get(cid)
        old_meta = (old or {}).get("metadata", {}) or {}
        old_covers = list(old_meta.get("covered_by") or [])
        if gist_id not in old_covers:
            await rt.bucket_mgr.update(cid, covered_by=old_covers + [gist_id])
    return (None, cover)


async def trace_core(
    bucket_id: str,
    name: Optional[str] = "",
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    tags: Optional[str] = "",
    pinned: Optional[int] = -1,
    delete: Optional[bool] = False,
    status: Optional[str] = "",
    weight: Optional[float] = -1,
    dont_surface: Optional[int] = -1,
    media_append: Optional[list | str] = None,
    media_replace: Optional[list | str] = None,
    hard_delete: Optional[bool] = False,
    delete_reason: Optional[str] = "",
    restore: Optional[bool] = False,
    old_str: Optional[str] = "",
    new_str: Optional[str] = None,
    room: Optional[str] = "",
    when: Optional[str] = "",
    folds_append: Optional[list | str] = None,
    closed_by: Optional[str] = "",
    mark_asked: Optional[bool] = False,
    direction_of_fit: Optional[str] = "",
    bound: Optional[list | str] = None,
) -> str:
    bucket_id = "" if bucket_id is None else str(bucket_id)
    if name is None:
        name = ""
    if domain is None:
        domain = ""
    if valence is None:
        valence = -1
    if arousal is None:
        arousal = -1
    if tags is None:
        tags = ""
    if pinned is None:
        pinned = -1
    if delete is None:
        delete = False
    if status is None:
        status = ""
    if weight is None:
        weight = -1
    if dont_surface is None:
        dont_surface = -1
    if media_append is None:
        media_append = []
    new_str_provided = new_str is not None
    old_str = "" if old_str is None else str(old_str)
    new_str = "" if new_str is None else str(new_str)
    name = str(name)
    domain = str(domain)
    tags = str(tags)
    status = str(status)
    delete = parse_bool(delete, default=False)
    hard_delete = parse_bool(hard_delete, default=False)
    restore = parse_bool(restore, default=False)
    delete_reason = "" if delete_reason is None else str(delete_reason).strip()
    room = "" if room is None else str(room).strip()
    when = "" if when is None else str(when).strip()
    direction_of_fit = "" if direction_of_fit is None else str(direction_of_fit).strip().lower()
    if direction_of_fit and direction_of_fit not in ("thetic", "telic"):
        return ('direction_of_fit 只有两个值："telic"（想让它发生：答应的、计划的、想要的）'
                '或 "thetic"（记下已经是这样的）。')
    bound_names: list | None = None
    if bound is not None:
        from .._subjects import normalize_bound
        bound_names, bound_err = normalize_bound(bound)
        if bound_err:
            return bound_err
    if folds_append is None:
        folds_append = []
    if isinstance(folds_append, str):
        folds_append = [x.strip() for x in folds_append.split(",") if x.strip()]
    folds_append = [str(x).strip() for x in folds_append if str(x).strip()]

    def _finite_float(value, default: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return numeric if math.isfinite(numeric) else default

    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    valence = _finite_float(valence, -1)
    arousal = _finite_float(arousal, -1)
    weight = _finite_float(weight, -1)
    pinned = _safe_int(pinned, -1)
    dont_surface = _safe_int(dont_surface, -1)

    metadata_err = check_metadata_size(
        bucket_id=bucket_id,
        name=name,
        domain=domain,
        tags=tags,
        status=status,
        delete_reason=delete_reason,
    )
    if metadata_err:
        return metadata_err
    if rt.mark_op:
        rt.mark_op("trace")
    rt.record_v3_tool_event("trace", {
        "bucket_id": bucket_id,
        "name": name,
        "domain": domain,
        "valence": valence,
        "arousal": arousal,
        "tags": tags,
        "pinned": pinned,
        "delete": delete,
        "hard_delete": hard_delete,
        "restore": restore,
        "delete_reason_length": len(delete_reason),
        "old_str_length": len(old_str),
        "new_str_length": len(new_str) if new_str_provided else 0,
        "status": status,
        "weight": weight,
        "dont_surface": dont_surface,
        "room": room,
        "when": when,
        "folds_append": folds_append,
    })

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # The handle breath prints next to an item is enough to close it. Resolved
    # here, before restore / delete / get, so each of those speaks of the full id.
    bucket_id, id_err = await resolve_bucket_id(bucket_id)
    if id_err:
        return id_err
    folds_append, folds_err = await resolve_bucket_ids(folds_append, "folds_append")
    if folds_err:
        return folds_err

    restore_conflicts = any((
        delete,
        hard_delete,
        bool(name),
        bool(domain),
        valence != -1,
        arousal != -1,
        bool(tags),
        pinned != -1,
        bool(status),
        weight != -1,
        dont_surface != -1,
        bool(media_append),
        media_replace is not None,
        bool(delete_reason),
        bool(old_str),
        new_str_provided,
        bool(room),
        bool(when),
        bool(folds_append),
    ))
    if restore and restore_conflicts:
        return (
            "参数冲突：restore=True 必须单独调用，不能同时删除或修改记忆；"
            "本次未恢复、未修改。"
        )
    if restore:
        result = await rt.bucket_mgr.restore_archived(bucket_id)
        if result.get("ok"):
            return f"已重新回忆并恢复记忆桶: {bucket_id}"
        if result.get("error") == "not_archived":
            return f"记忆桶仍在日常记忆中，无需恢复: {bucket_id}"
        if result.get("error") == "not_found":
            return f"未找到记忆桶: {bucket_id}"
        return f"恢复记忆桶失败: {result.get('error', 'unknown_error')}"

    patch_args_supplied = bool(old_str) or new_str_provided
    if patch_args_supplied and (delete or hard_delete):
        return (
            "参数冲突：old_str/new_str 局部替换不能与 delete/hard_delete 同时使用；"
            "本次未修改、未删除、未归档。"
        )
    if patch_args_supplied and (not old_str or not new_str_provided):
        return (
            "局部替换必须同时提供 old_str 和 new_str；new_str 可以是空字符串以删除片段。"
            "本次未修改。"
        )
    if patch_args_supplied and old_str == new_str:
        return "old_str 与 new_str 完全相同，没有内容需要替换；本次未修改。"

    # --- Delete mode (an ordinary memory may only be soft-deleted / archived) ---
    if hard_delete and delete:
        return (
            "参数冲突：delete=True 表示归档，hard_delete=True 仅表示清理测试桶，"
            "两者不能同时使用；本次未删除、未归档。"
        )
    if hard_delete:
        if not delete_reason:
            return (
                "拒绝永久删除：hard_delete 仅用于创建时明确标记为 test_data 的测试桶，"
                "并且必须提供非空 delete_reason；本次未删除、未归档。"
            )
        if len(delete_reason) > 500:
            return "拒绝永久删除：delete_reason 不能超过 500 个字符；本次未删除、未归档。"
        result = await rt.bucket_mgr.hard_delete_test_bucket(
            bucket_id, reason=delete_reason
        )
        if result.get("ok"):
            return f"已永久删除测试桶: {bucket_id}"
        if result.get("error") == "not_erasable_test_data":
            return (
                "拒绝永久删除：普通记忆桶不可被 trace 物理删除；"
                "只有创建时明确标记为 test_data 的测试桶可以清理。"
                "本次未删除、未归档；若只想从日常召回隐藏，请改用 delete=True 归档。"
            )
        if result.get("error") == "missing_delete_reason":
            return "拒绝永久删除：必须提供非空 delete_reason；本次未删除、未归档。"
        if result.get("error") == "delete_reason_too_long":
            return "拒绝永久删除：delete_reason 不能超过 500 个字符；本次未删除、未归档。"
        return f"永久删除失败: {result.get('error', 'unknown_error')}"

    if delete:
        success = await rt.bucket_mgr.delete(bucket_id)
        return f"已将记忆桶存入档案（不可在日常召回中浮现）: {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    meta = bucket.get("metadata", {})
    current_pinned = parse_bool(meta.get("pinned"), default=False)
    protected = parse_bool(meta.get("protected"), default=False)
    unpinning_now = pinned == 0 and current_pinned
    # Since the gate was loosened, pin's reminder trails the success receipt, so
    # it has to survive to the end of the function (outside the lock block)
    pin_hint: str | None = None
    # The quota decision and the write must happen inside the same lock:
    # between check_pinned_quota / enforce_high_importance_quota and the final
    # bucket_mgr.update() there is other field handling and an await, so two
    # concurrent trace() calls could both read the same "not full yet" snapshot
    # before either commits. Which lock is needed can be determined from the
    # arguments before updates is touched, so it is computed first and then the
    # whole check-plus-write is wrapped in the matching quota turn.
    current_importance = int(meta.get("importance") or 0)
    current_type = str(meta.get("type") or "dynamic").strip().lower()
    pin_state_changed = pinned in (0, 1) and bool(pinned) != current_pinned
    final_pinned = bool(pinned) if pinned in (0, 1) else current_pinned
    final_type = current_type
    if pinned == 1:
        final_type = "permanent"
    elif unpinning_now and not protected:
        final_type = "dynamic"
    # importance now has only two sources: pin locks it to 10, everything else
    # stays as it was (it cannot be changed from outside).
    # The name requested_importance is kept: the line below about "write it if the
    # quota pushed it down" compares "what was asked for" with "what was finally
    # given". The meaning is unchanged; it is just that "what was asked for" is
    # now always identical to the current state.
    requested_importance = current_importance
    # On unpinning, importance falls back to 8 (bucket_manager backstops the
    # actual write). It has to be computed here too, or the quota would judge on
    # "still 10 after unpinning" and push an OB-W003 that should never have been
    # pushed — **a warning describing something other than what happened on disk
    # is worse than no warning at all**.
    if pinned == 1:
        final_importance = 10
    elif unpinning_now and not protected:
        final_importance = min(requested_importance, 8)
    else:
        final_importance = requested_importance
    current_dont_surface = parse_bool(
        meta.get("dont_surface"), default=False
    )
    final_dont_surface = (
        bool(dont_surface)
        if dont_surface in (0, 1)
        else current_dont_surface
    )
    before_quota_meta = dict(meta)
    before_quota_meta.update({
        "importance": current_importance,
        "pinned": current_pinned,
        "protected": protected,
        "type": current_type,
        "dont_surface": current_dont_surface,
    })
    after_quota_meta = dict(before_quota_meta)
    after_quota_meta.update({
        "importance": final_importance,
        "pinned": final_pinned,
        "type": final_type,
        "dont_surface": final_dont_surface,
    })
    occupied_high_before = occupies_high_importance_quota_slot(
        before_quota_meta
    )
    occupies_high_after = occupies_high_importance_quota_slot(after_quota_meta)
    reserves_high_importance = occupies_high_after and not occupied_high_before
    eligibility_field_changed = (
        pin_state_changed or final_dont_surface != current_dont_surface
    )
    importance_changed = final_importance != current_importance
    needs_high_importance_lock = (
        eligibility_field_changed
        or (
            importance_changed
            and max(current_importance, final_importance)
            >= _HIGH_IMP_THRESHOLD
        )
    )
    need_pinned_lock = pin_state_changed

    async with AsyncExitStack() as quota_stack:
        if need_pinned_lock:
            await quota_stack.enter_async_context(_quota_turn("pinned"))
        if needs_high_importance_lock:
            await quota_stack.enter_async_context(_quota_turn("high_importance"))

        if need_pinned_lock or needs_high_importance_lock:
            locked_bucket = await rt.bucket_mgr.get(bucket_id)
            if not locked_bucket:
                return f"未找到记忆桶: {bucket_id}"
            locked_meta = locked_bucket.get("metadata", {})
            locked_snapshot = (
                parse_bool(locked_meta.get("pinned"), default=False),
                parse_bool(locked_meta.get("protected"), default=False),
                str(locked_meta.get("type") or "dynamic").strip().lower(),
                int(locked_meta.get("importance") or 0),
                parse_bool(locked_meta.get("dont_surface"), default=False),
            )
            original_snapshot = (
                current_pinned,
                protected,
                current_type,
                current_importance,
                current_dont_surface,
            )
            if locked_snapshot != original_snapshot:
                return (
                    f"记忆桶 {bucket_id} 在本次修改期间已被其他请求更新，"
                    "为避免覆盖或配额误判，请重试。"
                )

        if reserves_high_importance:
            final_importance = await enforce_high_importance_quota(
                final_importance
            )

        updates: dict = {}
        if name:
            updates["name"] = name
        if domain:
            updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
        if 0 <= valence <= 1:
            updates["valence"] = valence
        if 0 <= arousal <= 1:
            updates["arousal"] = arousal
        if tags:
            updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        if pinned in (0, 1):
            updates["pinned"] = bool(pinned)
            if pinned == 1:
                # --- The gate in front of pin (built once, later loosened) ---
                # What gets pinned is still a **principle** ("how I mean to act"),
                # but this gate no longer **blocks**:
                # it is not a judgement code can make well. A regex cannot tell
                # "I am always impatient" (should be stopped) from "the things I
                # value have always grown slowly" (should be kept); both are
                # descriptive sentences.
                # So it pins regardless and hangs the reminder off the success
                # receipt (see the epitaph in tools/_pin.py).
                # What it reads is still **the body as it will be after this
                # pin**: if the same call is also editing the body through
                # content or a partial replacement, it must look at the edited
                # version, or the reminder would be talking about the old text.
                _pin_text = (updates.get("content")
                             or str(bucket.get("content") or ""))
                if patch_args_supplied:
                    _pin_text = str(bucket.get("content") or "").replace(
                        old_str, new_str, 1)
                pin_hint = pin_note(_pin_text)
                if need_pinned_lock:
                    err = await check_pinned_quota()
                    if err:
                        return err
                updates["importance"] = 10
        if status:
            s = status.strip().lower()
            if s == "want":
                return ('status 不再有 "want"：想不想要是 direction_of_fit="telic"，'
                        'status 只管关没关。重新打开写 status="active"。')
            if s not in ("active", "resolved", "abandoned"):
                return (f'status 无效：{status}。"active"（还开着）/ "resolved"（做完了）'
                        '/ "abandoned"（不做了）。')
            updates["status"] = s
        # --- Who closed it ---
        # 🔴 The field is deliberately not called resolved_by — older data uses that
        #   name for *which bucket* closed something (a bucket_id or
        #   "manual"/"llm_judge"). This one says *which person* closed it; one name
        #   for both would fold two meanings into one word.
        # This parameter is also absent from the trace tool signature server.py
        # exposes to me — only the close button's route in web/loci.py, clicked
        # by hand, passes a closed_by naming the user. Calling trace myself over
        # MCP can never reach this parameter, so by construction it only ever
        # records a close made by a person.
        # It does not also hard-code "closed by me" while it is here — I am
        # always present; the only thing worth leaving a trace of is "this time
        # I did not notice it myself, I was told".
        if updates.get("status") in ("resolved", "abandoned") and closed_by:
            updates["closed_by"] = str(closed_by).strip()[:50]
        if 0 <= weight <= 1:
            updates["weight"] = float(weight)
        if dont_surface in (0, 1):
            updates["dont_surface"] = bool(dont_surface)

        # ---- Room / time / covering —— where metadata lives ----
        # 🔴 The axis: **regrow changes the content itself, trace changes
        #    metadata**. trace is for **correcting a memory's metadata**; regrow
        #    is for a memory that **went wrong, or that I now think differently
        #    about**.
        #    Neither room nor time affects what this memory says — changing them
        #    is more like correction fluid, and should not produce a new version.
        # ⚰️ `regrow` briefly accepted `room` (on the grounds that "moving is part
        #    of re-versioning") — that is one field with two entry points, exactly
        #    the disease deliberately killed off elsewhere in this system. Removed.
        if room:
            room_err = check_room(room, "")
            if room_err:
                return room_err
            updates["room"] = room
        if direction_of_fit:
            updates["direction_of_fit"] = direction_of_fit
        if bound_names is not None:
            updates["bound"] = bound_names
        if when:
            # Checked against the direction this same call leaves it in.
            when_meta = ({**meta, "direction_of_fit": direction_of_fit}
                         if direction_of_fit else meta)
            when_err = _check_when(when, when_meta)
            if when_err:
                return when_err
            updates["when"] = when
        if folds_append:
            fold_err, new_cover = await _append_folds(bucket_id, meta, folds_append)
            if fold_err:
                return fold_err
            updates["cover"] = new_cover
        if final_importance != requested_importance:
            # Unpinning/restoring surfacing can create an ordinary high slot.
            # Persist quota degradation in the same bucket transaction.
            # The condition was relaxed from "reserves_high_importance and it
            # changed" to simply "it changed" — an unpin falling back (10 -> 8)
            # does **not** occupy a high-importance slot, which is exactly why it
            # should never have been blocked by the earlier condition.
            updates["importance"] = final_importance

        # --- media —— appending is the everyday operation; wholesale replacement
        # is only for correcting or cleaning up ---
        # (meaning is retired and is rejected above; the old meanings on disk are
        # left untouched, and what becomes of them is decided only after a human
        # has read them)
        if media_append:
            updates["media_append"] = media_append
        if media_replace is not None:
            updates["media"] = media_replace

        # On reactivation, neutralise the old resolved boolean — leave it and
        # is_closed will push the just-reopened entry straight back down
        if updates.get("status") == "active" and bucket.get("metadata", {}).get("resolved"):
            updates["resolved"] = False
        # On reactivation, clear the previous round's "who closed it" as well —
        # otherwise, after reopening and then closing it myself, the panel would
        # still be showing the stale marker saying the user closed it.
        if updates.get("status") == "active" and bucket.get("metadata", {}).get("closed_by"):
            updates["closed_by"] = ""

        # --- The "last asked" timestamp ---
        # Only web/loci.py passes mark_asked=True, and only at the moment the
        # question is actually shown to the user — like closed_by, it is absent
        # from the trace tool signature in server.py, so I cannot stamp this out
        # of thin air.
        if mark_asked:
            from core._when import now as _now_local
            updates["last_asked"] = _now_local().isoformat()

        if not updates and not patch_args_supplied:
            return "没有任何字段需要修改。"

        if patch_args_supplied:
            patch_result = await rt.bucket_mgr.update_content_fragment(
                bucket_id,
                old_str=old_str,
                new_str=new_str,
                **updates,
            )
            if not patch_result.get("ok"):
                patch_error = patch_result.get("error")
                if patch_error == "not_found":
                    return f"未找到记忆桶: {bucket_id}"
                if patch_error == "old_str_not_found":
                    return (
                        "未找到 old_str，正文未修改。请从 Dashboard 或对应记忆类型的读取入口"
                        "核对当前原文；普通记忆也可用 "
                        f'breath_advanced(query="{bucket_id}", max_results=1, '
                        "max_tokens=20000) 按完整 bucket_id 读取。复制连续且逐字一致的片段后重试。"
                    )
                if patch_error == "old_str_ambiguous":
                    return (
                        "old_str 在正文中至少出现 2 次，"
                        "无法安全确定要修改哪一处；正文未修改。请提供更长且唯一的原文片段。"
                    )
                if patch_error == "invalid_content":
                    return str(patch_result.get("message") or "替换后的内容不符合存储限制。")
                if patch_error == "unchanged":
                    return "old_str 与 new_str 替换后正文没有变化；本次未修改。"
                return f"修改失败: {bucket_id}"
        else:
            success = await rt.bucket_mgr.update(bucket_id, **updates)
            if not success:
                return f"修改失败: {bucket_id}"

    # Note: both a full body update and a partial replacement funnel into
    # _update_locked(content=...) inside BucketManager, which posts to the
    # embedding outbox. Calling generate_and_store again here is unnecessary and
    # would be wrong — the same content would hit the vector API twice.

    _display_updates = {
        k: v for k, v in updates.items()
        if k not in ("content", "meaning_append", "meaning", "media_append", "media")
    }
    changed = ", ".join(f"{k}={v}" for k, v in _display_updates.items())
    if patch_args_supplied:
        changed += (", content=已局部替换" if changed else "content=已局部替换")
    if "media_append" in updates:
        changed += (", " if changed else "") + f"media=已追加{len(updates['media_append'])}项"
    if "media" in updates:
        changed += (", " if changed else "") + f"media=整体替换({len(updates['media'])}项)"
    if updates.get("status") in ("resolved", "abandoned"):
        changed += f" → {resolved_hint(True)}"
    elif updates.get("status") == "active":
        changed += f" → {resolved_hint(False)}"
    out = f"已修改记忆桶 {bucket_id}: {changed}"
    # pin's reminder trails the **success receipt**: it is not an error, the pin
    # is already on disk (see the epitaph in tools/_pin.py)
    if pin_hint:
        out += "\n\n" + pin_hint
    return out
