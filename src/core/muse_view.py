"""
========================================
core/muse_view.py — muse's page on the panel: the thoughts that look like one thing, and
the stretches of days without a name
========================================

The same clusters and gestures the muse tool and `/api/muse/pending` see, from the same
cached pass (core/_muse.both_sides) — the route hands them over; nothing here recomputes
them. Two lists, each paged on its own (`part`):

  clusters   the mind side: thoughts on one v/a shelf, with their evidence in words
             (`evidence_words`): the shelf, the from-chains, the semantic top-up — each
             said apart, as the tool's own evidence line keeps them apart.
  days       the event side: a word burst, a composition drift, a stretch no period
             covers; each gesture's `evidence` is already one sentence.

A cluster's id is `c_` + the first 10 hex of sha1 over its members' ids, sorted (the
same group as core/_muse.rejection_key: one member more or fewer is a new cluster). A
gesture is named the same way, by the ids it points at, or by its kind and days when it
points at none. A nudge on a cluster (`nudge`) is not kept anywhere: every
cluster says `none`.

Exports: PARTS · cluster_id · evidence_words · panel_view
========================================
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from . import _muse as M
from .activity import page, past

CLUSTERS, DAYS = "clusters", "days"
PARTS = (CLUSTERS, DAYS)

# The event side's gestures (core/_muse.propose_gist's keys) as the panel names them.
_GESTURES = {"词爆发": ("burst", "一个词扎堆出现"),
             "成分漂移": ("drift", "前后不一样了"),
             "空白记账": ("blank", "一段没有名字的日子")}


def cluster_id(ids) -> str:
    """`c_` + sha1 of the sorted member ids, first 10 hex."""
    key = M.rejection_key(ids)
    return "c_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def evidence_words(cluster: "M.Cluster") -> str:
    """A cluster's evidence as a person reads it: they sit on one shelf of feeling; how
    many grew one out of another (the from-chains I linked); how many were brought in by
    meaning alone."""
    parts = ["心情落在一块"]
    if cluster.from_core:
        parts.append(f"有 {len(cluster.from_core)} 条是一路长出来的")
    if cluster.semantic_add:
        parts.append(f"意思上又补进 {len(cluster.semantic_add)} 条")
    if not cluster.from_core:
        parts.append("没有一路长出来的痕迹，全靠意思相近")
    return " · ".join(parts)


def _day(dt) -> str | None:
    return dt.date().isoformat() if dt is not None else None


def _newest(items) -> datetime | None:
    stamps = [it.created for it in items if it.created is not None]
    return max(stamps) if stamps else None


def _cluster_row(c: "M.Cluster") -> dict:
    stamps = [it.created for it in c.items if it.created is not None]
    return {"id": cluster_id(c.ids), "kind": "thoughts", "n": len(c.ids),
            "ids": list(c.ids), "evidence_words": evidence_words(c),
            "oldest": _day(min(stamps)) if stamps else None,
            "nudge": {"state": "none"}}


def _finger_row(name: str, f: "M.Finger") -> dict:
    kind, words = _GESTURES.get(name, ("other", name))
    if f.ids:
        fid = cluster_id(f.ids)
    else:
        raw = f"{kind}|{_day(f.start)}|{_day(f.end)}|{_day(f.boundary)}"
        fid = "c_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return {"id": fid, "kind": kind, "kind_words": words, "n": len(f.ids),
            "ids": list(f.ids), "evidence_words": f.evidence, "start": _day(f.start),
            "end": _day(f.end), "boundary": _day(f.boundary)}


def panel_view(clusters: list, fingers: dict, *, part: str, offset: int, limit: int,
               as_of: datetime) -> dict:
    """One part of muse's page, paged: the clusters in the order muse lays them out, or
    the gestures (word bursts, then drifts, then blank stretches). A cluster or gesture
    with a member written after `as_of` is left out — it is a new group since the first
    page."""
    if part not in PARTS:
        raise ValueError(f"part must be one of {PARTS}")
    rows = []
    if part == CLUSTERS:
        for c in clusters or []:
            newest = _newest(c.items)
            if not past(newest, as_of):
                rows.append(_cluster_row(c))
    else:
        for name in _GESTURES:
            for f in (fingers or {}).get(name, []):
                newest = _newest(f.items)
                if not past(newest, as_of):
                    rows.append(_finger_row(name, f))
        for name, lst in (fingers or {}).items():
            if name not in _GESTURES:
                rows.extend(_finger_row(name, f) for f in lst)
    return {"part": part, **page(rows, offset, limit, as_of)}
