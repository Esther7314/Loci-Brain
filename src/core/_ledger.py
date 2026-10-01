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

`changes_since` is `GET /api/v2/changes?since=N`: the lines numbered above N, in order,
without "was merely touched" (TraceTouched), and only those about sources the calling
host's credential (`max_grant`) reaches. A memory line counts as about every source
standing behind the memory — its own records and, for anything derived, every root's
(the same walk as the read gate, core/scope.ScopeView); a memory with none, or with one
past the credential, is not shown to a host with a ceiling. A host without a ceiling (an
open host) sees every line.

Exports: SOURCE_CHANGED · SOURCE_CLEARED · TRACE_CLEARED · NOISE · payload_of ·
         source_keys · public_row · scrub_traces · changes_since
========================================
"""

from __future__ import annotations

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


class _CoverageView(_scope.ScopeView):
    """The read gate's walk to the roots, judging each record by the credential's
    ceiling alone: a host may reconcile what its material became whatever the material's
    state or rule."""

    def _judge_record(self, rec: dict, sid) -> bool:
        return self.registry.granted(self.request.scope.grant, sid)


def _coverage_view(host, metas: dict, registry) -> Optional[_CoverageView]:
    if host is None or host.max_grant is None:
        return None
    scope = _scope.Scope(entry=(host.name, None, None), venue="", audience=frozenset(),
                         grant=tuple(host.max_grant))
    req = _scope.RequestScope(host, _scope.RESTRICTED, scope=scope)
    return _CoverageView(req, metas, registry)


def _covered(view: Optional[_CoverageView], places, key: str) -> bool:
    if view is None:
        return True
    try:
        sid = _src.SourceId.parse(key)[0]
    except _src.SourceRecordError:
        return False
    return view.registry.granted(places, sid)


async def changes_since(store, host, since: int, limit: int = CHANGES_LIMIT) -> dict:
    """`GET /api/v2/changes?since=N` for `host` (a core.scope.Host; None reads nothing).

        {since, next, more, changes: [public_row, ...]}

    `next` is the seq to ask from next time (past every line this call looked at, shown
    or not); `more` says lines are left past it. No total is given: under a ceiling a
    count of what else happened is itself a leak."""
    limit = max(1, min(int(limit or CHANGES_LIMIT), CHANGES_LIMIT_MAX))
    metas: dict = {}
    view = None
    if host is not None and host.max_grant is not None:
        for b in await store.list_all(include_archive=True):
            meta = b.get("metadata") or {}
            bid = str(meta.get("id") or b.get("id") or "")
            if bid:
                metas[bid] = meta
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
    return {"since": since, "next": nxt, "more": more, "changes": out}
