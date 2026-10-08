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
        completed_from: registry # only when Loci filled in the container of a bare id:
                                 # registry (its line orders) or host_scope (the host's
                                 # one-container ceiling)

Identity is system + instance + container + id, plus `through` for a run of lines: two
runs starting at the same line and ending at different ones are two sources. Revision,
fingerprint, span, completed_from and the window it was read in are not identity. `span` is a fragment
of one piece and `through` a run of whole pieces, so a record has one or the other; a
`through` equal to `id` is that one line and is not written. The string form (logs, the
registry, prov lines) is `lento:home/private:U#m_20260925_0142`, or
`…#m_20260925_0142..m_20260925_0160` for a run, plus `@<revision>` when there is one.
One message can support several memories and several messages one memory; the source
side never records which memory it belongs to (that is `memories_of`, a reverse lookup).
A `wasQuotedFrom` prov line names a source by its string form, and a memory quoting a
source rests on it as much as one listing it in `sources` (`quoted_records`).

The host announces changes per piece, and a change to any line inside a run reaches the
run: its state is the worst of its own identity and every line it holds (`state_of`), and
each line's `use_changed` narrows it (`uses_of`), so a memory standing on the run is
blocked whole until it is rewritten — nothing trims a run automatically. A change to a run
whose lines are registered reaches the other way too: every line it holds, and every run
or piece holding one of them, reads at least as badly as it (`state_of`), is narrowed by
its `use_changed` (`uses_of`) and stands on it (`names_identity`); so does a hold on such a
run. Loci withdrawing an imported conversation is such a change: one per conversation, its
whole run (core/import_memory.py).
What a run holds is the host's order of its lines, registered when the host hands a
stretch of lines over for slicing or registers a run's lines on its own (`record_order`,
`<buckets>/_sources/line_orders.jsonl`: the ids in order, never their text, kept after the
slices are handled and after a withdrawal clears a body — they are what carries the next
withdrawal to the memories still standing on the run). One delivery of a run — its first
and last line under one watermark, or with no watermark (a delivery of its own, independent
of every watermarked one) — holds one list of lines: a second registration of it with
other lines or another order is a conflict (`members_differ`, `order_conflict`) and is not
recorded; the same list again is known. Registrations of one container that disagree
across deliveries are joined (`members_of`): a line is never taken out of a run again; a
registration checks and appends in one turn across processes. A run whose
lines were never registered is known only by its first and last line (`lines_of`) and is
refused as a basis and for reading under any grant (`places_cover`, `check_writable`).
`memories_of` a single line finds every run holding it.

A registration may carry the host's watermark for the delivery (`revision`) and each
line's revision as delivered (`revisions`). A run record whose `revision` names that
watermark adopted those line revisions (`adopted_revisions`); a line whose newest announced
revision differs from the adopted one, or whose adopted revision is unknown while the host
has announced one, counts as revised (`run_revisions`) — when the memory was written
proves nothing about which version the model read. A `revised` change for a run reaches
every line it holds, the way a withdrawal does (`revisions_reaching`): a line's newest
revision is the one applied last among its own and those of the registered runs holding
it, and a run's is compared by `revision` alone (its fingerprint hashes the whole run).

The registry holds each source's own state, which cannot be read back from the md
files: active / unreadable / withdrawn / deleted, the revisions the host announced,
and `host_seq`, the change counter of the source's change authority. Newer and older are
judged by host_seq alone, one order per source whoever sent the change, never by arrival
order or revision; a change_id belongs to the host that sent it. Only the source's
declared change authority sends its changes (core/_source_change.py), so its order is the
one order. Its truth is the append-only
`<buckets>/_sources/changes.jsonl`, one applied change per line, numbered by `seq`
(the line the host reconciles against, `applied_seq`). The index is an in-memory dict
rebuilt from that file on first use and whenever another process has appended to it;
at this size (a few thousand lines at most) no SQLite is needed, and if one is added
later it is only an index of this file.

A hold (`<buckets>/_sources/held.jsonl`, `hold`): a host serving an original said the
source is withdrawn or deleted while the registry records no such change. That answer was
not ordered, so it is never written as the source's state; the source reads as HELD — not
readable, not usable as a basis — until a change that settles it (withdrawn, deleted,
restored) is applied after the hold; one applied before the hold settles nothing of it.
Restoring a source that was never withdrawn needs no `may_restore`, but it is still an
ordered change from the source's authority. A hold on a run whose lines are registered
reaches every line it holds and every piece or run holding one of them (`held_over`). A
settling change for the run itself settles the whole hold; one for a single line, or for
another run whose lines are registered, settles it for the lines that change covers only
(`_line_settled_since`) — the run's other lines, the runs over them and the run itself
stay held until the run's own change or until every line it holds has been settled after
the hold (`held_of`). A run whose lines are unknown covers no line: a change for it settles
nothing of another run's hold. A held run whose lines are unknown reaches no line, and a
change to its first or last line settles nothing of it: it stays held until its own change.

What a change for a run reaches is the run's own; it never undoes a line's own change. A
line withdrawn by its own change stays withdrawn when the run over it is restored (a run is
the worst of its own identity and its lines, and a line the worst of itself and the runs
over it, `state_of`), and a hold on a line alone is settled only by that line's change.

Write keys: a host that resends a write (a retry, a restart) sends the same key, and
the same key is answered with the first result instead of a second memory. The key is
the host's turn plus the ordinal of the write call in it (`write_key`); one past
WRITE_KEY_MAX characters ends in a hash of the whole (`bounded_key`). Claims live in
`<buckets>/_sources/write_keys.jsonl`, append-only, with the ids written and the reply,
so a resend after a restart gets the same answer. They are not in the ledger: the
ledger is a best-effort mirror whose failed appends only log, and a claim that did not
land would let the resend write twice.

What reaches this module from a request (the turn's write key, the grant of sources,
whether the credential may restore) arrives through contextvars and explicit
parameters; nothing here reads a header.

A grant is a list of places (`Place`): a prefix of a source's identity, system →
instance → container → id. A place covers every source under it; a place naming an id
covers that one piece, never a run starting at it. A run passes a grant only when its
lines are registered and every one of them is granted (`SourceRegistry.granted`): by a
place at its container or wider, or by places naming each line; with its lines unknown,
nothing grants it. The read gate (core/scope.py) and the write-time check ask that same
question, so what a turn may read and what it may write from are the same set. A host's
ceiling (`max_grant`) asks where a source lies (`SourceRegistry.reaches`): a run lies in
its container whether its lines are registered or not.

`use` on a record is the host's rule for where the piece may be used: an object
`{venues: [...], audience: [...]}` — the venues it may be used in and the people allowed
to see it, compared literally with the turn's `venue` and `audience`. An absent list does
not narrow that side; an empty list allows nothing on it; `use: null` is no rule of the
piece's own (the grant, the host's ceiling and the source's state still decide). Text that
is not such an object, an unknown key or a name that is not text is kept as given and read
by the gate as a rule it cannot understand: under a scope it lets nothing through.

Exports: SOURCES_FIELD · SOURCES_MAX · COMPLETED_FROM · SourceId · SourceRecordError · normalize_sources ·
         coerce_sources_arg · record_id · record_string · same_delivery · same_reference ·
         Place · places_of · places_cover · parse_use · STATES · HELD · CHANGE_KINDS ·
         OUTCOMES · next_state · SourceRegistry (apply_change · prior_change · state_of ·
         read_state · granted · reaches · order_known · use_of · uses_of · revisions_of ·
         revisions_reaching · newer_revision · describe · record_order ·
         order_conflict · members_of · lines_named ·
         adopted_revisions · run_revisions · lines_of · hold · held_of · held_over · settled_after ·
         rebuild_index · check_writable · claimed · run_once) · names_identity · quoted_records ·
         basis_records · memories_of · write_key · bounded_key · current_write_key ·
         write_key_scope · current_grant · grant_scope · note_written · collect_written ·
         json_line
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
                "use", "completed_from")
# How Loci itself filled in the container of a line the model named by its bare id
# (tools/grow/_sources_check.check_sources): from the registered line orders, or from the
# host's one-container ceiling. Not identity; absent on a record written whole.
COMPLETED_FROM = ("registry", "host_scope")


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
    completed = raw.get("completed_from")
    if completed is not None:
        if completed not in COMPLETED_FROM:
            raise SourceRecordError(f"source{where} completed_from must be one of "
                                    f"{list(COMPLETED_FROM)}",
                                    f"来源{where}的 completed_from 只能是 "
                                    f"{' / '.join(COMPLETED_FROM)}")
        out["completed_from"] = completed
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
    """Do these places grant this source for reading or as a basis? A piece: one place
    covers it. A run: only with `members`, the ids it holds as the host registered them —
    then one place covering it whole (its container or wider, or the run itself) or places
    covering each of its lines. A run whose lines Loci does not know is granted by
    nothing: a line withdrawn in its middle could not be told."""
    sid = _identity(identity)
    places = tuple(places)
    if sid.through:
        if not members:
            return False
        if any(p.covers(sid) for p in places):
            return True
        return all(any(p.covers(sid.piece(m)) for p in places) for m in members)
    return any(p.covers(sid) for p in places)


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


def names_identity(have: SourceId, want: SourceId, registry=None) -> bool:
    """Does a reference to `have` stand on `want`: the same identity; a run holding the
    single piece `want` — by any of its lines when the registry knows them
    (`SourceRegistry.lines_of`), by its first or last line when it does not; or, `want`
    being a run whose lines the registry knows, a reference holding one of those lines (a
    change to a stretch reaches what stands on part of it)."""
    if have == want:
        return True
    if want.through is not None:
        if registry is None or (have.system, have.instance, have.container) != (
                want.system, want.instance, want.container):
            return False
        members = set(registry.members_of(want) or ())
        return bool(members) and any(x.id in members for x in registry.lines_of(have))
    if have.through is None:
        return False
    return want in (registry.lines_of(have) if registry is not None
                    else (have.first(), have.last()))


def quoted_records(meta) -> list[dict]:
    """The sources a memory's `wasQuotedFrom` lines name by string form that its own
    `sources` do not already name, each as a record (no use, no fingerprint). A line with
    the host's bare id (`m_0142`) names no container and is not one."""
    from utils import WAS_QUOTED_FROM, read_prov      # lazy: utils is heavier than this module

    have: set[str] = set()
    for rec in (meta or {}).get(SOURCES_FIELD) or []:
        try:
            have.add(record_id(rec).to_string())
        except (KeyError, TypeError, AttributeError):
            continue
    out: list[dict] = []
    for line in read_prov(meta or {}):
        target = str(line.get("target") or "")
        if line.get("rel") != WAS_QUOTED_FROM or "#" not in target:
            continue
        try:
            sid, revision = SourceId.parse(target)
        except SourceRecordError:
            continue
        if sid.to_string() in have:
            continue
        have.add(sid.to_string())
        out.append({"system": sid.system, "instance": sid.instance, "container": sid.container,
                    "id": sid.id, "through": sid.through, "revision": revision,
                    "fingerprint": None, "fingerprint_by": None, "use": None})
    return out


def basis_records(meta) -> list[dict]:
    """Every source a memory rests on: its `sources`, then what its quoted lines name
    (`quoted_records`)."""
    own = [r for r in (meta or {}).get(SOURCES_FIELD) or [] if isinstance(r, dict)]
    return own + quoted_records(meta)


async def memories_of(store, identity) -> list[str]:
    """The memories (archive included) resting on this identity or on a run containing
    it (`names_identity`): through their `sources`, or through a quoted line naming it by
    string form — a memory quoting a line rests on that line as much as one listing it.
    Found by scanning the library; the source side never stores this."""
    want = _identity(identity)
    registry = getattr(store, "sources", None)
    out: list[str] = []
    for b in await store.list_all(include_archive=True):
        meta = b.get("metadata") or {}
        for rec in basis_records(meta):
            try:
                if not isinstance(rec, dict):
                    continue
                have = record_id(rec)
            except KeyError:
                continue
            if names_identity(have, want, registry):
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


def bounded_key(key) -> Optional[str]:
    """A write key as it is kept: at most WRITE_KEY_MAX characters. A longer one (a long
    host name and turn) keeps its head and ends in a hash of the whole key, so the ordinal
    at its end still tells two writes of one turn apart."""
    import hashlib

    text = str(key).strip() if key else ""
    if len(text) <= WRITE_KEY_MAX:
        return text or None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{text[:WRITE_KEY_MAX - len(digest) - 1]}~{digest}"


@contextmanager
def write_key_scope(key: Optional[str]):
    """Run a block under a write key (the request layer sets it from the host's turn)."""
    key = bounded_key(key)
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
# Not a state a change records: a source a host said (while serving its original) was
# withdrawn or deleted, until an ordered change settles it (`SourceRegistry.hold`).
HELD = "held"
HOLD_WORDS = (WITHDRAWN, DELETED)
# The changes that settle a hold: the ones that say whether the source may be used.
_SETTLING = (WITHDRAWN, DELETED, "restored")
CHANGE_KINDS = ("withdrawn", "deleted", "use_changed", "unreadable", "restored", "revised")
APPLIED, DUPLICATE, CONFLICT, STALE, FORBIDDEN, UNKNOWN_SOURCE = (
    "applied", "duplicate", "conflict", "stale", "forbidden", "unknown_source")
OUTCOMES = (APPLIED, DUPLICATE, CONFLICT, STALE, FORBIDDEN, UNKNOWN_SOURCE)

SOURCES_DIR = "_sources"
CHANGES_FILE = "changes.jsonl"
WRITE_KEYS_FILE = "write_keys.jsonl"
LINE_ORDERS_FILE = "line_orders.jsonl"
HELD_FILE = "held.jsonl"
# The worse state wins when a run is judged by its lines.
_STATE_RANK = {ACTIVE: 0, UNREADABLE: 1, HELD: 2, WITHDRAWN: 3, DELETED: 4}
# Outcomes of registering a run's lines (`record_order`).
RECORDED, KNOWN = "recorded", "known"
# A conflict's reason (`order_conflict`): one delivery of a run given other lines.
MEMBERS_DIFFER = "members_differ"
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
    from .ledger_mirror import file_lease

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


def json_line(raw: bytes):
    """One line of a jsonl file as read in binary -> its value, or None for a blank line
    or one a crash tore (cut short, possibly inside a multi-byte character: decoded per
    line, so a torn line never stops the lines after it from being read)."""
    line = raw.strip()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _read_lines(path: Path) -> list[dict]:
    """Every whole JSON object line of a jsonl file, in order; torn lines skipped."""
    if not path.exists():
        return []
    out = []
    with path.open("rb") as f:
        for raw in f:
            row = json_line(raw)
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


def _opt_text(value) -> Optional[str]:
    """A revision or watermark as stored: text, or None for none."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return str(value).strip()


@dataclass(frozen=True)
class _Order:
    """One registration of the host's order of a container's lines."""
    ids: list
    pos: dict
    revision: Optional[str] = None       # the host's watermark for this delivery
    revisions: Optional[dict] = None     # {id: revision or None} as delivered; None = not said


class SourceRegistry:
    """The source registry and the write-key claims of one library (`<buckets>/_sources`).

    `store` is the BucketManager, read only for the reverse lookup (`memories_of`)."""

    def __init__(self, base_dir: str | os.PathLike, store=None):
        self.base_dir = str(base_dir)
        self.dir = Path(base_dir) / SOURCES_DIR
        self.changes_path = self.dir / CHANGES_FILE
        self.keys_path = self.dir / WRITE_KEYS_FILE
        self.orders_path = self.dir / LINE_ORDERS_FILE
        self.held_path = self.dir / HELD_FILE
        self.store = store
        self._guard = threading.RLock()
        self._changes_size = -1
        self._keys_size = -1
        self._orders_size = -1
        self._held_size = -1
        self._seq = 0
        self._by_source: dict[str, dict] = {}
        self._by_change: dict[str, dict] = {}
        self._claims: dict[str, dict] = {}
        # (system, instance, container) -> [_Order], newest first
        self._orders: dict[tuple, list] = {}
        # line id -> the containers whose registrations hold it (`lines_named`)
        self._named: dict[str, set] = {}
        # source key -> the newest hold row
        self._held: dict[str, dict] = {}
        # (system, instance, container) -> the keys of runs a change was recorded for
        self._runs: dict[tuple, set] = {}
        # `blocking_containers`, and the file sizes it was read at
        self._blocking: frozenset = frozenset()
        self._blocking_at: Optional[tuple] = None

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
            self._runs = {}
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
        if _RANGE_MARK in source:
            try:
                sid = SourceId.parse(source)[0]
            except SourceRecordError:
                sid = None
            if sid is not None and sid.through:
                self._runs.setdefault((sid.system, sid.instance, sid.container),
                                      set()).add(source)
        entry = self._by_source.setdefault(source, {
            "state": ACTIVE, "host_seq": -1, "use": None, "use_changed": False,
            "revisions": [], "seqs": {}, "settled_seq": 0})
        entry["seqs"][host_seq] = key
        entry["host_seq"] = max(entry["host_seq"], host_seq)
        entry["state"] = str(row.get("state") or entry["state"])
        if row.get("kind") in _SETTLING:
            entry["settled_seq"] = max(entry["settled_seq"], seq)
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
            named: dict[str, set] = {}
            for row in _read_lines(self.orders_path):
                try:
                    where = (str(row["system"]), str(row["instance"]), str(row["container"]))
                    ids = [str(i) for i in row["ids"]]
                except (KeyError, TypeError):
                    continue
                revs = row.get("revisions")
                for i in ids:
                    named.setdefault(i, set()).add(where)
                if ids:
                    orders.setdefault(where, []).insert(0, _Order(
                        ids, {i: n for n, i in enumerate(ids)},
                        _opt_text(row.get("revision")),
                        ({str(k): _opt_text(v) for k, v in revs.items()}
                         if isinstance(revs, dict) else None)))
            self._orders = orders
            self._named = named
            self._orders_size = size

    def order_conflict(self, source: dict, ids: list[str], revision: Optional[str] = None,
                       revisions: Optional[dict] = None) -> str:
        """Why registering these lines would contradict what is registered, or "". A
        watermark (`revision`) names one delivery of a container. One delivery of a run —
        the same first and last line under the same watermark, or under no watermark on
        both sides — holds one list of lines: a registration giving it another list (other
        lines, or the same in another order) is MEMBERS_DIFFER. A registration with no
        watermark is independent of every watermarked one. A second registration under
        the same watermark carrying per-line revisions may not give a line another
        revision."""
        ids = [str(i) for i in ids if str(i or "").strip()]
        revision = _opt_text(revision)
        where = (str(source.get("system")), str(source.get("instance")),
                 str(source.get("container")))
        self._fresh_orders()
        with self._guard:
            for known in self._orders.get(where, []) if ids else []:
                if (known.revision == revision and known.ids[0] == ids[0]
                        and known.ids[-1] == ids[-1] and known.ids != ids):
                    return MEMBERS_DIFFER
            if revision is None or revisions is None:
                return ""
            for known in self._orders.get(where, []):
                if known.revision != revision or known.revisions is None:
                    continue
                for line_id in ids:
                    if (line_id in known.revisions and line_id in revisions
                            and known.revisions[line_id] != revisions[line_id]):
                        return (f"watermark {revision} already gives {line_id} the revision "
                                f"{known.revisions[line_id]!r}, not {revisions[line_id]!r}")
        return ""

    def record_order(self, source: dict, ids: list[str], batch_id: str = "", *,
                     revision: Optional[str] = None,
                     revisions: Optional[dict] = None) -> str:
        """Keep the host's order of a stretch of lines — a batch handed over for slicing,
        or a run registered on its own (POST /api/v2/source/lines): ids only, never their
        text, kept after the batch itself is gone. It is how a run `first..last` is known
        to hold the lines between. `revision` is the host's watermark for this delivery
        (a run record naming it as its revision adopted these lines' revisions);
        `revisions` the revision of each line as delivered ({id: revision or None}; a line
        left out is unknown). Returns RECORDED, KNOWN (the same registration was there) or
        a conflict's reason (`order_conflict`: MEMBERS_DIFFER for one delivery of a run
        given other lines, a sentence for a line given another revision; nothing is
        recorded)."""
        ids = [str(i) for i in ids if str(i or "").strip()]
        if not ids:
            return KNOWN
        revision = _opt_text(revision)
        if revisions is not None:
            wanted = set(ids)
            revisions = {str(k): _opt_text(v) for k, v in revisions.items() if str(k) in wanted}
        where = (str(source.get("system")), str(source.get("instance")),
                 str(source.get("container")))
        from .ledger_mirror import file_lease

        # The check and the append are one turn across threads and processes: two
        # registrations racing would otherwise both pass the check and both land, giving
        # a line two revisions under one watermark.
        with file_lease(self.dir / (LINE_ORDERS_FILE + ".register.lock")):
            self._fresh_orders()
            with self._guard:
                why = self.order_conflict(source, ids, revision, revisions)
                if why:
                    return why
                for known in self._orders.get(where, []):
                    if (known.ids == ids and known.revision == revision
                            and (revisions is None or known.revisions == revisions)):
                        return KNOWN
                row = {"system": where[0], "instance": where[1], "container": where[2],
                       "ids": ids, "batch": str(batch_id or ""), "recorded_at": _now()}
                if revision is not None:
                    row["revision"] = revision
                if revisions is not None:
                    row["revisions"] = revisions
                _append_line(self.orders_path, row)
                self._orders_size = -1
                return RECORDED

    def _orders_holding(self, sid: SourceId) -> list:
        """[(registration, first position, last position)] of the run's container that
        hold both its ends in order, newest first."""
        self._fresh_orders()
        with self._guard:
            out = []
            for known in self._orders.get((sid.system, sid.instance, sid.container), []):
                a, z = known.pos.get(sid.id), known.pos.get(sid.through)
                if a is not None and z is not None and a <= z:
                    out.append((known, a, z))
            return out

    def lines_named(self, line_id: str) -> list[SourceId]:
        """Every registered line carrying this host id, one per container whose line orders
        hold it, sorted: what a bare id (`m_0142`) can be completed with. An id no
        registration holds gives []."""
        self._fresh_orders()
        with self._guard:
            where = sorted(self._named.get(str(line_id or ""), ()))
        return [SourceId(sy, inst, cont, str(line_id)) for sy, inst, cont in where]

    def members_of(self, identity) -> Optional[list[str]]:
        """The ids a run holds, when a registration of its container holds both its ends
        in order; None when none does (or for a single piece). Registrations that disagree
        are joined, the oldest's order first: every line any of them put inside the run
        counts, so a withdrawal of any of them reaches it and no registration can take a
        line out again."""
        sid = _identity(identity)
        if not sid.through:
            return None
        holding = self._orders_holding(sid)
        if not holding:
            return None
        out: list[str] = []
        seen: set[str] = set()
        for known, a, z in reversed(holding):
            for i in known.ids[a:z + 1]:
                if i not in seen:
                    seen.add(i)
                    out.append(i)
        return out

    def adopted_revisions(self, record) -> Optional[dict]:
        """The revision of each line a run record adopted: {line id: revision or None},
        from the registrations of its container holding the run under the watermark the
        record names as its `revision`. None when the record names none, or no such
        registration carries per-line revisions; a line missing from the dict is unknown."""
        sid = record_id(record) if isinstance(record, dict) else _identity(record)
        watermark = record.get("revision") if isinstance(record, dict) else None
        if not sid.through or watermark in (None, ""):
            return None
        found: dict = {}
        hit = False
        for known, a, z in self._orders_holding(sid):
            if known.revision != str(watermark) or known.revisions is None:
                continue
            hit = True
            for line_id in known.ids[a:z + 1]:
                if line_id in known.revisions:
                    found.setdefault(line_id, known.revisions[line_id])
        return found if hit else None

    def run_revisions(self, record) -> list[tuple]:
        """[(line identity, the host's newest announced revision of it, the revision the
        record adopted or None)] for every line of a run (`lines_of`) whose newest
        announced revision is not the one adopted (`adopted_revisions`). The newest is
        the newest reaching the line (`revisions_reaching`): its own, or one announced for
        a registered run holding it. A line whose adopted revision is unknown counts
        whenever the host has announced one for it: when the memory was written proves
        nothing about which version the model read. A run's announcement is compared by
        `revision` alone, against the line's adopted revision, else the record's
        watermark (the delivery it was cut from) — the way a batch's line is compared at
        the door (core/_slicer._behind_lines)."""
        sid = record_id(record) if isinstance(record, dict) else _identity(record)
        adopted = self.adopted_revisions(record) or {}
        watermark = record.get("revision") if isinstance(record, dict) else None
        out = []
        for line in self.lines_of(sid):
            revisions = self.revisions_reaching(line)
            if not revisions:
                continue
            latest = revisions[-1]
            mine = adopted.get(line.id)
            if latest["source"] == line.to_string():
                newer = str(latest.get("revision") or latest.get("fingerprint") or "")
            else:
                newer = str(latest.get("revision") or "")
                if mine in (None, ""):
                    mine = watermark
            if not newer:
                continue
            if mine not in (None, "") and str(mine) == newer:
                continue
            out.append((line, newer, mine if mine not in (None, "") else None))
        return out

    # ---------- holds: a host's word, while serving an original, awaiting its change ----------

    def _fresh_held(self) -> None:
        with self._guard:
            size = _size(self.held_path)
            if size == self._held_size:
                return
            held: dict[str, dict] = {}
            for row in _read_lines(self.held_path):
                if row.get("source") and row.get("said") in HOLD_WORDS:
                    held[str(row["source"])] = row
            self._held = held
            self._held_size = size

    def _settled_since(self, key: str, after: int) -> bool:
        """Has a settling change (withdrawn, deleted, restored) for exactly this key been
        applied after the registry's line `after`?"""
        entry = self._entry(key)
        with self._guard:
            return entry is not None and entry["settled_seq"] > after

    def _line_settled_since(self, line: SourceId, after: int) -> bool:
        """Has a settling change covering this line of a held run been applied after the
        registry's line `after`: one for the line itself, or for any run of its container
        whose registered lines hold it? A run whose lines are unknown covers no line."""
        if self._settled_since(line.to_string(), after):
            return True
        return any(self._settled_since(key, after) for key in self._runs_over(line, [line]))

    def held_of(self, identity) -> Optional[dict]:
        """The open hold on exactly this identity, or None. A hold is open until a change
        that settles whether the source may be used (withdrawn, deleted, restored) is
        applied after it to the same identity. A held run whose lines are registered is
        settled as well once every line it holds has had such a change after the hold —
        a change for the line, or for a run whose registered lines hold it
        (`_line_settled_since`); a change covering some of its lines settles the hold for
        those lines only (`held_over`) and leaves it open. A held run whose lines are
        unknown is settled only by its own change."""
        sid = _identity(identity)
        key = sid.to_string()
        self._fresh_held()
        with self._guard:
            row = self._held.get(key)
        if row is None:
            return None
        after = int(row.get("after_seq") or 0)
        if self._settled_since(key, after):
            return None
        members = self.members_of(sid) if sid.through else None
        if members and all(self._line_settled_since(sid.piece(m), after) for m in members):
            return None
        return dict(row)

    def held_over(self, identity) -> list[str]:
        """The keys of the open holds that cover this source: a hold on exactly it
        (`held_of`); for a run, a hold on any line it holds (`lines_of`); and a hold on
        another run of its container that still covers one of its lines
        (`_held_runs_over`). Empty when no open hold covers it."""
        sid = _identity(identity)
        return self._holds_over(sid, self.lines_of(sid))

    def _holds_over(self, sid: SourceId, lines: list) -> list[str]:
        keys = [sid.to_string()] + ([x.to_string() for x in lines] if sid.through else [])
        out = [k for k in dict.fromkeys(keys) if self.held_of(k) is not None]
        return out + [k for k in self._held_runs_over(sid, lines) if k not in out]

    def hold(self, identity, said: str, host: str = "") -> dict:
        """A host said, while serving the original, that this source is `said` (withdrawn
        or deleted) and Loci's registry does not record it: hold it, so nothing standing on
        it is used until the ordered change arrives. The registry's state is not touched —
        an answer to a fetch is not ordered. Returns the open hold row (the one already
        open, if any): {source, said, host, after_seq, recorded_at}."""
        if said not in HOLD_WORDS:
            raise ValueError(f"a hold says withdrawn or deleted, got {said!r}")
        key = _identity_key(identity)
        self._fresh()
        with self._guard:
            open_row = self.held_of(key)
            if open_row is not None:
                return open_row
            row = {"source": key, "said": said, "host": str(host or ""),
                   "after_seq": self._seq, "recorded_at": _now()}
            _append_line(self.held_path, row)
            self._held_size = -1
            return dict(row)

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

    def settled_after(self, identity, seq) -> bool:
        """Has a change that settles whether this exact source may be used (withdrawn,
        deleted, restored) been applied after the registry's line `seq`? A resend of the
        older change then finishes only what it had already done (core/_source_change.py)."""
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return False
        entry = self._entry(_identity_key(identity))
        with self._guard:
            return entry is not None and entry["settled_seq"] > seq

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

    def _runs_over(self, sid: SourceId, lines: list) -> list[str]:
        """The keys of runs of the same container a change was recorded for (other than
        `sid` itself) whose registered lines hold one of `lines`."""
        self._fresh()
        with self._guard:
            runs = sorted(self._runs.get((sid.system, sid.instance, sid.container), ()))
        own = sid.to_string()
        ids = {x.id for x in lines}
        out = []
        for key in runs:
            if key != own and ids & set(self.members_of(key) or ()):
                out.append(key)
        return out

    def _held_runs_over(self, sid: SourceId, lines: list) -> list[str]:
        """The keys of open holds on runs of the same container (other than `sid` itself)
        that still cover one of `lines`: a hold on a run reaches its registered lines the
        way a change to it does (`_runs_over`), except a line a settling change covered
        after the hold (`_line_settled_since`: the line's own, or a registered run's over
        it) — that change settled the hold for that line."""
        self._fresh_held()
        prefix = f"{sid.system}:{sid.instance}/{sid.container}#"
        with self._guard:
            runs = sorted(k for k in self._held if _RANGE_MARK in k and k.startswith(prefix))
        own = sid.to_string()
        ids = {x.id for x in lines}
        out = []
        for key in runs:
            covered = ids & set(self.members_of(key) or ()) if key != own else set()
            if not covered:
                continue
            row = self.held_of(key)
            if row is None:
                continue
            after = int(row.get("after_seq") or 0)
            if any(not self._line_settled_since(sid.piece(i), after) for i in covered):
                out.append(key)
        return out

    def state_of(self, identity) -> str:
        """The source's state; one the registry has never heard of is active. A run is as
        bad as the worst of its own identity and every line it holds (`lines_of`): a line
        withdrawn inside it withdraws the whole run. A run a change was recorded for
        reaches every line it holds: a piece or run holding one of them is no better than
        that run (`_runs_over`). An open hold covering the source (`held_over`: on it, on
        a line of it, or on a run over one of its lines not yet settled on its own) reads
        as HELD where the recorded state is no worse."""
        sid = _identity(identity)
        lines = self.lines_of(sid)
        keys = [sid.to_string()] + ([x.to_string() for x in lines] if sid.through else [])
        keys += self._runs_over(sid, lines)
        worst = ACTIVE
        for key in dict.fromkeys(keys):
            entry = self._entry(key)
            state = entry["state"] if entry is not None else ACTIVE
            if _STATE_RANK.get(state, 0) > _STATE_RANK[worst]:
                worst = state
        if _STATE_RANK[worst] < _STATE_RANK[HELD] and self._holds_over(sid, lines):
            worst = HELD
        return worst

    def read_state(self, record) -> str:
        """The state the read gate judges a memory's source record by (`state_of`: a run
        by every line inside it)."""
        return self.state_of(record_id(record) if isinstance(record, dict) else record)

    def stamp(self) -> tuple:
        """The sizes of the files the read gate's answers come from (changes, holds, the
        registered line orders): anything kept from reading against the registry is stale
        once this changes. The files are only ever appended to."""
        return (_size(self.changes_path), _size(self.held_path), _size(self.orders_path))

    def blocking_containers(self) -> frozenset:
        """The containers, as (system, instance, container), holding a source recorded
        withdrawn or deleted or named by a hold. `state_of` judges a source only by keys
        of its own container, so a source outside these reads as neither withdrawn,
        deleted nor held, and the read gate need not ask about it (core/scope.ScopeView.
        source_blocked). A hold already settled still counts here: the set only narrows
        what is asked."""
        self._fresh()
        self._fresh_held()
        with self._guard:
            at = (self._changes_size, self._held_size)
            if at == self._blocking_at:
                return self._blocking
            keys = [k for k, e in self._by_source.items() if e["state"] in (WITHDRAWN, DELETED)]
            keys += list(self._held)
            out = set()
            for key in keys:
                try:
                    sid = SourceId.parse(key)[0]
                except SourceRecordError:
                    continue
                out.add((sid.system, sid.instance, sid.container))
            self._blocking = frozenset(out)
            self._blocking_at = at
            return self._blocking

    def use_of(self, record: dict):
        """The `use` in force for a source record as a whole: the host's latest
        `use_changed` for its identity when there was one, else the record's own."""
        found = self.describe(record_id(record))
        if found and found.get("use_changed"):
            return found.get("use")
        return record.get("use")

    def granted(self, places, identity) -> bool:
        """Do these places grant this source for reading or as a basis (`places_cover`,
        with the run's registered lines; a run whose lines are not registered is never
        granted)?"""
        sid = _identity(identity)
        return places_cover(places_of(places) or (), sid,
                            self.members_of(sid) if sid.through else None)

    def reaches(self, places, identity) -> bool:
        """Does a host's ceiling (`max_grant`) reach this source? Where a source lies, not
        whether it may be read: a run lies within its container whether or not its lines
        are registered, and within places naming each of its lines when they are."""
        sid = _identity(identity)
        places = places_of(places) or ()
        if any(p.covers(sid) for p in places):
            return True
        members = self.members_of(sid) if sid.through else None
        return bool(members) and all(any(p.covers(sid.piece(m)) for p in places)
                                     for m in members)

    def order_known(self, identity) -> bool:
        """A single piece, or a run whose lines are registered."""
        sid = _identity(identity)
        return not sid.through or self.members_of(sid) is not None

    def uses_of(self, record: dict) -> list:
        """Every `use` a record has to satisfy: its own (`use_of`); for a run, the
        `use_changed` of each line inside it — a line narrowed narrows the run; and the
        `use_changed` of every run a change was recorded for that holds one of its lines
        (`_runs_over`) — a run narrowed narrows its lines and the runs overlapping them, the
        way `state_of` reaches them. An absent use is left out (it is no rule of its own)."""
        out = [self.use_of(record)]
        sid = record_id(record)
        lines = self.lines_of(sid)
        keys = ([x.to_string() for x in lines] if sid.through else [])
        keys += self._runs_over(sid, lines)
        own = sid.to_string()
        for key in dict.fromkeys(keys):
            if key == own:
                continue
            entry = self._entry(key)
            if entry is not None and entry["use_changed"]:
                out.append(entry["use"])
        return [u for u in out if u is not None]

    def revisions_of(self, identity) -> list[dict]:
        """The revisions the host announced (`revised`), oldest first by host_seq."""
        found = self.describe(identity)
        return found["revisions"] if found else []

    def revisions_reaching(self, identity) -> list[dict]:
        """The revisions the host announced (`revised`) that reach a line: those for the
        line itself and those for every run of its container a change was recorded for
        whose registered lines hold it (`_runs_over`, the reach a withdrawal on a run
        has). Each carries `source`, the key it was announced for. Oldest first by the
        registry's `seq`, the order the changes were applied: host_seq is one order per
        source, so a line's and a run's do not compare, while within one source a change
        older by host_seq is refused as stale and `seq` follows host_seq. A run's
        `revision` names the version of the whole run; its fingerprint hashes the whole
        run and says nothing of one line's text. A run given as `identity` reaches only
        its own key and the runs overlapping its lines, not its lines' own revisions
        (`run_revisions` reads those)."""
        sid = _identity(identity)
        own = sid.to_string()
        out: list[dict] = []
        for key in dict.fromkeys([own] + self._runs_over(sid, self.lines_of(sid))):
            entry = self._entry(key)
            if entry is None:
                continue
            with self._guard:
                out += [{**r, "source": key} for r in entry["revisions"]]
        return sorted(out, key=lambda r: int(r.get("seq") or 0))

    def newer_revision(self, record: dict) -> tuple[str, Optional[str]]:
        """For a record of one line: (the newest revision reaching it
        (`revisions_reaching`) when the record does not hold it, else "", what the record
        holds). Compared by `revision` when the newest names one, else by fingerprint —
        an announcement for a run only by `revision`: its fingerprint hashes the whole
        run. A record holding neither counts as behind any announcement it is compared
        with."""
        sid = record_id(record)
        revisions = self.revisions_reaching(sid)
        latest = revisions[-1] if revisions else {}
        if latest.get("revision"):
            newer, mine = str(latest["revision"]), record.get("revision")
        elif latest.get("fingerprint") and latest["source"] == sid.to_string():
            newer, mine = str(latest["fingerprint"]), record.get("fingerprint")
        else:
            return "", None
        if mine not in (None, "") and str(mine) == newer:
            return "", mine
        return newer, mine

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
        step acts on. `host` names the sender, whose change_id it is (two hosts may use
        one id). host_seq is one order per source, whoever sent the change: a change
        older than the newest applied one for the source is stale, and one reusing a
        host_seq already applied for it is a conflict. Who may send changes for a source
        at all (its declared authority) is the caller's check (core/_source_change.py).
        A malformed record raises ValueError."""
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
            base = {"change_id": change["change_id"], "source": source, "kind": change["kind"],
                    "state": current, "previous": current,
                    "host_seq": entry["host_seq"] if entry else None,
                    "applied_seq": None, "memories": list(memories or [])}
            prior = self._by_change.get(self.change_key(host, change["change_id"]))
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
        withdrawn or deleted, nor held. An unreadable source is taken (the host just handed
        its text over) with a note; one the registry has never seen is simply active. A run
        has to have its lines registered (a withdrawal in its middle could not be told
        otherwise, so it is refused under any grant and with none), is granted only when
        every line in it is (`granted`), and its state is the worst of its lines
        (`state_of`). A source whose newest announced revision is not the one the record
        adopted (`run_revisions` for a run, `newer_revision` for a piece) is taken
        with a note: the memory comes up in 依据变了的 at once. Writing never changes a
        source's state: only the host's change notices do."""
        granted = places_of(grant)
        notes: list[str] = []
        for rec in sources or []:
            sid = record_id(rec)
            key = sid.to_string()
            order_known = self.order_known(sid)
            # Outside the grant there is one answer, whatever the registry knows of the
            # source (its state, whether a run's lines were registered): telling them
            # apart would say something of material the turn may not touch. A run whose
            # lines are unknown is not granted by anything, so it is told as such only
            # where it lies inside the grant's places.
            if granted is not None and not self.granted(granted, sid) and (
                    order_known or not self.reaches(granted, sid)):
                return (f"来源 {key} 不在这一轮宿主交过来的材料里——只能用这一轮给的来源写。"
                        "本次什么都没写。"), []
            if not order_known:
                return (f"来源 {key} 是连着的一段，宿主没交过这段里有哪几行，Loci 没法知道中间哪一行"
                        "被撤回——不能拿它当依据。宿主先交这段的行（POST /api/v2/source/lines，"
                        "或走 /api/v2/slices），或者引单条。本次什么都没写。"), []
            state = self.state_of(sid)
            if state == WITHDRAWN:
                return (f"来源 {key} 已经被撤回了，不能再拿它写记忆。本次什么都没写。"), []
            if state == DELETED:
                return (f"来源 {key} 已经被删除了，不能再拿它写记忆。本次什么都没写。"), []
            if state == HELD:
                return (f"来源 {key} 宿主给原文时说它已经撤回或删除了，正等宿主的变化通知确认；"
                        "这段时间不能拿它写记忆。本次什么都没写。"), []
            if state == UNREADABLE:
                notes.append(f"来源 {key} 宿主那边眼下读不到；这次的原文是刚交过来的，照收。")
            notes += self._revision_notes(rec, sid)
        return "", notes

    def _revision_notes(self, rec: dict, sid: SourceId) -> list[str]:
        if sid.through:
            changed = self.run_revisions(rec)
        else:
            newer, mine = self.newer_revision(rec)
            changed = [(sid, newer, mine or None)] if newer else []
        out = []
        for line, newer, mine in changed:
            had = f"你依据的是 {mine}" if mine else "依据的是哪一版宿主没说"
            out.append(f"来源 {line} 宿主已经出了新版本 {newer}（{had}）——照写，"
                       "这条会挂在「依据变了的」里待确认。")
        return out

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

        key = bounded_key(key)
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
