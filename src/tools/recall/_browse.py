# -*- coding: utf-8 -*-
"""
tools/recall/_browse.py — the browsing path (no query)

Browsing is looking without a target: the near end comes out the way recall does, the far
end is cut hard to a few representatives, periods and gists stand in for what they cover,
and 「今天」 is listed entry by entry, never collapsed.

tools/recall/core.py re-exports _render_browse.
"""

from datetime import datetime, timedelta

from core import _fold as _F          # fold / gist: what is covered no longer surfaces on its own
from core import _bigevent as _big    # a big event: one sentence laid over a stretch of time
from core._slicer import _short_id    # the handle a read tool prints for an id
from core import _when as _w          # "today" as the user lives it (local timezone) — never call datetime.now() directly
from core import runtime as rt
from .._common import read_scope

from ._cells import (_cell_stats, _far_line, _fmt_highlights, _label_of, _pick_tags_n,
                     _split_calendar, common_tags, room_implied_tags)
from ._words import kind_badge, tags_in_words


# ── Two paths: browsing and searching ────────────────────────
# There is exactly one criterion: **is there a query**. when/room/tag are a
# **range**; query is a **target**.
#   browsing (no query): I am looking, I cannot recall what is there -> **cut the
#     far end hard**, order by time, no scores
#   searching (with a query): I am looking for something, I know what -> **give
#     more at the far end** (cutting here means missing), order by relevance, and
#     the score is the point
# The fork has always existed in the code (no query gate means no score); what was
# missing is that the two paths produced identically shaped output.
_BROWSE_NEAR_DAYS = 3     # "within three days it still comes out the way recall does"
_BROWSE_REP_DAYS = 21     # "give 2-3 entries from 2-3 weeks back" — picking out anything older is pointless
_BROWSE_REP_MAX = 3


# ============================================================
# Browsing (no query); searching is tools/recall/_search.py
# ============================================================

def _pick_reps(far: list[dict], k: int = _BROWSE_REP_MAX) -> list[tuple[str, dict]]:
    """Pick k representatives from the whole distant stretch.

    The rule: anything further back is labelled "some time ago" and given 2-3
    entries from 2-3 weeks back.
    So the last three weeks are preferred; only when there is nothing at all in
    those three weeks (the query is about something long ago) does it fall back to
    the whole stretch.
    The picking reuses the existing "the odd one + the heavy one" (the highlights
    from _cell_stats) rather than inventing a second method.
    """
    cutoff = _w.today() - timedelta(days=_BROWSE_REP_DAYS)
    pool = [e for e in far if e["ts"] >= cutoff] or far
    return _cell_stats(pool)["highlights"][:k]


def _rep_line(mark: str, e: dict) -> str:
    # 60 rather than 30: the gists recall returned were coming back incomplete —
    # and a representative entry is the only place that stretch of time gives any
    # content at all, so cutting it in half is the same as giving nothing. The
    # date stays as a handle (for drilling in), not as a classification.
    return (f"  {mark}{kind_badge(e['meta'])}{_label_of(e)[:60]}({_short_id(e['id'])}) "
            f"{e['ts'].strftime('%m-%d')}")


def _big_line(meta: dict, content: str, bid: str) -> str:
    """One line for a period.

    🔴 The `◈` symbol was replaced by the word 「时期」.
       A symbol has to be learned before it can be read, and this line was
       supposed to be understood at a glance.
    """
    span = _big.fmt_span(meta)
    return (f"  时期 {_big.first_line(content)[:38]}({_short_id(bid)})"
            + (f" {span}" if span else ""))


def _cell_span(cell: list[dict]) -> tuple[datetime, datetime]:
    """One cell's time range, as a **half-open interval**: the right edge is pushed
    to the day after the last entry.

    🔴 Both of these are pits, and both were fallen into:
    ① `entries` is ordered **newest to oldest**, so `cell[0]` is the newest entry
       and `cell[-1]` the oldest — passing them straight to `covering()` as
       `(t0, t1)` hands over the start and end **reversed**, and the only periods
       that then surface are the ones fully containing the whole stretch (a silent
       display failure that had been there for a long time before it was found).
    ② For a memory with `when=2026-12-25`, `ts` is **midnight** on that day. Taking
       `max(ts)` as the right edge collapses the interval to a single point, and
       since `covering()` tests for overlap (skipping when `s >= t1`), a period
       starting exactly at that midnight would be excluded from the very day it
       covers.
    """
    ts = [e["ts"] for e in cell]
    return min(ts), max(ts) + timedelta(days=1)


def _big_lines(all_buckets: list, t0, t1, seen: set[str]) -> list[str]:
    """The **periods** overlapping this cell, one title line each
    (`时期 <name> (id) 8-13~8-16`).

    🔴 A period is a **pure naming layer**: it never writes `covered_by`, so it
    cannot go down `_gist_lines`' "who got covered" path — its members are
    **computed live by date**, and the display layer should therefore compute them
    live too: `_bigevent.covering()` (the original mechanism, unchanged).
    ⚠️ A period surfaces only once per render (`seen`): covering three days does
       not mean saying it three times.
    ⚠️ This **only ever adds a line**: the per-entry area and the statistics below
       lose nothing (a period collapses no rows) — which is where the rule
       "information may only grow, never shrink" lands.
    🔴 **Only one per cell**. Periods accumulate over time,
       and browsing wants a gradient, not a list. `covering()` returns newest
       first, so taking the first one gives the period closest to this cell.
    """
    out: list[str] = []
    for meta, content, bid in _big.covering(all_buckets, t0, t1):
        if bid in seen:
            continue
        seen.add(bid)
        out.append(_big_line(meta, content, bid))
        break          # one per cell
    return out


async def _gist_lines(entries: list[dict], skip: set[str] | None = None, scope_view=None) -> list[str]:
    """Which gists cover the covered entries in this cell -> one title line per
    gist.

    **This is the "one extra line"**:
        08-13~08-16  「那几天在青岛做讲义」   <- the gist (newly added)
          56条 · 房间… · 标签… · 突出…        <- everything that was there before,
                                                to the character

    🔴 The rule: a covered entry does not appear on its own in the per-entry area,
    but **where it went has to stay visible** — so this line carries the gist's id
    (the handle for drilling in) and how many entries in this cell it covers.
    Information may only grow, never shrink: N single lines are gone, and a title
    line plus a drillable id has appeared.
    """
    covered: dict[str, int] = {}
    for e in entries:
        # Crossing: one entry can be covered by two threads at once -> both gist
        # titles count it
        for gid in _F.covers_of(e["meta"]):
            if gid and gid not in (skip or set()) and (scope_view is None or scope_view.permits_id(gid)):
                covered[gid] = covered.get(gid, 0) + 1
    out: list[str] = []
    for gid, n in sorted(covered.items(), key=lambda kv: -kv[1]):
        b = await rt.bucket_mgr.get_including_archive(gid)
        if not b:
            out.append(f"  ▣（盖着这里 {n} 条的 gist {_short_id(gid)} 查无此桶——链断了，报给AI）")
            continue
        meta = b.get("metadata", {}) or {}
        head = _big.first_line(str(b.get("content") or ""))[:38]
        mark = "◈" if _big.is_big(meta) else "▣"
        span = _big.fmt_span(meta) if _big.is_big(meta) else ""
        out.append(f"  {mark}{head}({_short_id(gid)})"
                   + (f" {span}" if span else "") + f" ▏盖着这里 {n} 条")
    return out


async def _render_browse(entries, gates, room, tag, all_buckets=None) -> str:
    """Browsing: I am looking, and cannot recall what is there. **The far end is
    cut hard** — the last three days stay as they were, everything older collapses
    into one stretch labelled "some time ago".

    Precise date bands like `07-13~07-19` read wrong: **that is a machine's way of
    dividing time; a person only thinks "some time ago"**. So the far end
    **stops being divided into cells at all** and 2-3 representatives are picked
    from the whole stretch.
    That also keeps **1 entry and 75 entries from taking up exactly the same amount
    of space**.
    """
    now = _w.now()
    today = _w.today()
    # 🔴 **The whole browse fetches the library exactly once here.** The period
    #    lines below are drawn once per cell, once per day and once for the far
    #    end — seven or eight times in a single browse — and fetching inside
    #    `covering()` would hide those fetches from every caller.
    #    The list lives here and whoever needs it reaches for it, so one extra
    #    fetch would be right there in plain sight.
    #    ⚠️ Periods need **the whole library**, not the entries this call filtered
    #       down to — whether a period covers this cell has nothing to do with
    #       whether the period itself passed the filters.
    if all_buckets is not None:
        span_buckets = all_buckets
    else:
        try:
            span_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            rt.logger.warning(f"时期那半的库没捞到，这次浏览不盖时期: {e}")
            span_buckets = []
    # A period the request may not read is not named (a read scope, core/scope.py).
    scope_view = await read_scope()
    if scope_view is not None:
        span_buckets = [b for b in span_buckets if scope_view.permits(b)]
    dn = today - timedelta(days=_BROWSE_NEAR_DAYS - 1)   # 今天 / 昨天 / 前天
    tomorrow = today + timedelta(days=1)

    future = [e for e in entries if e["ts"] >= tomorrow]
    near = [e for e in entries if dn <= e["ts"] < tomorrow]
    far = [e for e in entries if e["ts"] < dn]

    # A dimension that was filtered on is a constant — do not say it again; the
    # same goes for the tags common to the whole batch.
    fixed_room = bool(room.strip())
    drop = (room_implied_tags(room)
            | common_tags(entries)
            | ({tag.strip()} if tag.strip() else set()))

    def _head(label: str, st: dict) -> str:
        bits = [f"── {label} · {st['n']}条"]
        if not fixed_room and st["房间话"]:
            bits.append(st["房间话"])
        # Near-end tags carry counts too (`床 3` and `床 30` are two different
        # stretches of life).
        # With counts attached the 「围着…转」 frame is **dropped** — wrapping
        # numbers in it reads badly, the words and numbers are already given
        # exactly as stored, and the frame only helps where there are no counts.
        tags = _pick_tags_n(st, drop)
        if tags:
            bits.append(tags_in_words(tags, 2, with_counts=True, framed=False))
        if st["情绪话"]:
            bits.append(st["情绪话"])
        seeds = "".join(f"[[{x}]]" for x in st["seeds"])
        return " ▏".join(bits) + (" " + seeds if seeds else "")

    lines = [f"〔{gates}〕{len(entries)} 条 · 新→旧"]
    # Each period surfaces only once per render (once it has appeared at the near
    # end, the far stretch does not repeat it)
    spans_shown: set[str] = set()

    # Days that have not arrived yet: collapsed by calendar month, so however far
    # ahead they are they take only a few lines
    if future:
        lines.append("— 还没到的 —")
        for label, cell in reversed(_split_calendar(future, "month")):
            lines.append(_far_line(label, _cell_stats(cell), fixed_room, drop))
            lines.extend(_big_lines(span_buckets, *_cell_span(cell), spans_shown))

    # Within three days: unchanged (one line per day, with what stands out on its
    # own line)
    if near:
        days: dict[str, list] = {}
        for e in near:
            days.setdefault(e["ts"].strftime("%m-%d"), []).append(e)
        for label in sorted(days, reverse=True):
            st = _cell_stats(days[label])
            lines.append(_head(label, st))
            # The period/gist title goes **above** what stands out: say what these
            # days are called first, then which entry inside them jumps out
            lines.extend(_big_lines(span_buckets, *_cell_span(days[label]), spans_shown))
            hl = _fmt_highlights(st)
            if hl:
                lines.append(hl)

    # Anything older: **no cells at all**, one sentence for the whole stretch
    # ("some time ago") plus 2-3 representatives (replaced by a big event where
    # there is one)
    if far:
        st = _cell_stats(far)
        a = far[0]["ts"]
        # The title **does not report a precise date band**: saying "some time
        # ago" and then hanging `08-01~08-02` off it contradicts itself — that is
        # still a machine's way of dividing time. The date stays only after each
        # representative, where it is a handle rather than a classification.
        # ⚠️ The exception: if room/tag was filtered on, the user is **following
        # one thing** — "from when to when did this run" is exactly the question
        # being asked, so the span has to be given.
        if fixed_room or tag.strip():
            span_txt = f"{a.strftime('%m-%d')} ~ {far[-1]['ts'].strftime('%m-%d')}"
            bits = [f"— 前段时间（{span_txt}）· {len(far)}条"]
        else:
            bits = [f"— 前段时间 · {len(far)}条"]
        # 🔴 **The tag distribution carries counts** — this line is the far end's
        # only answer to "what were those days like": `亲密关系 30 · 接纳 12` and
        # `亲密关系 3 · 接纳 2` mean opposite things, and without the counts the
        # two lines look identical. breath has always had the counts; it was this
        # path that threw them away.
        #
        # ⚠️ Here **only the people a room implies are dropped**, never
        # common_tags (the frequent ones) — a frequent tag is noise elsewhere
        # (every cell showing the same three words), but on this line **it is the
        # answer**.
        # Frequency can never catch it on its own: once the counts are attached,
        # frequency stops being the handle and becomes the content.
        drop_lite = room_implied_tags(room) | ({tag.strip()} if tag.strip() else set())
        dist = tags_in_words([(t, n) for t, n in st["tags"] if t not in drop_lite],
                             5, with_counts=True, framed=False)
        if dist:
            bits.append(dist)
        if st["情绪话"]:
            bits.append(st["情绪话"])
        lines.append(" ▏".join(bits) + " —")
        # The rule: give 2-3 entries from 2-3 weeks back, **and where there is a
        # big event, use that instead**.
        # Big events take the slots first, and representatives fill whatever is
        # left — fewer than 3 never leaves a gap.
        # (It covers, it does not replace: the statistics line above and what
        # stands out lose nothing; there is simply one more sentence.)
        covering_spans = _big.covering(span_buckets, *_cell_span(far))
        covers = [x for x in covering_spans if x[2] not in spans_shown]
        for meta, content, bid in covers[:1]:      # one period per cell
            spans_shown.add(bid)
            lines.append(_big_line(meta, content, bid))
        # **The browse view does not list the gists covering this stretch.**
        #    🔴 The rule: **what recall is there to show is events.** Thinking can
        #       appear under "what stands out", but it should not be crowded into
        #       the same position as events and periods —
        #       and with a few gists listed, the cell would become nothing but title
        #       lines (gists have no cap).
        #    ⚠️ Entries folded away **still do not appear individually and still
        #       count in the statistics**; to see which gist covers one, use
        #       `recall(query=<full id>)` or the search path, both unchanged.
        for mark, e in _pick_reps(far, _BROWSE_REP_MAX - len(covers[:1])):
            lines.append(_rep_line(mark, e))
        # The trigger point hangs off "recall a stretch of time" — at that moment
        # I am looking back anyway, with the material spread out in front of me,
        # and "it feels like I was doing one particular thing back then" surfaces
        # naturally; I do not have to remember to go looking for it. So the prompt
        # appears once, and only when **nothing genuinely covers this stretch**.
        # ⚠️ The criterion is whether any period covers this stretch at all, not
        #    `covers` (the ones that have not yet surfaced in this cell) — a period
        #    already mentioned at the near end does not mean the stretch is
        #    uncovered.
        if not covering_spans and (now - a).days >= 7:
            # ⚠️ This suggests `fold`, not `grow(kind="big")`. That entry point was
            #    withdrawn — passing it now returns "use fold instead", so the older
            #    wording sent the reader down a path that answers with a correction.
            #    A system telling you to do something it will refuse costs a round trip
            #    and, worse, reads as the system not knowing its own shape.
            lines.append("  （这段时间上没有时期盖着。真觉得是在做一件什么事就写下来："
                         'fold(when="起..止", room=…, text=…, v=…, a=…)）')

    lines.append("（钻：缩小 when / 加 room·tag / slices=N 控格数；看原文：拿 id 搜）")
    return chr(10).join(lines)


# ── 「今天」 listed entry by entry: today's events still have me inside them,
#    so they do not collapse.
#    ⚠️ The `by="回看"` that was cut is a different thing: that read a stretch
#    of history oldest-to-newest (and slices=N covers it), while this views
#    what just happened newest-to-oldest.
async def _render_today(es: list[dict], g: str) -> str:
    # 🔴 The time of day comes from `created` (the moment it actually hit
    #    disk), never from `ts`: whenever a `when` exists, ts is **midnight on
    #    that day**, and most of today's entries carry when=今天, so listing
    #    them all as 00:00 shows nothing at all.
    #    created has no suffix, meaning UTC; parse_stamp converts it to local.
    # 🔴 **Ordering must use the same definition as the display**: sort by ts
    #    and then display created and today's events all pile up at 00:00, so
    #    the listed times come out scrambled.
    def _hm(e: dict):
        return _w.parse_stamp(e["meta"].get("created")) or e["ts"]

    lines = [f"〔{g}〕{len(es)} 条 · 今天（全列，不塌缩）· 新→旧"]
    # Covered entries are not listed individually today either; they are
    # replaced by the gist title line above.
    # **The count is still len(es)** (nothing was lost); only lines were
    # saved — which is where the "information may only grow" rule lands.
    scope_view = await read_scope()
    lines.extend(await _gist_lines(es, scope_view=scope_view))
    for e in sorted(es, key=_hm, reverse=True):
        # Covered by a gist the request may read: that gist's line stands for it.
        if _F.is_covered(e["meta"]) and (scope_view is None or any(
                scope_view.permits_id(g) for g in _F.covers_of(e["meta"]))):
            continue
        lines.append(f"{_hm(e).strftime('%H:%M')}  {kind_badge(e['meta'])}{_label_of(e)}  "
                     f"({_short_id(e['id'])})")
    lines.append("（看原文：拿 id 搜；要昨天/上周那种概览就换 when）")
    return chr(10).join(lines)
