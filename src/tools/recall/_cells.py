# -*- coding: utf-8 -*-
"""
tools/recall/_cells.py — one cell's statistics, the zoom, and the JSON skin's rows

_cell_stats is the one set of numbers both skins read (dominant rooms, mood, tags, what
stands out). _split_cells picks the granularity by density; the formatters turn a cell
into a header, a card or a far-end line; entry_json / _stats_json are the same facts for
the panel.

tools/recall/core.py re-exports the names the panel reads (_room_cn, _label_of).
"""

from collections import Counter

from core import _fold as _F          # fold / gist: what is covered no longer surfaces on its own
from core import profile as _P        # what counts as an open promise; days since written
from core._slicer import _short_id    # the handle a read tool prints for an id
from core import _when as _w          # "today" as the user lives it (local timezone) — never call datetime.now() directly
from core._rooms import is_mind_room, normalize_room, room_cn

from ._words import (_SEED_NAMES, _SEED_RE, _SYS_TAG_PREFIXES, is_human_tag, kind_badge,
                     mood_in_words, rooms_in_words, tags_in_words)


# The zoom target: how many cells each call returns (12-20)
_CELL_MAX = 20
_HIGHLIGHT_MAX = 3


# The granularity ladder (in seconds). Density decides granularity: try from fine
# to coarse and take the first one whose non-empty cell count is <= _CELL_MAX
_LADDER = [
    ("小时", 3600),
    ("半天", 12 * 3600),
    ("天", 24 * 3600),
    ("周", 7 * 24 * 3600),
    ("半月", 15 * 24 * 3600),
    ("月", 30 * 24 * 3600),
    ("季", 91 * 24 * 3600),
    ("年", 365 * 24 * 3600),
]


# ------------------------------------------------------------
# Statistics for one cell
# ------------------------------------------------------------

def _score_tag(e: dict) -> str:
    """Show relevance next to the handle. Only entries that went through the query
    gate have a score.

    Why show it: a search for a term meaning "taking care of one's health" (in the
    sense of massage and herbal medicine) came back mostly with entries about the
    body and intimacy — far too much of a stretch. The root cause turned out to be
    that the score had been sitting inside _collect() all along, used only to
    truncate when there were more than 60 results, **never as a threshold** — and
    the number was invisible from outside, so there was no way to judge on what
    grounds the system had dredged an entry up.

    ⚠️ This step deliberately does nothing but make it visible; it adds no
    threshold. The underlying `recall_meaning` line (0.65) may be loose for
    Chinese (two unrelated Chinese passages hitting a cosine of 0.7 is common),
    but how loose has to be judged against the real distribution — any threshold
    chosen before seeing that distribution is a guess. Live with it for a few days
    first, then decide.
    """
    s = e.get("score")
    if not isinstance(s, (int, float)) or not s:
        return ""
    return f" {float(s):.2f}"


# The display text for a memory (core/profile.label_of).
_label_of = _P.label_of


def _cell_stats(entries: list[dict]) -> dict:
    rooms = Counter()
    tags = Counter()
    seeds = Counter()
    v_sum = a_sum = v_n = 0.0
    for e in entries:
        meta = e["meta"]
        # Statistics are grouped under the **four new rooms**: old data is shown
        # under the new names, so the screen has only one vocabulary
        r = normalize_room(meta.get("room"))
        if r:
            rooms[r] += 1
        for t in (meta.get("tags") or []):
            t = str(t)
            if is_human_tag(t):     # machine-voiced tags are never shown
                tags[t] += 1
        for s in _SEED_RE.findall(e["content"]):
            if s in _SEED_NAMES:
                seeds[s] += 1
        try:
            v_sum += float(meta.get("valence", 0.5))
            a_sum += float(meta.get("arousal", 0.3))
            v_n += 1
        except (TypeError, ValueError):
            pass

    # What stands out = the odd one (its tags share nothing with the dominant
    # tags, highest importance) + the heavy one (arousal x importance)
    top_tags = {t for t, _ in tags.most_common(3)}
    def _imp(e):
        try:
            return float(e["meta"].get("importance", 5))
        except (TypeError, ValueError):
            return 5.0
    def _weigh(e):
        try:
            return float(e["meta"].get("arousal", 0.3)) * _imp(e)
        except (TypeError, ValueError):
            return 0.0
    # 🔴 **Covered entries never appear in "what stands out"** — that is the
    #    per-entry area, and a covered entry no longer surfaces on its own.
    #    But they **still count in the statistics above** (count, room, tags, V/A
    #    all intact): the acceptance rule is fixed at "the amount of information
    #    any recall can show may only grow, never shrink", and a gist merely
    #    **replaces those entries with one sentence** in the breakdown; it does not
    #    erase them from the books.
    surfacing = [e for e in entries if not _F.is_covered(e["meta"])]
    odd = [e for e in surfacing
           if top_tags and not (set(map(str, e["meta"].get("tags") or [])) & top_tags)]
    odd.sort(key=_imp, reverse=True)
    heavy = sorted(surfacing, key=_weigh, reverse=True)
    highlights: list[tuple[str, dict]] = []
    seen = set()
    for e in odd[:1]:
        highlights.append(("◇", e))
        seen.add(e["id"])
    for e in heavy:
        if len(highlights) >= _HIGHLIGHT_MAX:
            break
        if e["id"] not in seen:
            highlights.append(("★", e))
            seen.add(e["id"])
    # Heavy first, odd last — it reads better that way
    highlights.sort(key=lambda p: p[0] == "◇")

    v_avg = (v_sum / v_n) if v_n else None
    a_avg = (a_sum / v_n) if v_n else None
    return {
        "n": len(entries),
        "rooms": rooms.most_common(2),
        # The two plain-language sentences are computed **from the whole
        # Counter**, not from most_common(2) — 「多半是我自己在做事」 is a claim
        # about the whole cell, and looking at only the top two gets the
        # denominator wrong.
        "房间话": rooms_in_words(rooms, len(entries)),
        "情绪话": mood_in_words(v_avg, a_avg),
        # Six rather than four: **a tag is not an attribute of one memory, it is
        # the distribution across a pile of them** — a single entry's tags carry
        # almost no information (the body is right there anyway), and their value
        # is entirely in the moment of collapse. So the harder it collapses, the
        # more of them are needed, and with counts attached.
        "tags": tags.most_common(6),
        "v": v_avg,
        "a": a_avg,
        "seeds": [s for s, _ in seeds.most_common(2)],
        "highlights": highlights,
    }


# ------------------------------------------------------------
# Zooming and rendering
# ------------------------------------------------------------

def _split_cells(entries: list[dict], max_cells: int = _CELL_MAX) -> tuple[str, list[tuple[str, list[dict]]]]:
    """**Granularity is set by span, not by density.**

    What was rejected over and over is splitting by count: 5 entries spanning a
    month and 500 entries spanning a month **should both be split by week** —
    granularity answers "what scale should this stretch of time be viewed at",
    which has nothing to do with how many entries are inside it.
    Try the ladder from finest to coarsest and take the first scale that cuts the
    whole span into <= max_cells cells; empty cells are still discarded (never
    rendered), but **density plays no part in choosing the scale**.
    (An earlier version did choose by density, to cure "two entries six months
    apart cut into two half-month cells" — choosing by span cures that just as
    well: a six-month span naturally lands on the month or quarter scale.)
    """
    base = entries[0]["ts"].timestamp()
    span = max(1.0, entries[-1]["ts"].timestamp() - base)
    for gname, gsec in _LADDER:
        if span / gsec > max_cells:
            continue  # this scale cuts the span into too many cells — too fine, go one coarser
        slices: dict[int, list[dict]] = {}
        for e in entries:
            slices.setdefault(int((e["ts"].timestamp() - base) // gsec), []).append(e)
        this_year = _w.now().year
        labeled = []
        for k in sorted(slices):
            cell = slices[k]
            a, b = cell[0]["ts"], cell[-1]["ts"]
            day_a = a.strftime("%m-%d") if a.year == this_year else a.strftime("%Y-%m-%d")
            if gsec < 24 * 3600:
                # Hour / half-day cells: one day produces several, so the label
                # must carry the time (otherwise they all read as the same date)
                label = f"{day_a} {a.strftime('%H:%M')}"
            elif a.date() == b.date():
                label = day_a
            else:
                label = f"{day_a}~{b.strftime('%m-%d')}"
            labeled.append((label, cell))
        return gname, labeled
    return "全部", [("全部", entries)]


def _fmt_header(label: str, st: dict) -> str:
    """One line per cell (the overview path of slices=N). **Machine readouts
    become plain language; tag words are used exactly as stored.**"""
    bits = [f"{label} · {st['n']}条"]
    for x in (st["房间话"], tags_in_words(st["tags"], 2), st["情绪话"]):
        if x:
            bits.append(x)
    seeds = "".join(f"[[{s}]]" for s in st["seeds"])
    return " ▏".join(bits) + (" " + seeds if seeds else "")


def _fmt_highlights(st: dict) -> str:
    """What stands out, **one per line**, with the gist given in full.

    Three crammed onto one line, each cut short, would tell you only that something
    had happened, never what ("the gist is still incomplete, so what exactly are
    you looking at?"). Collapsing collapses **how many entries there are**, never
    **what each one says**.
    """
    return "\n".join(f"   {mark}{kind_badge(e['meta'])}{_label_of(e)}"
                     f"({_short_id(e['id'])}{_score_tag(e)})"
                     for mark, e in st["highlights"])


def _split_calendar(entries: list[dict], unit: str) -> list[tuple[str, list[dict]]]:
    """Split by **calendar week / calendar month** (used by the week and month
    bands of the time-gradient view).

    Not 7-day / 30-day blocks counted from the oldest entry: with those, 31 January
    and 1 February can land in the same "month" while 1 January and 31 January are
    split into two. When a person says "by week" or "by month" they mean weeks and months on the
    calendar, not "168 hours counted from some particular memory".
    """
    keyf = _w.year_week if unit == "week" else _w.year_month
    slices: dict[tuple, list[dict]] = {}
    for e in entries:
        slices.setdefault(keyf(e["ts"]), []).append(e)
    this_year = _w.now().year
    labeled = []
    for k in sorted(slices):
        cell = slices[k]
        a, b = cell[0]["ts"], cell[-1]["ts"]
        if unit == "month":
            label = f"{a.year}-{a.month:02d}" if a.year != this_year else f"{a.month} 月"
        else:
            day_a = a.strftime("%m-%d") if a.year == this_year else a.strftime("%Y-%m-%d")
            label = day_a if a.date() == b.date() else f"{day_a}~{b.strftime('%m-%d')}"
        labeled.append((label, cell))
    return labeled


# No person names live in this file: room_implied_tags() returns an empty set
#    unconditionally. The path that filters names is dehydrator._person_tags(),
#    which reads them from configuration.


def room_implied_tags(room: str) -> set[str]:
    """Once a room has been filtered on, **the people the room's definition
    already implies** carry zero information if they show up again in the tags.

    What made it obvious: filtering to the room for "things between us" means
    every entry is about the same two people — **the room already says it, and the
    tags then say it again**. Which is why three rows of 75/46/31 entries all had
    the same tags. It was not that those tags were frequent; it was that they were
    **repeating what the room had already said**.

    This is the same rule as "never repeat a dimension you have already filtered
    on", which earlier removed the `I/EVENT/SELF/WHO 100%` repeated dozens of
    times at the end of every row; this extends it from the room name to **the
    people the room implies**.

    ⚠️ With no room filter, none of them is removed — in that case a person tag
    genuinely carries information (it distinguishes who the entry is about).

    ------------------------------------------------------------
    🔴 This function currently **degenerates to an empty set on purpose**; it is
    not broken:
    with four rooms, a room name **does not imply any person** (`EVENT/SELF` says
    only "I was there", not who with). "Who it is about" lives in the `subjects`
    field.
    So the right home for this deduplication is there: once subjects is wired
    into retrieval, this becomes "if subjects was filtered on, that same name in
    the tags carries zero information" — **the same rule, a different field**.
    Until then, returning an empty set is correct: removing names now would
    **delete real information** (the room does not guarantee that person was
    present).
    """
    return set()


def common_tags(entries: list[dict], ratio: float = 0.55) -> set[str]:
    """Tags that **almost every entry in this batch has** — they define the batch
    rather than characterise any one cell.

    What made it obvious: after filtering to the room meaning "things between us",
    every cell carried the same handful of tags — the definition of that room,
    carrying zero information. A row collapsing 75 entries told the reader nothing
    at all.

    It is the same ailment as before: that time it was the room (every row
    followed by `I/EVENT/SELF/WHO 100%`, repeated dozens of times) — the room got
    deduplicated and **the tags were missed**.
    ⚠️ This branch is only a backstop (its threshold is conservative). What
    actually works is room_implied_tags() — frequency can never catch it: a tag
    present in only 50% of memories can still rank first in nearly every cell,
    because the problem was never "frequent".
    Pure statistics; nothing goes through a model.
    """
    cnt = Counter()
    for e in entries:
        for t in {str(x) for x in (e["meta"].get("tags") or [])}:
            if not t.startswith(_SYS_TAG_PREFIXES):
                cnt[t] += 1
    return {t for t, n in cnt.items() if n >= max(2, len(entries) * ratio)}


def _pick_tags_n(st: dict, drop: set[str], k: int = 2) -> list[tuple[str, int]]:
    """Same as _pick_tags but with counts (`床 3` and `床 30` describe two
    different stretches of life, and without the number you cannot tell)."""
    kept = [(t, n) for t, n in st["tags"] if t not in drop]
    return (kept or list(st["tags"]))[:k]


def _far_line(label: str, st: dict, fixed_room: bool = False,
              drop: set[str] | None = None) -> str:
    """A stretch collapsed into one sentence. Neither the dimension that was
    filtered on nor the tags common to the whole batch are repeated."""
    drop = drop or set()
    bits = [f"{label} · {st['n']}条"]
    if not fixed_room and st["房间话"]:
        bits.append(st["房间话"])
    tags = _pick_tags_n(st, drop)
    if tags:
        bits.append(tags_in_words(tags, 2))
    if st["情绪话"]:
        bits.append(st["情绪话"])
    line = " ▏".join(bits)
    if st["highlights"]:
        mark, e = st["highlights"][0]
        # 22 -> 40: this line really does have to fit stats + tags + mood + one
        # representative, so it cannot go entirely uncut; but 22 characters is the
        # same as giving no content at all
        line += (f" {mark}{kind_badge(e['meta'])}{_label_of(e)[:40]}"
                 f"({_short_id(e['id'])}{_score_tag(e)})")
    return line


def _fmt_card(label: str, st: dict) -> str:
    """One card (the 1-3 cell tier, and also breath's middle-term block).
    **All three lines speak plain language.**

    The row labels changed with it: `房间/标签/底色` are database column names,
    while 「在做什么 / 围着什么转 / 心里」 is how a person says it.
    """
    lines = [f"{label} · {st['n']}条 " + "─" * 24]
    lines.append("在做什么   " + (st["房间话"] or "-"))
    # This tag line **carries counts** (`床 3` and `床 30` are two different
    # stretches of life); the row label already says 「围着什么」, so the value does
    # not wrap itself in 「围着…转」 a second time
    lines.append("围着什么   " + (tags_in_words(st["tags"], 6, with_counts=True, framed=False) or "-"))
    lines.append("心里       " + (st["情绪话"] or "-")
                 + ("  " + " ".join(f"[[{s}]]" for s in st["seeds"]) if st["seeds"] else ""))
    if st["highlights"]:
        first = True
        for mark, e in st["highlights"]:
            prefix = "扎眼的     " if first else "           "
            # The gist is **never cut**: this card is breath's middle-term block
            # (it goes through slices=1), and a cut at 46 characters breaks two
            # lines out of three off mid-sentence.
            # 📌 The rule: **collapsing collapses how many entries there are, never
            #    what each one says.** A gist is only about 60 characters to begin
            #    with.
            lines.append(f"{prefix}{mark} {kind_badge(e['meta'])}{_label_of(e)} "
                         f"({_short_id(e['id'])}{_score_tag(e)})")
            first = False
    return "\n".join(lines)


# A room's Chinese display name (core/_rooms.room_cn); the panel's reads show the same.
_room_cn = room_cn


def entry_json(e: dict) -> dict:
    """A memory's shape for the front end. Every character was written down at
    storage time; this only moves it, never edits it."""
    meta = e["meta"]
    def _f(key, default):
        try:
            return float(meta.get(key, default))
        except (TypeError, ValueError):
            return default
    tags = [str(t) for t in (meta.get("tags") or [])]
    # The front end always receives the **four new** room names (old data is
    # normalised here), or the panel would show two sets of room names at once
    room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
    return {
        "id": e["id"],
        "short": _short_id(e["id"]),
        "label": _label_of(e),
        "room": room,
        "room_cn": _room_cn(meta.get("room")),
        "ts": e["ts"].isoformat(timespec="seconds"),
        "date": e["ts"].strftime("%Y-%m-%d"),
        "importance": _f("importance", 5.0),
        "v": _f("valence", 0.5),
        "a": _f("arousal", 0.3),
        "pinned": bool(meta.get("pinned")),
        # The three decay stages are for the dashboard skin only (someone has to
        # be able to judge whether the engine is doing its job right); the text
        # skin (breath/recall output) never mentions them — forgetting happens
        # quietly
        "decay": str(meta.get("decay_stage") or "") or "alive",
        "kind": "mind" if is_mind_room(meta.get("room")) else "event",
        "status": str(meta.get("status") or ""),
        "when": str(meta.get("when") or ""),
        "tags": [t for t in tags if not t.startswith(_SYS_TAG_PREFIXES)],
        # Relevance: present only when the query gate ran; None = filtered in via when/room/tag (see _score_tag)
        "score": e.get("score"),
        "dup_of": [t.split(":", 1)[1] for t in tags if t.startswith("疑似同件:")],
        "seeds": sorted({s for s in _SEED_RE.findall(e["content"]) if s in _SEED_NAMES}),
    }


def _stats_json(st: dict) -> dict:
    """The JSON shape of _cell_stats (each entry inside highlights becomes an id
    plus its text)."""
    return {
        "n": st["n"],
        # The two plain-language sentences go to the front end as well: the text
        # skin and the panel say **the same sentence**, while the panel still gets
        # the exact percentages and V/A — wherever numbers are wanted, every one
        # of them is there.
        "房间话": st["房间话"],
        "情绪话": st["情绪话"],
        "rooms": [{"room": r, "room_cn": _room_cn(r), "n": n,
                   "pct": round(100 * n / st["n"]) if st["n"] else 0}
                  for r, n in st["rooms"]],
        "tags": [{"tag": t, "n": n} for t, n in st["tags"]],
        "v": st["v"], "a": st["a"], "seeds": st["seeds"],
        "highlights": [{"mark": mark, "id": e["id"], "short": _short_id(e["id"]),
                        "label": _label_of(e), "score": e.get("score")}
                       for mark, e in st["highlights"]],
    }
