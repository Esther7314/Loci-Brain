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
- Never creates a bucket (that is grow's job)
- Never converts an ordinary memory into erasable test data, and never physically
  deletes an ordinary memory
- Returns no structured data; always one short sentence

Exports: trace_core(bucket_id, name, domain, valence, arousal, tags, pinned,
                    delete, status, weight, dont_surface, media_append,
                    media_replace, hard_delete, delete_reason, restore,
                    old_str, new_str, closed_by, mark_asked, direction_of_fit,
                    bound, cue, card_of, sources_append, invalidation) -> str
========================================
"""

import math
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Optional

from core.memory_messages import resolved_hint
from utils import (PROV_FIELD, PROV_MAX_LINES, WAS_QUOTED_FROM, is_telic, parse_bool,
                   read_prov)
from core import runtime as rt
from .._pin import pin_note
from core._rooms import check_room
from core._bigevent import SPAN_RE, is_big as _is_big
from core import _fold as _F
from core import _holds as _H
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


# Retired fields need no complaint here: trace's arg model is `extra="forbid"`, so
# **those names cannot get in through the tool face at all**.
# ⚠️ **`importance` as an internal field stays**: pinning a principle locks it to 10,
#    and the quota reads it. What trace lacks is an entry point for changing it from
#    outside, not the field.


_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
_DUR_RE = __import__("re").compile(r"^\d+[dwmy]$")


def _check_real_dates(*dates: str) -> str | None:
    """The right shape does not mean the calendar has that day. `2026-09-31` and
    `2026-13-45` look perfectly legal.

    🔴 A write path that checks shape only would **write it into the library**,
       and one mistyped date could make that whole stretch of memory unreadable.
       The read half (`parse_span`) falls back instead of throwing, but the place
       that really has to stop it is here:
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
    if _H.is_hold(meta):
        # A hold's when is the day it ends or the days it covers (core/_holds.py).
        return _H.check_hold_when(when) or None
    # A day with a clock time (`2026-09-01 20:00`, local unless it says otherwise) is the
    # same shape grow takes and the backfill writes, so a read hour can be corrected.
    from ..grow.rooms_path import clock_when
    if clock_when(when)[1]:
        return None
    if is_telic(meta):
        if not (_DATE_RE.match(when) or _DUR_RE.match(when)):
            return ('想发生的事，when 要么是个日子（"2026-09-01"，可带钟点 "2026-09-01 20:00"），'
                    '要么是段时长（"3w" / "10d" / "2m" / "1y"）。')
        return _check_real_dates(when) if _DATE_RE.match(when) else None
    if not _DATE_RE.match(when):
        return ('普通记忆的 when 是**它发生的那一天**："2026-07-06"（可带钟点 "2026-07-06 18:30"）。\n'
                '（"3w" 这种时长只对想发生的事有意义；起止范围只对时期有意义。）')
    return _check_real_dates(when)


async def _live_version(bucket_id: str, meta: dict) -> str:
    """The newest version along `superseded_by`: the last one that still reads, stopping at
    a loop or after 64 steps."""
    current, seen = bucket_id, {bucket_id}
    nxt = str(meta.get("superseded_by") or "").strip()
    while nxt and nxt not in seen and len(seen) < 64:
        b = await rt.bucket_mgr.get_including_archive(nxt)
        if not b:
            break
        current = nxt
        seen.add(nxt)
        nxt = str((b.get("metadata") or {}).get("superseded_by") or "").strip()
    return current


class _Refused(Exception):
    """A refusal decided under the bucket's lock (the entry changed since it was read);
    the message is what the caller is told."""


def _appended_cover(add: list):
    """The cover roster with `add` appended, decided on the gist as it is on disk."""
    def revise(meta: dict) -> dict:
        return {"cover": list(dict.fromkeys([*_F.cover_ids(meta), *add]))}
    return revise


async def _append_folds(gist_id: str, meta: dict, add: list) -> str | None:
    """Push **a few more entries** under an existing gist: checks them, and writes each
    covered entry's side (`covered_by`). Returns the refusal, or None; the gist's own
    `cover` goes in with the caller's write (`_appended_cover`).

    🔴 **Append only, never replace.** Passing a fresh list to replace the old one
       means that leaving one id out **quietly releases it** — another silent
       change of behaviour, and this codebase has already been bitten twice by
       that same shape (the silent filter and the silent truncation).
    Both rosters are appended to as they are on disk, under each entry's lock, so two
    appends at the same moment both stand.
    """
    if not _F.is_gist(meta):
        return (f"{gist_id} 不是 gist（它没盖着任何东西）。"
                "要把几条收成一句，用 fold；这个参数只往已有的 gist 底下加。")
    cover = list(dict.fromkeys([*_F.cover_ids(meta), *add]))
    covered_rooms = []
    for cid in add:
        if cid == gist_id:
            return "一条 gist 盖不了自己。"
        live = await rt.bucket_mgr.get(cid)
        if not live:
            arch = await rt.bucket_mgr.get_including_archive(cid)
            if arch:
                return (f'{cid} 在归档区，盖不上（盖上了只会留半条链）。'
                        f'先 trace(bucket_id="{cid}", restore=True) 捞回来。')
            return f"这些 id 不存在：{cid}。填真 bucket_id。"
        covered_rooms.append(str((live.get("metadata", {}) or {}).get("room") or ""))
    from core._rooms import is_event_room
    if len(cover) >= 2 and any(is_event_room(r) for r in covered_rooms):
        return ("盖一组事件不存在（跟 fold 同一条闸）：日子用时期画圈，"
                "看一条线用 recall(query)。")

    # Write both directions: the covered entries have to acknowledge this gist
    def acknowledge(old_meta: dict) -> dict:
        old_covers = _F._covered_list(old_meta)
        return {} if gist_id in old_covers else {"covered_by": old_covers + [gist_id]}

    for cid in add:
        await rt.bucket_mgr.update(cid, revise=acknowledge)
    return None


@dataclass
class _Call:
    """One trace call's arguments: as given, then normalized in place by _read_call.
    The first block mirrors trace_core's parameters name for name; the last three are
    what _read_call derives from them."""
    bucket_id: str
    name: str
    domain: str
    valence: float
    arousal: float
    tags: str
    pinned: int
    delete: bool
    status: str
    weight: float
    dont_surface: int
    media_append: list | str
    media_replace: list | str | None
    hard_delete: bool
    delete_reason: str
    restore: bool
    old_str: str
    new_str: str
    room: str
    when: str
    folds_append: list | str | None
    closed_by: str
    mark_asked: bool
    direction_of_fit: str
    bound: list | str | None
    cue: dict | str | None
    card_of: str | None
    sources_append: list | dict | str | None
    invalidation: str
    new_str_provided: bool = False
    bound_names: list | None = None
    # None leaves the cue alone; (value,) writes value, and (None,) takes the cue off.
    cue_update: tuple | None = None

    @property
    def patch(self) -> bool:
        """An old_str/new_str partial replacement was asked for."""
        return bool(self.old_str) or self.new_str_provided


@dataclass
class _QuotaPlan:
    """What this call does to the pinned and high-importance quotas, decided from the
    entry as read before any lock. `snapshot` is that reading (pinned, protected, type,
    importance, dont_surface), checked again once the locks are held."""
    snapshot: tuple
    requested_importance: int
    final_importance: int
    reserves_high_importance: bool
    need_pinned_lock: bool
    needs_high_importance_lock: bool


@dataclass
class _Edit:
    """What the call writes, gathered under the quota locks, and what the receipt needs
    from it afterwards."""
    updates: dict = field(default_factory=dict)
    # Changes decided on the entry as it is on disk, under its lock (BucketManager
    # `revise`); `appended` keeps what they wrote, for the receipt.
    revisers: list = field(default_factory=list)
    appended: dict = field(default_factory=dict)
    pin_hint: str | None = None
    card_note: str = ""
    confirmed_on: str = ""
    source_notes: list = field(default_factory=list)


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


def _id_list(ids) -> list:
    """A list parameter of ids: a list, or one comma-separated string; blanks dropped."""
    if ids is None:
        ids = []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()]
    return [str(x).strip() for x in ids if str(x).strip()]


# ------------------------------------------------------------
# Reading the arguments
# ------------------------------------------------------------

def _read_call(c: _Call) -> str | None:
    """Normalize every argument in place; returns the refusal for one that cannot be
    read. -1, "" and None mean "leave it alone" throughout (None as a number or a flag
    falls to the same default as -1 or False)."""
    c.bucket_id = "" if c.bucket_id is None else str(c.bucket_id)
    # The keep-as-is gesture of 依据变了的 (core/_invalidation.py): the one value is
    # "confirmed". Anything else is refused rather than read as something nearby.
    c.invalidation = "" if c.invalidation is None else str(c.invalidation).strip().lower()
    if c.invalidation and c.invalidation != "confirmed":
        return ('invalidation 只认 "confirmed"：看过了，依据变了但这条照留。'
                "要重写用 regrow，要收起来用 delete=True。")
    c.name = "" if c.name is None else str(c.name)
    c.domain = "" if c.domain is None else str(c.domain)
    c.tags = "" if c.tags is None else str(c.tags)
    c.status = "" if c.status is None else str(c.status)
    if c.media_append is None:
        c.media_append = []
    c.new_str_provided = c.new_str is not None
    c.old_str = "" if c.old_str is None else str(c.old_str)
    c.new_str = "" if c.new_str is None else str(c.new_str)
    c.delete = parse_bool(c.delete, default=False)
    c.hard_delete = parse_bool(c.hard_delete, default=False)
    c.restore = parse_bool(c.restore, default=False)
    c.delete_reason = "" if c.delete_reason is None else str(c.delete_reason).strip()
    c.room = "" if c.room is None else str(c.room).strip()
    c.when = "" if c.when is None else str(c.when).strip()
    c.direction_of_fit = ("" if c.direction_of_fit is None
                          else str(c.direction_of_fit).strip().lower())
    if c.direction_of_fit and c.direction_of_fit not in ("thetic", "telic"):
        return ('direction_of_fit 只有两个值："telic"（想让它发生：答应的、计划的、想要的）'
                '或 "thetic"（记下已经是这样的）。')
    bound_err = _read_bound(c)
    if bound_err:
        return bound_err
    cue_err = _read_cue(c)
    if cue_err:
        return cue_err
    c.folds_append = _id_list(c.folds_append)
    c.valence = _finite_float(c.valence, -1)
    c.arousal = _finite_float(c.arousal, -1)
    c.weight = _finite_float(c.weight, -1)
    c.pinned = _safe_int(c.pinned, -1)
    c.dont_surface = _safe_int(c.dont_surface, -1)
    metadata_err = check_metadata_size(
        bucket_id=c.bucket_id,
        name=c.name,
        domain=c.domain,
        tags=c.tags,
        status=c.status,
        delete_reason=c.delete_reason,
    )
    if metadata_err:
        return metadata_err
    return None


def _read_bound(c: _Call) -> str | None:
    if c.bound is None:
        return None
    from core.names import normalize_bound
    c.bound_names, bound_err = normalize_bound(c.bound)
    if bound_err:
        return bound_err
    return None


def _read_cue(c: _Call) -> str | None:
    """cue: None leaves it alone, "" takes it off, anything else has to be a whole cue
    (grow's check, so a cue means the same thing whichever tool wrote it)."""
    cue = c.cue
    if cue is None:
        return None
    if isinstance(cue, str) and not cue.strip():
        c.cue_update = (None,)
        return None
    from ..grow.rooms_path import check_cue
    if isinstance(cue, str) and cue.strip().startswith("{"):
        try:
            cue = __import__("json").loads(cue)
        except ValueError:
            pass
    cue_value, cue_err = check_cue(cue)
    if cue_err:
        return cue_err
    c.cue_update = (cue_value,)
    return None


async def _resolve_ids(c: _Call) -> str | None:
    """The handle breath prints next to an item is enough to close it. Resolved before
    restore / delete / get, so each of those speaks of the full id."""
    c.bucket_id, id_err = await resolve_bucket_id(c.bucket_id)
    if id_err:
        return id_err
    c.folds_append, folds_err = await resolve_bucket_ids(c.folds_append, "folds_append")
    if folds_err:
        return folds_err
    return None


# ------------------------------------------------------------
# Calls that do not edit: restore, delete
# ------------------------------------------------------------

async def _restore(c: _Call) -> str:
    """restore=True brings an archived entry back, and only on a call that asks for
    nothing else."""
    restore_conflicts = any((
        c.delete,
        c.hard_delete,
        bool(c.name),
        bool(c.domain),
        c.valence != -1,
        c.arousal != -1,
        bool(c.tags),
        c.pinned != -1,
        bool(c.status),
        c.weight != -1,
        c.dont_surface != -1,
        bool(c.media_append),
        c.media_replace is not None,
        bool(c.delete_reason),
        bool(c.old_str),
        c.new_str_provided,
        bool(c.room),
        bool(c.when),
        bool(c.folds_append),
        c.cue_update is not None,
        c.card_of is not None,
        bool(c.sources_append),
        bool(c.invalidation),
    ))
    if restore_conflicts:
        return (
            "参数冲突：restore=True 必须单独调用，不能同时删除或修改记忆；"
            "本次未恢复、未修改。"
        )
    result = await rt.bucket_mgr.restore_archived(c.bucket_id)
    if result.get("ok"):
        return f"已重新回忆并恢复记忆桶: {c.bucket_id}"
    if result.get("error") == "not_archived":
        return f"记忆桶仍在日常记忆中，无需恢复: {c.bucket_id}"
    if result.get("error") == "not_found":
        return f"未找到记忆桶: {c.bucket_id}"
    return f"恢复记忆桶失败: {result.get('error', 'unknown_error')}"


def _check_patch(c: _Call) -> str | None:
    """A partial replacement needs both halves, a real change, and no delete beside it."""
    if c.patch and (c.delete or c.hard_delete):
        return (
            "参数冲突：old_str/new_str 局部替换不能与 delete/hard_delete 同时使用；"
            "本次未修改、未删除、未归档。"
        )
    if c.patch and (not c.old_str or not c.new_str_provided):
        return (
            "局部替换必须同时提供 old_str 和 new_str；new_str 可以是空字符串以删除片段。"
            "本次未修改。"
        )
    if c.patch and c.old_str == c.new_str:
        return "old_str 与 new_str 完全相同，没有内容需要替换；本次未修改。"
    return None


async def _delete(c: _Call) -> str:
    """An ordinary memory may only be archived (delete=True); hard_delete only clears a
    test bucket."""
    if c.hard_delete and c.delete:
        return (
            "参数冲突：delete=True 表示归档，hard_delete=True 仅表示清理测试桶，"
            "两者不能同时使用；本次未删除、未归档。"
        )
    if c.hard_delete:
        return await _hard_delete(c)
    success = await rt.bucket_mgr.delete(c.bucket_id)
    return f"已将记忆桶存入档案（不可在日常召回中浮现）: {c.bucket_id}" if success else f"未找到记忆桶: {c.bucket_id}"


async def _hard_delete(c: _Call) -> str:
    if not c.delete_reason:
        return (
            "拒绝永久删除：hard_delete 仅用于创建时明确标记为 test_data 的测试桶，"
            "并且必须提供非空 delete_reason；本次未删除、未归档。"
        )
    if len(c.delete_reason) > 500:
        return "拒绝永久删除：delete_reason 不能超过 500 个字符；本次未删除、未归档。"
    result = await rt.bucket_mgr.hard_delete_test_bucket(
        c.bucket_id, reason=c.delete_reason
    )
    if result.get("ok"):
        return f"已永久删除测试桶: {c.bucket_id}"
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


# ------------------------------------------------------------
# The entry being edited, and the quota locks around the edit
# ------------------------------------------------------------

async def _entry_in_use(bucket_id: str) -> tuple[dict | None, dict | None, str | None]:
    """(bucket, its metadata, refusal). An old version (regrow replaced it) is on file,
    not in use: an edit there would succeed and change nothing anyone reads, while the
    live version keeps asking. Refused, naming the version in use. Deleting or restoring
    an old one goes through _delete / _restore, before this."""
    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        return None, None, f"未找到记忆桶: {bucket_id}"
    meta = bucket.get("metadata", {})
    if str(meta.get("superseded_by") or "").strip():
        live = await _live_version(bucket_id, meta)
        return None, None, (f"{bucket_id} 是旧版，已经换成 {live} 了——改旧版动不到正在用的那一版。"
                            f"要改就在 {live} 上 trace；本次未修改。")
    return bucket, meta, None


def _plan_quota(c: _Call, meta: dict) -> _QuotaPlan:
    """Which quota locks this call needs, and the importance it ends with.

    The quota decision and the write happen inside the same lock: between
    check_pinned_quota / enforce_high_importance_quota and the final bucket_mgr.update()
    there is other field handling and an await, so two concurrent trace() calls could
    both read the same "not full yet" snapshot before either commits. Which lock is
    needed follows from the arguments alone, so it is decided here, before the edit,
    and trace_core holds the matching quota turns around the whole check-plus-write.
    """
    current_pinned = parse_bool(meta.get("pinned"), default=False)
    protected = parse_bool(meta.get("protected"), default=False)
    unpinning_now = c.pinned == 0 and current_pinned
    current_importance = int(meta.get("importance") or 0)
    current_type = str(meta.get("type") or "dynamic").strip().lower()
    pin_state_changed = c.pinned in (0, 1) and bool(c.pinned) != current_pinned
    final_pinned = bool(c.pinned) if c.pinned in (0, 1) else current_pinned
    final_type = current_type
    if c.pinned == 1:
        final_type = "permanent"
    elif unpinning_now and not protected:
        final_type = "dynamic"
    # importance has two sources: pin locks it to 10, everything else leaves it as it
    # is (it cannot be changed from outside). requested_importance is "what was asked
    # for", compared with what the quota finally gives to decide whether to write it.
    requested_importance = current_importance
    # On unpinning, importance falls back to 8 (bucket_manager backstops the actual
    # write). It is computed here too, or the quota would judge on "still 10 after
    # unpinning" and push an OB-W003 that should never have been pushed — **a warning
    # describing something other than what happened on disk is worse than no warning
    # at all**.
    if c.pinned == 1:
        final_importance = 10
    elif unpinning_now and not protected:
        final_importance = min(requested_importance, 8)
    else:
        final_importance = requested_importance
    current_dont_surface = parse_bool(
        meta.get("dont_surface"), default=False
    )
    final_dont_surface = (
        bool(c.dont_surface)
        if c.dont_surface in (0, 1)
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
    eligibility_field_changed = (
        pin_state_changed or final_dont_surface != current_dont_surface
    )
    importance_changed = final_importance != current_importance
    return _QuotaPlan(
        snapshot=(
            current_pinned,
            protected,
            current_type,
            current_importance,
            current_dont_surface,
        ),
        requested_importance=requested_importance,
        final_importance=final_importance,
        reserves_high_importance=occupies_high_after and not occupied_high_before,
        need_pinned_lock=pin_state_changed,
        needs_high_importance_lock=(
            eligibility_field_changed
            or (
                importance_changed
                and max(current_importance, final_importance)
                >= _HIGH_IMP_THRESHOLD
            )
        ),
    )


async def _changed_since_read(bucket_id: str, plan: _QuotaPlan) -> str | None:
    """Under the quota locks: refused if the fields the plan was decided on changed
    since they were read."""
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
    if locked_snapshot != plan.snapshot:
        return (
            f"记忆桶 {bucket_id} 在本次修改期间已被其他请求更新，"
            "为避免覆盖或配额误判，请重试。"
        )
    return None


async def _edit_in_turn(c: _Call, bucket: dict, meta: dict, plan: _QuotaPlan,
                        edit: _Edit) -> str | None:
    """Everything from the quota decision to the write, run while trace_core holds the
    plan's quota turns. Each step adds to `edit` or returns the refusal; the order of
    the steps is the order of the receipt's fields."""
    if plan.need_pinned_lock or plan.needs_high_importance_lock:
        refusal = await _changed_since_read(c.bucket_id, plan)
        if refusal:
            return refusal

    if plan.reserves_high_importance:
        plan.final_importance = await enforce_high_importance_quota(
            plan.final_importance
        )

    _set_labels(c, edit)
    refusal = await _set_pin(c, bucket, plan, edit)
    if refusal:
        return refusal
    refusal = _set_status(c, edit)
    if refusal:
        return refusal
    _set_weight_and_surfacing(c, edit)
    refusal = _set_filing(c, edit)
    if refusal:
        return refusal
    refusal = await _set_card(c, meta, edit)
    if refusal:
        return refusal
    refusal = _set_when(c, meta, edit)
    if refusal:
        return refusal
    refusal = await _add_folds(c, meta, edit)
    if refusal:
        return refusal
    refusal = _confirm_invalidation(c, meta, edit)
    if refusal:
        return refusal
    refusal = await _add_sources(c, meta, edit)
    if refusal:
        return refusal
    if plan.final_importance != plan.requested_importance:
        # The quota's degradation, or an unpin falling back (10 -> 8), lands in the same
        # bucket transaction. An unpin does not occupy a high-importance slot, so the
        # condition is only "it changed".
        edit.updates["importance"] = plan.final_importance
    _set_media(c, edit)
    _reopen(bucket, edit)
    _stamp_asked(c, edit)

    if not edit.updates and not c.patch and not edit.revisers:
        return "没有任何字段需要修改。"
    return await _write(c, edit)


# ------------------------------------------------------------
# The kinds of edit
# ------------------------------------------------------------

def _set_labels(c: _Call, edit: _Edit) -> None:
    updates = edit.updates
    if c.name:
        updates["name"] = c.name
    if c.domain:
        updates["domain"] = [d.strip() for d in c.domain.split(",") if d.strip()]
    if 0 <= c.valence <= 1:
        updates["valence"] = c.valence
    if 0 <= c.arousal <= 1:
        updates["arousal"] = c.arousal
    if c.tags:
        updates["tags"] = [t.strip() for t in c.tags.split(",") if t.strip()]


async def _set_pin(c: _Call, bucket: dict, plan: _QuotaPlan, edit: _Edit) -> str | None:
    """pinned=1 forces importance=10 and runs the pinned quota check (when the pin state
    changes); pinned=0 only clears the flag.

    The gate in front of pin: what gets pinned is a **principle** ("how I mean to act"),
    but this gate does not **block**: it is not a judgement code can make well. A regex
    cannot tell "I am always impatient" (should be stopped) from "the things I value
    have always grown slowly" (should be kept); both are descriptive sentences. So it
    pins regardless and hangs the reminder off the success receipt (the reasoning is in
    tools/_pin.py). What it reads is **the body as it will be after this pin**: if the
    same call also edits the body through a partial replacement, it looks at the edited
    version, or the reminder would be talking about the old text.
    """
    if c.pinned not in (0, 1):
        return None
    edit.updates["pinned"] = bool(c.pinned)
    if c.pinned != 1:
        return None
    _pin_text = (edit.updates.get("content")
                 or str(bucket.get("content") or ""))
    if c.patch:
        _pin_text = str(bucket.get("content") or "").replace(
            c.old_str, c.new_str, 1)
    edit.pin_hint = pin_note(_pin_text)
    if plan.need_pinned_lock:
        err = await check_pinned_quota()
        if err:
            return err
    edit.updates["importance"] = 10
    return None


def _set_status(c: _Call, edit: _Edit) -> str | None:
    """status, and who closed it.

    🔴 The field is deliberately not called resolved_by — older data uses that name for
      *which bucket* closed something (a bucket_id or "manual"/"llm_judge"). This one
      says *which person* closed it; one name for both would fold two meanings into
      one word.
    closed_by is absent from the trace tool signature server_tools/trace.py exposes to me — only
    the close button's route in web/loci.py, clicked by hand, passes a closed_by naming
    the user. Calling trace myself over MCP can never reach it, so by construction it
    only ever records a close made by a person. It does not also hard-code "closed by
    me" — I am always present; the only thing worth leaving a trace of is "this time I
    did not notice it myself, I was told".
    """
    if c.status:
        s = c.status.strip().lower()
        if s == "want":
            return ('status 没有 "want"：想不想要是 direction_of_fit="telic"，'
                    'status 只管关没关。重新打开写 status="active"。')
        if s not in ("active", "resolved", "abandoned"):
            return (f'status 无效：{c.status}。"active"（还开着）/ "resolved"（做完了）'
                    '/ "abandoned"（不做了）。')
        edit.updates["status"] = s
    if edit.updates.get("status") in ("resolved", "abandoned") and c.closed_by:
        edit.updates["closed_by"] = str(c.closed_by).strip()[:50]
    return None


def _set_weight_and_surfacing(c: _Call, edit: _Edit) -> None:
    if 0 <= c.weight <= 1:
        edit.updates["weight"] = float(c.weight)
    if c.dont_surface in (0, 1):
        edit.updates["dont_surface"] = bool(c.dont_surface)


# Room / time / covering — where metadata lives.
# 🔴 The axis: **regrow changes the content itself, trace changes metadata**. trace is
#    for **correcting a memory's metadata**; regrow is for a memory that **went wrong,
#    or that I now think differently about**.
#    Neither room nor time affects what this memory says — changing them is more like
#    correction fluid, and should not produce a new version. `regrow` does not take
#    `room`: one field with two entry points is exactly what this system avoids.

def _set_filing(c: _Call, edit: _Edit) -> str | None:
    """room, direction_of_fit, bound and cue."""
    if c.room:
        room_err = check_room(c.room, "")
        if room_err:
            return room_err
        edit.updates["room"] = c.room
    if c.direction_of_fit:
        edit.updates["direction_of_fit"] = c.direction_of_fit
    if c.bound_names is not None:
        edit.updates["bound"] = c.bound_names
    if c.cue_update is not None:
        edit.updates["cue"] = c.cue_update[0]
    return None


async def _set_card(c: _Call, meta: dict, edit: _Edit) -> str | None:
    """card_of: None leaves it alone, "" takes the entry off as a card, a name makes it
    that name's card (grow's check). A card moved to another room is checked against
    the rule again, so a person's card cannot drift into MIND/VIEWS."""
    if c.card_of is not None:
        if not str(c.card_of).strip():
            edit.updates["card_of"] = None
        else:
            from ..grow.rooms_path import check_card
            card, edit.card_note, card_err = await check_card(
                c.card_of, c.room or str(meta.get("room") or ""), exclude=c.bucket_id)
            if card_err:
                return card_err
            edit.updates["card_of"] = card
    elif c.room and str(meta.get("card_of") or "").strip():
        from ..grow.rooms_path import card_room_rule
        card_err, edit.card_note = card_room_rule(str(meta["card_of"]).strip(), c.room)
        if card_err:
            return f"{c.bucket_id} 是「{meta['card_of']}」的名字卡。{card_err}"
    return None


def _set_when(c: _Call, meta: dict, edit: _Edit) -> str | None:
    if not c.when:
        return None
    # Checked against the direction this same call leaves it in.
    when_meta = ({**meta, "direction_of_fit": c.direction_of_fit}
                 if c.direction_of_fit else meta)
    when_err = _check_when(c.when, when_meta)
    if when_err:
        return when_err
    from ..grow.rooms_path import clock_when
    edit.updates["when"] = clock_when(c.when)[0]
    return None


# What depends on lists already on the entry — the cover roster, the invalidation
# records, the sources and their quoted lines — is decided on the entry as it is on
# disk, under its lock (BucketManager `revise`): two appends at the same moment both
# land. The three steps below each add a reviser.

async def _add_folds(c: _Call, meta: dict, edit: _Edit) -> str | None:
    if not c.folds_append:
        return None
    fold_err = await _append_folds(c.bucket_id, meta, c.folds_append)
    if fold_err:
        return fold_err
    edit.revisers.append(_appended_cover(c.folds_append))
    return None


def _confirm_invalidation(c: _Call, meta: dict, edit: _Edit) -> str | None:
    """invalidation="confirmed": looked at, the basis changed, this stands. Every open
    record gains confirmed_at (today); a source revision or a panel correction seen now
    is recorded as a confirmed record. Refused while a source it stands on is withdrawn
    or deleted: that one can only be rewritten or put away."""
    if not c.invalidation:
        return None
    from core import _invalidation as _I
    from core._when import now as _now_local
    from core.profile import _EDITED_BY_USER_TAG
    confirmed_on = _now_local().date().isoformat()
    edit.confirmed_on = confirmed_on

    def confirmed(m: dict) -> tuple[list, str]:
        return _I.confirm(
            m, getattr(rt.bucket_mgr, "sources", None),
            edited=_EDITED_BY_USER_TAG in [str(t) for t in (m.get("tags") or [])],
            today=confirmed_on)
    nothing_open = (f"{c.bucket_id} 没有待看的依据变化（不在「依据变了的」里），"
                    "不用确认；本次未修改。")
    records, refusal = confirmed(meta)
    if refusal:
        return refusal
    if not records:
        return nothing_open

    def confirm_now(m: dict) -> dict:
        now_records, now_refusal = confirmed(m)
        if now_refusal or not now_records:
            raise _Refused(now_refusal or nothing_open)
        return {"invalidation": now_records}
    edit.revisers.append(confirm_now)
    return None


async def _add_sources(c: _Call, meta: dict, edit: _Edit) -> str | None:
    """sources_append: more of the host's material this entry was formed from. Append
    only, like folds_append; each record is checked like a write's (the registry, the
    grant) and brings its quoted prov line with it."""
    if not c.sources_append:
        return None
    from ..grow.rooms_path import check_sources
    from core import _sources as _src
    new_sources, _lines, edit.source_notes, sources_err = await check_sources(
        c.sources_append, read_prov(meta), exclude={c.bucket_id}, from_call=False)
    if sources_err:
        return sources_err
    if not new_sources:
        return "sources_append 是空的：要追加的来源写成 [{system, instance, container, id}]。"
    try:
        _src.normalize_sources(list(meta.get(_src.SOURCES_FIELD) or []) + new_sources)
    except _src.SourceRecordError as e:
        return f"sources 不对：{e.zh}。"
    # The same two changes check_sources made to the lines it read, made again to
    # the lines on disk: each new record's quoted line is added, and a bare host
    # id the entry already had is linked to the one new record carrying that id.
    strings = [_src.record_string(r) for r in new_sources]
    by_id: dict[str, set] = {}
    for r, s in zip(new_sources, strings):
        by_id.setdefault(str(r["id"]), set()).add(s)
    linked = {i: next(iter(s)) for i, s in by_id.items() if len(s) == 1}
    added = [{"rel": WAS_QUOTED_FROM, "target": s} for s in strings]

    def append_sources(m: dict) -> dict:
        try:
            merged = _src.normalize_sources(
                list(m.get(_src.SOURCES_FIELD) or []) + new_sources)
        except _src.SourceRecordError as e:
            raise _Refused(f"sources 不对：{e.zh}。") from e
        lines = [{"rel": WAS_QUOTED_FROM, "target": linked[ln["target"]]}
                 if ln["rel"] == WAS_QUOTED_FROM and "#" not in ln["target"]
                 and ln["target"] in linked else ln for ln in read_prov(m)]
        lines = list({(ln["rel"], ln["target"]): ln for ln in lines + added}.values())
        if len(lines) > PROV_MAX_LINES:
            raise _Refused(f"来源加起来 {len(lines)} 条，超过 {PROV_MAX_LINES}"
                           "——一条记忆挂不了这么多来源，拆开分别存。")
        return {_src.SOURCES_FIELD: merged, PROV_FIELD: lines}
    edit.revisers.append(append_sources)
    return None


def _set_media(c: _Call, edit: _Edit) -> None:
    """media: appending is the everyday operation; wholesale replacement is only for
    correcting or cleaning up. (meaning is retired; the old meanings on disk are left
    untouched, and what becomes of them is decided only after a human has read them.)"""
    if c.media_append:
        edit.updates["media_append"] = c.media_append
    if c.media_replace is not None:
        edit.updates["media"] = c.media_replace


def _reopen(bucket: dict, edit: _Edit) -> None:
    """On reactivation, neutralise the old resolved boolean — left alone, is_closed would
    push the just-reopened entry straight back down — and clear the previous round's
    "who closed it", or after reopening and then closing it myself the panel would
    still show the stale marker saying the user closed it."""
    updates = edit.updates
    if updates.get("status") == "active" and bucket.get("metadata", {}).get("resolved"):
        updates["resolved"] = False
    if updates.get("status") == "active" and bucket.get("metadata", {}).get("closed_by"):
        updates["closed_by"] = ""


def _stamp_asked(c: _Call, edit: _Edit) -> None:
    """The "last asked" timestamp. Only web/loci.py passes mark_asked=True, and only at
    the moment the question is actually shown to the user — like closed_by, it is absent
    from the trace tool signature in server_tools/trace.py, so I cannot stamp this out of thin air."""
    if c.mark_asked:
        from core._when import now as _now_local
        edit.updates["last_asked"] = _now_local().isoformat()


# ------------------------------------------------------------
# The write and the receipt
# ------------------------------------------------------------

async def _write(c: _Call, edit: _Edit) -> str | None:
    """One write: the field updates plus every reviser, through update(), or through
    update_content_fragment() when the body is partially replaced as well.

    Both funnel into _update_locked(content=...) inside BucketManager, which posts to
    the embedding outbox; calling generate_and_store here as well would send the same
    content to the vector API twice."""
    write = dict(edit.updates)
    if edit.revisers:
        def revise(m: dict) -> dict:
            out: dict = {}
            for fn in edit.revisers:
                out.update(fn(m))
            edit.appended.clear()
            edit.appended.update(out)
            return out
        write["revise"] = revise

    if c.patch:
        try:
            patch_result = await rt.bucket_mgr.update_content_fragment(
                c.bucket_id,
                old_str=c.old_str,
                new_str=c.new_str,
                **write,
            )
        except _Refused as refused:
            return str(refused)
        if not patch_result.get("ok"):
            return _patch_refusal(c.bucket_id, patch_result)
    else:
        try:
            success = await rt.bucket_mgr.update(c.bucket_id, **write)
        except _Refused as refused:
            return str(refused)
        if not success:
            return f"修改失败: {c.bucket_id}"
    edit.updates.update(edit.appended)
    return None


def _patch_refusal(bucket_id: str, patch_result: dict) -> str:
    patch_error = patch_result.get("error")
    if patch_error == "not_found":
        return f"未找到记忆桶: {bucket_id}"
    if patch_error == "old_str_not_found":
        return (
            "未找到 old_str，正文未修改。先用 "
            f'recall(query="{bucket_id}") 按完整 bucket_id 逐字读出当前正文，'
            "再复制一段连续且逐字一致的原文重试。"
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


def _receipt(c: _Call, edit: _Edit) -> str:
    """One short sentence: the fields that changed, then the card / source notes, then
    pin's reminder."""
    updates = edit.updates
    _display_updates = {
        k: v for k, v in updates.items()
        if k not in ("content", "meaning_append", "meaning", "media_append", "media",
                     "sources", PROV_FIELD, "invalidation")
    }
    changed = ", ".join(f"{k}={v}" for k, v in _display_updates.items())
    if edit.confirmed_on:
        changed += ((", " if changed else "")
                    + f"依据变化已确认照留（{edit.confirmed_on}）→ 离开「依据变了的」，记录留在它自己身上")
    if c.patch:
        changed += (", content=已局部替换" if changed else "content=已局部替换")
    if "media_append" in updates:
        changed += (", " if changed else "") + f"media=已追加{len(updates['media_append'])}项"
    if "media" in updates:
        changed += (", " if changed else "") + f"media=整体替换({len(updates['media'])}项)"
    if "sources" in updates:
        changed += (", " if changed else "") + f"sources=现有{len(updates['sources'])}条"
    if updates.get("status") in ("resolved", "abandoned"):
        changed += f" → {resolved_hint(True)}"
    elif updates.get("status") == "active":
        changed += f" → {resolved_hint(False)}"
    out = f"已修改记忆桶 {c.bucket_id}: {changed}"
    if edit.card_note:
        out += "\n" + edit.card_note
    out += "".join("\n" + note for note in edit.source_notes)
    # pin's reminder trails the **success receipt**: it is not an error, the pin is
    # already on disk (see tools/_pin.py)
    if edit.pin_hint:
        out += "\n\n" + edit.pin_hint
    return out


# ------------------------------------------------------------
# trace
# ------------------------------------------------------------

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
    cue: Optional[dict | str] = None,
    card_of: Optional[str] = None,
    sources_append: Optional[list | dict | str] = None,
    invalidation: Optional[str] = "",
) -> str:
    # The first statement, so locals() is exactly the arguments as given.
    call = _Call(**locals())
    refusal = _read_call(call)
    if refusal:
        return refusal

    if not call.bucket_id or not call.bucket_id.strip():
        return "请提供有效的 bucket_id。"
    refusal = await _resolve_ids(call)
    if refusal:
        return refusal

    if call.restore:
        return await _restore(call)
    refusal = _check_patch(call)
    if refusal:
        return refusal
    if call.hard_delete or call.delete:
        return await _delete(call)

    bucket, meta, refusal = await _entry_in_use(call.bucket_id)
    if refusal:
        return refusal
    plan = _plan_quota(call, meta)
    edit = _Edit()
    async with AsyncExitStack() as quota_stack:
        if plan.need_pinned_lock:
            await quota_stack.enter_async_context(_quota_turn("pinned"))
        if plan.needs_high_importance_lock:
            await quota_stack.enter_async_context(_quota_turn("high_importance"))
        refusal = await _edit_in_turn(call, bucket, meta, plan, edit)
    if refusal:
        return refusal
    return _receipt(call, edit)
