"""
========================================
core/periods.py — the regrow/fold page's 日子 / 想法 column: what fold drew, and the
owner's edits on it
========================================

fold makes two things (core/_fold.py):

  a period (日子)        `__gist__` + `__大event__`; its one date is `when`,
                         "YYYY-MM-DD..YYYY-MM-DD" (end inclusive) or "YYYY-MM-DD.." while it
                         goes on. It keeps no roster: who is in it is whoever's date falls
                         in the range, worked out on the spot (the same definition as
                         _fold.span_members: on the timeline, not a gist, `ts_of`).
  a merged thought (想法) `__gist__` only; `cover` is its roster, and each covered entry
                         names it in `covered_by`. Its `when`, when written, is one day.

The panel lists both and lets the owner change them. Every change here goes through the
tools the model uses where one exists (trace: name, when, delete, folds_append), is
checked the way the model's call is, and leaves one line in core/_panel_changes.py, so
the model hears of it on his next tool call.

  overview(buckets)          {periods, thoughts}: every live, current period — its
                             title, span, how many memories fall in it, and the other
                             periods it overlaps — and every live, current merged thought
                             with its day and how many it covers.
  detail(buckets, id)        one of them with its members: a period's by date, a
                             thought's in cover order.
  edit_period(id, name, when)   rename it and/or move its span (trace name= / when=).
  delete_period(id)          put it away (trace delete): its members are not touched.
  merge_periods(keep, drop, name)  one span over both (trace when= on `keep`, renamed
                             when a name is given), `drop` put away.
  set_day(id, day)           a memory's own day (what a period's ➕ / ➖ change: it moves
                             into or out of the span), or a thought's day; "" clears a
                             thought's day. A clock time on the old day is kept. Not a
                             promise's or a wish's (its `when` is a deadline) nor a yearly
                             date's.
  change_cover(id, add, remove)  a merged thought's roster: added through trace
                             folds_append; taken off here, both sides, under each
                             entry's lock. At least one stays.

A refusal is a PeriodError with the words to show; nothing was written.

Exports: PeriodError · title_of · overview · detail · edit_period · delete_period ·
         merge_periods · set_day · change_cover
========================================
"""

from __future__ import annotations

import re
from datetime import datetime

from utils import is_telic

from . import _bigevent as _B
from . import _fold as _F
from . import _panel_changes as _PC
from . import _when as _w
from . import runtime as rt
from . import visibility as _V
from .profile import entry_label, short_id


class PeriodError(Exception):
    """A refusal, in the words the panel shows. Nothing was written."""


_STAMP_HEAD = re.compile(r"^[\d\- :]+")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── reading ──────────────────────────────────────────────────────────────────

def _id_of(b: dict) -> str:
    meta = b.get("metadata") or {}
    return str(meta.get("id") or b.get("id") or "")


def _current(meta: dict) -> bool:
    return (_V.state_of(meta) == _V.LIVE
            and not str(meta.get("superseded_by") or "").strip())


def _is_period(meta: dict) -> bool:
    return _B.is_big(meta)


def _is_thought(meta: dict) -> bool:
    return _F.is_gist(meta) and not _B.is_big(meta)


def title_of(meta: dict, content: str) -> str:
    """What a period or a thought is called on the column: a period's own name (a
    timestamp fold left in front of it dropped), else the line every list shows."""
    if _is_period(meta):
        name = _STAMP_HEAD.sub("", str(meta.get("name") or "")).strip()
        if name:
            return name[:60]
    return entry_label(meta, content)


def _span(meta: dict) -> tuple[str, str | None]:
    """(start day, end day or None while it goes on) as written; ("", None) when the
    span does not read."""
    m = _B.SPAN_RE.match(str(meta.get("when") or "").strip())
    if not m:
        return "", None
    return m.group(1), m.group(2)


def _bounds(meta: dict) -> tuple[datetime | None, datetime | None]:
    t0, t1, _err = _F.check_span(str(meta.get("when") or "").strip())
    return t0, t1


def _day_of(meta: dict) -> str:
    """A memory's day as the timeline reads it (`ts_of`: `when` first, else written)."""
    ts = _w.ts_of(meta)
    return _w.to_local(ts).date().isoformat() if ts else ""


def _thought_day(meta: dict) -> str:
    """A thought's own day: its `when` when it names one, else ""."""
    w = str(meta.get("when") or "").strip()
    stamp = _w.parse_stamp(w) if w and ".." not in w else None
    return _w.to_local(stamp).date().isoformat() if stamp else ""


def _timeline(buckets: list) -> list[tuple[datetime, dict]]:
    """Who can be in a period: _fold.span_members' definition, read off a listing."""
    out = []
    for b in buckets:
        meta = b.get("metadata") or {}
        if _F.is_gist(meta) or not _V.on_timeline(meta):
            continue
        ts = _w.ts_of(meta)
        if ts is not None:
            out.append((ts, b))
    out.sort(key=lambda x: x[0])
    return out


def _inside(ts: datetime, t0, t1) -> bool:
    return t0 is not None and ts >= t0 and (t1 is None or ts < t1)


def _overlap(a: tuple, b: tuple) -> bool:
    (a0, a1), (b0, b1) = a, b
    if a0 is None or b0 is None:
        return False
    return (a1 is None or b0 < a1) and (b1 is None or a0 < b1)


def _member(b: dict) -> dict:
    meta = b.get("metadata") or {}
    bid = _id_of(b)
    return {"id": bid, "short": short_id(bid),
            "text": entry_label(meta, str(b.get("content") or "")), "day": _day_of(meta)}


def _period_row(b: dict, timeline: list, periods: list) -> dict:
    meta = b.get("metadata") or {}
    bid = _id_of(b)
    start, end = _span(meta)
    bounds = _bounds(meta)
    overlaps = [{"id": _id_of(o), "text": title_of(o.get("metadata") or {}, str(o.get("content") or ""))}
                for o in periods
                if _id_of(o) != bid and _overlap(bounds, _bounds(o.get("metadata") or {}))]
    return {"id": bid, "short": short_id(bid), "kind": "period",
            "text": title_of(meta, str(b.get("content") or "")),
            "summary": entry_label(meta, str(b.get("content") or "")),
            "start": start, "end": end,
            "count": sum(1 for ts, _b in timeline if _inside(ts, *bounds)),
            "overlaps": overlaps}


def _thought_row(b: dict) -> dict:
    meta = b.get("metadata") or {}
    bid = _id_of(b)
    return {"id": bid, "short": short_id(bid), "kind": "thought",
            "text": title_of(meta, str(b.get("content") or "")),
            "day": _thought_day(meta), "count": len(_F.cover_ids(meta))}


def overview(buckets) -> dict:
    """{periods: [...], thoughts: [...]}: periods newest start first; thoughts newest day
    first, those with no day last."""
    buckets = list(buckets or [])
    periods = [b for b in buckets
               if _is_period(b.get("metadata") or {}) and _current(b.get("metadata") or {})]
    thoughts = [b for b in buckets
                if _is_thought(b.get("metadata") or {}) and _current(b.get("metadata") or {})]
    timeline = _timeline(buckets)
    prow = [_period_row(b, timeline, periods) for b in periods]
    prow.sort(key=lambda r: (r["start"], r["id"]), reverse=True)
    trow = [_thought_row(b) for b in thoughts]
    trow.sort(key=lambda r: (bool(r["day"]), r["day"], r["id"]), reverse=True)
    return {"periods": prow, "thoughts": trow}


def detail(buckets, bucket_id: str) -> dict | None:
    """One period or thought with `members`; None when it is neither (or not current)."""
    buckets = list(buckets or [])
    by_id = {_id_of(b): b for b in buckets}
    b = by_id.get(str(bucket_id or "").strip())
    meta = (b or {}).get("metadata") or {}
    if not b or not _current(meta):
        return None
    if _is_period(meta):
        periods = [o for o in buckets
                   if _is_period(o.get("metadata") or {}) and _current(o.get("metadata") or {})]
        timeline = _timeline(buckets)
        out = _period_row(b, timeline, periods)
        bounds = _bounds(meta)
        out["members"] = [_member(m) for ts, m in timeline if _inside(ts, *bounds)]
        return out
    if _is_thought(meta):
        out = _thought_row(b)
        out["members"] = [_member(by_id[c]) for c in _F.cover_ids(meta) if c in by_id]
        return out
    return None


# ── writing ──────────────────────────────────────────────────────────────────

async def _load(bucket_id: str) -> tuple[dict, dict]:
    b = await rt.bucket_mgr.get(str(bucket_id or "").strip())
    meta = (b or {}).get("metadata") or {}
    if not b or not _current(meta):
        raise PeriodError(f"找不到这一条（可能已经收起来或换了版本）：{bucket_id}")
    return b, meta


async def _period(bucket_id: str) -> tuple[dict, dict]:
    b, meta = await _load(bucket_id)
    if not _is_period(meta):
        raise PeriodError("这条不是一段日子")
    return b, meta


async def _trace(**kwargs) -> str:
    """trace as the panel's hand (closed_by="user"); a reply that is not a done edit is a
    refusal in trace's own words."""
    from tools.trace.core import trace_core
    msg = str(await trace_core(closed_by="user", **kwargs))
    if not (msg.startswith("已修改记忆桶") or msg.startswith("已将记忆桶存入档案")):
        raise PeriodError(msg)
    return msg


def _span_words(start: str, end: str | None) -> str:
    return f"{start[5:]}..{end[5:] if end else '至今'}"


def _now() -> datetime:
    return _w.now()


async def edit_period(bucket_id: str, *, name: str | None = None,
                      when: str | None = None) -> dict:
    """Rename a period and/or move its span. `when` is "start..end" or "start.."."""
    b, meta = await _period(bucket_id)
    old_title = title_of(meta, str(b.get("content") or ""))
    old_span = _span(meta)
    kwargs = {}
    name = (name or "").strip()
    when = (when or "").strip()
    if name and name != old_title:
        kwargs["name"] = name
    if when and when != str(meta.get("when") or "").strip():
        err = _F.check_span(when)[2]
        if err:
            raise PeriodError(err)
        kwargs["when"] = when
    if not kwargs:
        raise PeriodError("没有要改的")
    await _trace(bucket_id=bucket_id, **kwargs)
    lines = []
    if "name" in kwargs:
        lines.append(f"日子「{old_title}」改了名字，现在叫「{name}」")
    if "when" in kwargs:
        new = _B.SPAN_RE.match(when)
        lines.append(f"日子「{name or old_title}」改了起止：{_span_words(*old_span)} → "
                     f"{_span_words(new.group(1), new.group(2))}")
    for line in lines:
        _PC.record(rt.bucket_mgr.base_dir, line, [bucket_id], _now())
    return {"ok": True, "id": bucket_id}


async def delete_period(bucket_id: str) -> dict:
    """Put a period away. The memories in its span are not touched."""
    b, meta = await _period(bucket_id)
    title = title_of(meta, str(b.get("content") or ""))
    await _trace(bucket_id=bucket_id, delete=True)
    _PC.record(rt.bucket_mgr.base_dir, f"日子「{title}」删掉了（里面的记忆都没动）",
               [bucket_id], _now())
    return {"ok": True, "id": bucket_id}


async def merge_periods(keep: str, drop: str, *, name: str | None = None) -> dict:
    """`keep` takes the span over both (an ongoing one keeps it ongoing) and, when given,
    the new name; `drop` is put away."""
    if str(keep or "").strip() == str(drop or "").strip():
        raise PeriodError("合并要两段不同的日子")
    kb, kmeta = await _period(keep)
    db, dmeta = await _period(drop)
    (k0, k1), (d0, d1) = _span(kmeta), _span(dmeta)
    if not k0 or not d0:
        raise PeriodError("有一段的起止读不出来，先把它的起止改对再合")
    start = min(k0, d0)
    end = None if (k1 is None or d1 is None) else max(k1, d1)
    when = f"{start}..{end or ''}"
    keep_title = title_of(kmeta, str(kb.get("content") or ""))
    drop_title = title_of(dmeta, str(db.get("content") or ""))
    name = (name or "").strip()
    kwargs = {"when": when} if when != str(kmeta.get("when") or "").strip() else {}
    if name and name != keep_title:
        kwargs["name"] = name
    if kwargs:
        await _trace(bucket_id=keep, **kwargs)
    await _trace(bucket_id=drop, delete=True)
    _PC.record(rt.bucket_mgr.base_dir,
               f"日子「{keep_title}」和「{drop_title}」合成了「{name or keep_title}」"
               f"（{_span_words(start, end)}）", [keep, drop], _now())
    return {"ok": True, "id": keep}


def _with_clock(day: str, old_when: str) -> str:
    """`day` carrying the old `when`'s local clock time, when it had one."""
    old = str(old_when or "").strip()
    if len(old) <= 10 or ".." in old:
        return day
    stamp = _w.parse_stamp(old)
    if stamp is None:
        return day
    return f"{day} {_w.to_local(stamp).strftime('%H:%M')}"


async def set_day(bucket_id: str, day: str) -> dict:
    """A memory's day, or a merged thought's ("" clears a thought's)."""
    b, meta = await _load(bucket_id)
    day = str(day or "").strip()
    if day and not _DAY.match(day):
        raise PeriodError("日期写成 2026-10-14 这样")
    label = entry_label(meta, str(b.get("content") or ""))
    if _is_thought(meta):
        old = _thought_day(meta)
        if day == old:
            raise PeriodError("日期没变")
        if day:
            await _trace(bucket_id=bucket_id, when=day)
        elif not await rt.bucket_mgr.update(bucket_id, when=None):
            raise PeriodError("没清掉，再试一次")
        line = (f"想法「{label}」清掉了日期" if not day
                else f"想法「{label}」补上了日期 {day}" if not old
                else f"想法「{label}」的日期 {old} → {day}")
    elif _V.on_timeline(meta) and not _F.is_gist(meta):
        if not day:
            raise PeriodError("记忆的日期不能空着")
        # A promise's or a wish's `when` is its deadline, and a yearly date's is the day
        # it comes round: neither is the day something happened.
        if is_telic(meta) or meta.get("recurrence"):
            raise PeriodError("这条的日期是期限或者每年的日子，不是哪天发生的，不从这儿改")
        old = _day_of(meta)
        if day == old:
            raise PeriodError("日期没变")
        await _trace(bucket_id=bucket_id, when=_with_clock(day, str(meta.get("when") or "")))
        line = f"记忆「{label}」的日期 {old} → {day}"
    else:
        raise PeriodError("这条的日期不从这儿改")
    _PC.record(rt.bucket_mgr.base_dir, line, [bucket_id], _now())
    return {"ok": True, "id": bucket_id}


async def change_cover(bucket_id: str, *, add=(), remove=()) -> dict:
    """Add to and take off a merged thought's roster. At least one entry stays."""
    b, meta = await _load(bucket_id)
    if not _is_thought(meta):
        raise PeriodError("这条不是并起来的想法")
    have = _F.cover_ids(meta)
    add = [str(x).strip() for x in add or [] if str(x).strip() and str(x).strip() not in have]
    remove = [str(x).strip() for x in remove or [] if str(x).strip() in have]
    if not add and not remove:
        raise PeriodError("没有要改的")
    if not [c for c in have if c not in remove] and not add:
        raise PeriodError("至少要留一条；整条不要的话，跟他说，让他自己处理")
    label = entry_label(meta, str(b.get("content") or ""))
    if add:
        await _trace(bucket_id=bucket_id, folds_append=add)
    if remove:
        gone = set(remove)

        def roster(m: dict) -> dict:
            return {"cover": [c for c in _F.cover_ids(m) if c not in gone]}

        def release(m: dict) -> dict:
            left = [g for g in _F._covered_list(m) if g != bucket_id]
            return {"covered_by": left or None}

        if not await rt.bucket_mgr.update(bucket_id, revise=roster):
            raise PeriodError("名单没改成，再试一次")
        for cid in remove:
            await rt.bucket_mgr.update(cid, revise=release)
    said = "、".join(w for w in (f"去掉了 {len(remove)} 条" if remove else "",
                                 f"加了 {len(add)} 条" if add else "") if w)
    _PC.record(rt.bucket_mgr.base_dir, f"想法「{label}」{said}", [bucket_id, *add, *remove],
               _now())
    return {"ok": True, "id": bucket_id}
