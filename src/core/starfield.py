"""
========================================
core/starfield.py — the starfield: stars, the lines between them, constellations
========================================

The panel's sky (GET /api/loci/graph) is built here from one listing of the live
store: a star per current memory, real edges (`from`, `supersedes`, `[[wikilinks]]`),
weak edges (same day, same tag), the constellations big events light up, and how many
dreams are left on disk (meteors). The route only lists the store, reads the clock and
turns the dict into JSON.

**Only the current version is drawn.** Superseded versions — folded under a gist, or
replaced by regrow — do not go into the sky. Search, direct id lookup and the version
chain are all unaffected; this one screen simply does not draw them. The test is
`_F.is_covered()`, the same contract source awaken and recall use; a second copy of it
drifts (a room rename made in one copy and not the other).

Exports: build(all_buckets, now) · node_ts(meta) · bigevent_members(content, entries,
         since, until) · split_ids(raw)
========================================
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import _fold as _F
from . import _when as _w
from ._rooms import is_mind_room, normalize_room, room_cn
from ._slicer import _short_id
from .profile import _BIGEVENT_TAG, label_of
from .visibility import on_timeline
from utils import read_from_ids

_WIKI_RE = re.compile(r"\[\[([^\[\]|]{1,40})\]\]")
_SEED_NAMES = frozenset({
    "joy", "anger", "sorrow", "fear", "love", "aversion", "desire",
    "lust", "sound", "scent", "taste", "touch", "dharma", "greed",
})


def split_ids(raw) -> list[str]:
    """An id field persisted as a comma-separated string (`supersedes`); a list is
    accepted too."""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").split(",")
    return [x.strip() for x in items if x.strip()]


def node_ts(meta: dict) -> datetime | None:
    """Where a star sits in the sky: `when` first, `created` as fallback — the same rule
    recall uses.

    It goes through `core/_when`, the same ruler recall uses, and returns timezone-aware
    local time (never a `[:19]` slice, which cuts off the Z or the +08:00 offset).
    """
    for k in ("when", "created"):
        ts = _w.parse_stamp(meta.get(k))
        if ts is not None:
            return ts
    return None


def bigevent_members(content: str, entries: list,
                     since: str = "", until: str = "") -> tuple[list[str], str]:
    """The member stars of a big event -> (list of member ids, a plain-language description
    of this query).

    **A constellation is the patch of sky one big event lights up.** The timeline and the
    constellation are the same thing, and the name for it is "constellation".
    The start and end come from `when` first, where the caller passes them in; the
    `range: ...` line in the body is only a fallback for older buckets. The filter
    conditions are still read out of the body:

        filter: tag=<tag name>       -> tag
        filter: room=EVENT/SELF      -> room (prefix match)

    **They are deliberately left as text**: that line is part of the body, so when the
    model recalls this big event it **can read it**. Hidden in metadata, it would be
    invisible to the model.
    What is actually fragile is not free text, it is "getting it wrong without saying so" —
    so a parse failure returns an empty query_text and the page says so out loud, rather
    than quietly drawing an empty constellation.
    """
    m_from = re.search(r"范围[:：]\s*(\d{4}-\d{2}-\d{2})(?:\s*\.\.\s*(\d{4}-\d{2}-\d{2}))?", content)
    m_tag = re.search(r"过滤[:：][^\n]*?\btag\s*=\s*([^\s,，;；]+)", content)
    m_room = re.search(r"过滤[:：][^\n]*?\broom\s*=\s*([A-Za-z/]+)", content)
    if not since and m_from:
        since = m_from.group(1)
        until = m_from.group(2) or ""
    if not (since or m_tag or m_room):
        return [], ""
    want_tag = m_tag.group(1) if m_tag else ""
    want_room = (m_room.group(1).rstrip("/") if m_room else "")

    out = []
    for e in entries:
        if since and e["date"] < since:
            continue
        if until and e["date"] > until:
            continue
        if want_tag and want_tag not in e["_tags_all"]:
            continue
        if want_room:
            r = e.get("room") or ""
            if not (r == want_room or r.startswith(want_room + "/")):
                continue
        out.append(e["id"])

    bits = []
    if since:
        bits.append(f"{since} 起" if not until else f"{since} ~ {until}")
    if want_room:
        bits.append(f"room={want_room}")
    if want_tag:
        bits.append(f"tag={want_tag}")
    return out, " · ".join(bits)


def build(all_buckets: list, now: datetime) -> dict:
    """Starfield: nodes + real edges + weak edges + constellations + meteors, from one
    listing of the live store (`list_all(include_archive=False)`) and the local `now`."""
    fresh_line = now - timedelta(hours=24)

    nodes: list[dict] = []
    by_id: dict[str, dict] = {}
    by_name: dict[str, str] = {}
    raw: dict[str, dict] = {}
    big_events: list[tuple] = []

    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        content = str(b.get("content") or "")
        tags_all = [str(t) for t in (meta.get("tags") or [])]
        if _BIGEVENT_TAG in tags_all:
            # A superseded version does not hang in the sky: constellations draw only the
            # current version. The evolution is in the version chain, still visible when a
            # single bucket is opened. The test goes through _F.is_covered() — the same gate
            # ordinary nodes use below, so there are not two copies of it.
            if not _F.is_covered(meta):
                big_events.append((bid, meta, content))
            continue
        if not bid or not on_timeline(meta):
            continue
        # Superseded versions (folded under a gist, or replaced by regrow) do not go into
        # the sky. This affects the starfield only; search, direct id lookup and the version
        # chain all still reach the superseded entry.
        if _F.is_covered(meta):
            continue
        ts = node_ts(meta)
        if ts is None:
            continue

        def _f(key, default, _meta=meta):
            try:
                return float(_meta.get(key, default))
            except (TypeError, ValueError):
                return default

        room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
        created_dt = node_ts({"created": meta.get("created")}) or ts
        node = {
            "id": bid,
            "short": _short_id(bid),
            "label": label_of({"meta": meta, "content": content}),
            "room": room,
            "room_cn": room_cn(meta.get("room")),
            "v": _f("valence", 0.5),
            "a": _f("arousal", 0.3),
            "date": ts.strftime("%Y-%m-%d"),
            "ts": ts.isoformat(timespec="seconds"),
            "pinned": bool(meta.get("pinned")),
            # Anything stored tonight is the brightest star in the sky, and still faintly
            # twinkling.
            "fresh": created_dt >= fresh_line,
            "kind": "mind" if is_mind_room(meta.get("room")) else "event",
            "seeds": sorted({s for s in _WIKI_RE.findall(content) if s in _SEED_NAMES}),
        }
        nodes.append(node)
        by_id[bid] = node
        nm = str(meta.get("name") or "").strip()
        if nm and nm not in by_name:
            by_name[nm] = bid
        raw[bid] = {"meta": meta, "content": content, "tags_all": tags_all}

    # ---- Real edges: from / supersedes / [[wikilinks]] in the body ----
    edges: list[dict] = []
    seen_edge: set = set()

    def _add(a: str, b: str, kind: str) -> None:
        if a == b or a not in by_id or b not in by_id:
            return
        k = (a, b, kind)
        if k in seen_edge:
            return
        seen_edge.add(k)
        edges.append({"from": a, "to": b, "kind": kind})

    unresolved: Counter = Counter()
    for bid, r in raw.items():
        meta, content = r["meta"], r["content"]
        # The edge kind carries the tools' word, `from`: the memories this one stands
        # on. Its own previous version is drawn once, by the supersedes edge below, and
        # a quoted line points outside the library, where there is no star.
        for src in read_from_ids(meta):
            _add(src, bid, "from")
        for old in split_ids(meta.get("supersedes")):
            _add(bid, old, "supersedes")
        for target in _WIKI_RE.findall(content):
            t = target.strip()
            if t in _SEED_NAMES:
                continue          # [[love]] is an emotional root, not a pointer to another memory
            if re.fullmatch(r"[0-9a-f]{12}", t):
                _add(bid, t, "wikilink")
            elif t in by_name:
                _add(bid, by_name[t], "wikilink")
            else:
                unresolved[t] += 1

    # ---- Weak edges: real edges bright, weak edges dim, and toggleable ----
    weak: list[dict] = []
    seen_weak: set = set()

    def _add_weak(a: str, b: str, kind: str) -> None:
        if a == b:
            return
        k = (a, b) if a < b else (b, a)
        if k in seen_weak:
            return
        seen_weak.add(k)
        weak.append({"from": k[0], "to": k[1], "kind": kind})

    by_day: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_day[n["date"]].append(n)
    for _day, group in by_day.items():
        group.sort(key=lambda n: n["ts"])
        # Chain them, do not mesh them: 30 entries on one day meshed together is 435 lines,
        # which is not a relationship, it is a smear.
        for a, b in zip(group, group[1:]):
            _add_weak(a["id"], b["id"], "same_day")

    by_tag: dict[str, list] = defaultdict(list)
    for bid, r in raw.items():
        for t in r["tags_all"]:
            if not t.startswith(("__", "aspect:", "疑似同件:")):
                by_tag[t].append(bid)
    for _t, members in by_tag.items():
        if not (2 <= len(members) <= 12):
            continue    # an over-large tag (50 entries under one word) draws as a blob and carries no information
        # Chain rather than mesh here too: 12 entries meshed is 66 lines, chained is 11 —
        # and a chain in time order shows how this tag's thread ran, which is far more
        # interesting than a blob.
        members.sort(key=lambda b: by_id[b]["ts"] if b in by_id else "")
        for a, b in zip(members, members[1:]):
            _add_weak(a, b, "same_tag")

    # ---- Constellations, built from big events: the timeline and the constellation are the
    # ---- same thing ----
    entries_for_big = [{"id": n["id"], "date": n["date"], "room": n["room"],
                        "_tags_all": raw[n["id"]]["tags_all"]} for n in nodes]
    _span_re = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})?$")
    constellations = []
    for bid, meta, content in big_events:
        first = (content.strip().splitlines() or [""])[0]
        # Start and end come from `when` first; older buckets fall back to the range line in
        # the body.
        m = _span_re.match(str(meta.get("when") or "").strip())
        since = m.group(1) if m else ""
        until = (m.group(2) or "") if m else ""
        members, query_text = bigevent_members(content, entries_for_big,
                                               since=since, until=until)
        # The title has to be short; do not put the summary up there. Even the fallback for
        # an unnamed entry takes only the head of the body and cuts at the first punctuation
        # mark — a name is a name, and the sentence is for after you click.
        name = re.sub(r"^[\d\- :]+", "", str(meta.get("name") or "")).strip()
        if not name:
            name = re.split(r"[：:，,。；;——]", first, 1)[0][:12]
        constellations.append({
            "id": bid,
            "short": _short_id(bid),
            "name": name,
            "line": first,
            "members": members,
            # empty string = the range is missing or malformed. The front-end raises on
            # this rather than quietly drawing an empty constellation.
            "query_text": query_text,
            "start": since,
            "ongoing": bool(since) and not until,
            "resolved": str(meta.get("status") or "") == "resolved",
        })
    # Stack them one on another, newest on top — a stack, not a side-by-side timeline.
    constellations.sort(key=lambda c: c.get("start") or "", reverse=True)

    # ---- Meteors: how many dreams are left on disk. They expire on their own, so this
    # ---- number changes over the course of a day. ----
    try:
        from . import _dream as _D
        meteors = len(_D.load_dreams())
    except Exception:                       # noqa: BLE001 - the starfield must not crash because dreams could not be counted
        meteors = 0

    return {
        "nodes": nodes,
        "edges": edges,
        "weak_edges": weak,
        "constellations": constellations,
        "meteors": meteors,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "weak": len(weak),
            "by_kind": dict(Counter(e["kind"] for e in edges)),
            # Wikilinks that resolve to nothing (a person or project with no bucket of that
            # name). Report them honestly rather than pretending they connected.
            "unresolved_links": unresolved.most_common(8),
        },
    }
