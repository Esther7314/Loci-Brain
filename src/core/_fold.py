# -*- coding: utf-8 -*-
"""
========================================
core/_fold.py — the bones of fold / gist
========================================

**One action, `fold` (fold it up; what is underneath is still there) · one product, a
`gist` · one inverse, `unfold` (drill down).**
📌 `gist` / `verbatim` are the proper terms from fuzzy-trace theory (Brainerd & Reyna),
the same theory the forgetting curve came from.
📌 The name was chosen for the analogy: **it works like a folder.**

------------------------------------------------------------
Three ways of drawing the circle, one action
------------------------------------------------------------
| what gets covered | what the caller hands in | the tool that does it |
|---|---|---|
| a new version of one insight | one id | `regrow` |
| a set of fragments | a set of ids | `fold(cover=...)` |
| a stretch of days | a time range | `fold(when=...)` |

🔴 **`regrow` is this same action with n=1.**
The difference between "I changed my mind" and "I summarised these" lives **in the
body text**, not in the action.

------------------------------------------------------------
🔴 **Draw a circle and write a name on it; only merged thoughts keep books**
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

📌 **Why a period keeps no roster — written down so one does not get copied in**:
   consolidation/ACP freeze a list of ids because **they compress and replace, so they
   have no choice**; here **nothing is ever deleted, things are only given names**, so a
   frozen roster would buy nothing and would quietly turn "who was in this stretch of
   days" from a fact into a snapshot.

🔪 **There is no "cover a set of events"**: a through-line is something you look at
   with `recall(query=)` — search for the thing and the whole run of memories about it
   comes up. **Folding a single entry is regrow's job**, not fold's.

------------------------------------------------------------
Three hard rules (rule 1 is the mind half)
------------------------------------------------------------
1. 🔴 **`cover` always stores a definite list of ids** (the snapshot half). A time range
   is **never** resolved into ids — a period keeps no books; see above.
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
         user_tags() · carried_state() · check_span() · span_members() · save_gist()
         format_report()
🔪 `resolve_span_ids()` is **retired** (a period keeps no books). Its read-side half
   lives on under the name `span_members()` — **computed on the spot, never persisted.**
========================================
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from . import runtime as rt
from utils import is_closed, parse_bool, prov_targets
from ._bigevent import BIGEVENT_TAG, SPAN_RE
from .bucket_manager import V2_FIELDS

# The system tag for a gist: what fold (n >= 2) produces. The ones produced by the
# time-circle carry `__大event__` **as well**, so none of the existing big-event machinery
# (laid over a recalled stretch of time, kept out of the timeline, versioned with regrow)
# needs a single line changed.
# 🔴 A new version of one memory (regrow, n=1) is **not** a gist: it is the same memory
#    in new words, and what treats gists apart (a period's members leave them out; muse
#    and dreams leave them out while they are covered, `_muse._is_utility_record`) must
#    keep seeing it as the memory. Only a chain that **began** as a gist keeps the tag
#    across versions (`_began_as_gist`).
GIST_TAG = "__gist__"

# Tags the machinery writes for itself: what kind of record this is (`__…__`), or what the
# backfill noticed (`aspect:` · `疑似同件:` · `相似认知:`). None of them says what the
# memory is about, so a new version does not inherit them — its kind is decided afresh in
# save_gist and the backfill looks at the new body again.
_MACHINE_TAG_PREFIXES = ("__", "aspect:", "疑似同件:", "相似认知:")

# What a new version inherits from the one it replaces: everything that says where the
# entry stands in the world — its time, whether it is wanted and by whom, how heavily,
# whether it is closed, who it is about, when it was last asked about or dreamt of, why
# it was worth keeping and what it has meant since (`meaning`, whose own vector update()
# regenerates for the new id). What describes the text itself (name, summary, the
# backfill's tags, the vector) is computed again from the new body, and the version chain
# (cover / covered_by / supersedes / superseded_by / dont_surface) is written fresh by
# save_gist. Attachments (`media`) come across separately: they are files to copy under
# the new id, not a field to repeat. What the old version held **exclusively** — the pin,
# the anchor — moves rather than copies, after the chain (see below).
_CARRIED_FIELDS = ("when", "status", "weight", "subjects", "why_remembered", "meaning",
                   "last_asked", "closed_by", "last_dreamt", *V2_FIELDS)


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


def user_tags(meta: dict) -> list[str]:
    """The tags a person or the model put on this memory, with the machinery's own left
    out (`_MACHINE_TAG_PREFIXES`, and the mark the panel writes when a person corrected
    an entry — that mark names one edit, not the memory)."""
    from .profile import _EDITED_BY_USER_TAG      # lazy: core.profile imports this module

    out: list[str] = []
    for raw in (meta or {}).get("tags") or []:
        t = str(raw).strip()
        if not t or t.startswith(_MACHINE_TAG_PREFIXES) or t == _EDITED_BY_USER_TAG:
            continue
        if t not in out:
            out.append(t)
    return out


# What the backfill reads off the wording itself rather than off where the entry stands: a
# new version has new words, so these are read again from them, not carried — but only
# when the backfill is who wrote them (listed in `backfilled`). The same slot set by the
# main model is what it said and comes across like the rest.
_READ_OFF_WORDING = ("looks_like_promise", "internally_generated", "recurrence", "evidential")


def carried_state(old_meta: dict, *, period: bool = False) -> dict:
    """What `update()` has to write on a new version so that it stands where the old one
    stood (`_CARRIED_FIELDS`, those that are set). A period's `when` is left out: the span
    went in at creation. An entry closed only by the older `resolved` boolean comes across
    as `status="resolved"`, the field every write path uses now. A slot the backfill read off
    the old words (`_READ_OFF_WORDING`) is left for the backfill to read off the new ones.
    A deliberate `dont_surface` comes across: the old version's own one only means "keep
    this out of sight" while nothing has replaced it yet."""
    old_meta = old_meta or {}
    read_off = {str(f) for f in old_meta.get("backfilled") or []} & set(_READ_OFF_WORDING)
    out: dict = {}
    for k in _CARRIED_FIELDS:
        if (k == "when" and period) or k in read_off:
            continue
        v = old_meta.get(k)
        if v is None or v == "" or v == []:
            continue
        out[k] = v
    if out.get("backfilled"):
        out["backfilled"] = [f for f in out["backfilled"] if str(f) not in read_off]
        if not out["backfilled"]:
            del out["backfilled"]
    if "status" not in out and is_closed(old_meta):
        out["status"] = "resolved"
    if (parse_bool(old_meta.get("dont_surface"), default=False)
            and not str(old_meta.get("superseded_by") or "").strip()):
        out["dont_surface"] = True
    return out


def carried_media(old_meta: dict) -> list[dict]:
    """The old version's attachments, as `update(media=...)` wants them: it persists each
    item again under the new id (a copy of the bytes, so neither version can lose the
    file when the other is removed). Stored paths are relative to the vault, so they are
    made absolute here; an item whose file is gone is left out rather than failing the
    whole carry."""
    items = (old_meta or {}).get("media") or []
    if not isinstance(items, list):
        return []
    vault = getattr(getattr(rt.bucket_mgr, "media_store", None), "vault_dir", None)
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not str(item.get("path") or "").strip():
            continue
        path = Path(str(item["path"]))
        if not path.is_absolute() and vault is not None:
            path = Path(vault) / path
        if not path.is_file():
            continue
        entry = {"path": str(path)}
        for key in ("title", "type", "note"):
            if item.get(key):
                entry[key] = item[key]
        out.append(entry)
    return out


async def _began_as_gist(meta: dict) -> bool:
    """Whether this version chain started life as a gist (fold's product), read off its
    first version. The tag alone cannot say: every new version written before this rule
    carries it, including plain re-versions of ordinary memories. The walk follows
    `supersedes` and stops at a missing or archived predecessor, judging the last
    version it could read."""
    cur = meta or {}
    seen: set[str] = set()
    while True:
        prev = str(cur.get("supersedes") or "").strip()
        if not prev or prev in seen or len(seen) >= 64:
            break
        seen.add(prev)
        b = await rt.bucket_mgr.get(prev)
        if not b:
            break
        cur = (b.get("metadata") or {})
    return GIST_TAG in [str(t) for t in (cur.get("tags") or [])]


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
    # Not called bare: this is **the only gate a period passes through**. Getting past `SPAN_RE` only proves the shape is right
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

    It uses the same definition as recall's browse path (`visibility.on_timeline` +
    `_when.ts_of`): whatever is visible on screen for that stretch of time is exactly what
    should be inside the period, and the two disagreeing is by definition a bug.
    ⚠️ Periods and gists are not members themselves (`on_timeline` already excludes
    `__大event__`, and `__gist__` is refused here as well — the members of a period are
    memories, not other names).
    """
    # Lazy import: the gate imports this module (its read side needs is_covered), and
    # importing both ways at module level goes in circles
    from . import _when as _w
    from .visibility import on_timeline

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
        if is_gist(meta) or not on_timeline(meta):
            continue
        ts = _w.ts_of(meta)    # the one definition: `when` first, `created` as fallback
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
                    cover: list[str], *, when: str = "", prov: list[dict] | None = None,
                    sources: list[dict] | None = None,
                    supersedes: str = "", test_data: bool = False) -> tuple[str, dict]:
    """The moment it actually hits disk. **All three entry points (fold / regrow /
    grow(kind="big")) share this one function.**

    🔴 Constitutional: `text` was written by the caller and is persisted verbatim — **not
    one word of it passes through a model.** There is no LLM summarisation path in this
    function and there must never be one. Background backfill only adds tags, a summary
    and a name; that is derived metadata, not the body.

    prov: the provenance lines as the entry point built them (`_normalize_from`; regrow
      adds the wasRevisionOf line to the old version itself). Stored as given.

    sources: the source records as the entry point checked them (`check_sources`). When
      given on a version change they replace the old version's, which regrow has already
      merged in; None leaves the old version's to come across with the rest of its
      standing.

    supersedes: for n=1 (a version change) pass the old id, and the version chain
      supersedes / superseded_by plus dont_surface gets written, and the old version's
      standing (`_CARRIED_FIELDS`, its user tags, its pin, the profile page's job) comes
      across onto the new one.
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
    tags = [BIGEVENT_TAG] if when else []
    # ---- 🔴 a version change (n=1) carries the old version's standing across ----
    # Tags go on at creation, because backfill merges into whatever tags exist at that
    # moment; the fields follow right after creation with one update (`carried_state`),
    # and the pin further down, after the chain.
    # · The profile page's job: the door finds its page by the tag alone and skips a
    #   superseded page, so a new version created without the tag leaves the door's cell
    #   empty.
    # · The gist tag only when the chain began as a gist. A re-version of an ordinary
    #   memory is that memory, not machinery.
    # · The user's tags; the machinery's own are left behind (`user_tags`).
    # · `protected` goes in at creation too: update() has no opening for it (it is set
    #   once, at birth), and it locks importance the way the pin does.
    carries_profile = False
    carried: dict = {}
    media: list[dict] = []
    protected = False
    if supersedes:
        from .profile import _PROFILE_TAG      # lazy: core.profile imports this module
        old_page = await rt.bucket_mgr.get(supersedes)
        old_meta = (old_page or {}).get("metadata") or {}
        old_tags = [str(t) for t in (old_meta.get("tags") or [])]
        if GIST_TAG in old_tags and await _began_as_gist(old_meta):
            tags.insert(0, GIST_TAG)
        if _PROFILE_TAG in old_tags:
            tags.append(_PROFILE_TAG)
            carries_profile = True
        tags += [t for t in user_tags(old_meta) if t not in tags]
        carried = carried_state(old_meta, period=bool(when))
        if sources is not None:
            carried.pop("sources", None)
        media = carried_media(old_meta)
        protected = bool(old_meta.get("protected"))
    else:
        tags.insert(0, GIST_TAG)
    new_id = await rt.bucket_mgr.create(
        content=text,
        tags=tags,                       # set at create time: backfill merges and never replaces, so this cannot be washed off
        importance=5,                    # neutral placeholder; importance is retired and no longer assigned by hand
        domain=["未分类"],
        valence=v,
        arousal=a,
        name=None,
        prov=prov,
        sources=sources,
        source_tool="fold",
        room=room,
        when=when,                       # the time-circle: the span goes into the existing `when`, there is no second field
        protected=protected,
        test_data=test_data,
    )

    report: dict = {"cover": cover, "叠盖": [], "没写上": [], "链没写全": False,
                    "接着当门口": carries_profile, "状态没带过去": False, "附件没带过去": False}

    # The old version's standing, before the chain: a new version that is wanted, dated
    # or closed has to be so from the moment it takes over. A failure here is the silent
    # kind (the body landed, the fields did not), so it is reported, never swallowed.
    if carried and not await rt.bucket_mgr.update(new_id, **carried):
        report["状态没带过去"] = True
    # Attachments are a write of their own: persisting them can raise (a file gone, a
    # size cap), and that must not take the standing above down with it.
    if media:
        try:
            if not await rt.bucket_mgr.update(new_id, media=media):
                report["附件没带过去"] = True
        except Exception as e:
            report["附件没带过去"] = True
            try:
                rt.logger.warning(f"regrow carry-media failed {supersedes}->{new_id}: {e}")
            except Exception:
                pass

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
        stacked: list = []

        def chain_end(old_meta: dict, cid=cid, stacked=stacked) -> dict:
            # Decided on the entry as it is on disk, under its lock: a second fold covering
            # the same entry at the same moment appends after this one instead of writing
            # back a roster without it.
            kwargs: dict = {}
            if cid in cover:
                old_covers = _covered_list(old_meta)
                if old_covers and new_id not in old_covers:
                    # Rule 6: crossing is a fact, not a conflict -> explicitly covering
                    # something already covered **stacks** (append); nobody evicts anybody,
                    # both layers stay visible and both can be drilled into. (This replaced
                    # a first implementation in which the new cover took ownership.)
                    stacked[:] = [list(old_covers)]
                kwargs["covered_by"] = old_covers + ([new_id] if new_id not in old_covers else [])
            if supersedes and cid == supersedes:
                # Only the version-change case writes the chain and dont_surface (regrow's
                # long-standing behaviour, untouched)
                kwargs["superseded_by"] = new_id
                kwargs["dont_surface"] = True
            return kwargs

        if not await rt.bucket_mgr.update(cid, revise=chain_end):
            report["没写上"].append(cid)
        elif stacked:
            report["叠盖"].append((cid, stacked[0]))
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
        # ---- the anchor moves the same way, through set_anchor (it also swaps
        # source_tool, and keeps the original for the release) ----
        # The cap is counted inside the write and the old version still holds its slot,
        # so here the order is the reverse of the pin's: release the old first, then
        # anchor the new. If the second step fails, the old is anchored again — an anchor
        # is a mark that keeps something down, and a moment with neither is harmless
        # where a moment with neither pin is the door going empty.
        try:
            old_b = await rt.bucket_mgr.get(supersedes)
            old_meta = (old_b or {}).get("metadata", {}) or {}
            if old_meta.get("anchor") and hasattr(rt.bucket_mgr, "set_anchor"):
                await rt.bucket_mgr.set_anchor(supersedes, False)
                moved = await rt.bucket_mgr.set_anchor(new_id, True)
                report["接着锚"] = bool(moved.get("ok"))
                if not moved.get("ok"):
                    await rt.bucket_mgr.set_anchor(supersedes, True)
        except Exception as e:
            report["接着锚"] = False
            try:
                rt.logger.warning(f"regrow carry-anchor failed {supersedes}->{new_id}: {e}")
            except Exception:
                pass
    elif not ok_cover:
        report["链没写全"] = True

    # The covered entries have in effect "been recalled once more" (the same reasoning
    # behind regrow touching its sources)
    try:
        await rt.bucket_mgr.touch_many(prov_targets(prov or []), road="fold")
    except Exception:
        pass

    # Metadata is filled in afterwards: tags, summary and naming go to the background.
    # keep_va=True — v/a were assigned by hand, and backfill never touches them
    # Upward call, kept lazy: the backfill is the grow tool's behaviour (tools imports core).
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
    if report.get("状态没带过去"):
        out.append("⚠️ 旧版的 when / 状态 / 重量 / 主体没带到新版——新版正文落了盘，但它现在不知道自己"
                   "是什么时候的、是不是还想做的。把这条报给AI查。")
    if report.get("附件没带过去"):
        out.append("⚠️ 旧版的附件（media）没带到新版——旧版 id 直查还看得到它们。把这条报给AI查。")
    if report.get("接着锚") is True:
        out.append("⚓ 旧版是坐标系（anchor），新版**接着当**，旧版已松开。")
    elif report.get("接着锚") is False:
        out.append("🔴 旧版是坐标系（anchor），但没挪到新版上——旧版还锚着，新版没锚。把这条报给AI查。")
    return "\n".join(out)
