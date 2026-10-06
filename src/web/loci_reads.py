"""
========================================
web/loci_reads.py — the panel's reads, and the builders behind them
========================================

    GET  /api/loci/recall             -> recall's second skin (card + list)
    GET  /api/loci/rooms              -> the four rooms and what is in them
    GET  /api/loci/graph              -> starfield: nodes + real edges + weak edges + constellations
    GET  /api/loci/profile            -> the note by the door
    GET  /api/loci/recollect          -> pull a faded or sunk memory back up
    GET  /api/loci/subjects           -> the "who is in here" screen
    GET  /api/loci/bucket/{id}        -> one bucket, verbatim, plus its metadata
========================================
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from core import _when as _w      # "today" in the user's local timezone — never call datetime.now() directly
from utils import read_from_ids

logger = sh.logger

_PROFILE_TAG = "__档案事实__"
_BIGEVENT_TAG = "__大event__"
# `_REMIND_DAYS` (30 days) and `_is_closed` live with the note by the door's contract source
# (`tools/breath/awaken.py`) — **do not put a second 30 here**. Two 30s are two rulesets, and
# that is exactly how one gets changed and the other forgotten.


# ============================================================
# Starfield: nodes / edges / constellations
# ============================================================

_WIKI_RE = re.compile(r"\[\[([^\[\]|]{1,40})\]\]")
_SEED_NAMES = frozenset({
    "joy", "anger", "sorrow", "fear", "love", "aversion", "desire",
    "lust", "sound", "scent", "taste", "touch", "dharma", "greed",
})


def _split_ids(raw) -> list[str]:
    """An id field persisted as a comma-separated string (`supersedes`); a list is
    accepted too."""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").split(",")
    return [x.strip() for x in items if x.strip()]


def _bigevent_members(content: str, entries: list,
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

    Whether to make these structured fields instead has been asked. **They are deliberately
    left as text**: that line is part of the body, so when the model recalls this big event
    it **can read it**. Hidden in metadata, it would be invisible to the model.
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


# ============================================================
# The builders are **deliberately apart from the route handlers**, so each can be run on
# its own without faking a login session
# (`python -c "asyncio.run(loci.build_graph())"`). The routes handle only auth and the JSON
# envelope.
# ============================================================

async def build_rooms() -> dict:
    """The directory both doors open onto: how many entries in each of the four rooms, plus
    the ten most frequent tags.

    The rooms went from ten to four, and the doors from I/YOU to EVENT/MIND.
    The I/YOU dimension was not lost, it moved to subjects — "who is this about" is no
    longer carried by the room.
    """
    from tools.recall.core import _room_cn, _visible
    from core._rooms import ALL_ROOMS, normalize_room
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    counts: Counter = Counter()
    tags: Counter = Counter()
    homeless = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not _visible(meta):
            continue
        # Normalize before counting: older stores still carry the ten-room names, and
        # without normalization all four doors read 0 while `homeless` swells to the entire
        # store — and that 0 looks quite a lot like "there is simply no data".
        r = normalize_room(meta.get("room"))
        if r:
            counts[r] += 1
        else:
            homeless += 1
        for t in (meta.get("tags") or []):
            t = str(t)
            if not t.startswith(("__", "aspect:", "疑似同件:")):
                tags[t] += 1
    doors: dict[str, list] = {"EVENT": [], "MIND": []}
    for r in ALL_ROOMS:
        doors["MIND" if r.startswith("MIND/") else "EVENT"].append(
            {"room": r, "cn": _room_cn(r), "n": counts.get(r, 0)})
    return {
        "doors": doors,
        "homeless": homeless,
        "total": sum(counts.values()) + homeless,
        "top_tags": [{"tag": t, "n": n} for t, n in tags.most_common(10)],
    }


async def build_subjects() -> dict:
    """"Who is in here": every subject that has appeared in the store, how many entries each
    has, and when each last appeared. **Read-only; nothing is written to disk.**

    Why this screen exists: `aliases.yaml` is maintained by hand, and maintaining anything by
    hand presupposes knowing there is something to change — **and that step was missing
    entirely**. Nobody tells you when a new person appears; nobody tells you when one person
    splits into two spellings; nobody tells you when extraction noise creeps in. A full-store
    scan turned up one "subject" that was the model mistaking a fragment of ordinary prose
    for a name.

    Same principle as muse and fold: **the system only lays things out; the merge itself is
    a human click.**
    So this endpoint counts, and writes not one character into aliases.yaml.

    It measures the same way recall and rooms do: `_visible` filters first (superseded
    realizations, anything folded under a gist, and seeds do not count), and time comes from
    `_node_ts` (when first, created as fallback). The number on the panel has to be the same
    number the model sees on waking.
    """
    from tools.recall.core import _visible
    from tools import _subjects as subj
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    table = subj.load_alias_table()           # {alias in lowercase: canonical name}
    blocked = subj.load_not_person()          # entries marked "this is not a person" (lowercase)
    counts: Counter = Counter()
    variants: dict[str, set] = {}             # canonical name -> the spellings actually found on disk
    last: dict[str, datetime] = {}
    last_bucket: dict[str, str] = {}
    blocked_hits: Counter = Counter()
    total = 0
    with_subj = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not _visible(meta):
            continue
        total += 1
        raws = [str(x).strip() for x in (meta.get("subjects") or []) if str(x).strip()]
        if raws:
            with_subj += 1
        ts = _node_ts(meta)
        bid = str(meta.get("id") or "")
        seen = set()
        for raw in raws:
            low = raw.lower()
            if low in blocked:
                blocked_hits[raw] += 1
                continue
            # This deliberately does not call subj.canonical(): that returns an empty string
            # for pronouns too, which would quietly swallow the fact that a pronoun leaked
            # into subjects. That leak is exactly what this screen is for.
            canon = table.get(low, raw)
            if canon in seen:                 # two spellings of one person in the same entry count once
                continue
            seen.add(canon)
            counts[canon] += 1
            if canon != raw:
                variants.setdefault(canon, set()).add(raw)
            if ts is not None and (canon not in last or ts > last[canon]):
                last[canon] = ts
                last_bucket[canon] = bid
    names_out = []
    for nm, c in counts.most_common():
        ts = last.get(nm)
        rec = subj.record_of(nm)
        names_out.append({
            "name": nm,
            "n": c,
            "last": ts.strftime("%Y-%m-%d") if ts else "",
            "last_bucket": last_bucket.get(nm, ""),
            # empty = not in the alias table yet (a newly appeared name)
            "canonical": table.get(nm.lower(), ""),
            # older spellings still on disk; a merge applies going forward and never
            # rewrites history
            "variants": sorted(variants.get(nm, ())),
            # a pronoun should never be a subject (the gate is on the write side), so one
            # showing up here means that gate leaked — flag it
            "pronoun": subj.is_pronoun(nm),
            # what the table says the name is (instance_of) and where it appears; empty
            # when it says nothing (a table of aliases alone, or a name not in it)
            "kind": rec.instance_of if rec else "",
            "present_in": list(rec.present_in) if rec else [],
            "member_of": list(rec.member_of) if rec else [],
        })
    return {
        "total": total,                       # visible entries
        "with_subjects": with_subj,           # of those, the ones a person was extracted from
        "distinct": len(counts),              # how many distinct names, after alias merging
        "names": names_out,                   # descending by count
        "alias_table_size": len(table),
        # Entries marked "this is not a person": kept out of the table above, but they must
        # not vanish into thin air — nobody can undo something that disappeared quietly.
        "blocked": [{"name": k, "n": v} for k, v in blocked_hits.most_common()],
    }


def _node_ts(meta: dict) -> datetime | None:
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


async def build_graph() -> dict:
    """Starfield: nodes + real edges + weak edges + constellations + meteors.

    WARNING: **only the current version is drawn.** Superseded versions — folded under a
    gist, or replaced by regrow — do not go into the sky. Search, direct id lookup and the
    version chain are all unaffected; this one screen simply does not draw them. The test is
    `_F.is_covered()`, the same contract source awaken and recall use. Do not write a second
    one: when the rooms were renamed, the same test had been written out in two places, one
    got fixed and the other did not, and that lesson still stands (see the header of
    tools/breath/awaken.py).
    """
    from tools.recall.core import _room_cn, _visible, _label_of, _short_id
    from core._rooms import is_mind_room, normalize_room
    from core import _fold as _F
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    now = _w.now()          # local timezone
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
        if not bid or not _visible(meta):
            continue
        # Superseded versions (folded under a gist, or replaced by regrow) do not go into
        # the sky. This affects the starfield only; search, direct id lookup and the version
        # chain all still reach the superseded entry.
        if _F.is_covered(meta):
            continue
        ts = _node_ts(meta)
        if ts is None:
            continue

        def _f(key, default, _meta=meta):
            try:
                return float(_meta.get(key, default))
            except (TypeError, ValueError):
                return default

        room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
        created_dt = _node_ts({"created": meta.get("created")}) or ts
        node = {
            "id": bid,
            "short": _short_id(bid),
            "label": _label_of({"meta": meta, "content": content}),
            "room": room,
            "room_cn": _room_cn(meta.get("room")),
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
        for old in _split_ids(meta.get("supersedes")):
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
        members, query_text = _bigevent_members(content, entries_for_big,
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
    # What is counted moved from night_fall's `.md` files to the new engine's dream files;
    # the directory is the same one, kept as it was.
    try:
        from core import _dream as _D
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


def _collect_events(all_buckets: list) -> list[dict]:
    """The pool behind the "something comes back to you" section of the waking screen.
    **The rule is not here** — it is in the contract source.

    It does exactly one thing: call `core.profile.event_pool()` and reshape the dict into
    what the front-end wants. (Copying the same line into two files is not sharing a
    source.)
    WARNING: the direction is fixed at **web -> core/tools**, never the reverse. The MCP
       surface must not depend on the panel.
    WARNING: a parallel implementation drifts: a room rename has to be made in both copies,
       a gate added on one side lets entries keep surfacing on the other.
    """
    from tools.recall.core import _room_cn, _label_of, _short_id
    from core._rooms import normalize_room
    from core.profile import event_pool
    pool: list[dict] = []
    for e in event_pool(all_buckets):
        meta, content, bid = e["meta"], e["content"], e["id"]
        room = normalize_room(meta.get("room"))
        pool.append({
            "id": bid, "short": _short_id(bid), "room": room,
            "room_cn": _room_cn(room),
            "label": _label_of({"meta": meta, "content": content}),
            "created": str(meta.get("created") or "")[:10],
        })
    return pool


def _pick_recollect(pool: list[dict], n: int = 2) -> list[dict]:
    """One or two at random. **The absence of a reason is precisely what makes this feel
    like a mind rather than a database** — no sorting, no weighting.

    The cap is 2 because the waking screen's contract is one to two entries (`awaken.py`
    hardcodes `min(2, ...)`).
    It is clamped here rather than in the route, so that adding another route, or calling
    this function directly some day, cannot get around the contract.
    """
    import random
    if not pool:
        return []
    return random.sample(pool, max(1, min(n, 2, len(pool))))


async def build_recollect(n: int = 2) -> dict:
    """The "give me another" button hits this endpoint on its own, rather than re-fetching
    the whole profile page."""
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    pool = _collect_events(all_buckets)
    return {"recollect": _pick_recollect(pool, n), "pool": len(pool)}


async def build_profile() -> dict:
    """The note by the door: name, principles with their provenance, reminders, and what is
    weighing on the mind. **Every rule lives in the contract source.**

    There is one rule and one place: `core.profile.door_note()`, the same one the waking
    screen reads. This function only turns the dict into JSON.

    WARNING: there is no "things that keep coming up about a person" section. Ranking by
    how often something is mentioned is wrong: **what gets mentioned most is not what is
    most true**. Reading a dossier of "what this person is like" on waking and then
    treating them according to the dossier turns a person into a character sheet. The
    rule is about timing instead: **only what there is no time to look up before speaking
    belongs by the door.**
    """
    from tools.recall.core import _room_cn, _label_of, _short_id
    from core._rooms import normalize_room
    from core.profile import door_note, edited_by_user
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    now = _w.now()          # local timezone
    door = door_note(all_buckets, now)
    heavy_q_id = door["heavy_question_id"]      # ask only about the longest-standing one

    def _label(x) -> str:
        return _label_of({"meta": x["meta"], "content": x["content"]})

    facts = [{"id": f["id"], "short": _short_id(f["id"]),
              "created": f["created"], "content": f["content"].strip()}
             for f in door["facts"]]
    big = [{"id": g["id"], "short": _short_id(g["id"]),
            "line": (g["content"].strip().splitlines() or [""])[0]}
           for g in door["big"]]
    reminders = [{"id": r["id"], "short": _short_id(r["id"]), "days": r["days"],
                  "when": r["when"], "status": r["status"],
                  "label": _label(r), "loud": r["loud"]}
                 for r in door["reminders"]]
    # **No truncation here.** The waking screen shows only two, because one screen is
    # finite; this is a page someone is browsing deliberately, so however many are pending
    # is however many they should see. The ordering — heaviest first, and among equals the
    # longest-standing first — lives in the contract source.
    # clock/clock_note are the category decided by the three kinds of clock, plus a note for
    # legacy data (a read-side judgement; see core/profile._want_clock). is_question marks
    # the "ask only about the longest-standing one" entry. last_asked/closed_by pass meta
    # straight through, for the front-end to build the "never asked about this" phrase and
    # to decide whether to show the close button, which only appears for an open telic entry.
    heavy = [{"id": h["id"], "short": _short_id(h["id"]), "held": h["held"],
              "weight": h["weight"], "label": _label(h), "loud": h["loud"],
              "clock": h["clock"], "clock_note": h["clock_note"],
              "last_asked": h["last_asked"],
              "closed_by": str(h["meta"].get("closed_by") or ""),
              "is_question": h["id"] == heavy_q_id}
             for h in door["heavy"]]
    # The notification pool: entries the user edited that have not yet been read or folded.
    edited = [{"id": e["id"], "short": _short_id(e["id"]),
               "label": _label(e), "content": e["content"].strip(),
               "corrects": (read_from_ids(e["meta"]) or [""])[0]}
              for e in edited_by_user(all_buckets)]
    rules = []
    for r in door["rules"]:
        room = normalize_room(r["meta"].get("room"))
        rules.append({"id": r["id"], "short": _short_id(r["id"]), "room": room,
                      "room_cn": _room_cn(room), "label": _label(r),
                      "content": r["content"].strip()})

    # The mid-range span (the last three days). This cell was found missing once. The waking
    # screen has six parts — profile, reminders, **mid-range**, long-range, something coming
    # back, and dreams — and the profile page has to lay them out the same way to line up.
    # It uses the same call awaken.py makes (recall over 3d, collapsed into one card) rather
    # than computing its own.
    try:
        from tools.recall.core import recall_core
        mid = await recall_core(when="3d", room="", tag="", query="", max_cells=1)
        if "没有东西" in mid:
            mid = ""
    except Exception as e:
        logger.warning(f"[loci] profile 取中期失败: {e}")
        mid = ""

    # "Something comes back to you", the fifth of the six parts — reusing the same list_all
    # result rather than hitting the store a second time.
    _ev_pool = _collect_events(all_buckets)

    return {
        "mid": mid,
        "recollect": _pick_recollect(_ev_pool, 2),
        "recollect_pool": len(_ev_pool),
        "facts": facts,
        "facts_warning": (f"有 {len(facts)} 个 {_PROFILE_TAG} 桶——只该有一个，去合并"
                          if len(facts) > 1 else
                          f"名字页 {door['facts_covered'][0]['id']} 被换掉了，"
                          f"新版没带 {_PROFILE_TAG}——门口那格是空的"
                          if not facts and door["facts_covered"] else ""),
        "rules": rules,
        # No freq_i / freq_you keys: a front-end still reading them gets undefined rather
        # than an empty array, so the section simply disappears. That is intended:
        # half-rendering an empty section makes it much harder to notice it should not be
        # there than having it vanish outright.
        "reminders": reminders,
        "heavy": heavy,
        "edited": edited,     # the notification pool: user-edited entries not yet handled
        "big_events": big,
    }


# ---------------------------------------------------------
# The main pool: recall's two skins — the card above, for the model, and the list below,
# for a person.
# ---------------------------------------------------------
async def api_loci_recall(request: Request) -> Response:
    from starlette.responses import JSONResponse
    q = request.query_params
    # There is no `by`; `view="scene"` does that job.
    # The filters on this page match the tool surface's parameter list **word for word**:
    # never leave the panel with a knob the tool does not have.
    gates = {k: (q.get(k) or "").strip()
             for k in ("when", "room", "tag", "query", "view")}
    try:
        slices = int(q.get("slices") or 0)
    except (TypeError, ValueError):
        slices = 0
    # The relevance floor is dragged on the page, following the similarity page's
    # existing pattern: the person draws the line, and this side only lays out the
    # distribution. Unset, it falls back to RELEVANCE_FLOOR (currently 35, the same
    # number as the slider's default).
    floor = None
    try:
        if (q.get("floor") or "").strip():
            floor = max(0.0, min(100.0, float(q.get("floor"))))
    except (TypeError, ValueError):
        floor = None
    try:
        # Not recall_data() and recall_core() separately: each runs its own _collect —
        #    **the same search computed twice** (measured with a query: 3 seconds on the
        #    tool surface, 8.6 seconds here).
        #    recall_text_and_data() gathers once, and both skins share it.
        from tools.recall.core import recall_text_and_data
        data = await recall_text_and_data(**gates, floor=floor, max_cells=slices)
        if not data.get("ok"):
            return JSONResponse(data, status_code=400)
        return JSONResponse(data)
    except Exception as e:
        logger.warning(f"[loci] recall 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# The room directory plus frequent tags — the first thing seen through either door.
async def api_loci_rooms(request: Request) -> Response:
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_rooms())
    except Exception as e:
        logger.warning(f"[loci] rooms 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------
# Starfield
# ---------------------------------------------------------
async def api_loci_graph(request: Request) -> Response:
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_graph())
    except Exception as e:
        logger.warning(f"[loci] graph 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------
# The profile — the note by the door
# ---------------------------------------------------------
async def api_loci_profile(request: Request) -> Response:
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_profile())
    except Exception as e:
        logger.warning(f"[loci] profile 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------
# "Something comes back to you" — give me another
# ---------------------------------------------------------
async def api_loci_recollect(request: Request) -> Response:
    from starlette.responses import JSONResponse
    try:
        n = int(request.query_params.get("n") or 2)
    except ValueError:
        n = 2
    try:
        return JSONResponse(await build_recollect(max(1, min(n, 5))))
    except Exception as e:
        logger.warning(f"[loci] recollect 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_subjects(request: Request) -> Response:
    """The data behind the "who is in here" screen. **Read-only** — it counts, and never
    touches aliases.yaml.

    Before this existed, the only way to get these numbers was a hand-written script
    scanning the whole store, which the panel obviously cannot do on every page load.
    Merging and renaming are **write** operations and live on a separate endpoint, so
    that the rule holds: the system lays things out, and the merge is a human click.
    """
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_subjects())
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] subjects 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------
# Click through to the original text, reusing recall's direct-id-lookup rules: verbatim,
# never truncated.
# ---------------------------------------------------------
async def api_loci_bucket(request: Request) -> Response:
    from starlette.responses import JSONResponse
    bucket_id = str(request.path_params.get("bucket_id") or "").strip()
    if not bucket_id:
        return JSONResponse({"error": "missing id"}, status_code=400)
    try:
        from tools.recall.core import _room_cn, _short_id
        from core._rooms import normalize_room, is_event_room
        b = await sh.bucket_mgr.get_including_archive(bucket_id)
        if not b:
            return JSONResponse(
                {"error": f"查无此桶：{bucket_id}（可能已物理删除或打错，不做语义联想）"},
                status_code=404)
        meta = b.get("metadata", {}) or {}
        room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
        archived = (str(meta.get("type") or "") == "archived"
                    or bool(meta.get("tombstone")) or bool(meta.get("deleted_at")))
        return JSONResponse({
            "id": bucket_id,
            "short": _short_id(bucket_id),
            "name": str(meta.get("name") or ""),
            "summary": str(meta.get("summary") or ""),
            "content": str(b.get("content") or ""),   # verbatim, never truncated
            "room": room,
            "room_cn": _room_cn(meta.get("room")),
            "when": str(meta.get("when") or ""),
            "created": str(meta.get("created") or ""),
            "last_active": str(meta.get("last_active") or ""),
            "valence": meta.get("valence"),
            "arousal": meta.get("arousal"),
            "status": str(meta.get("status") or ""),
            "pinned": bool(meta.get("pinned")),
            "tags": [str(t) for t in (meta.get("tags") or [])],
            # Subjects are a third kind of tag, sitting alongside tags and aliases and
            # never mixed with them.
            "subjects": [str(s) for s in (meta.get("subjects") or [])],
            "from": read_from_ids(meta),
            "supersedes": _split_ids(meta.get("supersedes")),
            "superseded_by": str(meta.get("superseded_by") or ""),
            "archived": archived,
            # The `meaning` write path is retired, but **existing data on disk is still
            # displayed** — there are true things in there, and where they end up should
            # be decided after reading them, not before.
            "meaning": meta.get("meaning") or [],
            "why_remembered": str(meta.get("why_remembered") or ""),
            # Only an event may be corrected here; mind gets no such opening. Archived
            # buckets cannot be edited either — correcting one means restoring it first,
            # the same rule regrow follows.
            "can_edit": bool(is_event_room(room)) and not archived,
        })
    except Exception as e:
        logger.warning(f"[loci] bucket 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
