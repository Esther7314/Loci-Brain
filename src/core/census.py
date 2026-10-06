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

Exports: rooms(all_buckets) · subjects(all_buckets)
========================================
"""

from collections import Counter
from datetime import datetime

from . import names as subj
from ._rooms import ALL_ROOMS, normalize_room, room_cn
from .starfield import node_ts
from .visibility import on_timeline


def rooms(all_buckets: list) -> dict:
    """The directory both doors open onto: how many entries in each of the four rooms, plus
    the ten most frequent tags.

    The I/YOU dimension lives in subjects — "who is this about" is not carried by the room.
    """
    counts: Counter = Counter()
    tags: Counter = Counter()
    homeless = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not on_timeline(meta):
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
