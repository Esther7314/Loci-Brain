"""
========================================
core/name_guesses.py — what the side model said a name is, waiting for the owner
========================================

The backfill asks the side model what each name in an entry is (core/dehydrator, subjects
`{name, kind, card}`) and keeps the kind here as a guess (tools/grow/_backfill._record_kinds)
for a name the table does not have, one it has without a kind, and a spelling several
entries of the table claim — so the names page can say 「看着像 <kind>」 on that name while it waits to be
recognised (core/census.pending_names). For a name the table does not have, the card the
side model says it is (another spelling of someone the table already has) is kept beside
the kind. A guess never reaches the table by itself: only a click on the names page does
that.

Only hashes of lowercased spellings — the name's, and the card's — and the kind are kept,
never a name itself, so a withdrawn or deleted source leaves nothing readable here; a
guess is shown only for a name that a live entry still carries, and a card only while the
table still has it. The newest `KEEP` guesses are kept.

One small file, `<buckets>/_state/name_guesses.json`:
    {"version": 1, "guesses": {"<hash>": {"kind": "<kind>", "card": "<hash>", "at": iso}}}

Exports: FILE · KEEP · key_of(name) · load(base_dir) · guess_of(guesses, name) ·
         card_of(guesses, name, cards) · record(base_dir, name, kind, at, card="")
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
    """{key: {"kind", "card", "at"}}, each with a kind or a card; unreadable or absent is
    empty (no guess is shown)."""
    try:
        with open(_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    got = data.get("guesses") if isinstance(data, dict) else None
    if not isinstance(got, dict):
        return {}
    return {str(k): v for k, v in got.items()
            if isinstance(v, dict) and (str(v.get("kind") or "").strip()
                                        or str(v.get("card") or "").strip())}


def guess_of(guesses: dict, name) -> str:
    """The kind guessed for this spelling, or ""."""
    rec = (guesses or {}).get(key_of(name))
    return str(rec.get("kind") or "").strip() if isinstance(rec, dict) else ""


def card_of(guesses: dict, name, cards) -> str:
    """The card guessed for this spelling, as one of `cards` (the names the table has
    now), or "" when none was guessed or the table no longer has it."""
    rec = (guesses or {}).get(key_of(name))
    kept = str(rec.get("card") or "").strip() if isinstance(rec, dict) else ""
    if not kept:
        return ""
    return next((c for c in cards if key_of(c) == kept), "")


def record(base_dir: str, name, kind, at: datetime, card="") -> None:
    """Keep `kind`, and `card` when one was named, as the guess for `name` (the newest
    guess for a spelling wins), and drop the oldest past `KEEP`."""
    kind, card = str(kind or "").strip(), str(card or "").strip()
    if not (kind or card) or not str(name or "").strip():
        return
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(path.with_name(path.name + ".lock")):
        guesses = load(base_dir)
        guess = {"kind": kind, "at": at.isoformat(timespec="seconds")}
        if card:
            guess["card"] = key_of(card)
        guesses[key_of(name)] = guess
        if len(guesses) > KEEP:
            newest = sorted(guesses.items(), key=lambda kv: str(kv[1].get("at") or ""),
                            reverse=True)[:KEEP]
            guesses = dict(newest)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _VERSION, "guesses": guesses}, f, ensure_ascii=False,
                      indent=1)
        os.replace(tmp, path)
