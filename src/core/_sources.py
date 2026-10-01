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
        through: m_20260925_0160 # a run of lines only: the last line's id (id is the first)
        revision: null           # the host's version / edit number; never the read time
        fingerprint: "sha256:…"  # says same / different, never which came first
        fingerprint_by: adapter  # who computed the fingerprint
        span: {unit: utf16, start: 120, end: 188}   # a fragment only; half-open
        use: null                # permission; null inherits the material's

Identity is system + instance + container + id, plus `through` for a run of lines: two
runs starting at the same line and ending at different ones are two sources. Revision,
fingerprint, span and the window it was read in are not identity. `span` is a fragment
of one piece and `through` a run of whole pieces, so a record has one or the other; a
`through` equal to `id` is that one line and is not written. The string form (logs, the
registry, prov lines) is `lento:home/private:U#m_20260925_0142`, or
`…#m_20260925_0142..m_20260925_0160` for a run, plus `@<revision>` when there is one.
One message can support several memories and several messages one memory; the source
side never records which memory it belongs to (that is `memories_of`, a reverse lookup).
A `wasQuotedFrom` prov line names a source by its string form.

The host announces changes per piece, never per run, and a change to any line inside a
run reaches the run: its state is the worst of its own identity and every line it holds
(`state_of`), and each line's `use_changed` narrows it (`uses_of`), so a memory standing
on the run is blocked whole until it is rewritten — nothing trims a run automatically.
What a run holds is the host's order of its lines, kept when the host hands a stretch of
lines over for slicing (`record_order`, `<buckets>/_sources/line_orders.jsonl`: the ids in
order, never their text, kept after the slices themselves are handled). A run whose order
was never handed over is known only by its first and last line (`lines_of`).
`memories_of` a single line finds every run holding it.

The registry holds each source's own state, which cannot be read back from the md
files: active / unreadable / withdrawn / deleted, the revisions the host announced,
and `host_seq`, the host's per-source change counter. Newer and older are judged by
host_seq alone, never by arrival order or revision; a change_id and the host_seq order
belong to the host that sent them (two hosts never collide or stale each other). Its
truth is the append-only
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

A grant is a list of places (`Place`): a prefix of a source's identity, system →
instance → container → id. A place covers every source under it; a place naming an id
covers that one piece, never a run starting at it. A run passes a grant only when every
line in it is granted (`SourceRegistry.granted`): a place at its container or wider
covers it, and so do places naming each of its lines, when the host's order of them is
known; with the order unknown, only the container or wider will do. The read gate
(core/scope.py), the write-time check and `/changes` all ask the same question, so what a
turn may read and what it may write from are the same set.

`use` on a record is the host's rule for where the piece may be used: an object
`{venues: [...], audience: [...]}` — the venues it may be used in and the people allowed
to see it, compared literally with the turn's `venue` and `audience`. An absent list does
not narrow that side; an empty list allows nothing on it; `use: null` is no rule of the
piece's own (the grant, the host's ceiling and the source's state still decide). Text that
is not such an object, an unknown key or a name that is not text is kept as given and read
by the gate as a rule it cannot understand: under a scope it lets nothing through.

Exports: SOURCES_FIELD · SOURCES_MAX · SourceId · SourceRecordError · normalize_sources ·
         coerce_sources_arg · record_id · record_string · same_delivery · same_reference ·
         Place · places_of · places_cover · parse_use · STATES · CHANGE_KINDS · OUTCOMES ·
         next_state · SourceRegistry (apply_change · prior_change · state_of · read_state ·
         granted · use_of · uses_of · revisions_of · describe · record_order · members_of ·
         lines_of ·
         rebuild_index · check_writable · claimed · run_once) ·
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

_FIELD_MAX = {"system": 32, "instance": 64, "container": 64, "id": 64, "through": 64,
              "revision": 64, "fingerprint": 128, "fingerprint_by": 32, "use": 64}
_IDENTITY = ("system", "instance", "container", "id")
# The characters that delimit the string form, refused inside the part they would split.
_DELIMITERS = {"system": ":/#@", "instance": "/#@", "container": "#@", "id": "#@",
               "through": "#@", "revision": "#@"}
_RANGE_MARK = ".."              # between id and through in the string form
_RECORD_KEYS = (*_IDENTITY, "through", "revision", "fingerprint", "fingerprint_by", "span",
                "use")


class SourceRecordError(ValueError):
    """A source record the store will not keep. str() is the store's English reason;
    `zh` is the same reason as a tool says it."""

    def __init__(self, message: str, zh: str):
        super().__init__(message)
        self.zh = zh


@dataclass(frozen=True)
class SourceId:
    """The identity of one piece of outside material, or of a run of consecutive pieces
    (`through` = the last one's id)."""
    system: str
    instance: str
    container: str
    id: str
    through: Optional[str] = None

    def to_string(self, revision: Optional[str] = None) -> str:
        head = f"{self.system}:{self.instance}/{self.container}#{self.id}"
        if self.through:
            head = f"{head}{_RANGE_MARK}{self.through}"
        return f"{head}@{revision}" if revision not in (None, "") else head

    def __str__(self) -> str:
        return self.to_string()

    def first(self) -> "SourceId":
        """The identity of the first piece (itself when it is not a run)."""
        return SourceId(self.system, self.instance, self.container, self.id)

    def last(self) -> "SourceId":
        """The identity of the last piece (itself when it is not a run)."""
        return SourceId(self.system, self.instance, self.container, self.through or self.id)

    def piece(self, piece_id: str) -> "SourceId":
        """Another piece of the same container."""
        return SourceId(self.system, self.instance, self.container, str(piece_id))

    @classmethod
    def parse(cls, text: str) -> tuple["SourceId", Optional[str]]:
        """`system:instance/container#id[..through][@revision]` -> (identity, revision or
        None). Raises SourceRecordError on anything else."""
        raw = str(text or "").strip()
        left, hash_, right = raw.partition("#")
        system, colon, rest = left.partition(":")
        instance, slash, container = rest.partition("/")
        pieces, at, revision = right.partition("@")
        sid, dots, through = pieces.partition(_RANGE_MARK)
        if not (hash_ and colon and slash) or (dots and not through):
            raise SourceRecordError(
                f"not a source string form (system:instance/container#id): {raw[:STRING_MAX]!r}",
                f"「{raw[:40]}」不是来源的写法（system:instance/container#id）")
        record = {"system": system, "instance": instance, "container": container, "id": sid,
                  "through": through if dots else None, "revision": revision if at else None}
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
    if key in ("id", "through") and _RANGE_MARK in text:
        bad.append(_RANGE_MARK)
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


USE_KEYS = ("venues", "audience")
USE_LIST_MAX = 64               # names per list in a `use`
USE_NAME_MAX = 128              # one venue or one person


def _use_names(raw, key: str, where: str) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise SourceRecordError(f"source{where} use.{key} must be a list of names",
                                f"来源{where}的 use.{key} 要是一列名字")
    out: list[str] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (str, int)) or not str(item).strip():
            raise SourceRecordError(f"source{where} use.{key} holds something that is not a "
                                    "name", f"来源{where}的 use.{key} 里有一项不是名字")
        name = str(item).strip()
        if len(name) > USE_NAME_MAX or "\n" in name or "\r" in name:
            raise SourceRecordError(f"source{where} use.{key} name is over {USE_NAME_MAX} "
                                    "characters or more than one line",
                                    f"来源{where}的 use.{key} 里有一项太长或不止一行")
        out.append(name)
    if len(out) > USE_LIST_MAX:
        raise SourceRecordError(f"source{where} use.{key} holds more than {USE_LIST_MAX} names",
                                f"来源{where}的 use.{key} 最多 {USE_LIST_MAX} 个")
    return sorted(set(out))


def _use_field(raw, where: str):
    """A record's `use` as stored: None, the object form {venues?, audience?} with sorted
    unique names, or text kept as given (a JSON object written as text is read as the
    object form)."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                raw = json.loads(text)
            except ValueError:
                pass
    if isinstance(raw, dict):
        extra = sorted(set(map(str, raw)) - set(USE_KEYS))
        if extra:
            raise SourceRecordError(f"source{where} use has unknown keys {extra} (known: "
                                    f"{', '.join(USE_KEYS)})",
                                    f"来源{where}的 use 不认识 {', '.join(extra)}"
                                    f"（只认 {' / '.join(USE_KEYS)}）")
        return {k: _use_names(raw[k], k, where) for k in USE_KEYS if k in raw}
    return _text_field({"use": raw}, "use", where, required=False)


def parse_use(stored) -> Optional[dict]:
    """A stored `use` read as a rule: {"venues": set | None, "audience": set | None}, or
    None when it cannot be read as one (text that is not the object form, an unknown key,
    a list that is not a list of names). None for a side = that side is not narrowed; an
    empty set = nothing is allowed on it. Call it only on a use that is present; an absent
    use is no rule of its own."""
    if isinstance(stored, str):
        try:
            stored = _use_field(stored, where="")
        except SourceRecordError:
            return None
    if not isinstance(stored, dict) or set(map(str, stored)) - set(USE_KEYS):
        return None
    out: dict = {}
    for k in USE_KEYS:
        if k not in stored:
            out[k] = None
            continue
        names = stored[k]
        if not isinstance(names, (list, tuple)):
            return None
        if any(not isinstance(n, str) or not n.strip() for n in names):
            return None
        out[k] = frozenset(n.strip() for n in names)
    return out


def _normalize_record(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise SourceRecordError(f"source{where} must be a record, got {type(raw).__name__}",
                                f"来源{where}要是一条记录（{{system, instance, container, id, …}}）")
    extra = sorted(set(map(str, raw)) - set(_RECORD_KEYS))
    if extra:
        raise SourceRecordError(f"source{where} has unknown keys {extra}",
                                f"来源{where}不认识 {', '.join(extra)}")
    out: dict = {k: _text_field(raw, k, where, required=True) for k in _IDENTITY}
    through = _text_field(raw, "through", where, required=False)
    if through and through != out["id"]:
        out["through"] = through
    for k in ("revision", "fingerprint", "fingerprint_by"):
        out[k] = _text_field(raw, k, where, required=False)
    span = _span(raw.get("span"), where)
    if span and through:
        raise SourceRecordError(f"source{where} has both span and through: a span is a "
                                "fragment of one piece, through a run of whole ones",
                                f"来源{where}的 span 和 through 不能一起用：span 是一条里的一段，"
                                "through 是连着的好几条")
    if span:
        out["span"] = span
    out["use"] = _use_field(raw.get("use"), where)
    text = record_string(out)
    if len(text) > STRING_MAX:
        raise SourceRecordError(f"source{where} string form is {len(text)} characters, over "
                                f"{STRING_MAX}", f"来源{where}写全了有 {len(text)} 字，超过 "
                                f"{STRING_MAX}（不截断）")
    return out


def normalize_sources(raw) -> list[dict]:
    """The stored form of `sources`: one record per piece or run, order kept. A record
    repeated with the same identity, revision and span is kept once. Raises
    SourceRecordError on a record that is malformed (identity missing, a field too long or
    holding a delimiter, a bad span, span with through) or on more than SOURCES_MAX
    records; nothing is cut to fit."""
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
    through = record.get("through")
    return SourceId(*(str(record[k]) for k in _IDENTITY),
                    through=str(through) if through not in (None, "") else None)


def record_string(record: dict) -> str:
    """A stored record's string form, with its revision when it has one."""
    return record_id(record).to_string(record.get("revision"))


def same_delivery(a: dict, b: dict) -> bool:
    """Do two records name the same piece as delivered: same identity and same
    fingerprint, or — with no fingerprint on either side — the same revision, which then
    has to be named. Two records with neither a fingerprint nor a revision say nothing
    about their content (`same_reference` is all that can be said of them)."""
    if record_id(a) != record_id(b):
        return False
    fa, fb = a.get("fingerprint"), b.get("fingerprint")
    if fa or fb:
        return fa == fb
    ra, rb = a.get("revision"), b.get("revision")
    return ra not in (None, "") and ra == rb


def same_reference(a: dict, b: dict) -> bool:
    """Do two records name the same piece, whatever its content was when each was made."""
    return record_id(a) == record_id(b)


_PLACE_LEVELS = ("system", "instance", "container", "id")
PLACE_KEYS = _PLACE_LEVELS


@dataclass(frozen=True)
class Place:
    """A prefix of source identity: a system, an instance of it, a container in it, or
    one piece (`id`). `through` is set only on a place made from a run's own string form,
    which then names exactly that run (a host's grant never carries one)."""
    system: str
    instance: Optional[str] = None
    container: Optional[str] = None
    id: Optional[str] = None
    through: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw, where: str = "") -> "Place":
        """{system, instance?, container?, id?} -> Place. Every level named needs the ones
        above it. Raises SourceRecordError on anything else."""
        if not isinstance(raw, dict):
            raise SourceRecordError(f"place{where} must be an object {{system, instance?, "
                                    "container?, id?}", f"{where}要写成 {{system, instance?, "
                                    "container?, id?}")
        extra = sorted(set(map(str, raw)) - set(_PLACE_LEVELS))
        if extra:
            raise SourceRecordError(f"place{where} has unknown keys {extra}",
                                    f"{where}不认识 {', '.join(extra)}（只认 system / instance / "
                                    "container / id）")
        values = [_text_field(raw, k, where, required=(k == "system")) for k in _PLACE_LEVELS]
        for upper, lower, value in zip(_PLACE_LEVELS, _PLACE_LEVELS[1:], values[1:]):
            if value is not None and values[_PLACE_LEVELS.index(upper)] is None:
                raise SourceRecordError(f"place{where} names {lower} without {upper}",
                                        f"{where}写了 {lower} 却没写 {upper}——要从上往下写全")
        return cls(*values)

    @classmethod
    def coerce(cls, value) -> "Place":
        """A Place, a mapping, a SourceId, or a source string form (a revision on it is
        dropped: a grant is about identity)."""
        if isinstance(value, Place):
            return value
        if isinstance(value, SourceId):
            return cls(value.system, value.instance, value.container, value.id, value.through)
        if isinstance(value, dict):
            if "through" in value or "revision" in value:
                return cls.coerce(_identity(value))
            return cls.from_mapping(value)
        return cls.coerce(SourceId.parse(str(value))[0])

    def covers(self, record) -> bool:
        """Does this place cover a source (a record or a SourceId) on its own? A place
        naming an id covers that one piece and no run starting at it; one naming a run
        covers exactly that run. Whether several places cover a run line by line is
        `SourceRegistry.granted`'s."""
        sid = record if isinstance(record, SourceId) else record_id(record)
        if sid.system != self.system:
            return False
        for level in ("instance", "container"):
            want = getattr(self, level)
            if want is not None and getattr(sid, level) != want:
                return False
        if self.id is None:
            return True
        return sid.id == self.id and sid.through == self.through

    def within(self, outer: "Place") -> bool:
        """Is everything this place covers covered by `outer`?"""
        for level in _PLACE_LEVELS:
            want = getattr(outer, level)
            if want is not None and getattr(self, level) != want:
                return False
        if outer.through is not None:
            return self.through == outer.through
        return True

    def label(self) -> str:
        """How a reply names it: the levels joined with `/`, the piece after `#`."""
        head = "/".join(x for x in (self.system, self.instance, self.container) if x)
        if self.id is None:
            return head
        return f"{head}#{self.id}" + (f"{_RANGE_MARK}{self.through}" if self.through else "")


def places_cover(places: Iterable, identity, members: Optional[list] = None) -> bool:
    """Do these places grant this source? A piece: one place covers it. A run: one place
    covers it whole (its container or wider, or the run itself), or — with `members`, the
    ids it holds in the host's order — every one of its lines is covered."""
    sid = _identity(identity)
    places = tuple(places)
    if any(p.covers(sid) for p in places):
        return True
    if sid.through and members:
        return all(any(p.covers(sid.piece(m)) for p in places) for m in members)
    return False


def places_of(grant: Optional[Iterable]) -> Optional[tuple]:
    """A grant as places (None stays None: not enforced)."""
    if grant is None:
        return None
    return tuple(Place.coerce(g) for g in grant)


def _identity(identity) -> SourceId:
    """Accept a SourceId, a record, or a string form (a revision on it is dropped)."""
    if isinstance(identity, SourceId):
        return identity
    if isinstance(identity, dict):
        return record_id(_normalize_record(identity, where=""))
    return SourceId.parse(str(identity))[0]


def _identity_key(identity) -> str:
    return _identity(identity).to_string()


async def memories_of(store, identity) -> list[str]:
    """The memories (archive included) whose `sources` name this identity or contain it,
    by scanning the library. A single piece is found in every run holding it: a run
    whose lines the registry knows (`SourceRegistry.lines_of`) by any of them, one it
    does not by its first or last line. The source side never stores this."""
    want = _identity(identity)
    registry = getattr(store, "sources", None)
    out: list[str] = []
    for b in await store.list_all(include_archive=True):
        meta = b.get("metadata") or {}
        for rec in meta.get(SOURCES_FIELD) or []:
            try:
                if not isinstance(rec, dict):
                    continue
                have = record_id(rec)
            except KeyError:
                continue
            if have == want or (want.through is None and have.through is not None and (
                    want in (registry.lines_of(have) if registry is not None
                             else (have.first(), have.last())))):
                out.append(str(meta.get("id") or b.get("id") or ""))
                break
    return [i for i in dict.fromkeys(out) if i]


# ============================================================
# What a request carries: the write key, the grant, what this call wrote
# ============================================================

_WRITE_KEY: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "loci_write_key", default=None)
_GRANT: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar(
    "loci_source_grant", default=None)
# The ids written under the keyed run in progress; None outside one.
_WRITTEN: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "loci_written_ids", default=None)
WRITE_KEY_MAX = 256


def write_key(turn: str, ordinal: int, host: str = "") -> str:
    """The key of one write call: the host's turn plus the call's ordinal in it, under
    the host that sent it (two hosts sending the same turn and ordinal are two writes).
    A host name holds no `:`, so the first `:` ends it."""
    key = f"{str(turn).strip()}#{int(ordinal)}"
    return f"{host}:{key}" if host else key


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


def current_grant() -> Optional[tuple]:
    """The places this turn's host granted (`Place`); None = not enforced."""
    return _GRANT.get()


@contextmanager
def grant_scope(grant: Optional[Iterable]):
    """Run a block under a grant of sources: places, mappings or string forms (None lifts
    it)."""
    token = _GRANT.set(places_of(grant))
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
LINE_ORDERS_FILE = "line_orders.jsonl"
# The worse state wins when a run is judged by its lines.
_STATE_RANK = {ACTIVE: 0, UNREADABLE: 1, WITHDRAWN: 2, DELETED: 3}
_CHANGE_ID_MAX = 128
# The fields that make two sends of one change_id the same change.
_CHANGE_CONTENT = ("source", "kind", "host_seq", "revision", "fingerprint", "fingerprint_by",
                   "use")


def _now() -> str:
    """Now, on the clock every stored stamp is written by (utils.utc_now): a change's
    time is compared with a memory's `created`."""
    from utils import utc_now
    return utc_now().replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def _append_line(path: Path, row: dict) -> None:
    """One JSON line, appended whole: the file is started on a fresh line if a crash
    left the last one torn (a torn line is skipped on read), and the write is flushed
    to disk before it counts. The append holds the file's own lease (`<file>.lock`), so
    two processes appending at once cannot interleave whatever lock their callers hold."""
    from locibrain.eventsourcing.ledger_mirror import file_lease

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
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
        sid = record_id(_normalize_record({k: record.get(k) for k in (*_IDENTITY, "through")},
                                          where=""))
    fields = {"system": sid.system, "instance": sid.instance, "container": sid.container,
              "id": sid.id, "through": sid.through}
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
        self.orders_path = self.dir / LINE_ORDERS_FILE
        self.store = store
        self._guard = threading.RLock()
        self._changes_size = -1
        self._keys_size = -1
        self._orders_size = -1
        self._seq = 0
        self._by_source: dict[str, dict] = {}
        self._by_change: dict[str, dict] = {}
        self._claims: dict[str, dict] = {}
        # (system, instance, container) -> [(ids in the host's order, {id: position})],
        # newest first
        self._orders: dict[tuple, list] = {}

    # ---------- the index ----------

    def rebuild_index(self) -> None:
        """Read changes.jsonl again from the top. Only applied changes are written, in the
        order they were applied, and each line carries the state it left: the index is the
        last line per source, not a replay of the machine."""
        with self._guard:
            size = _size(self.changes_path)
            self._seq = 0
            self._by_source = {}
            self._by_change = {}
            for row in _read_lines(self.changes_path):
                self._index(row)
            self._changes_size = size

    @staticmethod
    def change_key(host: str, change_id: str) -> str:
        """A change is named by its host and its change_id: two hosts may use one id."""
        return f"{host or ''}\x00{change_id}"

    def _index(self, row: dict) -> None:
        try:
            seq = int(row.get("seq") or 0)
            source = str(row["source"])
            host_seq = int(row["host_seq"])
        except (KeyError, TypeError, ValueError):
            return
        host = str(row.get("host") or "")
        key = self.change_key(host, str(row.get("change_id") or ""))
        self._seq = max(self._seq, seq)
        self._by_change[key] = row
        entry = self._by_source.setdefault(source, {
            "state": ACTIVE, "host_seq": -1, "use": None, "use_changed": False,
            "revisions": [], "hosts": {}})
        mine = entry["hosts"].setdefault(host, {"host_seq": -1, "seqs": {}})
        mine["seqs"][host_seq] = key
        mine["host_seq"] = max(mine["host_seq"], host_seq)
        entry["host_seq"] = host_seq
        entry["state"] = str(row.get("state") or entry["state"])
        if row.get("kind") == "use_changed":
            entry["use"] = row.get("use")
            entry["use_changed"] = True
        if row.get("kind") == "revised":
            entry["revisions"].append({k: row.get(k) for k in (
                "revision", "fingerprint", "fingerprint_by", "host_seq", "seq",
                "recorded_at")})

    def _fresh(self) -> None:
        with self._guard:
            if _size(self.changes_path) != self._changes_size:
                self.rebuild_index()

    # ---------- the host's order of lines (what a run holds) ----------

    def _fresh_orders(self) -> None:
        with self._guard:
            size = _size(self.orders_path)
            if size == self._orders_size:
                return
            orders: dict[tuple, list] = {}
            for row in _read_lines(self.orders_path):
                try:
                    where = (str(row["system"]), str(row["instance"]), str(row["container"]))
                    ids = [str(i) for i in row["ids"]]
                except (KeyError, TypeError):
                    continue
                if ids:
                    orders.setdefault(where, []).insert(
                        0, (ids, {i: n for n, i in enumerate(ids)}))
            self._orders = orders
            self._orders_size = size

    def record_order(self, source: dict, ids: list[str], batch_id: str = "") -> None:
        """Keep the host's order of a stretch of lines (a batch handed over for slicing):
        ids only, never their text. It is how a run `first..last` is known to hold the
        lines between, after the batch itself is gone."""
        ids = [str(i) for i in ids if str(i or "").strip()]
        if not ids:
            return
        where = (str(source.get("system")), str(source.get("instance")),
                 str(source.get("container")))
        self._fresh_orders()
        with self._guard:
            for known, _pos in self._orders.get(where, []):
                if known == ids:
                    return
            _append_line(self.orders_path, {
                "system": where[0], "instance": where[1], "container": where[2],
                "ids": ids, "batch": str(batch_id or ""), "recorded_at": _now()})
            self._orders_size = -1

    def members_of(self, identity) -> Optional[list[str]]:
        """The ids a run holds, first to last, when a recorded order of its container has
        both its ends in order; None when none does (or for a single piece)."""
        sid = _identity(identity)
        if not sid.through:
            return None
        self._fresh_orders()
        with self._guard:
            for ids, pos in self._orders.get((sid.system, sid.instance, sid.container), []):
                a, z = pos.get(sid.id), pos.get(sid.through)
                if a is not None and z is not None and a <= z:
                    return ids[a:z + 1]
        return None

    def lines_of(self, identity) -> list[SourceId]:
        """The single pieces a record stands on: itself for a piece; for a run, every line
        it holds when its order is known (`members_of`), else its first and last line —
        all that can be told of a run the host never handed over in order."""
        sid = _identity(identity)
        if not sid.through:
            return [sid]
        members = self.members_of(sid)
        if members is None:
            return [sid.first(), sid.last()]
        return [sid.piece(i) for i in members]

    # ---------- reading ----------

    def _entry(self, key: str) -> Optional[dict]:
        self._fresh()
        with self._guard:
            return self._by_source.get(key)

    def describe(self, identity) -> Optional[dict]:
        """What the registry knows of a source under its own identity: {state, host_seq,
        use, use_changed, revisions}, or None when it has never heard of it. A run it has
        never heard of is described by its first line. A run's state is `state_of`'s, which
        reads every line of it."""
        sid = _identity(identity)
        entry = self._entry(sid.to_string())
        if entry is None and sid.through:
            entry = self._entry(sid.first().to_string())
        if entry is None:
            return None
        with self._guard:
            return {"state": entry["state"], "host_seq": entry["host_seq"],
                    "use": entry["use"], "use_changed": entry["use_changed"],
                    "revisions": sorted((dict(r) for r in entry["revisions"]),
                                        key=lambda r: int(r.get("host_seq") or 0))}

    def state_of(self, identity) -> str:
        """The source's state; one the registry has never heard of is active. A run is as
        bad as the worst of its own identity and every line it holds (`lines_of`): a line
        withdrawn inside it withdraws the whole run."""
        sid = _identity(identity)
        keys = [sid.to_string()] + ([x.to_string() for x in self.lines_of(sid)]
                                    if sid.through else [])
        worst = ACTIVE
        for key in dict.fromkeys(keys):
            entry = self._entry(key)
            if entry is not None and _STATE_RANK.get(entry["state"], 0) > _STATE_RANK[worst]:
                worst = entry["state"]
        return worst

    def read_state(self, record) -> str:
        """The state the read gate judges a memory's source record by (`state_of`: a run
        by every line inside it)."""
        return self.state_of(record_id(record) if isinstance(record, dict) else record)

    def use_of(self, record: dict):
        """The `use` in force for a source record as a whole: the host's latest
        `use_changed` for its identity when there was one, else the record's own."""
        found = self.describe(record_id(record))
        if found and found.get("use_changed"):
            return found.get("use")
        return record.get("use")

    def granted(self, places, identity) -> bool:
        """Do these places grant this source (`places_cover`, with the run's lines when
        the host's order of them is known)?"""
        sid = _identity(identity)
        return places_cover(places_of(places) or (), sid,
                            self.members_of(sid) if sid.through else None)

    def uses_of(self, record: dict) -> list:
        """Every `use` a record has to satisfy: its own (`use_of`) and, for a run, the
        `use_changed` of each line inside it — a line narrowed narrows the run. An absent
        use is left out (it is no rule of its own)."""
        out = [self.use_of(record)]
        sid = record_id(record)
        if sid.through:
            for line in self.lines_of(sid):
                entry = self._entry(line.to_string())
                if entry is not None and entry["use_changed"]:
                    out.append(entry["use"])
        return [u for u in out if u is not None]

    def revisions_of(self, identity) -> list[dict]:
        """The revisions the host announced (`revised`), oldest first by host_seq."""
        found = self.describe(identity)
        return found["revisions"] if found else []

    # ---------- applying a change ----------

    async def apply_change(self, record: dict, *, may_restore: bool = False,
                           host: str = "") -> dict:
        """Apply one change the host sent. Returns the outcome:

            {outcome, change_id, source, kind, state, previous, host_seq, applied_seq,
             memories, note?, result?, conflict_with?}

        outcome is one of OUTCOMES. Only `applied` and `unknown_source` write a line;
        `unknown_source` is an applied change for a source no memory names yet, recorded
        all the same so a later delivery from it meets its real state. `memories` lists
        the memories naming the source or holding it in a run, which is what a cleanup
        step acts on. `host` names the sender: a change_id and the order of host_seq are
        the sending host's own, so two hosts never collide or stale each other. A
        malformed record raises ValueError."""
        from .bucket_manager import _filesystem_turn      # lazy: bucket_manager imports this module

        change = _change_from(record)
        memories = await memories_of(self.store, change["source"]) if self.store else None
        async with _filesystem_turn(self.base_dir, "source-registry"):
            return self._apply_locked(change, may_restore, memories, str(host or ""))

    def prior_change(self, host: str, change_id: str) -> Optional[dict]:
        """The applied line of this host's change_id, or None."""
        self._fresh()
        with self._guard:
            row = self._by_change.get(self.change_key(host, str(change_id)))
            return dict(row) if row else None

    def _apply_locked(self, change: dict, may_restore: bool,
                      memories: Optional[list[str]], host: str = "") -> dict:
        self._fresh()
        with self._guard:
            source = change["source"]
            entry = self._by_source.get(source)
            current = entry["state"] if entry else ACTIVE
            mine = (entry or {}).get("hosts", {}).get(host)
            base = {"change_id": change["change_id"], "source": source, "kind": change["kind"],
                    "state": current, "previous": current,
                    "host_seq": mine["host_seq"] if mine else None,
                    "applied_seq": None, "memories": list(memories or [])}
            prior = self._by_change.get(self.change_key(host, change["change_id"]))
            if prior is not None:
                if all(prior.get(k) == change.get(k) for k in _CHANGE_CONTENT):
                    return {**base, "outcome": DUPLICATE, "result": prior.get("outcome"),
                            "state": prior.get("state"), "previous": prior.get("previous"),
                            "applied_seq": prior.get("seq")}
                return {**base, "outcome": CONFLICT, "conflict_with": dict(prior),
                        "note": "change_id_reused"}
            if mine is not None:
                other = mine["seqs"].get(change["host_seq"])
                if other is not None:
                    return {**base, "outcome": CONFLICT,
                            "conflict_with": dict(self._by_change.get(other) or {}),
                            "note": "host_seq_reused"}
                if change["host_seq"] < mine["host_seq"]:
                    return {**base, "outcome": STALE}
            state, note = next_state(current, change["kind"], may_restore)
            if state is None:
                return {**base, "outcome": FORBIDDEN, "note": note}
            outcome = UNKNOWN_SOURCE if memories == [] else APPLIED
            row = {**change, "seq": self._seq + 1, "state": state, "previous": current,
                   "outcome": outcome, "recorded_at": _now()}
            if host:
                row["host"] = host
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
        notes for the receipt). Each source has to be covered by the turn's grant when
        one is given (places, mappings or string forms; None = not enforced), and not
        withdrawn or deleted. An unreadable source is taken (the host just handed its text
        over) with a note; one the registry has never seen is simply active. A run is
        granted only when every line in it is (`granted`), and its state is the worst of
        its lines (`state_of`). Writing never changes a source's state: only the host's
        change notices do."""
        granted = places_of(grant)
        notes: list[str] = []
        for rec in sources or []:
            sid = record_id(rec)
            key = sid.to_string()
            if granted is not None and not self.granted(granted, sid):
                return (f"来源 {key} 不在这一轮宿主交过来的材料里——只能用这一轮给的来源写。"
                        "本次什么都没写。"), []
            state = self.state_of(sid)
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
