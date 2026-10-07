"""
========================================
core/similar_kept.py — the suspected-duplicate pairs the owner said to keep
========================================

The panel's 疑似重复 (web/loci_similar.py) lists pairs of live memories whose vectors
score close. 「留着」 says "these two are not one thing written twice", and the pair is
then left off the list for as long as neither entry changes.

An entry has changed when its text has: the text is what its vector, and so the pair's
score, is made from. It is told by the marker the vector queue already uses for "the
exact text a vector represents" (core/embedding_outbox.content_hash). Once either entry's
marker differs from the one kept, the pair is judged again and may come back. A regrow
writes a new entry with a new id, so a pair with the new version is a new pair.

Only ids and those markers are kept, never a memory's text, so a withdrawn or deleted
source has nothing to clear here. A pair one of whose entries has left the page
(archived, deleted, folded, gone) is not listed anyway; its record is dropped the next
time a pair is kept, together with every record whose marker no longer matches.

One small file, `<buckets>/_state/similar_kept.json`:
    {"version": 1, "pairs": {"<a>|<b>": {"marks": {a: marker, b: marker}, "at": iso}}}
with the two ids sorted in the key.

Exports: FILE · marker(content) · key(a, b) · load(base_dir) ·
         kept_now(base_dir, marker_of) · keep(base_dir, a, b, marker_of, at)
========================================
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from .embedding_outbox import content_hash
from .ledger_mirror import file_lease

FILE = "similar_kept.json"
_DIR = "_state"
_VERSION = 1

# id -> the entry's marker now, or None when the entry is not on the page.
MarkerOf = Callable[[str], "str | None"]


def _path(base_dir: str) -> Path:
    return Path(str(base_dir or ".")) / _DIR / FILE


def marker(content: str) -> str:
    """An entry's version marker: its text's hash, as the vector queue keys it."""
    return content_hash(str(content or ""))


def key(a: str, b: str) -> str:
    """One pair, whichever end comes first."""
    return "|".join(sorted((str(a), str(b))))


def load(base_dir: str) -> dict:
    """{pair key: {"marks": {id: marker}, "at": iso}}; unreadable or absent is empty (every
    pair is then judged again, the safe side)."""
    try:
        with open(_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    got = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(got, dict):
        return {}
    return {str(k): v for k, v in got.items()
            if isinstance(v, dict) and isinstance(v.get("marks"), dict)}


def _holds(record: dict, marker_of: MarkerOf) -> bool:
    """Both entries are on the page now with the markers they had when the pair was
    kept."""
    marks = record.get("marks") or {}
    return len(marks) == 2 and all(
        (now := marker_of(str(i))) is not None and now == str(m) for i, m in marks.items())


def kept_now(base_dir: str, marker_of: MarkerOf) -> set[str]:
    """The keys of the kept pairs that still hold: neither entry changed or left."""
    return {k for k, rec in load(base_dir).items() if _holds(rec, marker_of)}


def keep(base_dir: str, a: str, b: str, marker_of: MarkerOf, at: datetime) -> dict:
    """Keep the pair (a, b) with both entries' markers now, and drop every record that no
    longer holds. Returns the record written. ValueError when an end is not on the page."""
    marks = {str(i): marker_of(str(i)) for i in (a, b)}
    missing = [i for i, m in marks.items() if m is None]
    if missing or len(marks) != 2:
        raise ValueError(f"not on the page: {', '.join(missing) or a}")
    record = {"marks": marks, "at": at.isoformat(timespec="seconds")}
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(path.with_name(path.name + ".lock")):
        pairs = {k: rec for k, rec in load(base_dir).items() if _holds(rec, marker_of)}
        pairs[key(a, b)] = record
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _VERSION, "pairs": pairs}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    return record
