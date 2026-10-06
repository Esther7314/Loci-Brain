"""
========================================
_package_plan.py — what the receiving library makes of a package before writing
========================================

Which package ids collide with the library's entries (at parse time); which entries are
already there byte for byte; what is refused because the library has withdrawn or holds
what it stands on (core/package_import.REFUSAL_WORDS); and the id each entry is written
under. After the library's state is restored, pending slices over withdrawn lines are
cleared here too. core/package_import.MigrateEngine runs these in order and keeps what they
return.
========================================
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any

from ._package_read import _ParsedBucket, _safe_str, read_bucket_member
from ._package_write import _new_entry_id, _same_file

from utils import read_from_ids  # type: ignore


@dataclass
class ConflictInfo:
    """A description of one bucket_id in the package colliding with the current system."""
    bucket_id: str
    import_name: str
    import_created: str
    current_name: str
    current_created: str


async def _list_all(list_all: Any) -> Any:
    """The bucket manager's list of every entry, archive included where it can say so."""
    try:
        return await list_all(include_archive=True)
    except TypeError:
        return await list_all()


async def identify_conflicts(bucket_mgr: Any,
                             parsed_buckets: list[_ParsedBucket]) -> list[ConflictInfo]:
    """Find parse-time conflicts from one vault snapshot.

    Calling ``get`` once per imported ID turns a legitimate large import
    into an O(imported * existing) filesystem/frontmatter scan.  Production
    managers expose ``list_all``; build one ID map from that single scan.
    The fallback only supports minimal legacy/test managers.
    """
    conflicts: list[ConflictInfo] = []
    existing_by_id: dict[str, dict[str, Any]] = {}
    list_all = getattr(bucket_mgr, "list_all", None)
    if callable(list_all):
        existing_buckets = await _list_all(list_all)
        for existing in existing_buckets or []:
            if not isinstance(existing, dict):
                continue
            bucket_id = existing.get("id")
            if isinstance(bucket_id, str) and bucket_id:
                existing_by_id.setdefault(bucket_id, existing)

    for pb in parsed_buckets:
        if callable(list_all):
            existing = existing_by_id.get(pb.bucket_id)
        else:
            existing = await bucket_mgr.get(pb.bucket_id)
        if existing is not None:
            emeta = existing.get("metadata", {})
            conflicts.append(ConflictInfo(
                bucket_id=pb.bucket_id,
                import_name=pb.name,
                import_created=pb.created,
                current_name=_safe_str(emeta.get("name", pb.bucket_id), 200),
                current_created=_safe_str(emeta.get("created", ""), 32),
            ))
    return conflicts


async def library_entries(bucket_mgr: Any) -> list[dict]:
    """Every entry of the receiving library, archive included, as the apply sees it."""
    list_all = getattr(bucket_mgr, "list_all", None)
    if not callable(list_all):
        return []
    found = await _list_all(list_all)
    return [b for b in found or [] if isinstance(b, dict)]


def survey_library(bucket_mgr: Any, config: dict, parsed_buckets: list[_ParsedBucket],
                   existing: list[dict], package_members: dict[str, Any],
                   parse_temp_dir: str) -> dict[str, Any]:
    """(Runs in a thread.) What of the receiving library the import has to respect:

        identical   package ids whose file in the library is byte for byte the
                    package's (already imported)
        own_basis   the sources the library's own entries stand on — every entry but
                    the identical ones, which are the package's — that the package's
                    registry rows may not settle (export_package._merge_registry)
        blocked     the library's entries blocked by a source change (an open
                    source_gone, source_held or source_restored record, or a source
                    its registry records as withdrawn or deleted)
        registry    the library's source registry; package_registry the package's own
    """
    from . import _invalidation as _I
    from . import _sources as _src
    from . import export_package as _ep

    registry = getattr(bucket_mgr, "sources", None)
    by_id = {str(b.get("id") or ""): b for b in existing}
    identical: set[str] = set()
    for pb in parsed_buckets:
        have = by_id.get(pb.bucket_id)
        if have and have.get("path") and _same_file(str(have["path"]),
                                                   read_bucket_member(config, pb)):
            identical.add(pb.bucket_id)
    own_basis: list = []
    blocked: set[str] = set()
    for bid, b in by_id.items():
        if bid in identical:
            continue
        meta = b.get("metadata") or {}
        for rec in _src.basis_records(meta):
            try:
                own_basis.append(_src.record_id(rec))
            except (KeyError, TypeError, AttributeError):
                continue
        if any(r.get("kind") in (_I.SOURCE_GONE, _I.SOURCE_HELD, _I.SOURCE_RESTORED)
               for r in _I.open_records(meta)) or _ep.withdrawn(meta, registry):
            blocked.add(bid)
    return {"identical": identical, "own_basis": own_basis, "blocked": blocked,
            "registry": registry,
            "package_registry": read_package_registry(package_members, parse_temp_dir)}


def read_package_registry(package_members: dict[str, Any], parse_temp_dir: str):
    """The package's own source registry, read from its state members in a scratch
    folder: a package whose entries stand on a source its own registry withdrew is not
    one an export writes, and is not trusted either."""
    from . import _sources as _src
    from . import export_package as _ep

    rows = {name: _ep.STATE_PREFIX + f"{_src.SOURCES_DIR}/{name}"
            for name in (_src.CHANGES_FILE, _src.HELD_FILE, _src.LINE_ORDERS_FILE)}
    if not any(member in package_members for member in rows.values()):
        return None
    scratch = tempfile.mkdtemp(prefix="loci-package-registry-",
                               dir=parse_temp_dir or None)
    folder = os.path.join(scratch, _src.SOURCES_DIR)
    os.makedirs(folder, exist_ok=True)
    for name, member in rows.items():
        source = package_members.get(member)
        if source is None:
            continue
        if isinstance(source, bytes):
            data = source
        else:
            with open(source, "rb") as handle:
                data = handle.read()
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(data)
    return _src.SourceRegistry(scratch)


def refusals(parsed_buckets: list[_ParsedBucket], decisions: dict[str, str],
             survey: dict[str, Any]) -> dict[str, str]:
    """(Runs in a thread.) package id -> why it is not written (REFUSAL_WORDS)."""
    from . import _invalidation as _I
    from . import _sources as _src
    from . import export_package as _ep

    registry = survey["registry"]
    package_registry = survey["package_registry"]
    refused: dict[str, str] = {}
    for pb in parsed_buckets:
        meta = pb.meta
        why = ""
        if (_ep.withdrawn(meta, None) or _ep.withdrawn(meta, registry)
                or (package_registry is not None
                    and _ep.withdrawn(meta, package_registry))):
            why = "withdrawn"
        elif registry is not None and not _I.open_records(meta, _I.SOURCE_HELD):
            # An entry that already carries the package's own hold arrives blocked by
            # it; one the library's hold would reach without a record is not written.
            for raw in _src.basis_records(meta):
                try:
                    [rec] = _src.normalize_sources([raw])
                except (_src.SourceRecordError, ValueError):
                    continue
                if registry.read_state(rec) == _src.HELD:
                    why = "held"
                    break
        if (not why and pb.bucket_id in survey["blocked"]
                and pb.bucket_id not in survey["identical"]
                and decisions.get(pb.bucket_id, "skip") in ("overwrite", "keep_both")):
            why = "blocked_here"
        if why:
            refused[pb.bucket_id] = why
    # Whatever is derived from a refused entry, or from one the library has blocked,
    # every generation.
    stop = set(refused) | survey["blocked"]
    grew = True
    while grew:
        grew = False
        for pb in parsed_buckets:
            if pb.bucket_id in refused or pb.bucket_id in survey["identical"]:
                continue
            if set(read_from_ids(pb.meta)) & stop:
                refused[pb.bucket_id] = "derived"
                stop.add(pb.bucket_id)
                grew = True
    return refused


def plan_ids(bucket_mgr: Any, parsed_buckets: list[_ParsedBucket], refused: dict[str, str],
             conflict_ids_at_parse: frozenset[str], decisions: dict[str, str],
             survey: dict[str, Any]) -> dict[str, str]:
    """package id -> the id it is written under: its own, or for keep_both a fresh id
    in the library's shape that neither the library nor the package uses."""
    finder = getattr(bucket_mgr, "_find_bucket_file", None)
    package_ids = {pb.bucket_id for pb in parsed_buckets}
    given: set[str] = set()

    def taken(candidate: str) -> bool:
        return (candidate in package_ids or candidate in given
                or (callable(finder) and bool(finder(candidate))))

    planned: dict[str, str] = {}
    for pb in parsed_buckets:
        if pb.bucket_id in refused:
            continue
        if (pb.bucket_id in conflict_ids_at_parse
                and pb.bucket_id not in survey["identical"]
                and decisions.get(pb.bucket_id) == "keep_both"):
            new_id = _new_entry_id(taken)
            given.add(new_id)
            planned[pb.bucket_id] = new_id
        else:
            planned[pb.bucket_id] = pb.bucket_id
    return planned


async def drop_withdrawn_slices(bucket_mgr: Any, errors: list[str]) -> None:
    """Pending slices restored from the package over lines the library's registry
    holds as withdrawn or deleted lose their gist and draft at once, as a withdrawal's
    own clearing does (PendingSlices.withdraw_lines). What cannot be read is added to
    `errors`."""
    from . import _sources as _src
    slices = getattr(bucket_mgr, "slices", None)
    registry = getattr(bucket_mgr, "sources", None)
    if slices is None or registry is None:
        return
    try:
        batches = slices.open_batches()
    except Exception as exc:                     # noqa: BLE001 - reported
        errors.append(f"待核切片读不出来：{exc}")
        return
    for batch in batches:
        source = batch.get("source") or {}
        for one in batch.get("slices") or []:
            span = one.get("span") or {}
            first, last = str(span.get("first") or ""), str(span.get("last") or "")
            try:
                sid = _src.SourceId(str(source.get("system")), str(source.get("instance")),
                                    str(source.get("container")), first,
                                    last if last and last != first else None)
                state = registry.state_of(sid)
            except Exception:                    # noqa: BLE001 - not a source it can read
                continue
            if state in (_src.WITHDRAWN, _src.DELETED):
                await slices.withdraw_lines(source, [first])
