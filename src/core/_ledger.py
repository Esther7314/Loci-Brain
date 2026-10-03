"""
========================================
core/_ledger.py — what a ledger line may hold, and the read-only `/changes` face
========================================

The ledger (`<buckets>/_ledger/events.jsonl`, locibrain/eventsourcing/ledger_mirror.py)
gets one line per memory mutation and, since source changes reached Loci, one line per
source change and per place a change cleared. Each line is numbered by `seq`; a host
reconciles against those numbers (`applied_seq` in a change's receipt, `since=` here).

A line holds no text of the memory. What it may hold:

    every line   seq · event_type · trace_id · trace_kind · body_hash · recorded_at
    a memory     payload {fields: the frontmatter keys present, changed_fields: the keys a
                 write touched, flags: the four booleans the footprint reads (dont_surface,
                 pinned, anchor, resolved), sources: the identities of its source records}
    a source     payload {source, change, change_id, host, host_seq, state, previous}
    a clearing   payload {source, change_id, place, entries}

`payload_of` builds the memory payload from a write's metadata; names, summaries, tags,
aliases, `when`, meanings and every other value stay out. Lines written before this rule
carry the whole metadata; `public_row` reads any line, old or new, down to the shape
above, so `/changes` never hands out text whichever kind of line it reads. The old lines
themselves are rewritten only for memories a withdrawn or deleted source clears
(`scrub_traces`, through the ledger's atomic rewrite), never wholesale.

`changes_since` is `GET /api/v2/changes`: the lines past a point, in order, without "was
merely touched" (TraceTouched), and only those about sources the calling host's
credential (`max_grant`) reaches. A memory line counts as about every source standing
behind the memory — its own records and, for anything derived, every root's (the same
walk as the read gate, core/scope.ScopeView, judging only where each source lies); a
memory with none, or with one past the credential, is not shown to a host with a ceiling,
and its id is left out of the `entries` a source line lists (`visible_ids` says the same
for a change's receipt).

Where a host reads from depends on whether it has a ceiling:

    open host (no max_grant)   `?since=N`: the ledger's own numbers; every line is shown,
                               rows carry `seq`, `next` is a number
    host with a max_grant      `?cursor=<opaque>`: rows carry `cursor` instead of `seq`,
                               `next` is a cursor. The ledger's numbers never reach it:
                               their gaps would count what it may not see.

A cursor (`cursor_of`) is the seq of the last line looked at, put through a keyed
permutation (a six-round Feistel network over 64 bits, HMAC-SHA256 rounds) under a key
kept beside the ledger (`_ledger/cursor.key`, made on first use), plus a tag binding it to
the host. Two cursors say nothing of how far apart they are; a page whose every line was
filtered out still hands back a new `next`, so the reader moves on. The source change
receipt gives such a host `applied_cursor` in place of `applied_seq`, the cursor of the
change's own line.

Exports: SOURCE_CHANGED · SOURCE_CLEARED · TRACE_CLEARED · NOISE · CursorError · payload_of ·
         source_keys · public_row · scrub_traces · cursor_of · seq_of_cursor · host_view ·
         visible_ids · changes_since
========================================
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from utils import parse_bool

from . import _sources as _src
from . import scope as _scope

SOURCE_CHANGED = "SourceChanged"
SOURCE_CLEARED = "SourceCleared"
TRACE_CLEARED = "TraceCleared"
NOISE = frozenset({"TraceTouched"})
SOURCE_EVENTS = frozenset({SOURCE_CHANGED, SOURCE_CLEARED})

FLAG_FIELDS = ("dont_surface", "pinned", "anchor", "resolved")
# Keys a caller's extra payload may add, all of them names or booleans.
_EXTRA_KEYS = ("changed_fields", "content_erased", "cleared", "change_id", "source")
_SOURCE_KEYS = ("source", "change", "change_id", "host", "host_seq", "state", "previous",
                "place", "entries", "note")

CHANGES_LIMIT = 1000
CHANGES_LIMIT_MAX = 5000


def source_keys(meta) -> list[str]:
    """The identities (string form, no revision) of a memory's source records."""
    out: list[str] = []
    for rec in (meta or {}).get(_src.SOURCES_FIELD) or []:
        if isinstance(rec, str):
            try:
                out.append(_src.SourceId.parse(rec)[0].to_string())
            except _src.SourceRecordError:
                continue
            continue
        if not isinstance(rec, dict):
            continue
        try:
            out.append(_src.record_id(rec).to_string())
        except (KeyError, TypeError):
            continue
    return list(dict.fromkeys(out))


def payload_of(metadata: Optional[dict], extra: Optional[dict] = None) -> dict:
    """A memory line's payload: names, flags and source identities, never a value of
    text."""
    meta = metadata or {}
    extra = extra or {}
    out: dict = {"fields": sorted(str(k) for k in meta)}
    flags = {k: parse_bool(meta[k], default=False) for k in FLAG_FIELDS if k in meta}
    if flags:
        out["flags"] = flags
    sources = source_keys(meta)
    if sources:
        out["sources"] = sources
    for k in _EXTRA_KEYS:
        if k not in extra:
            continue
        if k == "changed_fields":
            out[k] = sorted(str(x) for x in extra[k] or [])
        elif k in ("content_erased", "cleared"):
            out[k] = bool(extra[k])
        else:
            out[k] = str(extra[k])
    return out


def _is_safe(payload: dict) -> bool:
    allowed = {"fields", "flags", "sources", "redacted", *_EXTRA_KEYS}
    return set(payload) <= allowed


def public_row(event: dict) -> dict:
    """One ledger line as `/changes` hands it out: numbers, kinds, identities and hashes.
    Works on lines of any age; text in an old line's payload does not come through."""
    etype = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    row: dict = {"seq": int(event.get("seq") or 0), "type": etype,
                 "recorded_at": str(event.get("recorded_at") or "")}
    if etype in SOURCE_EVENTS:
        for k in ("source", "change", "change_id", "state", "previous", "place"):
            if payload.get(k) not in (None, ""):
                row[k] = str(payload[k])
        if isinstance(payload.get("entries"), list):
            row["entries"] = [str(x) for x in payload["entries"]]
        return row
    row["id"] = str(event.get("trace_id") or "")
    row["kind"] = str(event.get("trace_kind") or "")
    if event.get("body_hash"):
        row["body_hash"] = str(event["body_hash"])
    changed = payload.get("changed_fields")
    if isinstance(changed, list):
        row["changed_fields"] = sorted(str(x) for x in changed)
    sources = source_keys({_src.SOURCES_FIELD: payload.get("sources")})
    if sources:
        row["sources"] = sources
    return row


def scrub_traces(ledger, ids: Iterable[str]) -> int:
    """Rewrite the lines about these memories that still carry whole metadata, keeping
    only what `payload_of` keeps. Seq numbers and order stay. Returns how many lines
    changed."""
    wanted = {str(i) for i in ids if i}
    if not wanted:
        return 0

    def redact(event: dict) -> Optional[dict]:
        if str(event.get("trace_id") or "") not in wanted:
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict) or _is_safe(payload):
            return None
        safe = payload_of(payload, payload)
        safe["redacted"] = True
        return {**event, "payload": safe}

    return ledger.rewrite(redact)


# ------------------------------------------------------------
# A host's cursor
# ------------------------------------------------------------

CURSOR_KEY_FILE = "cursor.key"
_CURSOR_PREFIX = "c1"
_ROUNDS = 6
_MASK32 = 0xFFFFFFFF


class CursorError(ValueError):
    """A cursor this library did not hand to this host."""


_KEY_BYTES = 32


def _cursor_key(ledger) -> bytes:
    """The library's cursor key, made on first use. It is made under a lease beside it and
    written whole into a temporary file that is then renamed into place, so no reader ever
    sees half a key, and two processes racing to make it end up with the same one. A key
    file shorter than a key (an older crash between creating and writing it) never signed
    a cursor that verifies: it is made again."""
    from locibrain.eventsourcing.ledger_mirror import file_lease

    path = Path(ledger.path).with_name(CURSOR_KEY_FILE)
    try:
        data = path.read_bytes()
        if len(data) >= _KEY_BYTES:
            return data[:_KEY_BYTES]
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lease(path.with_name(CURSOR_KEY_FILE + ".lock")):
        try:
            data = path.read_bytes()
            if len(data) >= _KEY_BYTES:
                return data[:_KEY_BYTES]
        except OSError:
            pass
        tmp = path.with_name(f"{CURSOR_KEY_FILE}.{os.getpid()}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(_KEY_BYTES))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        data = path.read_bytes()
    if len(data) < _KEY_BYTES:
        raise CursorError("the cursor key is unreadable")
    return data[:_KEY_BYTES]


def _round(key: bytes, i: int, half: int) -> int:
    digest = hmac.new(key, bytes([i]) + half.to_bytes(4, "big"), hashlib.sha256).digest()
    return int.from_bytes(digest[:4], "big")


def _permute(key: bytes, value: int, back: bool = False) -> int:
    left, right = (value >> 32) & _MASK32, value & _MASK32
    if not back:
        for i in range(_ROUNDS):
            left, right = right, left ^ _round(key, i, right)
    else:
        for i in reversed(range(_ROUNDS)):
            left, right = right ^ _round(key, i, left), left
    return (left << 32) | right


def _tag(key: bytes, host_name: str, block: int) -> str:
    msg = f"cursor|{host_name}|".encode("utf-8") + block.to_bytes(8, "big")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]


def cursor_of(ledger, seq: int, host_name: str) -> str:
    """The opaque cursor standing for ledger seq `seq`, for `host_name`."""
    key = _cursor_key(ledger)
    block = _permute(key, int(seq) & ((1 << 64) - 1))
    return f"{_CURSOR_PREFIX}{block:016x}{_tag(key, host_name, block)}"


def seq_of_cursor(ledger, cursor: str, host_name: str) -> int:
    """The ledger seq a cursor stands for. Raises CursorError for one this library did not
    hand to this host."""
    text = str(cursor or "").strip()
    if len(text) != len(_CURSOR_PREFIX) + 32 or not text.startswith(_CURSOR_PREFIX):
        raise CursorError("not a cursor this library handed out")
    try:
        block = int(text[len(_CURSOR_PREFIX):len(_CURSOR_PREFIX) + 16], 16)
    except ValueError:
        raise CursorError("not a cursor this library handed out") from None
    key = _cursor_key(ledger)
    if not hmac.compare_digest(text[-16:], _tag(key, host_name, block)):
        raise CursorError("not a cursor this library handed to this host")
    return _permute(key, block, back=True)


def host_view(host) -> bool:
    """Does this host read the ledger through cursors (a host with a ceiling)?"""
    return host is not None and getattr(host, "max_grant", None) is not None


class _CoverageView(_scope.ScopeView):
    """The read gate's walk to the roots, judging each record by where it lies against
    the credential's ceiling alone: a host may reconcile what its material became whatever
    the material's state or rule, and whether its lines were registered."""

    def _judge_record(self, rec: dict, sid) -> bool:
        return self.registry.reaches(self.request.scope.grant, sid)


def _coverage_view(host, metas: dict, registry) -> Optional[_CoverageView]:
    if host is None or host.max_grant is None:
        return None
    scope = _scope.Scope(entry=(host.name, None, None), venue="", audience=frozenset(),
                         grant=tuple(host.max_grant))
    req = _scope.RequestScope(host, _scope.RESTRICTED, scope=scope)
    return _CoverageView(req, metas, registry)


async def _library_metas(store) -> dict:
    metas: dict = {}
    for b in await store.list_all(include_archive=True):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if bid:
            metas[bid] = meta
    return metas


async def visible_ids(store, host, ids: Iterable[str]) -> set:
    """Which of these memory ids `host` may be told of: all of them for a host without a
    ceiling; for one with a ceiling, those whose every source lies within it (the walk
    `/changes` judges memory lines by). An id the library no longer has is not told."""
    ids = [str(i) for i in ids if i]
    if not host_view(host):
        return set(ids)
    metas = await _library_metas(store)
    view = _coverage_view(host, metas, store.sources)
    return {i for i in ids if view is not None and view.permits_id(i)}


def _covered(view: Optional[_CoverageView], places, key: str) -> bool:
    if view is None:
        return True
    try:
        sid = _src.SourceId.parse(key)[0]
    except _src.SourceRecordError:
        return False
    return view.registry.reaches(places, sid)


async def changes_since(store, host, since: int = 0, limit: int = CHANGES_LIMIT, *,
                        cursor: Optional[str] = None) -> dict:
    """`GET /api/v2/changes` for `host` (a core.scope.Host; None reads nothing).

        open host            {since, next, more, changes: [public_row, ...]}
        host with a ceiling  {cursor, next, more, changes: [public_row without seq, with
                              cursor, ...]} — read from `cursor` (None: from the start);
                              `since` is not taken. Raises CursorError for a cursor this
                              library did not hand to this host.

    `next` is where to ask from next time (past every line this call looked at, shown or
    not); `more` says lines are left past it. No total is given: under a ceiling a count
    of what else happened is itself a leak."""
    restricted = host_view(host)
    if restricted:
        since = seq_of_cursor(store.ledger_mirror, cursor, host.name) if cursor else 0
    limit = max(1, min(int(limit or CHANGES_LIMIT), CHANGES_LIMIT_MAX))
    metas: dict = {}
    view = None
    if host is not None and host.max_grant is not None:
        metas = await _library_metas(store)
        view = _coverage_view(host, metas, store.sources)
    places = tuple(host.max_grant) if view is not None else ()
    out: list[dict] = []
    nxt = since
    more = False
    ledger = store.ledger_mirror
    for event in ledger.iter_since(since):
        seq = int(event.get("seq") or 0)
        if len(out) >= limit:
            more = True
            break
        nxt = max(nxt, seq)
        if host is None:
            continue
        etype = str(event.get("event_type") or "")
        if etype in NOISE:
            continue
        row = public_row(event)
        if view is not None:
            if etype in SOURCE_EVENTS:
                if not _covered(view, places, row.get("source", "")):
                    continue
                if "entries" in row:
                    # A memory it may not reconcile is not named, even on its own source's line.
                    row["entries"] = [b for b in row["entries"] if view.permits_id(b)]
            else:
                bid = row.get("id", "")
                meta = metas.get(bid)
                if meta is not None:
                    if not view.permits(meta):
                        continue
                else:
                    keys = row.get("sources") or []
                    if not keys or not all(_covered(view, places, k) for k in keys):
                        continue
        out.append(row)
    if not restricted:
        return {"since": since, "next": nxt, "more": more, "changes": out}
    ledger = store.ledger_mirror
    for row in out:
        row["cursor"] = cursor_of(ledger, row.pop("seq"), host.name)
    return {"cursor": cursor or None, "next": cursor_of(ledger, nxt, host.name), "more": more,
            "changes": out}
