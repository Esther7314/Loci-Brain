"""
========================================
core/census.py — counts over the live store: what is in each room, who is in here
========================================

Two of the panel's reads are counts over one listing of the live store
(`list_all(include_archive=False)`): the directory both doors open onto
(GET /api/loci/rooms) and the "who is in here" screen (GET /api/loci/subjects). The
route lists the store and turns the dict into JSON; the counting is here.

Both measure the way recall does: the timeline gate (`core/visibility.on_timeline`)
filters first, so the number on the panel is the number the model sees on waking.
**Read-only; nothing is written to disk.**

The names page and the name card (contract 「面板接口」 §四, §五 name) count over the same
listing: `names_page` (the names the table knows and gives a kind, by kind, most
mentioned first), `pending_names` (the names it does not know yet or knows without a
kind, each with the entry it first appeared in and what it looks like, newest first),
`name_card` (one name: what the table says, its card, the entries it
appears in). Their lists page the way every panel list does (core/paging.py), counting
only entries written before `as_of` so a page does not shift while it is read
(`written_before`).
`name_action` is the one way the names page's buttons write: it goes through
core/names (aliases.yaml only, never an entry) and says what it did in words.

Exports: rooms(all_buckets) · subjects(all_buckets) · written_before ·
         names_page · pending_names · name_card · NAME_ACTIONS · name_action
========================================
"""

from collections import Counter
from datetime import datetime

from . import names as subj
from ._rooms import ALL_ROOMS, normalize_room, room_cn
from .paging import PAGE_LIMIT, cut, page
from .starfield import node_ts
from .visibility import on_timeline


def rooms(all_buckets: list, scope=None) -> dict:
    """The directory both doors open onto: how many entries in each of the four rooms, plus
    the ten most frequent tags — counted over what `scope` (the request's read scope) may
    read.

    The I/YOU dimension lives in subjects — "who is this about" is not carried by the room.
    """
    counts: Counter = Counter()
    tags: Counter = Counter()
    homeless = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not on_timeline(meta, scope):
            continue
        # Normalize before counting: a room that is not one of the four counts as homeless,
        # and the doors read only the four.
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
            {"room": r, "cn": room_cn(r), "n": counts.get(r, 0)})
    return {
        "doors": doors,
        "homeless": homeless,
        "total": sum(counts.values()) + homeless,
        "top_tags": [{"tag": t, "n": n} for t, n in tags.most_common(10)],
    }


def subjects(all_buckets: list) -> dict:
    """"Who is in here": every subject that has appeared in the store, how many entries each
    has, and when each last appeared.

    Why this screen exists: `aliases.yaml` is maintained by hand, and maintaining anything by
    hand presupposes knowing there is something to change. Nobody tells you when a new
    person appears; nobody tells you when one person splits into two spellings; nobody
    tells you when extraction noise creeps in (a fragment of ordinary prose mistaken for a
    name).

    Same principle as muse and fold: **the system only lays things out; the merge itself is
    a human click.** So this counts, and writes not one character into aliases.yaml.

    Time comes from `node_ts` (when first, created as fallback).
    """
    table = subj.load_alias_table()           # {alias in lowercase: canonical name}
    blocked = subj.load_not_person()          # entries marked "this is not a person" (lowercase)
    counts: Counter = Counter()
    variants: dict[str, set] = {}             # canonical name -> the spellings actually found on disk
    last: dict[str, datetime] = {}
    last_bucket: dict[str, str] = {}
    first: dict[str, datetime] = {}
    first_bucket: dict[str, str] = {}
    blocked_hits: Counter = Counter()
    total = 0
    with_subj = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not on_timeline(meta):
            continue
        total += 1
        raws = [str(x).strip() for x in (meta.get("subjects") or []) if str(x).strip()]
        if raws:
            with_subj += 1
        ts = node_ts(meta)
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
            if ts is not None and (canon not in first or ts < first[canon]):
                first[canon] = ts
                first_bucket[canon] = bid
    names_out = []
    for nm, c in counts.most_common():
        ts = last.get(nm)
        rec = subj.record_of(nm)
        names_out.append({
            "name": nm,
            "n": c,
            "last": ts.strftime("%Y-%m-%d") if ts else "",
            "last_bucket": last_bucket.get(nm, ""),
            "first": first[nm].strftime("%Y-%m-%d") if nm in first else "",
            "first_bucket": first_bucket.get(nm, ""),
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


# ============================================================
# The names page and the name card
# ============================================================

def written_before(all_buckets: list, as_of: datetime | None) -> list:
    """The entries not written after `as_of` (core/paging.past; all of them when it is
    None); one with no readable `created` counts as written before."""
    from ._when import parse_stamp
    return cut(all_buckets, as_of,
               lambda b: parse_stamp((b.get("metadata") or {}).get("created")))


def _newest_first(b: dict) -> float:
    """Sort key: newest first by node_ts; an entry with no time last."""
    ts = node_ts(b.get("metadata") or {})
    return -ts.timestamp() if ts is not None else float("inf")


def _memory_line(b: dict) -> dict:
    from .detail import date_of
    from .profile import entry_label, short_id
    meta = b.get("metadata") or {}
    bid = str(meta.get("id") or b.get("id") or "")
    return {"id": bid, "short": short_id(bid), "date": date_of(meta),
            "text": entry_label(meta, str(b.get("content") or ""))}


def _recognised(row: dict) -> bool:
    """A name is recognised once the table knows it and says what it is. One the table
    knows without a kind (aliases alone, or a name merged into before kinds existed) is
    still waiting to be told what it is, so it waits on the pending page."""
    return bool(row["canonical"] and row["kind"])


def names_page(all_buckets: list, *, kind: str = "", offset: int = 0,
               limit: int = PAGE_LIMIT, as_of: datetime | None = None) -> dict:
    """The names page: the recognised names (`_recognised`) that appear in the store, most
    mentioned first, filtered to one `kind` when given; the kinds with how many names
    each; how many names wait to be recognised."""
    rows = subjects(written_before(all_buckets, as_of))["names"]
    known = [r for r in rows if _recognised(r)]
    kinds = Counter(r["kind"] for r in known)
    items = []
    for r in known:
        if kind and r["kind"] != kind:
            continue
        rec = subj.record_of(r["name"])
        items.append({"name": r["name"], "kind": r["kind"], "n": r["n"],
                      "aliases": list(rec.aliases) if rec else [], "last": r["last"]})
    return {"kinds": [{"kind": k, "n": n} for k, n in
                      sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
            **page(items, offset, limit, as_of),
            "pending_count": sum(1 for r in rows if not _recognised(r))}


def _guess(row: dict, guesses: dict | None) -> str | None:
    """What a waiting name looks like: the kind the side model said when the table could
    not take it (core/name_guesses); for a name the table knows without a kind but hangs
    in a work or a group (present_in / member_of), a person — characters and members are
    people in this table. None when nothing says."""
    from . import name_guesses as _G
    said = _G.guess_of(guesses or {}, row["name"])
    if said:
        return said
    if row["canonical"] and (row["present_in"] or row["member_of"]):
        return subj.KIND_PERSON
    return None


def pending_names(all_buckets: list, *, offset: int = 0, limit: int = PAGE_LIMIT,
                  as_of: datetime | None = None, guesses: dict | None = None) -> dict:
    """The names waiting to be recognised (`_recognised`), the most recently first seen on
    top, each with the entry it first appeared in, whether the table already lists it
    (`in_table`), and `guess`: what it looks like (`_guess`), or None. `guesses` is
    core/name_guesses.load()."""
    listing = written_before(all_buckets, as_of)
    by_id = {str((b.get("metadata") or {}).get("id") or b.get("id") or ""): b for b in listing}
    rows = [r for r in subjects(listing)["names"] if not _recognised(r)]
    rows.sort(key=lambda r: (r["first"], r["name"]), reverse=True)
    items = []
    for r in rows:
        b = by_id.get(r["first_bucket"])
        items.append({"name": r["name"], "n": r["n"], "pronoun": r["pronoun"],
                      "in_table": bool(r["canonical"]), "guess": _guess(r, guesses),
                      "first": _memory_line(b) if b else None})
    return page(items, offset, limit, as_of)


def name_card(all_buckets: list, name: str, *, offset: int = 0, limit: int = PAGE_LIMIT,
              as_of: datetime | None = None, scope=None) -> dict | None:
    """One name's card: what the table says it is and where it hangs, the MIND entry
    filed as its card, and the entries whose subjects name it (after the table's
    normalising), newest first. None when neither the table nor the store knows it, and
    under a narrowing `scope` when no entry it may read names it."""
    n = str(name or "").strip()
    if not n:
        return None
    rec = subj.record_of(n)
    key = rec.name if rec else n
    table = subj.load_alias_table()
    card_row = None
    hits = []
    for b in all_buckets:
        meta = b.get("metadata") or {}
        if not on_timeline(meta, scope):
            continue
        filed = str(meta.get("card_of") or "").strip()
        if filed and subj.name_key(filed) == key:
            if card_row is None or str(meta.get("created") or "") > str(
                    (card_row.get("metadata") or {}).get("created") or ""):
                card_row = b
        raws = {table.get(str(x).strip().lower(), str(x).strip())
                for x in (meta.get("subjects") or []) if str(x).strip()}
        if key in raws:
            hits.append(b)
    if rec is None and not hits and card_row is None:
        return None
    # The names table is the whole library's: a scoped read learns what it says of a
    # name only through an entry it may read that names it.
    from .scope import narrows
    if narrows(scope) and not hits and card_row is None:
        return None
    hits = written_before(hits, as_of)
    hits.sort(key=_newest_first)
    card = None
    if card_row is not None:
        line = _memory_line(card_row)
        card = {"id": line["id"], "short": line["short"],
                "text": str(card_row.get("content") or "").strip() or line["text"]}
    return {"name": key,
            "kind": rec.instance_of if rec else "",
            "aliases": list(rec.aliases) if rec else [],
            "present_in": list(rec.present_in) if rec else [],
            "member_of": list(rec.member_of) if rec else [],
            "card": card,
            "memories": page([_memory_line(b) for b in hits], offset, limit, as_of)}


NAME_ACTIONS = ("not_person", "merge", "rename", "set_kind")


def name_action(action: str, name, target=None, kind=None) -> dict:
    """One button on the names page: {changed, note}. Raises ValueError for an action
    it does not know or a name core/names refuses.

      not_person  this is not a name -> the not-a-person list; no entry is touched
      merge       these two are one -> `name` folded into `target`
      rename      its proper name -> the same, `target` the new canonical name
      set_kind    what it is (人 / 书 / 游戏 …) -> its instance_of

    Like every edit of the table, it applies going forward: entries keep the spelling
    they were written with, and the panel collapses them by the table."""
    action = str(action or "").strip()
    if action == "not_person":
        changed = subj.mark_not_person(name)
        note = "记下了，以后不再抽它（历史那几条没动）" if changed else "它已经在黑名单里了"
    elif action in ("merge", "rename"):
        changed = subj.merge_names(name, target)
        note = ("写进别名表了 —— 只管以后，老条目盘上还是老名字" if changed
                else "这条已经在表里了")
    elif action == "set_kind":
        changed = subj.set_kind(name, kind)
        note = (f"记下了：「{str(name).strip()}」是{str(kind).strip()} —— 只改名字表，老条目没动"
                if changed else "表里已经是这样了")
    else:
        raise ValueError("action 只有四个：not_person / merge / rename / set_kind")
    return {"changed": bool(changed), "note": note}
