"""
========================================
core/_source_change.py — a host's source change: the state, the block, the clearing
========================================

`POST /api/v2/source/change` (the contract's §三). The host says one piece of its
material changed; Loci records it in the source registry (core/_sources.py), blocks what
stood on it, clears what has to go, and answers with where every place stands:

    {"change_id": "c-7f3a9e", "source": {...} | "sys:inst/cont#id", "host_seq": 5012,
     "change": "withdrawn", "revision": null, "use": null}
 -> {"change_id": "c-7f3a9e", "status": "applied", "state": "withdrawn", "blocked": true,
     "applied_seq": 18422, "entries": ["3f9c1a2b7d40"], "derived_pending": ["a81e0c55d912"],
     "cleanup": {"body": "done", "embeddings": "done", "bm25": "done",
                 "dream_records": "none", "sunk_text": "done", "dehydration_cache": "none",
                 "media": "none", "slices": "none", "usage_log": "none", "ledger": "done"}}

What each kind of change does:

| change      | state                                   | blocked | cleared                     |
|-------------|-----------------------------------------|---------|-----------------------------|
| withdrawn   | withdrawn (from any state)              | yes     | every place, entries' only  |
| deleted     | deleted (from any state)                | yes     | every place, entries' only  |
| use_changed | unchanged; the new `use` narrows at once | no      | nothing                     |
| unreadable  | active -> unreadable; withdrawn/deleted stay | no  | nothing                     |
| restored    | -> active (withdrawn/deleted need the host's `may_restore`) | no | nothing; a cleared body does not come back |
| revised     | unchanged (unreadable -> active); the new revision is chained | no | nothing |

`entries` are the memories naming the source or holding it in a run; `derived_pending`
everything derived from them (prov's derived-from and primary-source lines, every
generation). Blocking comes first, before anything is cleared: each of them gets an open
`source_gone` invalidation record (`cleared: true` on the entries), which the read gate
honours on every road but 依据变了的 (core/visibility.py), and under a read scope the
registry blocks them again by itself. A restore closes the derived ones' records; the
entries' bodies are gone and stay gone, withdrawn or deleted alike (`note: redeliver`:
the host delivers the material again if it is to be remembered again).

The places, cleared in this order, each one of the entries only (what is derived is held
for review, not cleared):

    dream_records      dream files whose ingredients include an entry or a derived memory,
                       that were fed the changed source itself (the quote share's `来源`),
                       or whose text holds the entries' words
    dehydration_cache  cache rows keyed by an entry's body, or whose summary holds its words
    slices             pending slices over the changed lines: gist blanked, an open one dropped
    usage_log          the query of a lookup that listed what is cleared or typed its words
    sunk_text          archive/原文/<id>.txt
    body               the body, every text field, the name in the file name
                       (BucketManager.clear_body)
    embeddings         the entry's row in embeddings.db (content and meaning vectors)
    bm25               the in-memory keyword index, rebuilt from the cleared files; another
                       process sees the files change and rebuilds its own
    media              _media/<id>/
    ledger             the entries' ledger lines written with whole metadata, rewritten to
                       names only (core/_ledger.scrub_traces)

The ones that match words run before the body is cleared, while the words are still there
to match; "the words" are the body, name, summary, aliases, tags and subjects of each
entry and its sunk original, matched as runs of six CJK or twelve other characters
(`Words`).
Each place answers `done` (it held something and holds nothing now), `none` (it held
nothing) or `pending` (it failed; the places after it wait too, so a failed word match is
never left without the words to match). Every place done writes one ledger line
(`SourceCleared`), and the change itself one (`SourceChanged`, whose seq is
`applied_seq`).

Resending: the same change_id (from the same host) is a duplicate in the registry. If its
cleanup had a place left pending, this call carries on from there and answers `applied`
with the progress now; once every place is done or none the answer is `duplicate` with the
final result. Progress lives in `<buckets>/_sources/cleanup.jsonl`, one line per step, the
last line per change winning; every place is safe to run twice.

Exports: PLACES · CLEARING · Words · handle · request_record
========================================
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from typing import Optional

from utils import read_from_ids

from . import _invalidation as _I
from . import _ledger
from . import _sources as _src
from . import _when as _w

logger = logging.getLogger("loci_brain.source_change")

PLACES = ("dream_records", "dehydration_cache", "slices", "usage_log", "sunk_text", "body",
          "embeddings", "bm25", "media", "ledger")
# The places that match the entries' words: they run while the words are still there.
_WORD_PLACES = frozenset({"dream_records", "dehydration_cache", "usage_log"})
CLEARING = (_src.WITHDRAWN, _src.DELETED)
DONE, NONE, PENDING = "done", "none", "pending"
PROGRESS_FILE = "cleanup.jsonl"
_FRAGMENT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()\[\]{}<>《》…—\-~·/\\|]+")
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def request_record(body) -> dict:
    """The request body as the registry takes it: the contract calls the kind of change
    `change`. Raises ValueError on a body that is not an object or names the kind twice
    differently."""
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object")
    record = dict(body)
    kind = record.pop("change", None)
    if "kind" in record and kind is not None and record["kind"] != kind:
        raise ValueError("change and kind disagree; send change")
    if kind is not None:
        record["kind"] = kind
    allowed = {"change_id", "source", "host_seq", "kind", "revision", "use", "fingerprint",
               "fingerprint_by"}
    extra = sorted(set(map(str, record)) - allowed)
    if extra:
        raise ValueError(f"unknown fields {extra}")
    return record


_WINDOW = {True: 6, False: 12}        # characters of a matched run: CJK / other
_SHORTEST = {True: 4, False: 6}


class Words:
    """The cleared texts' words, matched as runs: every window of six CJK (or twelve
    other) characters of each piece between punctuation, and pieces shorter than a window
    (four CJK / six other characters at least) whole. `hit(text)` says whether a text holds
    any of them; matching slides over the text once per window size."""

    def __init__(self, texts):
        self.windows: dict[int, set] = {6: set(), 12: set()}
        self.pieces: list[str] = []
        for text in texts:
            for piece in _FRAGMENT.split(str(text or "")):
                piece = piece.strip()
                cjk = bool(_CJK.search(piece))
                n = _WINDOW[cjk]
                if len(piece) < _SHORTEST[cjk]:
                    continue
                if len(piece) <= n:
                    self.pieces.append(piece)
                    continue
                for i in range(len(piece) - n + 1):
                    self.windows[n].add(piece[i:i + n])
        self.pieces = list(dict.fromkeys(self.pieces))

    def __bool__(self) -> bool:
        return bool(self.pieces or any(self.windows.values()))

    def hit(self, text: str) -> bool:
        text = str(text or "")
        if any(p in text for p in self.pieces):
            return True
        for n, wins in self.windows.items():
            if wins and any(text[i:i + n] in wins for i in range(len(text) - n + 1)):
                return True
        return False


# ------------------------------------------------------------
# Progress
# ------------------------------------------------------------

def _progress_path(store):
    return store.sources.dir / PROGRESS_FILE


def _progress(store, host: str, change_id: str) -> Optional[dict]:
    key = _src.SourceRegistry.change_key(host, change_id)
    last = None
    for row in _src._read_lines(_progress_path(store)):
        if row.get("key") == key:
            last = row
    return last


def _save(store, prog: dict) -> None:
    prog = {**prog, "recorded_at": _src._now()}
    _src._append_line(_progress_path(store), prog)


def _complete(places: dict) -> bool:
    return all(v in (DONE, NONE) for v in places.values())


# ------------------------------------------------------------
# Who stood on it
# ------------------------------------------------------------

async def _derived(store, entries: list[str]) -> list[str]:
    """Everything derived from `entries`, every generation (derived-from and primary
    source; a revision's previous version is not a source)."""
    children: dict[str, list[str]] = {}
    for b in await store.list_all(include_archive=True):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        for parent in read_from_ids(meta):
            children.setdefault(str(parent), []).append(bid)
    seen = set(entries)
    out: list[str] = []
    todo = list(entries)
    while todo:
        for child in children.get(todo.pop(), []):
            if child and child not in seen:
                seen.add(child)
                out.append(child)
                todo.append(child)
    return out


# ------------------------------------------------------------
# The places
# ------------------------------------------------------------

def _dream_records(store, ctx) -> str:
    from . import _dream
    ids = set(ctx["entries"]) | set(ctx["derived"])
    registry = getattr(store, "sources", None)
    hit = 0
    for rec in _dream.load_dreams(store.base_dir):
        path = rec.get("_路径") or ""
        used = set(_dream.ingredient_ids(rec))
        text = json.dumps({k: v for k, v in rec.items() if not k.startswith("_")},
                          ensure_ascii=False)
        if ((used & ids) or _dream.fed_by(rec, ctx["sid"], registry)
                or ctx["words"].hit(text)):
            if path and os.path.exists(path):
                os.remove(path)
                hit += 1
    return DONE if hit else NONE


def _dehydration_cache(store, ctx) -> str:
    path = os.path.join(store.base_dir, "dehydration_cache.db")
    if not os.path.exists(path):
        return NONE
    keys: list[str] = []
    dehydrator = ctx.get("dehydrator")
    key_of = getattr(dehydrator, "_content_key", None)
    if callable(key_of):
        keys = [key_of(t) for t in ctx["bodies"] if t]
    conn = sqlite3.connect(path)
    try:
        # Deleted rows are overwritten, not left in free pages.
        conn.execute("PRAGMA secure_delete = ON")
        doomed = set()
        for content_hash, summary in conn.execute(
                "SELECT content_hash, summary FROM dehydration_cache"):
            if content_hash in keys or ctx["words"].hit(str(summary or "")):
                doomed.add(content_hash)
        for h in doomed:
            conn.execute("DELETE FROM dehydration_cache WHERE content_hash = ?", (h,))
        conn.commit()
    finally:
        conn.close()
    return DONE if doomed else NONE


async def _slices(store, ctx) -> str:
    slices = getattr(store, "slices", None)
    if slices is None:
        return NONE
    sid = ctx["sid"]
    lines = [x.id for x in store.sources.lines_of(sid)]
    hit, changed = await slices.withdraw_lines(
        {"system": sid.system, "instance": sid.instance, "container": sid.container}, lines)
    return DONE if (hit or changed) else NONE


def _usage_log(store, ctx) -> str:
    usage = getattr(store, "usage", None)
    if usage is None:
        return NONE
    n = usage.scrub(set(ctx["entries"]) | set(ctx["derived"]), ctx["words"].hit)
    return DONE if n else NONE


def _sunk_text(store, ctx) -> str:
    hit = 0
    for bid in ctx["entries"]:
        path = store._sunk_orig_path(bid)
        if os.path.exists(path):
            os.remove(path)
            hit += 1
    return DONE if hit else NONE


async def _body(store, ctx) -> str:
    hit = 0
    for bid in ctx["entries"]:
        got = await store.clear_body(bid, ctx["gone"](cleared=True))
        if got is not None:
            hit += 1
    return DONE if hit else NONE


def _embeddings(store, ctx) -> str:
    engine = getattr(store, "embedding_engine", None)
    path = getattr(engine, "db_path", None) or os.path.join(store.base_dir, "embeddings.db")
    outbox = getattr(store, "embedding_outbox", None)
    for bid in ctx["entries"]:
        if outbox is not None:
            try:
                outbox.discard(bid)
            except Exception:
                pass
    if not os.path.exists(path):
        return NONE
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        gone = 0
        for bid in ctx["entries"]:
            gone += conn.execute("DELETE FROM embeddings WHERE bucket_id = ?",
                                 (bid,)).rowcount
        conn.commit()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return NONE
        raise
    finally:
        conn.close()
    return DONE if gone else NONE


def _bm25(store, ctx) -> str:
    if not ctx["entries"]:
        return NONE
    store._invalidate_bm25()
    return DONE


def _media(store, ctx) -> str:
    root = getattr(getattr(store, "media_store", None), "media_dir", None)
    if root is None:
        return NONE
    hit = 0
    for bid in ctx["entries"]:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", bid)[:128]
        path = os.path.join(str(root), safe)
        if os.path.isdir(path):
            shutil.rmtree(path)
            hit += 1
    return DONE if hit else NONE


def _ledger_place(store, ctx) -> str:
    return DONE if _ledger.scrub_traces(store.ledger_mirror, ctx["entries"]) else NONE


_RUN = {"dream_records": _dream_records, "dehydration_cache": _dehydration_cache,
        "slices": _slices, "usage_log": _usage_log, "sunk_text": _sunk_text, "body": _body,
        "embeddings": _embeddings, "bm25": _bm25, "media": _media, "ledger": _ledger_place}


async def _words(store, entries: list[str]) -> tuple[list[str], Words]:
    """(the entries' bodies, the words of every text they hold), read before anything
    is cleared."""
    bodies, texts = [], []
    for bid in entries:
        b = await store.get_including_archive(bid)
        if b:
            meta = b.get("metadata") or {}
            body = str(b.get("content") or "")
            if body.strip() == store.CLEARED_BODY:
                continue
            bodies.append(body)
            texts.append(body)
            texts += [str(meta.get(k) or "") for k in ("name", "summary")]
            texts += [str(x) for k in ("aliases", "tags", "subjects")
                      for x in (meta.get(k) or []) if isinstance(x, str)]
        path = store._sunk_orig_path(bid)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                sunk = f.read()
            bodies.append(sunk)
            texts.append(sunk)
    return bodies, Words(texts)


# ------------------------------------------------------------
# The request
# ------------------------------------------------------------

def _none_places() -> dict:
    return {p: NONE for p in PLACES}


def _reply(change: dict, status: str, **kw) -> dict:
    out = {"change_id": change["change_id"], "status": status,
           "state": kw.pop("state", None), "blocked": kw.pop("blocked", False),
           "applied_seq": kw.pop("applied_seq", None), "entries": kw.pop("entries", []),
           "derived_pending": kw.pop("derived_pending", []),
           "cleanup": kw.pop("cleanup", _none_places())}
    out.update({k: v for k, v in kw.items() if v not in (None, "")})
    if out["state"] is None:
        out.pop("state")
    return out


def _outward(row: dict) -> dict:
    """A recorded change as a reply names it."""
    return {"change_id": row.get("change_id"), "change": row.get("kind"),
            "source": row.get("source"), "host_seq": row.get("host_seq"),
            "revision": row.get("revision"), "fingerprint": row.get("fingerprint"),
            "use": row.get("use")}


async def handle(store, body, host, *, dehydrator=None) -> tuple[int, dict]:
    """One change from `host` (a core.scope.Host). Returns (HTTP status, reply): 400 for a
    malformed body, 403 when the caller is not a host; every outcome of a well-formed
    change — applied, duplicate, conflict, stale, forbidden, unknown_source — is a 200
    whose `status` says which."""
    from .scope import Host

    if not isinstance(host, Host):
        return 403, {"error": "source changes come from a host's credential "
                              "(x-loci-hook-token), not from the panel"}
    try:
        record = request_record(body)
        change = _src._change_from(record)
    except (ValueError, _src.SourceRecordError) as e:
        return 400, {"error": str(e)}
    sid = _src.SourceId.parse(change["source"])[0]
    if host.max_grant is not None and not store.sources.granted(host.max_grant, sid):
        return 200, _reply(change, _src.FORBIDDEN, note="exceeds_max_grant",
                           exceeds=_src.Place.coerce(sid).label())
    registry = store.sources
    out = await registry.apply_change(record, may_restore=host.may_restore, host=host.name)
    outcome = out["outcome"]
    if outcome == _src.CONFLICT:
        return 200, _reply(change, outcome, note=out.get("note"),
                           conflict={"sent": _outward(change),
                                     "recorded": _outward(out.get("conflict_with") or {})})
    if outcome == _src.STALE:
        return 200, _reply(change, outcome, state=registry.state_of(sid),
                           blocked=registry.state_of(sid) in CLEARING)
    if outcome == _src.FORBIDDEN:
        return 200, _reply(change, outcome, note=out.get("note"))

    prior = registry.prior_change(host.name, change["change_id"]) or {}
    prog = _progress(store, host.name, change["change_id"])
    if outcome == _src.DUPLICATE and prog is not None and _complete(prog.get("places") or {}):
        return 200, _reply(change, _src.DUPLICATE, state=prog.get("state"),
                           blocked=bool(prog.get("blocked")),
                           applied_seq=prog.get("applied_seq"),
                           entries=list(prog.get("entries") or []),
                           derived_pending=list(prog.get("derived") or []),
                           cleanup=dict(prog.get("places") or {}), note=prog.get("note"),
                           result=prog.get("result"))
    status = prior.get("outcome") or outcome
    if outcome == _src.DUPLICATE:
        # Left unfinished: carry on, and say how far it got now.
        status = _src.APPLIED if prior.get("outcome") == _src.APPLIED else prior.get("outcome")
    return 200, await _carry_out(store, host, change, sid, prior, prog, status,
                                 dehydrator=dehydrator)


async def _carry_out(store, host, change: dict, sid, prior: dict, prog: Optional[dict],
                     status: str, *, dehydrator=None) -> dict:
    kind, state = change["kind"], str(prior.get("state") or _src.ACTIVE)
    note = prior.get("note") or ""
    if kind == "restored" and prior.get("previous") in CLEARING:
        note = "redeliver"          # what was cleared does not come back with the state
    key = _src.SourceRegistry.change_key(host.name, change["change_id"])
    entries = await _src.memories_of(store, sid)
    clearing = kind in CLEARING and state in CLEARING
    derived = await _derived(store, entries) if clearing else []
    if prog is None:
        prog = {"key": key, "host": host.name, "change_id": change["change_id"],
                "applied_seq": None, "places": {p: (PENDING if clearing else NONE)
                                                for p in PLACES}}
    prog.update({"state": state, "blocked": state in CLEARING, "entries": entries,
                 "derived": derived, "note": note,
                 "result": prior.get("outcome")})
    if prog.get("applied_seq") is None:
        event = store.ledger_mirror.append_event(
            event_type=_ledger.SOURCE_CHANGED, trace_id="", trace_kind="source",
            payload={"source": change["source"], "change": kind,
                     "change_id": change["change_id"], "host": host.name,
                     "host_seq": change["host_seq"], "state": state,
                     "previous": prior.get("previous")})
        prog["applied_seq"] = event["seq"]
        _save(store, prog)

    stamp = _w.now().isoformat(timespec="seconds")

    def gone(cleared: bool = False) -> dict:
        rec = {"kind": _I.SOURCE_GONE, "of": change["source"], "by": state, "at": stamp,
               "change": key.replace("\x00", ":")}
        if cleared:
            rec["cleared"] = True
        return rec

    if clearing:
        # Blocked first, cleared after.
        for bid in derived:
            await store.add_invalidation_record(bid, gone())
        for bid in entries:
            await store.add_invalidation_record(bid, gone(cleared=True))
        bodies, words = await _words(store, entries)
        ctx = {"entries": entries, "derived": derived, "sid": sid, "gone": gone,
               "bodies": bodies, "words": words, "dehydrator": dehydrator}
        places = dict(prog["places"])
        for place in PLACES:
            if places.get(place) in (DONE, NONE):
                continue
            # A dream may have been fed the source itself with no entry naming it.
            if (place in _WORD_PLACES and place != "dream_records"
                    and not words and not entries and not derived):
                places[place] = NONE
                continue
            try:
                run = _RUN[place]
                got = run(store, ctx)
                if hasattr(got, "__await__"):
                    got = await got
            except Exception as e:                          # noqa: BLE001 - reported, resumable
                logger.warning("source change %s: %s failed: %s", change["change_id"],
                               place, e)
                places[place] = PENDING
                break
            places[place] = got
            if got == DONE:
                store.ledger_mirror.append_event(
                    event_type=_ledger.SOURCE_CLEARED, trace_id="", trace_kind="source",
                    payload={"source": change["source"], "change_id": change["change_id"],
                             "place": place, "entries": entries})
            prog["places"] = dict(places)
            _save(store, prog)
        prog["places"] = places
    elif kind == "restored" and state == _src.ACTIVE:
        for bid in await _derived(store, entries):
            await store.close_invalidation_records(bid, _I.SOURCE_GONE, change["source"],
                                                   stamp, by=key.replace("\x00", ":"))
    _save(store, prog)
    return _reply(change, status, state=state, blocked=state in CLEARING,
                  applied_seq=prog["applied_seq"], entries=entries,
                  derived_pending=derived, cleanup=dict(prog["places"]),
                  note=note)
