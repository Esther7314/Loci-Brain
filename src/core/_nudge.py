"""
========================================
core/_nudge.py — 「戳一下」 on muse's page: one line the model is told once
========================================

The panel's muse page has a button on each cluster of thoughts (`POST
/api/loci/muse/nudge`, web/loci_activity.py). A poke is not a summary and not a
suggestion: it is a tap on his shoulder about that cluster, told in one fixed line
(`line_of`, worded by the owner) with how many entries it holds and the first one's label
as the panel shows it (core/profile.entry_label).

When he hears it: the next Loci tool he calls carries the line at the end of its reply
(server._run_with_notice, every tool, every op) — the same rule breath's questions are
asked by (tools/breath/awaken.stamp_asked): handing it out is saying it, and it is stamped
`seen_at` in the same step, under the file's lease, so two calls at once never both say
it. Said once, it is never said again; poking the same cluster after that changes
nothing (`seen` stays `seen`). It wakes nobody and queues nothing.

A call that may not see every member of the cluster on muse's road (core/visibility,
`muse`) — under a read scope that leaves one out, or with one archived, deleted or
standing on a withdrawn source since — is not told, and the poke waits for a call that may.
The label is read from the entry at that moment, never stored: the file holds ids and
moments only, so a source change has nothing to clear here.

Nothing here goes into the ledger (panel contract §七). One small file,
`<buckets>/_state/muse_nudges.json`:

    {"version": 1, "nudges": {cluster_id: {"ids": [...], "poked_at": iso,
                                           "seen_at": iso | null}}}

The cluster id is core/muse_view.cluster_id (members sorted, so one member more or fewer
is another cluster); `ids` keep the order the panel lists them in, which is what makes
the first one the panel's first.

Exports: FILE · NONE · POKED · SEEN · line_of · load · states · poke · take_lines
========================================
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .ledger_mirror import file_lease

FILE = "muse_nudges.json"
_DIR = "_state"
_VERSION = 1

NONE, POKED, SEEN = "none", "poked", "seen"


def line_of(n: int, label: str) -> str:
    """The line he is told."""
    return f"ta戳了戳你：这团（{n} 条，第一条是《{label}》）"


def _path(base_dir: str) -> Path:
    return Path(str(base_dir or ".")) / _DIR / FILE


def _lock(base_dir: str) -> Path:
    p = _path(base_dir)
    return p.with_name(p.name + ".lock")


def load(base_dir: str) -> dict:
    """{cluster_id: {ids, poked_at, seen_at}}; unreadable or absent is empty."""
    try:
        with open(_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    got = data.get("nudges") if isinstance(data, dict) else None
    if not isinstance(got, dict):
        return {}
    return {str(k): v for k, v in got.items()
            if isinstance(v, dict) and isinstance(v.get("ids"), list)}


def _save(base_dir: str, nudges: dict) -> None:
    path = _path(base_dir)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": _VERSION, "nudges": nudges}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _state(entry: dict | None) -> dict:
    if not entry:
        return {"state": NONE}
    if entry.get("seen_at"):
        return {"state": SEEN, "at": entry["seen_at"]}
    return {"state": POKED, "at": entry.get("poked_at")}


def states(base_dir: str) -> dict:
    """{cluster_id: {state, at}} for every cluster poked."""
    return {cid: _state(e) for cid, e in load(base_dir).items()}


def poke(base_dir: str, cluster_id: str, ids: list, now: datetime) -> dict:
    """Poke a cluster: {state, at}. Poked again before it was said, or after, it stays
    as it is — several pokes are one."""
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(_lock(base_dir)):
        nudges = load(base_dir)
        entry = nudges.get(cluster_id)
        if entry is None:
            entry = {"ids": [str(i) for i in ids], "poked_at": now.isoformat(timespec="seconds"),
                     "seen_at": None}
            nudges[cluster_id] = entry
            _save(base_dir, nudges)
        return _state(entry)


async def _sayable(store, entry: dict, scope, now: datetime) -> str | None:
    """The line for a poked cluster when every member may be seen on muse's road by this
    call, else None."""
    from . import visibility as _V
    from .profile import entry_label
    ids = [str(i) for i in entry.get("ids") or []]
    if not ids:
        return None
    first = None
    for bid in ids:
        b = await store.get_including_archive(bid)
        if not b or not _V.visible_for(b, scope, road=_V.MUSE, now=now):
            return None
        if first is None:
            first = b
    meta = first.get("metadata") or {}
    return line_of(len(ids), entry_label(meta, str(first.get("content") or "")))


async def take_lines(base_dir: str, store, view_of, now: datetime) -> list[str]:
    """The lines due to this call, stamped `seen_at` as they are handed out. `view_of`
    is an async callable giving this call's read scope (core/scope.ScopeView), called
    only when something is waiting to be said."""
    if not os.path.exists(_path(base_dir)):
        return []
    waiting = {cid: e for cid, e in load(base_dir).items() if not e.get("seen_at")}
    if not waiting:
        return []
    scope = await view_of()
    lines: dict[str, str] = {}
    for cid, entry in waiting.items():
        line = await _sayable(store, entry, scope, now)
        if line is not None:
            lines[cid] = line
    if not lines:
        return []
    said: list[str] = []
    with file_lease(_lock(base_dir)):
        nudges = load(base_dir)
        for cid, line in lines.items():
            entry = nudges.get(cid)
            if entry is None or entry.get("seen_at"):
                continue          # another call said it meanwhile
            entry["seen_at"] = now.isoformat(timespec="seconds")
            said.append(line)
        if said:
            _save(base_dir, nudges)
    return said
