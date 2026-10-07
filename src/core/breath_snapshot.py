"""
========================================
core/breath_snapshot.py — the last breath each host handed out, kept for the panel
========================================

The panel's breath page shows the screen the model was actually given, not one computed
now: the last breath handed out (the breath tool, `/api/v2/breath` without `peek`), one
per host, with the scope line it was handed out under. tools/breath/awaken.keep_last saves
it at the moment it is handed out, beside the usage log's `shown` lines.

Only the structure is kept — ids and short ids, kinds, reasons and their words, counts,
how and why something came up — never a memory's text: every `text` is cut on the way in
(`skeleton`). The panel reads the titles fresh through the gate (`relabel`), so an entry
deleted, archived or standing on a withdrawn source since then shows as that, not as what
it said. Nothing here has to be cleared when a source is withdrawn.

One small file, `<buckets>/_state/breath_last.json`:
    {"version": 1, "hosts": {host: {"at", "host", "scope", "breath"}}}
Each host's entry is replaced whole every time. `host` is "" for a call made outside any
host's request (a direct call).

Exports: FILE · skeleton(breath) · save(base_dir, breath, *, host, scope_line, at) ·
         load(base_dir, host=None) · hosts(base_dir) · relabel(breath, all_buckets, scope=None)
========================================
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import visibility as _V
from .ledger_mirror import file_lease

FILE = "breath_last.json"
_DIR = "_state"
_VERSION = 1


def _path(base_dir: str) -> Path:
    return Path(str(base_dir or ".")) / _DIR / FILE


def skeleton(breath: dict):
    """The breath object with every `text` taken out, at any depth."""
    if isinstance(breath, dict):
        return {k: skeleton(v) for k, v in breath.items() if k != "text"}
    if isinstance(breath, list):
        return [skeleton(v) for v in breath]
    return copy.deepcopy(breath)


def _read(base_dir: str) -> dict:
    """{host: entry}; unreadable is empty (the page then says nothing was handed out)."""
    try:
        with open(_path(base_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    got = data.get("hosts") if isinstance(data, dict) else None
    return {str(k): v for k, v in got.items() if isinstance(v, dict)} if isinstance(got, dict) else {}


def save(base_dir: str, breath: dict, *, host: str, scope_line: str, at: datetime) -> None:
    """Keep `breath` as `host`'s last one handed out (its skeleton), replacing the one
    before."""
    path = _path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"at": at.isoformat(timespec="seconds"), "host": str(host or ""),
             "scope": str(scope_line or ""), "breath": skeleton(breath)}
    with file_lease(path.with_name(path.name + ".lock")):
        kept = _read(base_dir)
        kept.pop(entry["host"], None)
        kept[entry["host"]] = entry          # the file lists hosts in the order they were written
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _VERSION, "hosts": kept}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def hosts(base_dir: str) -> list[str]:
    """The hosts with a last breath, the most recently written first."""
    return list(reversed(list(_read(base_dir))))


def load(base_dir: str, host: str | None = None) -> dict | None:
    """`host`'s last breath ({at, host, scope, breath}), or the most recent of all when
    `host` is None; None when there is none."""
    kept = _read(base_dir)
    if host is None:
        order = hosts(base_dir)
        return copy.deepcopy(kept[order[0]]) if order else None
    entry = kept.get(str(host))
    return copy.deepcopy(entry) if entry else None


# ------------------------------------------------------------
# Titles, read now
# ------------------------------------------------------------

def _rule_text(content: str) -> str:
    """A pinned rule is shown whole, as breath prints it: its body on one line."""
    return re.sub(r"\s+", " ", str(content or "")).strip()


_STATE_WORDS = {_V.ARCHIVED: "在归档区", _V.DELETED: "在归档区（已删除）"}


def relabel(breath: dict, all_buckets: list, scope=None) -> dict:
    """The skeleton with each item's title read now from `all_buckets` (archive included)
    through the gate's `read` road: `text`, and `state_words` when the entry is no longer
    live. An entry missing, out of the request's scope, or standing on a withdrawn or
    deleted source gets `text` None. The name page shows its whole body and a rule its
    body on one line, as breath prints them; everything else its one-line label."""
    from .profile import entry_label, label_of   # lazy: profile is the heavier import

    by_id = {str((b.get("metadata") or {}).get("id") or b.get("id") or ""): b
             for b in all_buckets}
    out = copy.deepcopy(breath)

    def fill(item, how: str = "label") -> None:
        if not isinstance(item, dict) or not item.get("id"):
            return
        row = by_id.get(str(item["id"]))
        item["text"] = None
        if row is None:
            item["state_words"] = "找不到了"
            return
        meta = row.get("metadata") or {}
        content = str(row.get("content") or "")
        verdict = _V.visible_for(meta, scope, road=_V.READ)
        if not verdict.shown:
            if _V.SOURCE_GONE in verdict.reasons:
                item["state_words"] = "依据的来源撤回或删除了，正文不给"
            return
        if verdict.state in _STATE_WORDS:
            item["state_words"] = _STATE_WORDS[verdict.state]
        if how == "page":
            item["text"] = content.strip()
        elif how == "rule":
            item["text"] = _rule_text(content)
        elif how == "recent":
            item["text"] = label_of({"meta": meta, "content": content})
        else:
            item["text"] = entry_label(meta, content)

    core = out.get("core") or {}
    fill(core.get("facts"), "page")
    for r in core.get("rules") or []:
        fill(r, "rule")
    plan = out.get("prospective") or {}
    for it in plan.get("items") or []:
        fill(it)
        fill(it.get("hold"))
    for q in plan.get("questions") or []:
        fill(q)
    for it in (out.get("recent") or {}).get("items") or []:
        fill(it, "recent")
    for it in (out.get("involuntary") or {}).get("items") or []:
        fill(it)
    for it in (out.get("invalidation") or {}).get("items") or []:
        fill(it)
    return out
