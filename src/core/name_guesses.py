"""
========================================
core/name_guesses.py — what the side model said a name is, when the names table could not take it
========================================

The backfill asks the side model what each name in an entry is (core/dehydrator, subjects
`{name, kind}`) and files the kind into the names table in the two ways it may
(tools/grow/_backfill._record_kinds). When it cannot — the spelling is claimed by several
entries of the table, or the table could not be written — the kind is kept here as a
guess, so the names page can say 「看着像 <kind>」 on that name while it waits to be
recognised (core/census.pending_names). A guess never reaches the table by itself: only a
click on the names page does that.

Only a hash of the lowercased spelling and the kind are kept, never the name itself, so a
withdrawn or deleted source leaves nothing readable here; a guess is shown only for a
name that a live entry still carries. The newest `KEEP` guesses are kept.

One small file, `<buckets>/_state/name_guesses.json`:
    {"version": 1, "guesses": {"<hash>": {"kind": "<kind>", "at": iso}}}

Exports: FILE · KEEP · key_of(name) · load(base_dir) · guess_of(guesses, name) ·
         record(base_dir, name, kind, at)
========================================
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from .ledger_mirror import file_lease

FILE = "name_guesses.json"
_DIR = "_state"
_VERSION = 1
KEEP = 1000


def _path(base_dir: str) -> Path:
    return Path(str(base_dir or ".")) / _DIR / FILE


def key_of(name) -> str:
    """The key a spelling is kept under: a hash of it lowercased (the names table compares
    spellings case-blind)."""
    low = str(name or "").strip().lower()
    return "sha256:" + hashlib.sha256(low.encode("utf-8")).hexdigest()[:32]


def load(base_dir: str) -> dict:
    """{key: {"kind", "at"}}; unreadable or absent is empty (no guess is shown)."""
    try:
        with open(_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    got = data.get("guesses") if isinstance(data, dict) else None
    if not isinstance(got, dict):
        return {}
    return {str(k): v for k, v in got.items()
            if isinstance(v, dict) and str(v.get("kind") or "").strip()}


def guess_of(guesses: dict, name) -> str:
    """The kind guessed for this spelling, or ""."""
    rec = (guesses or {}).get(key_of(name))
    return str(rec.get("kind") or "").strip() if isinstance(rec, dict) else ""


def record(base_dir: str, name, kind, at: datetime) -> None:
    """Keep `kind` as the guess for `name` (the newest guess for a spelling wins), and
    drop the oldest past `KEEP`."""
    kind = str(kind or "").strip()
    if not kind or not str(name or "").strip():
        return
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(path.with_name(path.name + ".lock")):
        guesses = load(base_dir)
        guesses[key_of(name)] = {"kind": kind, "at": at.isoformat(timespec="seconds")}
        if len(guesses) > KEEP:
            newest = sorted(guesses.items(), key=lambda kv: str(kv[1].get("at") or ""),
                            reverse=True)[:KEEP]
            guesses = dict(newest)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _VERSION, "guesses": guesses}, f, ensure_ascii=False,
                      indent=1)
        os.replace(tmp, path)
