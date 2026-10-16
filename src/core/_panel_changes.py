"""
========================================
core/_panel_changes.py — what the owner changed on the panel, told to the model once
========================================

The regrow/fold page's 日子 / 想法 column (core/periods.py) changes things the model drew:
a period's name or span, a merged thought's day or roster, a memory's day. Each change
leaves one line here, in the panel's words at that moment. The model hears them on his
next Loci tool call, whichever tool it is — breath is too rarely called to wait for —
at the end of its reply (server_call._run_with_notice), the way muse's poke is said
(core/_nudge.py):

    <name>在面板上改了 N 处：
    · 日子「搬家那周」改了起止：10-01..10-07 → 09-29..10-06
    · …                                    (the first SHOWN, oldest first)
    ……（还有 M 处没列，都在面板 REGROW/FOLD 里）

Handing them out is saying them: every line waiting is stamped `seen_at` in the same
step, under the file's lease, so two calls at once never both say them. Only a call that
reads the whole library is told (the lines name entries); a scoped host's call is not,
and the lines wait.

Nothing here goes into the ledger: the edits themselves went through trace, which
records them. One small file, `<buckets>/_state/panel_changes.json`:

    {"version": 1, "changes": [{"at": iso, "line": str, "ids": [...], "seen_at": iso|null}]}

The newest KEEP lines already said are kept; older said ones are dropped.

Exports: FILE · SHOWN · record · waiting · take_text
========================================
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .ledger_mirror import file_lease

FILE = "panel_changes.json"
_DIR = "_state"
_VERSION = 1
SHOWN = 10          # lines listed in one notice; the rest are counted
KEEP = 200          # lines already said that stay on file


def _path(base_dir: str) -> Path:
    return Path(str(base_dir or ".")) / _DIR / FILE


def _lock(base_dir: str) -> Path:
    p = _path(base_dir)
    return p.with_name(p.name + ".lock")


def _load(base_dir: str) -> list[dict]:
    try:
        with open(_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    got = data.get("changes") if isinstance(data, dict) else None
    return [c for c in got if isinstance(c, dict) and c.get("line")] if isinstance(got, list) else []


def _save(base_dir: str, changes: list[dict]) -> None:
    said = [c for c in changes if c.get("seen_at")]
    if len(said) > KEEP:
        drop = {id(c) for c in said[:-KEEP]}
        changes = [c for c in changes if id(c) not in drop]
    path = _path(base_dir)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": _VERSION, "changes": changes}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def record(base_dir: str, line: str, ids: list, now: datetime) -> None:
    """One change, waiting to be told."""
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(_lock(base_dir)):
        changes = _load(base_dir)
        changes.append({"at": now.isoformat(timespec="seconds"), "line": str(line),
                        "ids": [str(i) for i in ids or []], "seen_at": None})
        _save(base_dir, changes)


def waiting(base_dir: str) -> list[dict]:
    """The changes not yet told, oldest first."""
    if not os.path.exists(_path(base_dir)):
        return []
    return [c for c in _load(base_dir) if not c.get("seen_at")]


def _text(lines: list[str], actor: str) -> str:
    head = f"{actor}在面板上改了 {len(lines)} 处："
    shown = [f"· {line}" for line in lines[:SHOWN]]
    rest = len(lines) - SHOWN
    tail = [f"……（还有 {rest} 处没列，都在面板 REGROW/FOLD 里）"] if rest > 0 else []
    return "\n".join([head, *shown, *tail])


def take_text(base_dir: str, now: datetime, *, whole_library: bool, actor: str) -> str:
    """The notice due to this call ("" when nothing waits or the call may not hear it),
    every waiting line stamped `seen_at` as it is handed out."""
    if not whole_library or not waiting(base_dir):
        return ""
    with file_lease(_lock(base_dir)):
        changes = _load(base_dir)
        due = [c for c in changes if not c.get("seen_at")]
        if not due:
            return ""
        stamp = now.isoformat(timespec="seconds")
        for c in due:
            c["seen_at"] = stamp
        _save(base_dir, changes)
    return _text([c["line"] for c in due], actor)
