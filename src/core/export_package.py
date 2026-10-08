# -*- coding: utf-8 -*-
"""
========================================
core/export_package.py — one file that carries a library away and brings it back
========================================

`GET /api/loci/export` writes it; `POST /api/loci/import-package` reads it back through
the package importer (core/package_import.py). Its uses are backup, restore and moving to
another machine, so it carries everything that cannot be rebuilt from the Markdown and
nothing that is a secret.

The package is a ZIP:

    backup_manifest.json   every other member with its size and sha256, plus a `package`
                           section: what was filtered out, what could not travel
                           (`missing`), and what was left behind on purpose
                           (`not_included`)
    export_meta.json       Loci version, export time, the embedding model, the library's
                           schema version, the package format
    SCHEMA.md / schema.json  what every frontmatter field means (core/fields.py), with how
                           many entries carry it and any key the table does not describe
    buckets/<path>.md      every entry, byte for byte, at its path in the library
    embeddings.db          a consistent snapshot of the vectors, cut down to the entries
                           in the package and vacuumed
    originals/<id>.txt     the original text of each sunk entry (archive/原文/<id>.txt)
    media/<path>           attachments the entries reference, at their library path
    state/<path>           the library's own state files (`STATE_FILES`) and the names
                           table (`state/aliases.yaml`)

What is left out, and why:
  · withdrawn entries — standing on a source the registry records as withdrawn or deleted,
    or carrying an open `source_gone` record, and everything derived from such an entry —
    and soft-deleted ones (`deleted_at` /
    `tombstone`). Their ids are listed under `package.filtered`; their text, originals,
    attachments, vectors, dream records and usage-log queries are not in the package.
  · secrets: config.yaml (credentials), the ledger's cursor key, the panel password; and
    host resend claims, whose stored replies can hold text no withdrawal reaches.
  · what is rebuilt: the dehydration cache, the vector queue, logs, earlier backups.
A held source travels as held: the registry's hold goes with it and the entries standing
on it stay blocked in the new library until the host's ordered change settles it. The
permission state that belongs to the library travels the same way — each source record's
`use`, the registry's `use_changed` and states, the entries' own marks — while who may ask
(the hosts table: credentials, ceilings, change authority) is the receiving installation's
config and is listed as left behind.

The material a source names stays with its host: the package carries each source's
identity, revision and fingerprint, and lists per host how many records rest on material
only that host can serve again (`missing.host_material`). An imported conversation is the
exception — Loci itself hosts it (core/import_memory.py), so its batch record and lines
travel under `state/_sources/imports/`, and a batch no longer on disk is listed as missing.

Bringing a package back (`restore_library_state`, called by the importer after it has
written the entries): into a library that had no entries, every state file is put in place
as it was. Into a library that already has entries, four kinds join what is there — the
source registry (a source the library has never heard of comes in with its whole chain,
renumbered after the library's own; a source both know, or one the library's own entries
stand on, keeps the library's: a package is no change authority), the host's line orders
(registrations are joined by design), imported conversation batches (a batch the library
does not have), and the names table (names it does not know are added) — and the rest,
which name entries and windows of the other library, are reported as not merged. What the
importer refused to write (core/package_import.REFUSAL_WORDS) is taken off the state files
the way an export takes off what it leaves out, and its originals and attachments stay
out; a file already in place byte for byte counts as restored, so a second run of an
interrupted import finishes the first.

Adding a kind of library state to the package is one row in `STATE_FILES`.

Exports: PACKAGE_FORMAT · STATE_FILES · StateFile · build_package · restore_library_state ·
         library_snapshot · withdrawn · partition
========================================
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Optional

import frontmatter

from .ledger_mirror import file_lease
from . import backup_archive as BA
from utils import read_from_ids

from . import _dream
from . import _invalidation as _I
from . import _ledger
from . import _sources as _src
from . import import_memory as _imports
from . import names as _names
from . import fields as _fields
from . import schema as _schema
from . import prompts as _prompts
from . import similar_kept as _similar_kept
from . import visibility as _V
from .scope import IMPORT_SYSTEM

logger = logging.getLogger("loci_brain.export")

PACKAGE_FORMAT = 2

SCHEMA_NOTE = "SCHEMA.md"
SCHEMA_JSON = "schema.json"
EXPORT_META = "export_meta.json"
STATE_PREFIX = BA.PACKAGE_STATE_PREFIX
ORIGINALS_PREFIX = BA.PACKAGE_ORIGINALS_PREFIX
MEDIA_PREFIX = BA.PACKAGE_MEDIA_PREFIX
NAMES_MEMBER = STATE_PREFIX + "aliases.yaml"
MEDIA_DIR = "_media"
IMPORTS_DIR = f"{_src.SOURCES_DIR}/{_imports.IMPORTS_DIR}"     # import_memory.ImportStore

# How a state file joins a library that already has its own.
REGISTRY = "registry"   # by source identity, renumbered after the library's own changes
ORDERS = "orders"       # rows the library does not have are appended
ADD = "add"             # put in place wherever the library has no such file; never replaced
FRESH = "fresh"         # only into a library that had no entries and no such file

_MISSING_LIST_MAX = 200


@dataclass(frozen=True)
class StateFile:
    """One kind of library state the package carries. `path` is under the library folder;
    a `*` matches within one part of it (`night_fall/dreams/x_*.json`, `a/*/*.jsonl`).
    `scrub` takes the file's bytes and a `_Scrub` (the ids left out of the package, the
    source registry, the file's path) and returns what may travel (None: nothing).
    `not_merged` is why a FRESH file is not restored into a library that has entries,
    when the general reason (it names that library's entries and windows) is not it."""
    path: str
    merge: str
    what: str
    scrub: Optional[Callable[[bytes, "_Scrub"], Optional[bytes]]] = None
    not_merged: str = ""


@dataclass(frozen=True)
class _Scrub:
    left_out: frozenset
    registry: Any = None
    rel: str = ""


def _rows(data: bytes) -> list[dict]:
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _jsonl(rows: Iterable[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                   for r in rows).encode("utf-8")


def _redact_ledger(data: bytes, _ctx: _Scrub) -> Optional[bytes]:
    """Every ledger line down to what a line of its kind may hold (core/_ledger.
    redact_line): a memory line written before that rule carries whole metadata, and its
    names and summaries do not travel; a source line travels whole (ids and states only).
    Seq numbers and order stay, so an open host keeps reading by them."""
    out = []
    for event in _rows(data):
        out.append(_ledger.redact_line(event) or event)
    return _jsonl(out) if out else None


def _scrub_usage(data: bytes, ctx: _Scrub) -> Optional[bytes]:
    """Ids left out come off every line; a lookup that listed one loses its query (the
    same rule as UsageLog.scrub); a line left naming nothing it is about goes."""
    left_out = ctx.left_out
    out = []
    for row in _rows(data):
        ids = [str(i) for i in row.get("ids") or []]
        kept = [i for i in ids if i not in left_out]
        if len(kept) != len(ids):
            row = {k: v for k, v in row.items() if k not in ("query", "gates")}
            row["ids"] = kept
            if not kept and row.get("kind") != "found":
                continue
        out.append(row)
    return _jsonl(out) if out else None


def _card_entry(key: str) -> str:
    return str(key).rsplit("@", 1)[0]


def _scrub_cue_ledger(data: bytes, ctx: _Scrub) -> Optional[bytes]:
    """Cards about entries left out come off every line of the card ledger."""
    left_out = ctx.left_out
    def keep_key(k) -> bool:
        return _card_entry(k) not in left_out

    def keep_card(c) -> bool:
        return not (isinstance(c, dict) and str(c.get("id") or "") in left_out)

    out = []
    for row in _rows(data):
        row = dict(row)
        if isinstance(row.get("baseline"), list):
            row["baseline"] = [k for k in row["baseline"] if keep_key(k)]
        for field in ("cards", "ever"):
            if isinstance(row.get(field), list):
                row[field] = [c for c in row[field]
                              if (keep_card(c) if isinstance(c, dict) else keep_key(c))]
        if isinstance(row.get("ids"), dict):
            row["ids"] = {k: v for k, v in row["ids"].items() if k not in left_out}
        out.append(row)
    return _jsonl(out) if out else None


def _scrub_dream(data: bytes, ctx: _Scrub) -> Optional[bytes]:
    """A dream woven from an entry left out does not travel: a dream cannot be cut."""
    try:
        rec = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict) or set(_dream.ingredient_ids(rec)) & ctx.left_out:
        return None
    return data


def _scrub_similar_kept(data: bytes, ctx: _Scrub) -> Optional[bytes]:
    """A kept pair naming an entry left out does not travel (core/similar_kept.py)."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    pairs = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(pairs, dict):
        return None
    kept = {k: rec for k, rec in pairs.items()
            if isinstance(rec, dict) and isinstance(rec.get("marks"), dict)
            and not set(map(str, rec["marks"])) & ctx.left_out}
    if not kept:
        return None
    return json.dumps({**doc, "pairs": kept}, ensure_ascii=False, indent=1).encode("utf-8")


def _scrub_import_lines(data: bytes, ctx: _Scrub) -> Optional[bytes]:
    """An imported conversation's lines (core/import_memory.py): a line the registry holds
    as withdrawn or deleted does not travel. Withdrawing a whole batch deletes its folder,
    so this only meets a line a change reached on its own."""
    parts = PurePosixPath(ctx.rel).parts          # _sources/imports/<batch>/<container>.jsonl
    if ctx.registry is None or len(parts) != 4:
        return data
    batch, container = parts[2], PurePosixPath(parts[3]).stem
    kept, dropped = [], 0
    for row in _rows(data):
        sid = _src.SourceId(IMPORT_SYSTEM, batch, container, str(row.get("id") or ""))
        if ctx.registry.state_of(sid) in (_src.WITHDRAWN, _src.DELETED):
            dropped += 1
            continue
        kept.append(row)
    if not dropped:
        return data
    return _jsonl(kept) if kept else None


STATE_FILES: tuple[StateFile, ...] = (
    StateFile("_sources/changes.jsonl", REGISTRY,
              "source registry: every applied change and the state it left"),
    StateFile("_sources/held.jsonl", REGISTRY,
              "source holds: a host's unordered word that a source is gone"),
    StateFile("_sources/line_orders.jsonl", ORDERS,
              "the host's order of each run's lines, with watermarks and the line "
              "revisions a run adopted"),
    StateFile("_sources/pending_slices.jsonl", FRESH,
              "slices waiting for the main model to check them, an import's drafts "
              "among them"),
    # An imported conversation is material Loci itself hosts (core/import_memory.py): its
    # batch record and its lines are the originals of the `import:` sources. Batch ids are
    # random, so a batch joins a library that has entries as well.
    StateFile(f"{IMPORTS_DIR}/*/{_imports.BATCH_FILE}", ADD,
              "an imported conversation batch: what was imported and how far its "
              "drafting got"),
    StateFile(f"{IMPORTS_DIR}/*/c*.jsonl", ADD,
              "an imported conversation's lines, the originals of its import: sources",
              _scrub_import_lines),
    StateFile("_ledger/events.jsonl", FRESH,
              "the ledger behind /api/v2/changes (text redacted)", _redact_ledger),
    StateFile("_cue/ledger.jsonl", FRESH,
              "the card ledger: what each host window was offered and given",
              _scrub_cue_ledger),
    StateFile("_usage/usage.jsonl", FRESH, "the usage log", _scrub_usage),
    StateFile("_state/dream_state.json", FRESH, "the dream clock"),
    StateFile("_state/case_recall_asked.json", FRESH,
              "scene words already asked about"),
    StateFile("_state/muse_rejected.json", FRESH, "musing pairs set aside"),
    StateFile(f"_state/{_similar_kept.FILE}", FRESH,
              "suspected-duplicate pairs kept on the panel (ids and version markers)",
              _scrub_similar_kept),
    # The owner's rewrites of the side model's prompts move with her library; merged into
    # a living library they would change how that library tags and dreams.
    StateFile(f"_state/{_prompts.FILE}", FRESH,
              "the side model's prompts as the owner rewrote them on the panel",
              not_merged="it would change how this library's side model tags and dreams; "
                         "restored only into a library with no entries"),
    StateFile(f"night_fall/dreams/{_dream.FILE_PREFIX}*.json", FRESH, "dream records",
              _scrub_dream),
)

# What a library folder holds that the package leaves behind, and why. Checked in order;
# the first pattern that matches a file names it.
_LEFT_BEHIND: tuple[tuple[str, str], ...] = (
    ("config.yaml*", "credentials and this installation's settings; the receiving "
                     "installation keeps its own (hosts, models, keys)"),
    ("_ledger/" + _ledger.CURSOR_KEY_FILE, "secret: the key behind host cursors; the "
     "receiving library makes a new one, so a host reading by cursor starts over"),
    (".dashboard_auth.json", "secret: the panel password"),
    (".dashboard_sessions.json", "panel login sessions"),
    ("_sources/" + _src.WRITE_KEYS_FILE, "host resend claims: their stored replies can "
     "hold text no withdrawal reaches; a resend after the move is written again"),
    ("_sources/cleanup.jsonl", "the progress of a clearing in flight"),
    ("_state/import_package.json", "the receiving library's own record of a package "
                                   "import"),
    ("_state/schema.json", "the receiving library stamps its own version; the package's "
                           "is in export_meta.json"),
    ("_state/breath_last.json", "the panel's copy of the last breath each host was handed "
                                "here; the receiving library keeps its own"),
    ("_state/thresholds_model.json", "the embedding model this installation's similarity "
                                     "lines were last looked at with; the lines themselves "
                                     "are config"),
    ("_state/name_guesses.json", "what the side model said a name is when the names table "
                                 "could not take it: hints for the names page, said again "
                                 "by the next backfill"),
    ("_state/dream_archive/*", "the panel's copy of the last three days' dreams: for the "
                               "panel alone, never a road back to a dream"),
    ("embeddings.db.backup", "the vectors before a model switch"),
    ("embeddings.db.migrating", "a model switch's unfinished vectors"),
    ("embeddings.db*", "the vector database's side files"),
    ("dehydration_cache.db*", "a cache, rebuilt by use"),
    (".embedding_outbox.json", "the vector queue; the receiving library queues what "
                               "has no vector"),
    ("_backups/*", "earlier backups"),
    (".logs/*", "logs"),
    ("*.lock", "a lock file"),
    ("*.tmp", "a temporary file"),
    ("*.rewrite", "a temporary file"),
)


def _left_behind_reason(rel: str) -> tuple[str, str]:
    for pattern, reason in _LEFT_BEHIND:
        if fnmatch.fnmatch(rel, pattern):
            return pattern, reason
    top = rel.split("/", 1)[0]
    label = top + "/*" if "/" in rel else top
    return label, "not part of the library format this version reads"


# ============================================================
# What is left out
# ============================================================

def withdrawn(meta: dict, registry) -> bool:
    """Does the entry stand on a source that is withdrawn or deleted — its own records or
    quoted lines read against the registry, or an open `source_gone` record (written on
    everything derived from such an entry too)? A held source is not withdrawn: it
    travels held."""
    if any(r.get("kind") == _I.SOURCE_GONE for r in _I.open_records(meta)):
        return True
    if registry is None:
        return False
    for raw in _src.basis_records(meta):
        try:
            [rec] = _src.normalize_sources([raw])
        except (_src.SourceRecordError, ValueError):
            continue
        if registry.read_state(rec) in (_src.WITHDRAWN, _src.DELETED):
            return True
    return False


def partition(entries: list[dict], registry) -> tuple[list[dict], list[str], list[str]]:
    """(the entries that travel, the ids of the soft-deleted, the ids of the withdrawn).
    Withdrawn is `withdrawn` and everything derived from such an entry, every generation:
    the change writes `source_gone` on it, but a source the registry holds as withdrawn
    without that record (a change recorded before its clearing reached the entries) is
    found here by the same walk to the roots the read gate takes. Every export draws its
    line here (this package, core/export_originals.py)."""
    kept: list[dict] = []
    deleted: list[str] = []
    gone: list[str] = []
    for b in entries:
        meta = b.get("metadata") or {}
        bid = str(b.get("id") or meta.get("id") or "")
        if _V.state_of(meta) == _V.DELETED:
            deleted.append(bid)
        elif withdrawn(meta, registry):
            gone.append(bid)
        else:
            kept.append(b)
    moved = True
    while moved:
        gone_set = set(gone)
        stays = [b for b in kept if not set(read_from_ids(b.get("metadata") or {})) & gone_set]
        moved = len(stays) != len(kept)
        gone += [str(b.get("id") or (b.get("metadata") or {}).get("id") or "")
                 for b in kept if b not in stays]
        kept = stays
    return kept, deleted, gone


def _linked_ids(meta: dict) -> list[tuple[str, str]]:
    """(field, id) for every entry this one names: prov lines inside the library, its
    versions, covers and the standing entry of a hold."""
    from utils import WAS_QUOTED_FROM, read_prov
    out = [("prov", str(line.get("target") or "")) for line in read_prov(meta)
           if line.get("rel") != WAS_QUOTED_FROM]
    for field in ("supersedes", "superseded_by", "covered_by", "cover", "exception_of"):
        value = meta.get(field)
        items = value if isinstance(value, list) else str(value or "").split(",")
        out += [(field, str(t).strip()) for t in items]
    return [(f, t) for f, t in out if t]


# ============================================================
# Writing the package
# ============================================================

class _Writer:
    """The ZIP being written, with the importer's limits counted as it grows."""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.entries: list[dict] = []
        self.total = 0

    def _record(self, entry: dict) -> None:
        self.entries.append(entry)
        if len(self.entries) + 1 > BA.MIGRATE_MAX_MEMBERS:
            raise BA.BackupArchiveError(f"导出包文件项过多（上限 {BA.MIGRATE_MAX_MEMBERS}）")
        self.total += int(entry["size"])
        if self.total > BA.MIGRATE_MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BA.BackupArchiveError("导出包解压后超过 512 MiB 上限")

    def file(self, source: Path, arc_path: str) -> None:
        arc_path = BA._normalize_member_path(arc_path)
        self._record(BA._stream_file_member(self.archive, source=source, arc_path=arc_path))

    def data(self, arc_path: str, data: bytes) -> None:
        arc_path = BA._normalize_member_path(arc_path)
        if len(data) > BA._migration_member_limit(arc_path):
            raise BA.BackupArchiveError(f"导出包成员过大: {arc_path}")
        self.archive.writestr(arc_path, data)
        self._record({"path": arc_path, "size": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()})


def _vector_snapshot(db_path: str, keep: set[str], target: str) -> tuple[bool, set[str]]:
    """A consistent copy of the vectors holding only the entries in `keep`, vacuumed so no
    deleted row lingers in a free page. Returns (written, ids that have a content vector)."""
    if not BA._snapshot_sqlite_to_file(db_path, target):
        return False, set()
    con = sqlite3.connect(target)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        have: set[str] = set()
        if "embeddings" in tables:
            con.execute("CREATE TEMP TABLE keep_ids (bucket_id TEXT PRIMARY KEY)")
            con.executemany("INSERT INTO keep_ids VALUES (?)", ((i,) for i in keep))
            con.execute("DELETE FROM embeddings WHERE bucket_id NOT IN "
                        "(SELECT bucket_id FROM keep_ids)")
            con.commit()
            have = {str(r[0]) for r in con.execute(
                "SELECT bucket_id FROM embeddings WHERE TRIM(embedding) <> ''")}
        con.execute("VACUUM")
    finally:
        con.close()
    return True, have


def _matches(rel: str, pattern: str) -> bool:
    """A path against a state row's pattern, part by part (a `*` never spans a folder)."""
    a, b = PurePosixPath(rel).parts, PurePosixPath(pattern).parts
    return len(a) == len(b) and all(fnmatch.fnmatchcase(x, y) for x, y in zip(a, b))


def _state_sources(base: Path, spec: StateFile) -> list[tuple[str, Path]]:
    """(path under the library, file) for each file a state row names that exists."""
    if "*" not in spec.path:
        p = base / spec.path
        return [(spec.path, p)] if p.is_file() else []
    found = []
    for p in sorted(base.glob(spec.path)):
        rel = p.relative_to(base).as_posix()
        if p.is_file() and not p.is_symlink() and _matches(rel, spec.path):
            found.append((rel, p))
    return found


def _alias_path() -> str:
    return _names._alias_path()


async def build_package(store, *, embedding_db_path: str, export_meta: dict,
                        alias_path: Optional[str] = None) -> tuple[str, dict]:
    """Write the package to a temporary file and return (its path, the manifest). The
    caller owns the file. Raises BackupArchiveError when it cannot be built whole."""
    import asyncio

    entries = await store.list_all(include_archive=True)
    # Walking the library folder and reading the registry is file work: off the loop.
    plan = await asyncio.to_thread(_plan, store, entries, alias_path or _alias_path())
    return await asyncio.to_thread(_write_package, plan, embedding_db_path, export_meta)


def _plan(store, entries: list[dict], alias_path: str) -> dict:
    base = Path(store.base_dir).resolve()
    registry = getattr(store, "sources", None)
    kept, deleted, gone = partition(entries, registry)
    kept_ids = {str(b.get("id")) for b in kept}
    left_out = set(deleted) | set(gone)

    memories: list[tuple[Path, str]] = []
    originals: list[tuple[Path, str]] = []
    media: list[tuple[Path, str]] = []
    missing = {"sunk_originals": [], "media": [], "linked_entries": [],
               "host_material": [], "import_batches": [], "vectors": {}}
    present: dict[str, int] = {}
    hosts: dict[tuple[str, str], set] = {}
    for b in kept:
        meta = b.get("metadata") or {}
        bid = str(b.get("id"))
        path = Path(b["path"]).resolve()
        memories.append((path, "buckets/" + path.relative_to(base).as_posix()))
        for k in meta:
            present[k] = present.get(k, 0) + 1
        if str(meta.get("decay_stage") or "") == "sunk":
            txt = Path(store._sunk_orig_path(bid))
            if txt.is_file():
                originals.append((txt, f"{ORIGINALS_PREFIX}{bid}.txt"))
            else:
                missing["sunk_originals"].append(bid)
        for item in meta.get("media") or []:
            raw = str((item or {}).get("path") or "") if isinstance(item, dict) else ""
            if not raw:
                continue
            candidate = Path(raw)
            where = (candidate if candidate.is_absolute() else base / candidate).resolve()
            inside = where.is_relative_to(base / MEDIA_DIR)
            if inside and where.is_file():
                media.append((where, MEDIA_PREFIX + where.relative_to(base).as_posix()))
            else:
                missing["media"].append({"id": bid, "path": raw, "why": (
                    "outside the library's _media folder" if not inside
                    else "the file is not on disk")})
        for field, target in _linked_ids(meta):
            if target in kept_ids:
                continue
            why = ("deleted" if target in deleted else "withdrawn" if target in gone
                   else "not_in_library")
            missing["linked_entries"].append({"id": bid, "field": field, "target": target,
                                              "why": why})
        for rec in _src.basis_records(meta):
            try:
                sid = _src.record_id(rec)
            except (KeyError, TypeError):
                continue
            if sid.system == IMPORT_SYSTEM:
                if not (base / IMPORTS_DIR / sid.instance / _imports.BATCH_FILE).is_file():
                    missing["import_batches"].append({"id": bid, "source": sid.to_string(),
                                                      "why": "the import batch is not on disk"})
                continue
            hosts.setdefault((sid.system, sid.instance), set()).add(sid.to_string())
    missing["host_material"] = [
        {"system": s, "instance": i, "sources": len(ids),
         "why": "the material stays with the host; this library fetches an original "
                "through the host's fetch_url in its hosts table, and reads its own "
                "body when the host cannot answer"}
        for (s, i), ids in sorted(hosts.items())]

    state: list[tuple[str, Path, StateFile]] = []
    for spec in STATE_FILES:
        for rel, p in _state_sources(base, spec):
            state.append((rel, p, spec))
    names = Path(alias_path) if alias_path and os.path.isfile(alias_path) else None

    carried = {Path(p).resolve() for p, _a in memories + originals + media}
    carried |= {p.resolve() for _r, p, _s in state}
    if names is not None:
        carried.add(names.resolve())
    left_paths = {Path(b["path"]).resolve() for b in entries
                  if str(b.get("id")) in left_out}
    left_paths |= {Path(store._sunk_orig_path(i)).resolve() for i in left_out}
    db = (base / "embeddings.db").resolve()
    not_included: dict[str, dict] = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rp = p.resolve()
        if rp in carried or rp in left_paths or rp == db:
            continue
        rel = p.relative_to(base).as_posix()
        if (rel.startswith("archive/") and "/" in rel[len("archive/"):]
                and rel.endswith(".txt")):
            label, reason = "archive/原文/*", "originals of entries not in the package"
        elif rel.startswith(MEDIA_DIR + "/"):
            label, reason = MEDIA_DIR + "/*", "attachments no entry in the package references"
        else:
            label, reason = _left_behind_reason(rel)
        slot = not_included.setdefault(label, {"path": label, "files": 0, "reason": reason})
        slot["files"] += 1

    return {"base": base, "library_version": _schema.status(base)["version"],
            "registry": registry,
            "memories": memories, "originals": originals, "media": media,
            "state": state, "names": names, "kept_ids": kept_ids, "left_out": left_out,
            "filtered": {"deleted": sorted(deleted), "withdrawn": sorted(gone)},
            "missing": missing, "present": present,
            "not_included": sorted(not_included.values(), key=lambda r: r["path"])}


def _write_package(plan: dict, embedding_db_path: str, export_meta: dict) -> tuple[str, dict]:
    archive_fd, archive_path = tempfile.mkstemp(prefix="loci-export-", suffix=".zip")
    os.close(archive_fd)
    snapshot_fd, snapshot_path = tempfile.mkstemp(prefix="loci-vectors-", suffix=".db")
    os.close(snapshot_fd)
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED,
                             allowZip64=True) as archive:
            w = _Writer(archive)
            for source, arc in plan["memories"]:
                w.file(source, arc)
            for source, arc in plan["originals"] + plan["media"]:
                w.file(source, arc)
            state_paths = []
            for rel, source, spec in plan["state"]:
                data = source.read_bytes()
                if spec.scrub is not None:
                    data = spec.scrub(data, _Scrub(frozenset(plan["left_out"]),
                                                   plan["registry"], rel))
                if data:
                    w.data(STATE_PREFIX + rel, data)
                    state_paths.append(rel)
            if plan["names"] is not None:
                w.data(NAMES_MEMBER, plan["names"].read_bytes())
                state_paths.append("aliases.yaml")

            written, have = _vector_snapshot(embedding_db_path, plan["kept_ids"],
                                             snapshot_path)
            if written:
                w.file(Path(snapshot_path), "embeddings.db")
            without = sorted(plan["kept_ids"] - have)
            plan["missing"]["vectors"] = {
                "entries": len(without), "ids": without[:_MISSING_LIST_MAX],
                "why": "no vector in this library; the receiving library computes them"}

            doc = _fields.schema_document(plan["library_version"], plan["present"],
                                          code_version=_schema.CURRENT_VERSION)
            w.data(SCHEMA_JSON, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
            w.data(SCHEMA_NOTE, _fields.render_schema_note(doc).encode("utf-8"))

            meta = {**export_meta, "package_format": PACKAGE_FORMAT,
                    "library_schema_version": plan["library_version"]}
            meta_bytes = json.dumps(meta, ensure_ascii=False, indent=2,
                                    default=str).encode("utf-8")
            w.data(EXPORT_META, meta_bytes)

            for key in ("sunk_originals",):
                plan["missing"][key] = plan["missing"][key][:_MISSING_LIST_MAX]
            entries = sorted(w.entries, key=lambda e: e["path"])
            manifest = {
                "schema_version": BA.MANIFEST_SCHEMA_VERSION,
                "kind": BA.MANIFEST_KIND,
                "created_at": str(export_meta.get("exported_at") or ""),
                "version": str(export_meta.get("version") or ""),
                "file_count": len(entries),
                "total_bytes": sum(int(e["size"]) for e in entries),
                "files": entries,
                "package": {
                    "format": PACKAGE_FORMAT,
                    "library_schema_version": plan["library_version"],
                    "entries": len(plan["memories"]),
                    "sections": {"memories": len(plan["memories"]),
                                 "originals": len(plan["originals"]),
                                 "media": len(plan["media"]),
                                 "vectors": len(have), "state": state_paths},
                    "filtered": plan["filtered"],
                    "missing": plan["missing"],
                    "not_included": plan["not_included"],
                },
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            if len(manifest_bytes) > BA.MAX_MANIFEST_BYTES:
                raise BA.BackupArchiveError("backup_manifest.json 过大")
            archive.writestr(BA.MANIFEST_NAME, manifest_bytes)
        if os.path.getsize(archive_path) > BA.MAX_ARCHIVE_BYTES:
            raise BA.BackupArchiveError("导出包压缩后超过 512 MiB 上限")
        return archive_path, manifest
    except Exception:
        try:
            os.unlink(archive_path)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass


# ============================================================
# Bringing the state back
# ============================================================

def state_member_spec(member: str) -> Optional[StateFile]:
    """The state row a package member belongs to, or None (not one this version restores)."""
    if not member.startswith(STATE_PREFIX):
        return None
    rel = member[len(STATE_PREFIX):]
    for spec in STATE_FILES:
        if rel == spec.path or ("*" in spec.path and _matches(rel, spec.path)):
            return spec
    return None


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".restore.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _has_content(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _append_rows(path: Path, rows: list[dict]) -> None:
    """Append rows under the file's own lease, as the registry's own appends do."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _jsonl(rows)
    with file_lease(path.with_name(path.name + ".lock")):
        with path.open("ab") as f:
            if f.tell() > 0:
                with path.open("rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        data = b"\n" + data
            f.write(data)
            f.flush()
            os.fsync(f.fileno())


def _stood_on(registry, own_basis: Iterable) -> Callable[[str], bool]:
    """`check(source key)`: does any of the library's own entries stand on that source
    — the same identity, a run holding it, a line inside it, or an overlapping run, read
    with the registry's line orders (core/_sources.names_identity, both ways)?"""
    by_place: dict[tuple, list] = {}
    for have in own_basis:
        by_place.setdefault((have.system, have.instance, have.container), []).append(have)

    def check(key: str) -> bool:
        try:
            want = _src.SourceId.parse(str(key))[0]
        except (_src.SourceRecordError, ValueError):
            return False
        for have in by_place.get((want.system, want.instance, want.container), ()):
            if (_src.names_identity(have, want, registry)
                    or _src.names_identity(want, have, registry)):
                return True
        return False
    return check


def _merge_registry(base: Path, changes: list[dict], held: list[dict],
                    stood_on: Optional[Callable[[str], bool]] = None) -> dict:
    """A source the library has never heard of comes in with its whole chain, its seq
    numbers moved past the library's own (so the order within the chain, and between it
    and its holds, stays as it was); a source the library knows keeps the library's chain
    and holds. A change the library already applied (same host and change_id) is skipped.

    A source the library's own entries stand on (`stood_on`) is settled by the library's
    host alone: a package row for it — a withdrawal, a hold, a narrowed use, a revision —
    is not appended (`refused_stood_on`). A package is no change authority, and a row
    appended without the clearing a change does would leave those entries half withdrawn;
    the host sends its change to this library if the source changed."""
    changes_path = base / _src.SOURCES_DIR / _src.CHANGES_FILE
    held_path = base / _src.SOURCES_DIR / _src.HELD_FILE
    own = _src._read_lines(changes_path)
    own_held = _src._read_lines(held_path)
    known = {str(r.get("source") or "") for r in own} | {
        str(r.get("source") or "") for r in own_held}
    applied = {(str(r.get("host") or ""), str(r.get("change_id") or "")) for r in own}
    shift = max((int(r.get("seq") or 0) for r in own), default=0)
    kept_own: set[str] = set()
    refused: set[str] = set()
    verdict: dict[str, bool] = {}

    def own_ground(source: str) -> bool:
        if stood_on is None:
            return False
        if source not in verdict:
            verdict[source] = stood_on(source)
        return verdict[source]

    add: list[dict] = []
    for r in changes:
        source = str(r.get("source") or "")
        if source in known:
            kept_own.add(source)
            continue
        if own_ground(source):
            refused.add(source)
            continue
        if (str(r.get("host") or ""), str(r.get("change_id") or "")) in applied:
            continue
        add.append({**r, "seq": int(r.get("seq") or 0) + shift})
    add_held = []
    for r in held:
        source = str(r.get("source") or "")
        if source in known:
            kept_own.add(source)
            continue
        if own_ground(source):
            refused.add(source)
            continue
        add_held.append({**r, "after_seq": int(r.get("after_seq") or 0) + shift})
    _append_rows(changes_path, add)
    _append_rows(held_path, add_held)
    return {"sources_added": len({r["source"] for r in add} | {r["source"] for r in add_held}),
            "changes_added": len(add), "holds_added": len(add_held),
            "kept_own": sorted(kept_own), "refused_stood_on": sorted(refused)}


def _merge_orders(path: Path, rows: list[dict]) -> dict:
    have = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in _src._read_lines(path)}
    add = [r for r in rows if json.dumps(r, sort_keys=True, ensure_ascii=False) not in have]
    _append_rows(path, add)
    return {"rows_added": len(add)}


def _merge_names(data: bytes) -> dict:
    """Names the library does not know come in with their aliases, kind and links; a name
    it knows keeps the library's entry. An alias another entry already claims is reported,
    not forced."""
    import yaml

    raw = yaml.safe_load(data.decode("utf-8")) or {}
    _flat, blocked, records = _names._parse(raw)
    added, kept, refused = [], [], []
    for name, rec in records.items():
        if _names.record_of(name) is not None or _names.candidates(name):
            kept.append(name)
            continue
        try:
            if rec.instance_of:
                _names.set_kind(name, rec.instance_of)
            for alias in rec.aliases:
                try:
                    _names.add_alias(name, alias)
                except ValueError as exc:
                    refused.append({"name": name, "alias": alias, "why": str(exc)})
            if not rec.instance_of and not rec.aliases:
                _names._edit_table(lambda lines, n=name: _names._ensure_key(lines, n))
            added.append(name)
        except ValueError as exc:
            refused.append({"name": name, "why": str(exc)})
    for name, rec in records.items():
        if name not in added:
            continue
        for rel in _names.LINK_RELS:
            for target in getattr(rec, rel):
                try:
                    _names.link_name(name, rel, target)
                except ValueError as exc:
                    refused.append({"name": name, rel: target, "why": str(exc)})
    for name in sorted(blocked):
        if _names.record_of(name) is None:
            try:
                _names.mark_not_person(name)
            except ValueError as exc:
                refused.append({"name": name, "why": str(exc)})
    return {"added": added, "kept_own": kept, "refused": refused}


def _scrubbed(spec: StateFile, data: bytes, ctx: _Scrub) -> Optional[bytes]:
    """A state file through its row's filter, for the receiving library: the entries the
    import did not write leave it, and lines the library's registry withdrew. When the
    filter takes nothing out, the package's own bytes go back (a filter re-serialises)."""
    if spec.scrub is None:
        return data
    out = spec.scrub(data, ctx)
    if out is None or out == data:
        return out
    try:
        if json.loads(out) == json.loads(data):
            return data
    except ValueError:
        if _rows(out) == _rows(data):
            return data
    return out


def _same_bytes(path: Path, data: bytes) -> bool:
    try:
        return path.stat().st_size == len(data) and path.read_bytes() == data
    except OSError:
        return False


def restore_library_state(store, members: dict[str, str], *, fresh: bool,
                          id_map: dict[str, str], left_out: Iterable[str] = (),
                          own_basis: Iterable = ()) -> dict:
    """Put the package's library state in place after its entries were written.

    `members`: package member -> local file, for the state/, originals/ and media/ members.
    `fresh`: the library had no entries before this import (a second run of an interrupted
    import keeps the first run's word). `id_map`: package id -> the id it was written
    under, for every entry imported (an entry skipped or refused is absent; its original
    and attachments stay out too). `left_out`: the package ids the import refused, taken
    off the state files the way an export takes off what it leaves out. `own_basis`: the
    sources the library's own entries stand on (SourceIds), which no package row settles
    (`_merge_registry`). A file already in place byte for byte counts as restored.
    Returns what was restored, merged, and left as it was."""
    base = Path(store.base_dir).resolve()
    report: dict[str, Any] = {"mode": "fresh" if fresh else "merge", "restored": [],
                              "merged": {}, "not_merged": [], "unknown": [],
                              "originals": 0, "media": 0, "kept": [], "errors": []}
    registry = getattr(store, "sources", None)
    left = frozenset(left_out)

    def read(member: str) -> bytes:
        source = members[member]
        if isinstance(source, bytes):
            return source
        with open(source, "rb") as f:
            return f.read()

    def put(dest: Path, data: bytes, label: str, counter: str = "") -> None:
        if dest.exists() and not _same_bytes(dest, data):
            report["kept"].append(label)
            return
        if not dest.exists():
            _atomic_bytes(dest, data)
        if counter:
            report[counter] += 1
        else:
            report["restored"].append(label)

    registry_rows: dict[str, list] = {}
    for member in sorted(members):
        try:
            if member == NAMES_MEMBER:
                _restore_names(read(member), report)
                continue
            if member.startswith(ORIGINALS_PREFIX):
                source_id = PurePosixPath(member).stem
                target_id = id_map.get(source_id)
                if not target_id:
                    continue
                put(Path(store._sunk_orig_path(target_id)), read(member),
                    f"archive/原文/{target_id}.txt", "originals")
                continue
            if member.startswith(MEDIA_PREFIX):
                rel = member[len(MEDIA_PREFIX):]
                parts = rel.split("/")
                if not (len(parts) >= 3 and parts[0] == MEDIA_DIR
                        and (base / rel).resolve().is_relative_to(base / MEDIA_DIR)):
                    report["unknown"].append(member)
                    continue
                # An attachment goes with its entry: not at all for one not written, under
                # the new id for a copy written under one.
                target_id = id_map.get(parts[1])
                if not target_id:
                    continue
                rel = "/".join([MEDIA_DIR, target_id, *parts[2:]])
                dest = (base / rel).resolve()
                if not dest.is_relative_to(base / MEDIA_DIR):
                    report["unknown"].append(member)
                    continue
                put(dest, read(member), rel, "media")
                continue
            spec = state_member_spec(member)
            if spec is None:
                report["unknown"].append(member)
                continue
            rel = member[len(STATE_PREFIX):]
            dest = base / rel
            if spec.merge == REGISTRY:
                registry_rows[spec.path] = _rows(read(member))
                continue
            data = _scrubbed(spec, read(member), _Scrub(left, registry, rel))
            if not data:
                continue
            if _same_bytes(dest, data):
                report["restored"].append(rel)
            elif not _has_content(dest) and (fresh or spec.merge != FRESH):
                _atomic_bytes(dest, data)
                report["restored"].append(rel)
            elif spec.merge == ORDERS:
                report["merged"][rel] = _merge_orders(dest, _rows(data))
            elif spec.merge == ADD:
                report["kept"].append(rel)
            else:
                report["not_merged"].append({
                    "path": rel, "what": spec.what,
                    "why": ("the library already has its own" if _has_content(dest) else
                            spec.not_merged or
                            "it names entries and windows of the library the package came "
                            "from; restored only into a library with no entries")})
        except Exception as exc:                      # noqa: BLE001 - one file, reported
            logger.warning("[import-package] restoring %s failed: %s", member, exc)
            report["errors"].append(f"{member}: {type(exc).__name__}: {exc}")
    if registry_rows:
        try:
            changes = registry_rows.get(f"{_src.SOURCES_DIR}/{_src.CHANGES_FILE}", [])
            held = registry_rows.get(f"{_src.SOURCES_DIR}/{_src.HELD_FILE}", [])
            own_changes = base / _src.SOURCES_DIR / _src.CHANGES_FILE
            own_held = base / _src.SOURCES_DIR / _src.HELD_FILE
            # Whole only into a library that had no entries: one with entries stands on
            # sources of its own, which the package's rows may not settle.
            if fresh and not _has_content(own_changes) and not _has_content(own_held):
                for rows, dest, name in ((changes, own_changes, _src.CHANGES_FILE),
                                         (held, own_held, _src.HELD_FILE)):
                    if rows:
                        _atomic_bytes(dest, _jsonl(rows))
                        report["restored"].append(f"{_src.SOURCES_DIR}/{name}")
            elif fresh and _src._read_lines(own_changes) == changes and _src._read_lines(
                    own_held) == held:
                # Put in place by an earlier run of this same import.
                report["restored"] += [f"{_src.SOURCES_DIR}/{n}" for n, rows in (
                    (_src.CHANGES_FILE, changes), (_src.HELD_FILE, held)) if rows]
            else:
                if registry is not None:
                    registry.rebuild_index()      # the line orders merged above
                report["merged"][_src.SOURCES_DIR] = _merge_registry(
                    base, changes, held, _stood_on(registry, own_basis))
            if registry is not None:
                registry.rebuild_index()
        except Exception as exc:                      # noqa: BLE001
            logger.warning("[import-package] restoring the source registry failed: %s", exc)
            report["errors"].append(f"source registry: {type(exc).__name__}: {exc}")
    return report


def _restore_names(data: bytes, report: dict) -> None:
    path = Path(_names._alias_path())
    if not _names.load_names_table() and not _names.load_not_person():
        # No names yet (no file, or one holding only comments): the package's table is
        # put in place as it was.
        path.parent.mkdir(parents=True, exist_ok=True)
        _names._write_table(data.decode("utf-8"))
        report["restored"].append("aliases.yaml")
    else:
        report["merged"]["aliases.yaml"] = _merge_names(data)


# ============================================================
# What a library holds, for comparing two
# ============================================================

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def library_snapshot(buckets_dir, *, alias_path: Optional[str] = None,
                     left_out: Iterable[str] = ()) -> dict:
    """Everything a package carries, read off a library folder in a form two libraries
    can be compared by: each entry's frontmatter and body, sunk originals, attachments,
    content and meaning vectors, every state file (passed through the export's own filters with `left_out`,
    so a library and its restored copy compare equal), the registry's answers per source,
    and the names table."""
    import yaml

    base = Path(buckets_dir).resolve()
    left = set(left_out)
    out: dict[str, Any] = {"entries": {}, "originals": {}, "media": {}, "state": {},
                           "vectors": {}, "meaning_vectors": {}, "registry": {},
                           "names": None}
    for sub in ("permanent", "dynamic", "feel", "archive"):
        d = base / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            post = frontmatter.load(p)
            bid = str(post.metadata.get("id") or "")
            if bid in left:
                continue
            out["entries"][bid] = {"meta": json.loads(json.dumps(post.metadata,
                                                                 default=str)),
                                   "body": post.content}
    orig = base / "archive" / "原文"
    if orig.is_dir():
        for p in sorted(orig.glob("*.txt")):
            if p.stem not in left:
                out["originals"][p.stem] = _sha(p)
    media = base / MEDIA_DIR
    if media.is_dir():
        referenced = {str(m.get("path")) for e in out["entries"].values()
                      for m in e["meta"].get("media") or [] if isinstance(m, dict)}
        for p in sorted(media.rglob("*")):
            rel = p.relative_to(base).as_posix()
            if p.is_file() and rel in referenced:
                out["media"][rel] = _sha(p)
    db = base / "embeddings.db"
    if db.is_file():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for bid, emb in con.execute("SELECT bucket_id, embedding FROM embeddings"):
                if bid in out["entries"] and str(emb or "").strip():
                    try:
                        out["vectors"][str(bid)] = json.loads(emb)
                    except (TypeError, ValueError):
                        out["vectors"][str(bid)] = str(emb)
        except sqlite3.Error:
            pass
        try:
            for bid, emb in con.execute("SELECT bucket_id, meaning_embedding FROM embeddings "
                                        "WHERE meaning_embedding IS NOT NULL"):
                if bid in out["entries"] and str(emb or "").strip():
                    try:
                        out["meaning_vectors"][str(bid)] = json.loads(emb)
                    except (TypeError, ValueError):
                        out["meaning_vectors"][str(bid)] = str(emb)
        except sqlite3.Error:
            pass                    # a database from before meaning vectors had a column
        finally:
            con.close()
    registry = _src.SourceRegistry(base)
    for spec in STATE_FILES:
        for rel, p in _state_sources(base, spec):
            data = p.read_bytes()
            if spec.scrub is not None:
                data = spec.scrub(data, _Scrub(frozenset(left), registry, rel))
            if not data:
                continue
            out["state"][rel] = (_rows(data) if rel.endswith(".jsonl")
                                 else json.loads(data.decode("utf-8")))
    keys = {str(r.get("source") or "") for r in _src._read_lines(registry.changes_path)}
    keys |= {str(r.get("source") or "") for r in _src._read_lines(registry.held_path)}
    for entry in out["entries"].values():
        for rec in _src.basis_records(entry["meta"]):
            try:
                keys.add(_src.record_id(rec).to_string())
            except (KeyError, TypeError):
                continue
    for key in sorted(k for k in keys if k):
        sid = _src.SourceId.parse(key)[0]
        out["registry"][key] = {"state": registry.state_of(sid),
                                "held": registry.held_of(sid) is not None,
                                "described": registry.describe(sid),
                                "lines": [str(x) for x in registry.lines_of(sid)]}
    names = Path(alias_path) if alias_path else None
    if names is not None and names.is_file():
        out["names"] = yaml.safe_load(names.read_text(encoding="utf-8"))
    return out
