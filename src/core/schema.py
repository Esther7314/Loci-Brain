# -*- coding: utf-8 -*-
"""
core/schema.py — which version of the library this is, and how to move it forward.

The Markdown files are the library; SQLite is only an index rebuilt from them. So a
change to what the files look like is a migration of the files, and a library has to
say which shape it is in. That is `_state/schema.json`:

    {"schema_version": 2, "history": [{"from": 1, "to": 2, "at": "...", "backup": "..."}]}

    · No file and no memories: a new library. It is stamped with the current version.
    · No file but memories on disk: a library from before versions existed (1.4.0),
      which is version 1.

Migrating is explicit (`scripts/migrate.py`), never on server start: it rewrites every
file, so it runs with the server stopped, dry by default, and only after a backup of
the whole library has been written. The server only reports a library that is behind
(`status()`, shown on the health page).

Each step is frozen: it carries its own copy of whatever old names it translates,
because the code that knew those names is deleted once the step exists.

Exports: CURRENT_VERSION · library_version() · stamp_new_library() · status()
         backup() · migrate()
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import frontmatter

from utils import atomic_write_text, now_iso

CURRENT_VERSION = 2

STATE_FILE = Path("_state") / "schema.json"
BACKUP_DIR = "_backups"

# Where memories live. archive/ counts: a migration must not leave old shapes behind in
# what can be restored.
MEMORY_DIRS = ("permanent", "dynamic", "feel", "plans", "letters", "archive")

# Not copied into a backup: earlier backups, the config (it holds API keys, and the
# library is restorable without it), lock files, SQLite side files mid-transaction.
_SKIP_NAMES_PREFIX = ("config.yaml",)
_SKIP_SUFFIXES = (".lock", "-journal", "-wal", "-shm")


# ───────────────────────── reading and writing the version ─────────────────────────

def _state_path(buckets_dir: str | Path) -> Path:
    return Path(buckets_dir) / STATE_FILE


def _memory_files(buckets_dir: str | Path):
    base = Path(buckets_dir)
    for sub in MEMORY_DIRS:
        d = base / sub
        if not d.is_dir():
            continue
        for path in sorted(d.rglob("*.md")):
            if path.is_file():
                yield path


def _read_state(buckets_dir: str | Path) -> dict | None:
    path = _state_path(buckets_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("schema_version"), int):
        raise ValueError(f"{path}: schema_version missing or not an integer")
    return data


def _write_state(buckets_dir: str | Path, state: dict) -> None:
    path = _state_path(buckets_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def library_version(buckets_dir: str | Path) -> int | None:
    """The library's version; None for a new library with no memories yet."""
    state = _read_state(buckets_dir)
    if state is not None:
        return state["schema_version"]
    if next(_memory_files(buckets_dir), None) is not None:
        return 1
    return None


def stamp_new_library(buckets_dir: str | Path) -> None:
    """A library with no version file and no memories starts at the current version."""
    if library_version(buckets_dir) is None:
        _write_state(buckets_dir, {"schema_version": CURRENT_VERSION, "history": []})


def status(buckets_dir: str | Path) -> dict:
    """For the health page: {version, current, behind, error}."""
    try:
        version = library_version(buckets_dir)
    except (OSError, ValueError) as exc:
        return {"version": None, "current": CURRENT_VERSION, "behind": False,
                "error": str(exc)}
    shown = CURRENT_VERSION if version is None else version
    return {"version": shown, "current": CURRENT_VERSION,
            "behind": shown < CURRENT_VERSION, "error": ""}


# ───────────────────────── backup ─────────────────────────

def _skipped(rel: Path) -> bool:
    if rel.parts and rel.parts[0] == BACKUP_DIR:
        return True
    name = rel.name
    return name.startswith(_SKIP_NAMES_PREFIX) or name.endswith(_SKIP_SUFFIXES)


def backup(buckets_dir: str | Path, label: str) -> Path:
    """Zip the whole library into `<buckets>/_backups/<label>-<UTC stamp>.zip`.

    Written to a `.part` file and renamed when complete, so a half-written backup never
    looks like a finished one."""
    base = Path(buckets_dir).resolve()
    out_dir = base / BACKUP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"{label}-{stamp}.zip"
    part = target.with_suffix(".zip.part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(base)
            if _skipped(rel):
                continue
            zf.write(path, rel.as_posix())
    os.replace(part, target)
    return target


# ───────────────────────── the steps ─────────────────────────

# 1 -> 2: the names 1.4.0 still translated on read. Both spellings of the old ten rooms
# ever used (core/_rooms and scripts/backfill_rooms carried different tables).
_V1_LEGACY_ROOMS = {
    "I/EVENT/SELF/WHO": "EVENT/SELF", "I/EVENT/SELF/WHAT": "EVENT/SELF",
    "I/EVENT/WORLD/WHO": "EVENT/WORLD", "I/EVENT/WORLD/WHAT": "EVENT/WORLD",
    "YOU/EVENT/SELF/WHO": "EVENT/SELF", "YOU/EVENT/SELF/WHAT": "EVENT/SELF",
    "I/MIND/TRAITS": "MIND/TRAITS", "I/MIND/VIEWS": "MIND/VIEWS",
    "YOU/MIND/TRAITS": "MIND/TRAITS", "YOU/MIND/VIEWS": "MIND/VIEWS",
    "I/EVENT/SELF": "EVENT/SELF", "I/EVENT/WHO": "EVENT/SELF",
    "I/EVENT/WHAT": "EVENT/SELF", "YOU/EVENT/WHO": "EVENT/WORLD",
    "YOU/EVENT/WHAT": "EVENT/WORLD", "I/MIND/WHO": "MIND/TRAITS",
    "I/MIND/WHAT": "MIND/VIEWS", "YOU/MIND/WHO": "MIND/TRAITS",
    "YOU/MIND/WHAT": "MIND/VIEWS", "I/MIND/SELF": "MIND/TRAITS",
}


def _v1_to_v2(meta: dict) -> list[str]:
    """Old room names -> the four rooms; `triggered_by` -> `from`. Returns what changed."""
    changed = []
    room = str(meta.get("room") or "").strip()
    if room in _V1_LEGACY_ROOMS:
        meta["room"] = _V1_LEGACY_ROOMS[room]
        changed.append("room")
    if "triggered_by" in meta:
        old = meta.pop("triggered_by")
        if isinstance(old, (list, tuple)):
            old = ",".join(str(s).strip() for s in old if str(s).strip())
        old = str(old or "").strip()
        if old and not str(meta.get("from") or "").strip():
            meta["from"] = old
        changed.append("triggered_by")
    return changed


# version it upgrades FROM -> what it does to one file's metadata
STEPS: dict[int, Callable[[dict], list[str]]] = {
    1: _v1_to_v2,
}


# ───────────────────────── running them ─────────────────────────

def migrate(buckets_dir: str | Path, *, apply: bool = False) -> dict:
    """Bring the library to CURRENT_VERSION.

    Dry (the default): count what each step would change and write nothing.
    With apply: back up the whole library first, then run each step over every memory
    file and record the new version after each step, so an interruption resumes from
    the last finished one.

    Returns {"from", "to", "backup", "steps": [{"from", "to", "files", "fields"}]}.
    """
    version = library_version(buckets_dir)
    report: dict = {"from": version, "to": CURRENT_VERSION, "backup": "", "steps": []}
    if version is None or version >= CURRENT_VERSION:
        return report
    missing = [v for v in range(version, CURRENT_VERSION) if v not in STEPS]
    if missing:
        raise RuntimeError(f"no migration step from version {missing[0]}")

    if apply:
        report["backup"] = str(backup(buckets_dir, f"before-v{version}-to-v{CURRENT_VERSION}"))

    state = _read_state(buckets_dir) or {"schema_version": version, "history": []}
    for v in range(version, CURRENT_VERSION):
        step = STEPS[v]
        files = 0
        fields: dict[str, int] = {}
        for path in _memory_files(buckets_dir):
            post = frontmatter.load(path)
            changed = step(post.metadata)
            if not changed:
                continue
            files += 1
            for name in changed:
                fields[name] = fields.get(name, 0) + 1
            if apply:
                atomic_write_text(path, frontmatter.dumps(post))
        report["steps"].append({"from": v, "to": v + 1, "files": files, "fields": fields})
        if apply:
            state["schema_version"] = v + 1
            state.setdefault("history", []).append(
                {"from": v, "to": v + 1, "at": now_iso(), "backup": report["backup"]})
            _write_state(buckets_dir, state)
    return report
