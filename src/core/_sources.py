"""
========================================
core/_sources.py — outside material: source records, the source registry, write keys
========================================

A memory's `prov` says which memories it stands on. `sources` says which pieces of the
host's own material it was formed from: a message, a post, a page. Each is one record
in the memory's frontmatter, and the md stays the truth for "this memory was formed
from that":

    sources:
      - system: lento            # which host
        instance: home           # which installation of it
        container: "private:U"   # where in it (a chat, a channel)
        id: m_20260925_0142      # the host's own stable id for the piece
        revision: null           # the host's version / edit number; never the read time
        fingerprint: "sha256:…"  # says same / different, never which came first
        fingerprint_by: adapter  # who computed the fingerprint
        span: {unit: utf16, start: 120, end: 188}   # a fragment only; half-open
        use: null                # permission; null inherits the material's

Identity is system + instance + container + id. Revision, fingerprint, span and the
window it was read in are not identity. The string form (logs, the registry, prov
lines) is `lento:home/private:U#m_20260925_0142`, plus `@<revision>` when there is one.
One message can support several memories and several messages one memory; the source
side never records which memory it belongs to (that is `memories_of`, a reverse lookup).
A `wasQuotedFrom` prov line names a source by its string form.

The registry holds each source's own state, which cannot be read back from the md
files: active / unreadable / withdrawn / deleted, the revisions the host announced,
and `host_seq`, the host's per-source change counter. Newer and older are judged by
host_seq alone, never by arrival order or revision. Its truth is the append-only
`<buckets>/_sources/changes.jsonl`, one applied change per line, numbered by `seq`
(the line the host reconciles against, `applied_seq`). The index is an in-memory dict
rebuilt from that file on first use and whenever another process has appended to it;
at this size (a few thousand lines at most) no SQLite is needed, and if one is added
later it is only an index of this file.

Write keys: a host that resends a write (a retry, a restart) sends the same key, and
the same key is answered with the first result instead of a second memory. The key is
the host's turn plus the ordinal of the write call in it (`write_key`). Claims live in
`<buckets>/_sources/write_keys.jsonl`, append-only, with the ids written and the reply,
so a resend after a restart gets the same answer. They are not in the ledger: the
ledger is a best-effort mirror whose failed appends only log, and a claim that did not
land would let the resend write twice.

What reaches this module from a request (the turn's write key, the grant of sources,
whether the credential may restore) arrives through contextvars and explicit
parameters; nothing here reads a header.

Exports: SOURCES_FIELD · SOURCES_MAX · SourceId · SourceRecordError · normalize_sources ·
         coerce_sources_arg · record_id · record_string · same_delivery · STATES ·
         CHANGE_KINDS · OUTCOMES · next_state · SourceRegistry (apply_change · state_of ·
         revisions_of · describe · rebuild_index · check_writable · claimed · run_once) ·
         memories_of · write_key · current_write_key · write_key_scope · current_grant ·
         grant_scope · note_written · collect_written
========================================
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

# ============================================================
# Source records
# ============================================================

SOURCES_FIELD = "sources"
SOURCES_MAX = 64                # records per memory; more is refused, never cut
STRING_MAX = 128                # a record's string form; it is a prov target (PROV_TARGET_MAX)
SPAN_UNITS = ("utf16", "utf8", "char")

_FIELD_MAX = {"system": 32, "instance": 64, "container": 64, "id": 64, "revision": 64,
              "fingerprint": 128, "fingerprint_by": 32, "use": 64}
_IDENTITY = ("system", "instance", "container", "id")
# The characters that delimit the string form, refused inside the part they would split.
_DELIMITERS = {"system": ":/#@", "instance": "/#@", "container": "#@", "id": "#@",
               "revision": "#@"}
_RECORD_KEYS = (*_IDENTITY, "revision", "fingerprint", "fingerprint_by", "span", "use")


class SourceRecordError(ValueError):
    """A source record the store will not keep. str() is the store's English reason;
    `zh` is the same reason as a tool says it."""

    def __init__(self, message: str, zh: str):
        super().__init__(message)
        self.zh = zh


@dataclass(frozen=True)
class SourceId:
    """The identity of one piece of outside material."""
    system: str
    instance: str
    container: str
    id: str

    def to_string(self, revision: Optional[str] = None) -> str:
        head = f"{self.system}:{self.instance}/{self.container}#{self.id}"
        return f"{head}@{revision}" if revision not in (None, "") else head

    def __str__(self) -> str:
        return self.to_string()

    @classmethod
    def parse(cls, text: str) -> tuple["SourceId", Optional[str]]:
        """`system:instance/container#id[@revision]` -> (identity, revision or None).
        Raises SourceRecordError on anything else."""
        raw = str(text or "").strip()
        left, hash_, right = raw.partition("#")
        system, colon, rest = left.partition(":")
        instance, slash, container = rest.partition("/")
        sid, at, revision = right.partition("@")
        if not (hash_ and colon and slash):
            raise SourceRecordError(
                f"not a source string form (system:instance/container#id): {raw[:STRING_MAX]!r}",
                f"「{raw[:40]}」不是来源的写法（system:instance/container#id）")
        record = {"system": system, "instance": instance, "container": container, "id": sid,
                  "revision": revision if at else None}
        norm = _normalize_record(record, where="")
        return record_id(norm), norm["revision"]


def _text_field(raw: dict, key: str, where: str, *, required: bool) -> Optional[str]:
    value = raw.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise SourceRecordError(f"source{where} needs a non-empty {key}",
                                    f"来源{where}缺 {key}")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SourceRecordError(f"source{where} {key} must be text, got {type(value).__name__}",
                                f"来源{where}的 {key} 要是一段字")
    text = str(value).strip()
    limit = _FIELD_MAX[key]
    if len(text) > limit:
        raise SourceRecordError(f"source{where} {key} is longer than {limit} characters",
                                f"来源{where}的 {key} 超过 {limit} 字（不截断）")
    if any(ch.isspace() for ch in text) and key not in ("use", "fingerprint_by"):
        raise SourceRecordError(f"source{where} {key} holds whitespace: {text!r}",
                                f"来源{where}的 {key} 带空白：{text}")
    if "\n" in text or "\r" in text:
        raise SourceRecordError(f"source{where} {key} is more than one line",
                                f"来源{where}的 {key} 只能一行")
    bad = [ch for ch in _DELIMITERS.get(key, "") if ch in text]
    if bad:
        raise SourceRecordError(f"source{where} {key} holds {''.join(bad)!r}, which splits "
                                "the string form", f"来源{where}的 {key} 里不能有 {''.join(bad)}")
    return text


def _span(raw, where: str) -> Optional[dict]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SourceRecordError(f"source{where} span must be {{unit, start, end}}",
                                f"来源{where}的 span 写成 {{unit, start, end}}")
    unit = str(raw.get("unit") or "").strip().lower()
    if unit not in SPAN_UNITS:
        raise SourceRecordError(f"source{where} span unit must be one of {', '.join(SPAN_UNITS)}",
                                f"来源{where}的 span.unit 只认 {' / '.join(SPAN_UNITS)}")
    start, end = raw.get("start"), raw.get("end")
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (start, end)):
        raise SourceRecordError(f"source{where} span start and end must be integers",
                                f"来源{where}的 span.start / end 要是整数")
    if not 0 <= start < end:
        raise SourceRecordError(f"source{where} span must have 0 <= start < end, got "
                                f"[{start}, {end})", f"来源{where}的 span 要 0 ≤ start < end"
                                f"（收到 {start}..{end}）")
    extra = sorted(set(raw) - {"unit", "start", "end"})
    if extra:
        raise SourceRecordError(f"source{where} span has unknown keys {extra}",
                                f"来源{where}的 span 不认识 {', '.join(map(str, extra))}")
    return {"unit": unit, "start": start, "end": end}


def _normalize_record(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise SourceRecordError(f"source{where} must be a record, got {type(raw).__name__}",
                                f"来源{where}要是一条记录（{{system, instance, container, id, …}}）")
    extra = sorted(set(map(str, raw)) - set(_RECORD_KEYS))
    if extra:
        raise SourceRecordError(f"source{where} has unknown keys {extra}",
                                f"来源{where}不认识 {', '.join(extra)}")
    out: dict = {k: _text_field(raw, k, where, required=True) for k in _IDENTITY}
    for k in ("revision", "fingerprint", "fingerprint_by"):
        out[k] = _text_field(raw, k, where, required=False)
    span = _span(raw.get("span"), where)
    if span:
        out["span"] = span
    out["use"] = _text_field(raw, "use", where, required=False)
    text = record_string(out)
    if len(text) > STRING_MAX:
        raise SourceRecordError(f"source{where} string form is {len(text)} characters, over "
                                f"{STRING_MAX}", f"来源{where}写全了有 {len(text)} 字，超过 "
                                f"{STRING_MAX}（不截断）")
    return out


def normalize_sources(raw) -> list[dict]:
    """The stored form of `sources`: one record per piece, order kept. A record repeated
    with the same identity, revision and span is kept once. Raises SourceRecordError on a
    record that is malformed (identity missing, a field too long or holding a delimiter,
    a bad span) or on more than SOURCES_MAX records; nothing is cut to fit."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise SourceRecordError(f"sources must be a list of records, got {type(raw).__name__}",
                                "sources 要传来源记录的列表")
    out: list[dict] = []
    seen: set = set()
    for i, item in enumerate(raw):
        rec = _normalize_record(item, where=f"[{i}]")
        key = (record_string(rec), json.dumps(rec.get("span"), sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    if len(out) > SOURCES_MAX:
        raise SourceRecordError(f"sources holds at most {SOURCES_MAX} records, got {len(out)}",
                                f"sources 最多 {SOURCES_MAX} 条（收到 {len(out)} 条）——拆开分别存")
    return out


def record_id(record: dict) -> SourceId:
    return SourceId(*(str(record[k]) for k in _IDENTITY))


def record_string(record: dict) -> str:
    """A stored record's string form, with its revision when it has one."""
    return record_id(record).to_string(record.get("revision"))


def same_delivery(a: dict, b: dict) -> bool:
    """Do two records name the same piece as delivered: same identity and same
    fingerprint, or — with no fingerprint on either side — the same revision."""
    if record_id(a) != record_id(b):
        return False
    fa, fb = a.get("fingerprint"), b.get("fingerprint")
    if fa or fb:
        return fa == fb
    return a.get("revision") == b.get("revision")


def _identity_key(identity) -> str:
    """Accept a SourceId, a record, or a string form (a revision on it is dropped)."""
    if isinstance(identity, SourceId):
        return identity.to_string()
    if isinstance(identity, dict):
        return record_id(_normalize_record(identity, where="")).to_string()
    return SourceId.parse(str(identity))[0].to_string()


async def memories_of(store, identity) -> list[str]:
    """The memories (archive included) whose `sources` name this identity, by scanning
    the library. The source side never stores this; an index of it is stage 5's."""
    want = _identity_key(identity)
    out: list[str] = []
    for b in await store.list_all(include_archive=True):
        meta = b.get("metadata") or {}
        for rec in meta.get(SOURCES_FIELD) or []:
            try:
                if isinstance(rec, dict) and record_id(rec).to_string() == want:
                    out.append(str(meta.get("id") or b.get("id") or ""))
                    break
            except KeyError:
                continue
    return [i for i in dict.fromkeys(out) if i]


# ============================================================
# What a request carries: the write key, the grant, what this call wrote
# ============================================================

_WRITE_KEY: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "loci_write_key", default=None)
_GRANT: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "loci_source_grant", default=None)
# The ids written under the keyed run in progress; None outside one.
_WRITTEN: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "loci_written_ids", default=None)
WRITE_KEY_MAX = 256


def write_key(turn: str, ordinal: int) -> str:
    """The key of one write call: the host's turn plus the call's ordinal in it."""
    return f"{str(turn).strip()}#{int(ordinal)}"


def current_write_key() -> Optional[str]:
    """This call's write key, or None (nothing is deduplicated by key)."""
    key = _WRITE_KEY.get()
    return key if key else None


@contextmanager
def write_key_scope(key: Optional[str]):
    """Run a block under a write key (the request layer sets it from the host's turn)."""
    key = str(key).strip()[:WRITE_KEY_MAX] if key else None
    token = _WRITE_KEY.set(key or None)
    try:
        yield
    finally:
        _WRITE_KEY.reset(token)


def current_grant() -> Optional[frozenset]:
    """The identities this turn's host handed over, as string forms; None = not enforced."""
    return _GRANT.get()


@contextmanager
def grant_scope(grant: Optional[Iterable]):
    """Run a block under a grant of sources (None lifts it)."""
    token = _GRANT.set(None if grant is None else frozenset(_identity_key(g) for g in grant))
    try:
        yield
    finally:
        _GRANT.reset(token)


def note_written(bucket_id: str) -> None:
    """Record that this call wrote `bucket_id` (create calls it; trace for an edit), so
    a keyed run can claim what it did."""
    written = _WRITTEN.get()
    if written is not None and bucket_id:
        written.append(str(bucket_id))


@contextmanager
def collect_written():
    """Collect the ids written inside the block into the list it yields (a write built
    from a slice closes the slice on what it wrote). What it collects is also handed on
    to an enclosing keyed run, so that run still claims it. Use it inside run_once,
    never around it: a run that starts inside this block takes itself for nested."""
    outer = _WRITTEN.get()
    mine: list[str] = []
    token = _WRITTEN.set(mine)
    try:
        yield mine
    finally:
        _WRITTEN.reset(token)
        if outer is not None:
            outer.extend(mine)


# ============================================================
# The registry
# ============================================================

ACTIVE, UNREADABLE, WITHDRAWN, DELETED = "active", "unreadable", "withdrawn", "deleted"
STATES = (ACTIVE, UNREADABLE, WITHDRAWN, DELETED)
CHANGE_KINDS = ("withdrawn", "deleted", "use_changed", "unreadable", "restored", "revised")
APPLIED, DUPLICATE, CONFLICT, STALE, FORBIDDEN, UNKNOWN_SOURCE = (
    "applied", "duplicate", "conflict", "stale", "forbidden", "unknown_source")
OUTCOMES = (APPLIED, DUPLICATE, CONFLICT, STALE, FORBIDDEN, UNKNOWN_SOURCE)

SOURCES_DIR = "_sources"
CHANGES_FILE = "changes.jsonl"
WRITE_KEYS_FILE = "write_keys.jsonl"
_CHANGE_ID_MAX = 128
# The fields that make two sends of one change_id the same change.
_CHANGE_CONTENT = ("source", "kind", "host_seq", "revision", "fingerprint", "fingerprint_by",
                   "use")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_line(path: Path, row: dict) -> None:
    """One JSON line, appended whole: the file is started on a fresh line if a crash
    left the last one torn (a torn line is skipped on read), and the write is flushed
    to disk before it counts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with path.open("ab") as f:
        if f.tell() > 0:
            with path.open("rb") as r:
                r.seek(-1, os.SEEK_END)
                if r.read(1) != b"\n":
                    data = b"\n" + data
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
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


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _change_from(record) -> dict:
    """A change as the host sends it -> its normalised form. Raises ValueError on a
    malformed one (the route answers that with a 400; it is no outcome)."""
    if not isinstance(record, dict):
        raise ValueError("a source change must be an object")
    change_id = str(record.get("change_id") or "").strip()
    if not change_id or len(change_id) > _CHANGE_ID_MAX or any(c.isspace() for c in change_id):
        raise ValueError(f"change_id must be one token of at most {_CHANGE_ID_MAX} characters")
    kind = str(record.get("kind") or "").strip()
    if kind not in CHANGE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(CHANGE_KINDS)}, got {kind!r}")
    host_seq = record.get("host_seq")
    if not isinstance(host_seq, int) or isinstance(host_seq, bool) or host_seq < 0:
        raise ValueError("host_seq must be a non-negative integer")
    source = record.get("source")
    if isinstance(source, dict):
        sid = record_id(_normalize_record(source, where=""))
    elif isinstance(source, str):
        sid = SourceId.parse(source)[0]
    else:
        sid = record_id(_normalize_record({k: record.get(k) for k in _IDENTITY}, where=""))
    fields = {"system": sid.system, "instance": sid.instance, "container": sid.container,
              "id": sid.id}
    for k in ("revision", "fingerprint", "fingerprint_by", "use"):
        fields[k] = record.get(k)
    norm = _normalize_record(fields, where="")
    if kind == "revised" and not (norm["revision"] or norm["fingerprint"]):
        raise ValueError("a revised change carries the new revision or fingerprint")
    return {"change_id": change_id, "source": sid.to_string(), "kind": kind,
            "host_seq": host_seq, "revision": norm["revision"],
            "fingerprint": norm["fingerprint"], "fingerprint_by": norm["fingerprint_by"],
            "use": norm["use"]}


def next_state(current: str, kind: str, may_restore: bool) -> tuple[Optional[str], str]:
    """The state machine: (state after, note). None = forbidden.

    withdrawn / deleted   become that state, from anything
    use_changed           leaves the state; the new `use` is recorded
    unreadable            masks only an active source; a withdrawn or deleted one stays so
    restored              lifts unreadable; lifts withdrawn / deleted only with may_restore,
                          and a deleted body is not restored with it (the host redelivers)
    revised               chains the new revision; a revised delivery is an active signal,
                          so it lifts unreadable, and a withdrawn or deleted source stays so
    """
    if kind in (WITHDRAWN, DELETED):
        return kind, ""
    if kind == "use_changed":
        return current, ""
    if kind == UNREADABLE:
        if current in (WITHDRAWN, DELETED):
            return current, "unreadable_cannot_mask"
        return UNREADABLE, ""
    if kind == "restored":
        if current in (WITHDRAWN, DELETED):
            if not may_restore:
                return None, "may_restore_required"
            return ACTIVE, "redeliver" if current == DELETED else ""
        return ACTIVE, ""
    if kind == "revised":
        return (ACTIVE if current == UNREADABLE else current), ""
    raise ValueError(f"unknown change kind {kind!r}")


class SourceRegistry:
    """The source registry and the write-key claims of one library (`<buckets>/_sources`).

    `store` is the BucketManager, read only for the reverse lookup (`memories_of`)."""

    def __init__(self, base_dir: str | os.PathLike, store=None):
        self.base_dir = str(base_dir)
        self.dir = Path(base_dir) / SOURCES_DIR
        self.changes_path = self.dir / CHANGES_FILE
        self.keys_path = self.dir / WRITE_KEYS_FILE
        self.store = store
        self._guard = threading.RLock()
        self._changes_size = -1
        self._keys_size = -1
        self._seq = 0
        self._by_source: dict[str, dict] = {}
        self._by_change: dict[str, dict] = {}
        self._claims: dict[str, dict] = {}

    # ---------- the index ----------

    def rebuild_index(self) -> None:
        """Read changes.jsonl again from the top. Each line carries the state it left,
        so the index is the last line per source, not a replay of the machine."""
        with self._guard:
            size = _size(self.changes_path)
            self._seq = 0
            self._by_source = {}
            self._by_change = {}
            for row in _read_lines(self.changes_path):
                self._index(row)
            self._changes_size = size

    def _index(self, row: dict) -> None:
        try:
            seq = int(row.get("seq") or 0)
            source = str(row["source"])
            host_seq = int(row["host_seq"])
        except (KeyError, TypeError, ValueError):
            return
        self._seq = max(self._seq, seq)
        self._by_change[str(row.get("change_id") or "")] = row
        entry = self._by_source.setdefault(source, {
            "state": ACTIVE, "host_seq": -1, "use": None, "revisions": [], "seqs": {}})
        entry["seqs"][host_seq] = str(row.get("change_id") or "")
        if host_seq >= entry["host_seq"]:
            entry["host_seq"] = host_seq
            entry["state"] = str(row.get("state") or entry["state"])
            if row.get("kind") == "use_changed":
                entry["use"] = row.get("use")
        if row.get("kind") == "revised":
            entry["revisions"].append({k: row.get(k) for k in (
                "revision", "fingerprint", "fingerprint_by", "host_seq", "seq")})
            entry["revisions"].sort(key=lambda r: int(r.get("host_seq") or 0))

    def _fresh(self) -> None:
        with self._guard:
            if _size(self.changes_path) != self._changes_size:
                self.rebuild_index()

    # ---------- reading ----------

    def describe(self, identity) -> Optional[dict]:
        """What the registry knows of a source: {state, host_seq, use, revisions}, or
        None when it has never heard of it."""
        key = _identity_key(identity)
        self._fresh()
        with self._guard:
            entry = self._by_source.get(key)
            if entry is None:
                return None
            return {"state": entry["state"], "host_seq": entry["host_seq"],
                    "use": entry["use"], "revisions": [dict(r) for r in entry["revisions"]]}

    def state_of(self, identity) -> str:
        """The source's state; one the registry has never heard of is active."""
        found = self.describe(identity)
        return found["state"] if found else ACTIVE

    def revisions_of(self, identity) -> list[dict]:
        """The revisions the host announced (`revised`), oldest first by host_seq."""
        found = self.describe(identity)
        return found["revisions"] if found else []

    # ---------- applying a change ----------

    async def apply_change(self, record: dict, *, may_restore: bool = False) -> dict:
        """Apply one change the host sent. Returns the outcome:

            {outcome, change_id, source, kind, state, previous, host_seq, applied_seq,
             memories, note?, result?, conflict_with?}

        outcome is one of OUTCOMES. Only `applied` and `unknown_source` write a line;
        `unknown_source` is an applied change for a source no memory names yet, recorded
        all the same so a later delivery from it meets its real state. `memories` lists
        the memories naming the source, which is what a cleanup step acts on. A malformed
        record raises ValueError."""
        from .bucket_manager import _filesystem_turn      # lazy: bucket_manager imports this module

        change = _change_from(record)
        memories = await memories_of(self.store, change["source"]) if self.store else None
        async with _filesystem_turn(self.base_dir, "source-registry"):
            return self._apply_locked(change, may_restore, memories)

    def _apply_locked(self, change: dict, may_restore: bool,
                      memories: Optional[list[str]]) -> dict:
        self._fresh()
        with self._guard:
            source = change["source"]
            entry = self._by_source.get(source)
            current = entry["state"] if entry else ACTIVE
            base = {"change_id": change["change_id"], "source": source, "kind": change["kind"],
                    "state": current, "previous": current,
                    "host_seq": entry["host_seq"] if entry else None,
                    "applied_seq": None, "memories": list(memories or [])}
            prior = self._by_change.get(change["change_id"])
            if prior is not None:
                if all(prior.get(k) == change.get(k) for k in _CHANGE_CONTENT):
                    return {**base, "outcome": DUPLICATE, "result": prior.get("outcome"),
                            "state": prior.get("state"), "previous": prior.get("previous"),
                            "applied_seq": prior.get("seq")}
                return {**base, "outcome": CONFLICT, "conflict_with": dict(prior),
                        "note": "change_id_reused"}
            if entry is not None:
                other = entry["seqs"].get(change["host_seq"])
                if other is not None:
                    return {**base, "outcome": CONFLICT,
                            "conflict_with": dict(self._by_change.get(other) or {}),
                            "note": "host_seq_reused"}
                if change["host_seq"] < entry["host_seq"]:
                    return {**base, "outcome": STALE}
            state, note = next_state(current, change["kind"], may_restore)
            if state is None:
                return {**base, "outcome": FORBIDDEN, "note": note}
            outcome = UNKNOWN_SOURCE if memories == [] else APPLIED
            row = {**change, "seq": self._seq + 1, "state": state, "previous": current,
                   "outcome": outcome, "recorded_at": _now()}
            if note:
                row["note"] = note
            _append_line(self.changes_path, row)
            self._index(row)
            self._changes_size = _size(self.changes_path)
            result = {**base, "outcome": outcome, "state": state, "host_seq": change["host_seq"],
                      "applied_seq": row["seq"]}
            if note:
                result["note"] = note
            return result

    # ---------- the write-time check ----------

    def check_writable(self, sources: list[dict],
                       grant: Optional[Iterable] = None) -> tuple[str, list[str]]:
        """May a memory be written from these (normalised) records? Returns (refusal,
        notes for the receipt). Each source has to be in the turn's grant when one is
        given (None = not enforced), and not withdrawn or deleted. An unreadable source
        is taken (the host just handed its text over) with a note; one the registry has
        never seen is simply active."""
        granted = None if grant is None else {_identity_key(g) for g in grant}
        notes: list[str] = []
        for rec in sources or []:
            key = record_id(rec).to_string()
            if granted is not None and key not in granted:
                return (f"来源 {key} 不在这一轮宿主交过来的材料里——只能用这一轮给的来源写。"
                        "本次什么都没写。"), []
            state = self.state_of(key)
            if state == WITHDRAWN:
                return (f"来源 {key} 已经被撤回了，不能再拿它写记忆。本次什么都没写。"), []
            if state == DELETED:
                return (f"来源 {key} 已经被删除了，不能再拿它写记忆。本次什么都没写。"), []
            if state == UNREADABLE:
                notes.append(f"来源 {key} 宿主那边眼下读不到；这次的原文是刚交过来的，照收。")
        return "", notes

    # ---------- write keys ----------

    def _fresh_claims(self) -> None:
        with self._guard:
            size = _size(self.keys_path)
            if size == self._keys_size:
                return
            self._claims = {}
            for row in _read_lines(self.keys_path):
                key = str(row.get("key") or "")
                if key and key not in self._claims:
                    self._claims[key] = row
            self._keys_size = size

    def claimed(self, key: str) -> Optional[dict]:
        """The first result recorded under this write key, or None."""
        self._fresh_claims()
        with self._guard:
            row = self._claims.get(str(key))
            return dict(row) if row else None

    def _claim(self, key: str, op: str, ids: list[str], reply: str) -> None:
        row = {"key": key, "op": op, "ids": list(dict.fromkeys(ids)), "reply": reply,
               "recorded_at": _now()}
        with self._guard:
            _append_line(self.keys_path, row)
            self._fresh_claims()
            self._claims.setdefault(key, row)

    async def run_once(self, key: Optional[str], do: Callable[[], Awaitable[str]],
                       op: str = "") -> str:
        """Run `do` once per write key. The same key again gets the first reply back and
        nothing is written. A run that wrote nothing (a refusal, an all-duplicate batch)
        claims nothing, so a resend runs again and is refused the same way. Without a key,
        or inside a keyed run already, `do` simply runs."""
        if not key or _WRITTEN.get() is not None:
            return await do()
        from .bucket_manager import _filesystem_turn      # lazy: bucket_manager imports this module

        key = str(key)[:WRITE_KEY_MAX]
        async with _filesystem_turn(self.base_dir, f"write-key-{key}"):
            prior = self.claimed(key)
            if prior is not None:
                return str(prior.get("reply") or "")
            written: list[str] = []
            token = _WRITTEN.set(written)
            try:
                reply = await do()
            except BaseException:
                if written:
                    self._claim(key, op, written, "这次写到一半出错了，已经落盘的："
                                + "、".join(dict.fromkeys(written)))
                raise
            finally:
                _WRITTEN.reset(token)
            if written:
                self._claim(key, op, written, str(reply))
            return reply


def coerce_sources_arg(raw: Any) -> Any:
    """A tool argument as clients send it: a list, one record, or either as a JSON
    string. Anything else is handed on for normalize_sources to refuse."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text[0] in "[{":
            try:
                return json.loads(text)
            except ValueError:
                return raw
    return raw
