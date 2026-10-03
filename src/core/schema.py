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
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

import frontmatter

from utils import atomic_write_text, now_iso, sanitize_name

CURRENT_VERSION = 5

STATE_FILE = Path("_state") / "schema.json"
BACKUP_DIR = "_backups"

# Where memories live. archive/ counts: a migration must not leave old shapes behind in
# what can be restored.
MEMORY_DIRS = ("permanent", "dynamic", "feel", "plans", "archive")

# Not copied into a backup: earlier backups, the config (it holds API keys, and the
# library is restorable without it), the panel's password and login sessions (secrets the
# library does not need back), lock files, SQLite side files mid-transaction.
_SKIP_NAMES_PREFIX = ("config.yaml", ".dashboard_auth.json", ".dashboard_sessions.json")
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


def _v1_to_v2(meta: dict, rel: PurePosixPath) -> tuple[list[str], str | None]:
    """Old room names -> the four rooms; `triggered_by` -> `from`."""
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
    return changed, None


# 2 -> 3: "wanted" moves out of status into direction_of_fit (plan part 1), and the old
# plan type becomes what it always was, something wanted that happened to me: an
# EVENT/SELF telic entry in dynamic/. Who is bound (`bound`) is not guessed here; the
# report lists every telic entry so a person can fill it in.
_V2_DEFAULT_DOMAIN = "未分类"


def _v2_to_v3(meta: dict, rel: PurePosixPath) -> tuple[list[str], str | None]:
    changed: list[str] = []
    if str(meta.get("status") or "").strip() == "want":
        meta["direction_of_fit"] = "telic"
        meta.pop("status", None)
        changed.append("want")
    new_rel = None
    if str(meta.get("type") or "") == "plan" or (rel.parts and rel.parts[0] == "plans"):
        meta["type"] = "dynamic"
        meta["direction_of_fit"] = "telic"
        if str(meta.get("status") or "").strip() not in ("resolved", "abandoned"):
            meta.pop("status", None)
        if not str(meta.get("room") or "").strip():
            meta["room"] = "EVENT/SELF"
        # Only files in plans/ move. A deleted plan sits in archive/ and stays there;
        # its type still changes, so restoring it lands in dynamic/.
        if rel.parts and rel.parts[0] == "plans":
            domain = meta.get("domain")
            primary = (domain if isinstance(domain, str) else (domain or [""])[0]) or ""
            primary = sanitize_name(str(primary).strip()) or _V2_DEFAULT_DOMAIN
            new_rel = f"dynamic/{primary}/{rel.name}"
        changed.append("plan")
    return changed, new_rel


# 3 -> 4: `__gist__` means "a fold's product" and nothing else. Every new version regrow
# wrote before stage 3 of v2 carried the tag too, which kept plain re-versions of ordinary
# memories out of the muse and dream pools and out of a period's members, as if they were
# machinery. Whether a chain is a gist is decided by its first version: a tagged memory
# whose root (walking `supersedes`) does not carry the tag loses it. Periods keep theirs
# (their root carries it), and so does every version of a fold's gist.
_V3_GIST_TAG = "__gist__"


class _V3ToV4:
    """A step that needs the whole library in view: the root of a version chain is
    another file. `prepare` indexes id -> (tags, supersedes) once per run; the step itself
    then judges one file at a time like the others."""

    def __init__(self):
        self._index: dict[str, tuple[set[str], str]] = {}

    def prepare(self, buckets_dir: str | Path) -> None:
        self._index = {}
        for path in _memory_files(buckets_dir):
            meta = frontmatter.load(path).metadata
            bid = str(meta.get("id") or "").strip()
            if bid:
                self._index[bid] = ({str(t) for t in (meta.get("tags") or [])},
                                    str(meta.get("supersedes") or "").strip())

    def _root_has_tag(self, meta: dict) -> bool:
        tags = {str(t) for t in (meta.get("tags") or [])}
        prev = str(meta.get("supersedes") or "").strip()
        seen: set[str] = {str(meta.get("id") or "")}
        while prev and prev not in seen and prev in self._index and len(seen) < 64:
            seen.add(prev)
            tags, prev = self._index[prev]
        return _V3_GIST_TAG in tags

    def __call__(self, meta: dict, rel: PurePosixPath) -> tuple[list[str], str | None]:
        tags = [str(t) for t in (meta.get("tags") or [])]
        if _V3_GIST_TAG not in tags or self._root_has_tag(meta):
            return [], None
        meta["tags"] = [t for t in tags if t != _V3_GIST_TAG]
        return ["gist_tag"], None


# 4 -> 5: where a memory came from becomes typed (W3C PROV). The comma string under
# `from` turns into `prov: [{rel, target}]`, one line per entry, kept verbatim and in
# order: a memory's id (12 hex, or a `feel_…` id) is wasDerivedFrom, and anything else
# names something outside the library and is wasQuotedFrom. A new version (`supersedes`)
# gains a wasRevisionOf line for the version it replaced, which before this lived only in
# the chain field. `supersedes` itself stays: the version chain still runs on it. The
# names and the id shape are copied here so the step stays frozen.
_V4_PROV = "prov"
_V4_DERIVED = "wasDerivedFrom"
_V4_QUOTED = "wasQuotedFrom"
_V4_REVISION = "wasRevisionOf"
_V4_BUCKET_ID = re.compile(r"[0-9a-f]{12}|feel_\S+")


def _v4_to_v5(meta: dict, rel: PurePosixPath) -> tuple[list[str], str | None]:
    changed: list[str] = []
    lines = [dict(ln) for ln in (meta.get(_V4_PROV) or []) if isinstance(ln, dict)]
    if "from" in meta:
        old = meta.pop("from")
        ids = old if isinstance(old, (list, tuple)) else str(old or "").split(",")
        for target in (str(s).strip() for s in ids):
            kind = _V4_DERIVED if _V4_BUCKET_ID.fullmatch(target) else _V4_QUOTED
            line = {"rel": kind, "target": target}
            if target and line not in lines:
                lines.append(line)
        changed.append("from")
    previous = str(meta.get("supersedes") or "").strip()
    if previous:
        line = {"rel": _V4_REVISION, "target": previous}
        if line not in lines:
            lines.append(line)
            changed.append("revision_of")
    if changed:
        if lines:
            meta[_V4_PROV] = lines
        else:
            meta.pop(_V4_PROV, None)
    return changed, None


# Wanted entries only change shape at 2 -> 3; this step touches them for their sources,
# and listing them for `bound` would ask for work it did not create.
_v4_to_v5.lists_telic = False


# version it upgrades FROM -> what it does to one file: (fields changed, where the file
# moves to relative to the library, or None to stay). A step with a `prepare(buckets_dir)`
# is given the library once before its first file; one with `lists_telic = False` keeps
# the entries it touches off the report's list of wanted entries.
STEPS: dict[int, Callable[[dict, PurePosixPath], tuple[list[str], str | None]]] = {
    1: _v1_to_v2,
    2: _v2_to_v3,
    3: _V3ToV4(),
    4: _v4_to_v5,
}


# ───────────────────────── running them ─────────────────────────

def migrate(buckets_dir: str | Path, *, apply: bool = False) -> dict:
    """Bring the library to CURRENT_VERSION.

    Dry (the default): count what each step would change and write nothing.
    With apply: back up the whole library first, then run each step over every memory
    file and record the new version after each step, so an interruption resumes from
    the last finished one.

    Returns {"from", "to", "backup", "steps": [{"from", "to", "files", "fields", "moved"}],
    "telic": [{"id", "path", "archived", "room", "status", "when", "bound", "text"}]} — the last is
    every wanted entry a step touched, for filling in `bound` by hand.
    """
    version = library_version(buckets_dir)
    report: dict = {"from": version, "to": CURRENT_VERSION, "backup": "", "steps": [],
                    "telic": []}
    if version is None or version >= CURRENT_VERSION:
        return report
    missing = [v for v in range(version, CURRENT_VERSION) if v not in STEPS]
    if missing:
        raise RuntimeError(f"no migration step from version {missing[0]}")

    if apply:
        report["backup"] = str(backup(buckets_dir, f"before-v{version}-to-v{CURRENT_VERSION}"))

    base = Path(buckets_dir)
    state = _read_state(buckets_dir) or {"schema_version": version, "history": []}
    for v in range(version, CURRENT_VERSION):
        step = STEPS[v]
        prepare = getattr(step, "prepare", None)
        if callable(prepare):
            prepare(buckets_dir)      # after the earlier steps wrote, so it sees their shape
        files = moved = 0
        fields: dict[str, int] = {}
        for path in list(_memory_files(buckets_dir)):
            rel = PurePosixPath(path.relative_to(base).as_posix())
            post = frontmatter.load(path)
            changed, new_rel = step(post.metadata, rel)
            if not changed:
                continue
            files += 1
            for name in changed:
                fields[name] = fields.get(name, 0) + 1
            target = path
            if new_rel and new_rel != str(rel):
                target = base / new_rel
                if target.exists():
                    raise RuntimeError(f"cannot move {rel} to {new_rel}: a file is already there")
                moved += 1
            meta = post.metadata
            if (str(meta.get("direction_of_fit") or "") == "telic"
                    and getattr(step, "lists_telic", True)):
                report["telic"].append({
                    "id": str(meta.get("id") or ""),
                    "path": new_rel or str(rel),
                    "archived": (rel.parts[0] == "archive") if rel.parts else False,
                    "room": str(meta.get("room") or ""),
                    "status": str(meta.get("status") or ""),
                    "when": str(meta.get("when") or ""),
                    "bound": list(meta.get("bound") or []),
                    "text": " ".join(str(post.content or "").split())[:120],
                })
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(target, frontmatter.dumps(post))
                if target != path:
                    path.unlink()
        report["steps"].append({"from": v, "to": v + 1, "files": files, "fields": fields,
                                "moved": moved})
        if apply:
            state["schema_version"] = v + 1
            state.setdefault("history", []).append(
                {"from": v, "to": v + 1, "at": now_iso(), "backup": report["backup"]})
            _write_state(buckets_dir, state)
    return report
