# -*- coding: utf-8 -*-
"""
========================================
tools/_fold.py — the bones of fold / gist
========================================

**One action, `fold` (fold it up; what is underneath is still there) · one product, a
`gist` · one inverse, `unfold` (drill down).**
📌 `gist` / `verbatim` are the proper terms from fuzzy-trace theory (Brainerd & Reyna),
the same theory the forgetting curve came from.
📌 The name was chosen for the analogy: **it works like a folder.**

------------------------------------------------------------
Three ways of drawing the circle, one action
------------------------------------------------------------
| what gets covered | what the caller hands in | what it used to be called |
|---|---|---|
| a new version of one insight | one id | `regrow` |
| a set of fragments | a set of ids | (new) condensing |
| a stretch of days | a time range | `grow(kind="big")` |

🔴 **`regrow` is not "do we still need it"; it always was this same action with n=1.**
The difference between "I changed my mind" and "I summarised these" lives **in the
body text**, not in the action.
So both old entry points stay and both map down to here (following the precedent set by
trace's resolved -> status).

------------------------------------------------------------
🔴 The final form: **draw a circle and write a name on it; only merged thoughts keep books**
------------------------------------------------------------
**In eight words: events use time, minds use a snapshot.**

| way of circling | what is persisted | who gets suppressed |
|---|---|---|
| period (`when=start..end`, the event half) | **name + range only** (`when`) | **nobody** |
| snapshot (`cover=[ids]`, the mind half) | the roster (`cover` / `covered_by`) | whoever is named stops surfacing on its own |

🔴 **A period is a pure naming layer**: it writes no `cover`, touches not one
   `covered_by`, collapses no rows, and evicts nothing from any pool.
   Who is inside a period = **whose date falls in the range, computed on the spot**
   (`span_members()`) — so backfilled entries join automatically, crossing and nesting
   hold by construction, and moving a boundary is just `regrow` with a new `when`
   (boundaries were always fuzzy anyway).
   Compression is the collapse layer's job; a period only **says the name out loud**,
   laid on top when `recall` covers that stretch of time (rule 5 of the nine in
   `_bigevent.py`, returning unchanged: **cover, do not replace**).

📌 **Why this was reworked — recorded so it does not get copied back in**: the first
   version had the time-circle resolve its range into a frozen list of ids
   (`resolve_span_ids`, since retired). That was copied from consolidation/ACP — **they
   compress and replace, so they have no choice but to freeze the roster**; here
   **nothing is ever deleted, things are only given names**, so the bookkeeping half was
   copied for nothing, and it quietly turned "who was in this stretch of days" from a
   fact into a snapshot. The warning in section 7 had already been written down, and
   half of it got copied anyway.

🔪 **"Cover a set of events" was cut entirely**: a through-line is something you look at
   with `recall(query=)` — search for the thing and the whole run of memories about it
   comes up. **"Fold a single entry" was withdrawn too**: that job belongs to regrow (see
   the epitaph in tools/fold).

------------------------------------------------------------
Three hard rules (rule 1 narrowed to mind in the final form)
------------------------------------------------------------
1. 🔴 **`cover` always stores a definite list of ids** (the snapshot half). A time range
   is **no longer** resolved into ids — a period keeps no books; see above.
2. 🔴 **`from` and `cover` are two parameters with different meanings, and must never be
   merged**:
   `from`  = which entries I grew **out of** (they **go on living independently**)
   `cover` = which entries I **cover** (they **stop surfacing on their own**)
   ⚠️ Saving a parameter by letting `kind` decide how `from` is read would be "one
   parameter doing two jobs" — precisely the thing this round set out to eliminate.
3. **You can cover things that are already covered** (recursively). **The number of
   layers grows out of the days themselves; T1/T2/T3 are not pre-declared.**

------------------------------------------------------------
Persistence: both ends are written (denormalised) on purpose — **in the snapshot half only**
------------------------------------------------------------
· the gist bucket:   `cover: [id, ...]`
· each covered entry: `covered_by: [gist_id, ...]` — **a list, not a single value**.
  🔴 This was caught and reverted (rule 6 of the nine big-event rules): one small memory
  can be covered by two through-lines at once — that is not a conflict, it is the truth.
  Writing it as a single value quietly turned crossing into exclusive ownership.
  Explicitly covering something already covered **stacks** (append); nobody evicts
  anybody. Single-value strings in old data are still accepted on the read side.
Why write both ends: the surfacing pools filter by **rescanning the whole store every
round**, and querying the single field `covered_by` is enough to do it — no reverse
index has to be maintained. The price of the duplicate is one update, far cheaper than
an index.

🔴 **On the read side, "is it covered" = either `covered_by` or `superseded_by` is
non-empty.** `superseded_by` is the older field regrow has always written (a version
chain, single-valued by nature) and there is plenty of it on disk, so it is kept for
read compatibility.

------------------------------------------------------------
Exactly how far "stops surfacing on its own" reaches — **only the snapshot half suppresses**
------------------------------------------------------------
Excluded from: breath's sudden-recollection pool · the dream candidate pool ·
       the muse candidate pool
NOT excluded from: recall search (a query still hits them; they are not dead) ·
       direct lookup by id · drilling down ·
       **the statistics in the browse view** (counts / rooms / tags / V·A all intact —
       what collapses is a row, not a number)
🔴 **A period occupies not one cell of that table**: it writes no `covered_by`, so every
   pool above behaves as though it did not exist. A memory sitting inside a period still
   surfaces on its own and still enters the dream candidate pool — **it gained a name and
   lost nothing.**

Exports: GIST_TAG · SPAN_HELP · is_covered() · covers_of() · cover_ids() · is_gist()
         check_span() · span_members() · save_gist() · format_report()
🔪 `resolve_span_ids()` is **retired** (a period keeps no books). Its read-side half
   lives on under the name `span_members()` — **computed on the spot, never persisted.**
========================================
"""

import asyncio
from datetime import datetime, timedelta

from tools import _runtime as rt
from ._bigevent import BIGEVENT_TAG, SPAN_RE

# The system tag for a gist. The ones produced by the time-circle carry `__大event__`
# **as well**, so none of the existing big-event machinery (laid over a recalled stretch
# of time, kept out of the timeline, versioned with regrow) needs a single line changed.
GIST_TAG = "__gist__"


def _is_live(bucket_id: str) -> bool:
    """A cover only counts while the one covering is in the active store. Without a
    store (unit tests that never started one) every recorded cover counts."""
    mgr = rt.bucket_mgr
    if mgr is None or not hasattr(mgr, "is_live"):
        return True
    return mgr.is_live(bucket_id)


def is_covered(meta: dict) -> bool:
    """Is this one covered by something still in the active store?

    🔴 Either field counts: `covered_by` (written by fold) and `superseded_by` (the
    older field regrow has always written; plenty of it on disk, kept read-only for
    compatibility).
    🔴 A cover whose gist / newer version was archived or deleted no longer counts, so
    the entry surfaces again. The fields on disk are left alone: restoring the gist
    from the archive covers it again with nothing to rewrite.
    """
    return bool(covers_of(meta))


def _covered_list(meta: dict) -> list[str]:
    """The `covered_by` roster itself (superseded_by excluded). Single-value strings in
    old data are accepted too."""
    raw = (meta or {}).get("covered_by")
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def covers_of(meta: dict) -> list[str]:
    """Every live gist id covering this entry (they may cross — rule 6). The
    `covered_by` roster plus `superseded_by` for compatibility; ids no longer in the
    active store are dropped (see `is_covered`)."""
    if not isinstance(meta, dict):
        return []
    out = _covered_list(meta)
    sup = str(meta.get("superseded_by") or "").strip()
    if sup and sup not in out:
        out.append(sup)
    return [cid for cid in out if _is_live(cid)]


def cover_ids(meta: dict) -> list[str]:
    """Who a gist covers. Persisted as a list; old data and hand-edits using a
    comma-separated string are accepted too (liberal in, strict out)."""
    if not isinstance(meta, dict):
        return []
    raw = meta.get("cover")
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(s).strip() for s in raw if str(s).strip()]
    return []


def is_gist(meta: dict) -> bool:
    tags = [str(t) for t in (meta.get("tags") or [])]
    return GIST_TAG in tags or BIGEVENT_TAG in tags


SPAN_HELP = ('when 要写成起止："2026-07-31..2026-08-05"；还在进行中就把止留空：'
             '"2026-07-31.."。（起止一次填完，别指望第二步回来补——那步照样会忘。）')


def check_span(span: str) -> tuple[datetime | None, datetime | None, str]:
    """`start..end` -> `[t0, t1)` (`t1=None` means still ongoing). Returns
    `(t0, t1, error message)`.

    Format and ordering checks only, **the store is never touched**. These two boundaries
    are the only thing a period persists (inside `when`; there is no second field), which
    makes this its one and only gate.
    """
    from . import _when as _w

    m = SPAN_RE.match(str(span or "").strip())
    if not m:
        return None, None, SPAN_HELP
    # These two lines used to call through bare, and this is **the only gate a period
    # passes through**. Getting past `SPAN_RE` only proves the shape is right
    # (`2026-13-45..` is a perfectly legal shape), so a day that does not exist would
    # raise here instead of coming back with "that date is not a real day".
    t0 = _w.parse_date_or_none(m.group(1))
    t1raw = _w.parse_date_or_none(m.group(2)) if m.group(2) else None
    if t0 is None or (m.group(2) and t1raw is None):
        bad_day = m.group(1) if t0 is None else m.group(2)
        return None, None, f"日历上没有 {bad_day} 这一天。{SPAN_HELP}"
    t1 = t1raw + timedelta(days=1) if t1raw else None
    if t1 is not None and t1 <= t0:
        return None, None, f"止（{m.group(2)}）在起（{m.group(1)}）前面了。"
    return t0, t1, ""


async def span_members(t0: datetime | None, t1: datetime | None) -> list[str]:
    """**Computed on the spot**: which memories have a date inside `[t0, t1)` as of right
    now (ordering by time ascending; newest-first does not matter here).

    🔴 **This roster is never persisted** — a period stores only its name and its range.
    It exists to **report a sense of scale** ("N entries in the range right now") and to
    **drill down** (spreading a period open when it is looked up by id).
    It is recomputed every time, which is what makes backfilled entries join
    automatically, crossing and nesting hold by construction, and a changed `when` swap
    the membership immediately.

    It uses the same definition as recall's browse path (`_visible` + `_ts_of`): whatever
    is visible on screen for that stretch of time is exactly what should be inside the
    period, and the two disagreeing is by definition a bug.
    ⚠️ Periods and gists are not members themselves (`_visible` already excludes
    `__大event__`, and `__gist__` is refused here as well — the members of a period are
    memories, not other names).
    """
    # Lazy import: recall.core imports this module (its read side needs is_covered), and
    # importing both ways at module level goes in circles
    from tools.recall.core import _visible, _ts_of

    if t0 is None:
        return []
    try:
        buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.warning(f"时期成员现场算失败: {e}")
        return []
    out: list[tuple[datetime, str]] = []
    for b in buckets:
        meta = b.get("metadata", {}) or {}
        if is_gist(meta) or not _visible(meta):
            continue
        ts = _ts_of(meta)      # `by` was cut; one definition remains (`when` first, `created` as fallback)
        if ts is None or ts < t0:
            continue
        if t1 is not None and ts >= t1:
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if bid:
            out.append((ts, bid))
    out.sort(key=lambda x: x[0])
    return [bid for _ts, bid in out]


async def save_gist(text: str, room: str, v: float, a: float,
                    cover: list[str], *, when: str = "", from_ids: list[str] | None = None,
                    supersedes: str = "", test_data: bool = False) -> tuple[str, dict]:
    """The moment it actually hits disk. **All three entry points (fold / regrow /
    grow(kind="big")) share this one function.**

    🔴 Constitutional: `text` was written by the caller and is persisted verbatim — **not
    one word of it passes through a model.** There is no LLM summarisation path in this
    function and there must never be one. Background backfill only adds tags, a summary
    and a name; that is derived metadata, not the body.

    supersedes: for n=1 (a version change) pass the old id, and the version chain
      supersedes / superseded_by plus dont_surface gets written.
      Why only at n=1: a version chain means "the previous version of this same entry",
      and at n>=2 there is no such thing as "the previous version".
      regrow's externally visible behaviour depends on this staying exactly as it is.

    🔴 **The moment `when` is given (= this is a period), `cover` is cleared right here**
      — a period stores only a name and a range, keeps no books and suppresses nothing.
      It is enforced here rather than only at the entry points because all three entry
      points persist through this function: **one fewer place to decide is one fewer
      opening for bookkeeping to creep back in.**

    Returns (new_id, report dict). The report carries how many cover entries were really
    written, which ones were stacked on top of an existing cover, and which ones failed —
    **a failure is never swallowed**: if the body landed but the chain did not, the
    caller has to know.
    """
    cover = [str(x).strip() for x in (cover or []) if str(x).strip()]
    if when:
        cover = []          # a period is a pure naming layer; this line is its foundation, do not remove it
    tags = [GIST_TAG] + ([BIGEVENT_TAG] if when else [])
    # ---- 🔴 a version change keeps the profile page's job at the door ----
    # The door finds its page by the tag alone and skips a superseded page, so a new
    # version created without the tag leaves the door's cell empty.
    # The pin is carried further down with an update; the tag goes on at creation
    # instead, because backfill merges into whatever tags exist at that moment.
    carries_profile = False
    if supersedes:
        from .profile import _PROFILE_TAG      # lazy: core.profile imports this module
        old_page = await rt.bucket_mgr.get(supersedes)
        old_tags = [str(t) for t in (((old_page or {}).get("metadata") or {}).get("tags") or [])]
        if _PROFILE_TAG in old_tags:
            tags.append(_PROFILE_TAG)
            carries_profile = True
    new_id = await rt.bucket_mgr.create(
        content=text,
        tags=tags,                       # set at create time: backfill merges and never replaces, so this cannot be washed off
        importance=5,                    # neutral placeholder; importance is retired and no longer assigned by hand
        domain=["未分类"],
        valence=v,
        arousal=a,
        name=None,
        from_ids=",".join(from_ids or []),
        source_tool="fold",
        room=room,
        when=when,                       # the time-circle: the span goes into the existing `when`, there is no second field
        test_data=test_data,
    )

    report: dict = {"cover": cover, "叠盖": [], "没写上": [], "链没写全": False,
                    "接着当门口": carries_profile}

    # ---- write both ends ----
    # 🔴 **A version chain does not count as bookkeeping**: when a period changes version
    #    `cover` is empty (cleared above), but the old entry **must** get
    #    superseded_by/dont_surface, or it would surface over that stretch of days
    #    alongside the new one.
    #    So what gets written is cover ∪ {supersedes}, and only the cover part writes
    #    `covered_by`.
    targets = list(cover) + ([supersedes] if supersedes and supersedes not in cover else [])
    ok_cover = await rt.bucket_mgr.update(new_id, cover=cover) if cover else True
    for cid in targets:
        old = await rt.bucket_mgr.get(cid)
        old_meta = (old or {}).get("metadata", {}) or {}
        kwargs: dict = {}
        if cid in cover:
            old_covers = _covered_list(old_meta)
            if old_covers and new_id not in old_covers:
                # Rule 6: crossing is a fact, not a conflict -> explicitly covering
                # something already covered **stacks** (append); nobody evicts anybody,
                # both layers stay visible and both can be drilled into. (This replaced a
                # first implementation in which the new cover took ownership.)
                report["叠盖"].append((cid, list(old_covers)))
            kwargs["covered_by"] = old_covers + ([new_id] if new_id not in old_covers else [])
        if supersedes and cid == supersedes:
            # Only the version-change case writes the chain and dont_surface (regrow's
            # long-standing behaviour, untouched)
            kwargs["superseded_by"] = new_id
            kwargs["dont_surface"] = True
        if not await rt.bucket_mgr.update(cid, **kwargs):
            report["没写上"].append(cid)
    if supersedes:
        ok_sup = await rt.bucket_mgr.update(new_id, supersedes=supersedes)
        report["链没写全"] = not (ok_sup and ok_cover and supersedes not in report["没写上"])
        # ---- 🔴 a version change has to carry the pin across (bug ①) ----
        # The old failure mode: regrowing a pinned rule **silently unpinned it**.
        #   The new version is a freshly created bucket (unpinned by default) while the
        #   old one is pushed down by dont_surface — put those together and the line at
        #   the door is simply **gone**, with **no error and not a word of warning**.
        #   This was walked into three times in a single day; it only survived because
        #   each change happened to get eyeballed right after.
        # So: if the old version was pinned, the new one stays pinned. **The quota is net
        # zero** (one pinned, one unpinned), so check_pinned_quota is deliberately not
        # called — that gate exists to stop an extra slot being taken, and none is.
        # Order matters: **pin the new one first, then unpin the old**. The other way
        # round leaves the door empty for an instant; and if the second step fails, it is
        # far better to end up with both pinned (visible, and it will get noticed) than
        # with neither pinned — which is this exact bug all over again.
        try:
            old_b = await rt.bucket_mgr.get(supersedes)
            old_meta = (old_b or {}).get("metadata", {}) or {}
            if old_meta.get("pinned"):
                report["接着钉"] = bool(await rt.bucket_mgr.update(new_id, pinned=True))
                await rt.bucket_mgr.update(supersedes, pinned=False)
        except Exception as e:
            report["接着钉"] = False
            try:
                rt.logger.warning(f"regrow carry-pin failed {supersedes}->{new_id}: {e}")
            except Exception:
                pass
    elif not ok_cover:
        report["链没写全"] = True

    # The covered entries have in effect "been recalled once more" (the same reasoning
    # behind regrow touching its sources)
    try:
        await rt.bucket_mgr.touch_many(list(from_ids or []))
    except Exception:
        pass

    # Metadata is filled in afterwards: tags, summary and naming go to the background.
    # keep_va=True — v/a were assigned by hand, and backfill never touches them
    from tools.grow.rooms_path import _backfill_batch
    kind = "big" if when else ("mind" if room.startswith("MIND") else "event")
    asyncio.create_task(_backfill_batch([(new_id, text, kind)]))
    return new_id, report


def format_report(report: dict) -> str:
    """Turn the persistence report into a human-readable tail (empty string when there is
    nothing worth saying)."""
    out = []
    if report["叠盖"]:
        out.append("ℹ️ 其中 " + str(len(report["叠盖"])) + " 条已被别的 gist 盖着，现在**叠着盖**（交叉）："
                   + "、".join(f"{cid}（已有 {'、'.join(olds[:3])}）" for cid, olds in report["叠盖"][:5])
                   + "。两层都在，都钻得到。")
    if report["没写上"]:
        out.append("⚠️ 这几条的 covered_by 没写上（归档区？）："
                   + "、".join(report["没写上"][:5]) + "——把这条报给AI查。")
    if report["链没写全"]:
        out.append("⚠️ cover/版本链有一半没写上——把这条报给AI查。")
    # Carrying the pin across a version change (bug ①): say so when it worked, and shout
    # when it did not — otherwise it is another silent unpinning. (The `接着钉` key.)
    if report.get("接着钉") is True:
        out.append("📌 旧版是钉着的，新版**接着钉**（门口那行没断），旧版已摘钉。")
    elif report.get("接着钉") is False:
        out.append("🔴 旧版是钉着的，但新版**没钉上**——门口那行现在是空的，"
                   "手动 trace(bucket_id=新版id, pinned=1) 补上。")
    if report.get("接着当门口"):
        out.append("📇 旧版是门口那张纸（名字页），新版**接着当**——下次 breath 门口显示的就是新版。")
    return "\n".join(out)
