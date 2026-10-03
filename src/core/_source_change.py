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
| restored    | -> active (withdrawn/deleted need the host's `may_restore`) | no | nothing; a cleared body does not come back, what was derived waits for review |
| revised     | unchanged (unreadable -> active); the new revision is chained | no | nothing |

Who may send it: the source's declared change authority (`authority:` in `hosts:`,
core/scope.Hosts.authority_for), within its `max_grant`; for a run, the authority for
every line it holds as well (`authority_refusal`). One order per source — the
authority's host_seq — so a newer change can never be overtaken by an older one arriving
later from somewhere else. A change past the ceiling is `forbidden` with `note:
exceeds_max_grant`; from a host that is not the authority, `forbidden` with `note:
not_change_authority` (another host relays through the authority); for a source no host is
declared the authority of, `forbidden` with `note: no_change_authority`. With no `hosts:`
table the one legacy host is the authority for everything.

A host with a ceiling never sees the ledger's own numbers: its receipt carries
`applied_cursor` (core/_ledger.cursor_of) where an open host's carries `applied_seq`.

A withdrawn, deleted or restored change also settles a hold (`hold`): a host serving the
original said the source was withdrawn or deleted before any ordered change did, and the
memories resting on it were held — open `source_held` records — not cleared. The ordered
change closes those records; withdrawn or deleted then blocks and clears as below.

`entries` are the memories resting on the source (naming it, holding it in a run, or
quoting it by string form, core/_sources.memories_of); `derived_pending`
everything derived from them (prov's derived-from and primary-source lines, every
generation). Blocking comes first, before anything is cleared: each of them gets an open
`source_gone` invalidation record (`cleared: true` on the entries), which the read gate
honours on every road but 依据变了的 (core/visibility.py), and under a read scope the
registry blocks them again by itself. A restore gives the source back its eligibility, not
what stood on it: the entries' bodies are gone and stay gone, withdrawn or deleted alike
(`note: redeliver`: the host delivers the material again if it is to be remembered again),
and each derived memory's `source_gone` record is traded for an open `source_restored`
record (`_await_review`) — still off every road but 依据变了的 and a read by id, both now
showing its text, until the model keeps it as is (trace invalidation="confirmed") or
rewrites it. The restore's
`derived_pending` lists those. A restore that settles a hold on a source never withdrawn
closes the `source_held` records and brings everything back, derived memories too: the
ordered word says the source was never gone, so nothing standing on it has a changed basis
to be reviewed for.

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
to match; "the words" are the body, name and summary of each entry and its sunk original
— never its tags, subjects or aliases, labels other memories share — matched as runs of
six CJK or twelve other characters (`Words`).
Each place answers `done` (it held something and holds nothing now), `none` (it held
nothing) or `pending` (it failed; the places after it wait too, so a failed word match is
never left without the words to match). Every place done writes one ledger line
(`SourceCleared`), and the change itself one (`SourceChanged`, whose seq is
`applied_seq`).

Resending: the same change_id (from the same host) is a duplicate in the registry. If its
cleanup had a place left pending, or the step after the places (a restore's review, the
holds a settling change closes) was not written, this call carries on from there and
answers `applied` with the progress now; once every place is done or none and that step is
written the answer is `duplicate` with the final result. A resend works on the memories
the first send found, never on what rests on the source now, and once a later change has
settled the source it blocks nothing new: it finishes the places for the memories this
change already blocked (`_carry_out`). Two sends of one change at once run one after the
other (a lease per host and change_id). Progress lives in
`<buckets>/_sources/cleanup.jsonl`, one line per step, the last line per change winning;
every place is safe to run twice.

Exports: PLACES · CLEARING · NO_AUTHORITY · NOT_AUTHORITY · LINES_MAX · Words · handle ·
         handle_lines · hold · request_record · authority_refusal · registration_refusal
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
# Why a change is forbidden although the host's ceiling reaches the source.
NO_AUTHORITY = "no_change_authority"      # no host is declared the source's authority
NOT_AUTHORITY = "not_change_authority"    # another host is
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


def _finished(prog: dict) -> bool:
    """Every place done or none, and the step after them (a restore's review, the holds a
    settling change closes) written. A progress line without `settled` is from before that
    step was tracked, and counts as written."""
    return _complete(prog.get("places") or {}) and bool(prog.get("settled", True))


# How long a second send of one change waits for the first to finish: clearing a large
# library can take a while, and the second send must not run alongside it.
_CHANGE_LEASE_SECONDS = 600.0


def _change_turn(store, key: str):
    """The lease of one change (host, change_id): two sends of it at once run one after
    the other, so the second finds the first one's progress instead of running it again."""
    from .bucket_manager import _filesystem_turn      # lazy: bucket_manager imports _sources
    return _filesystem_turn(store.base_dir, f"source-change-{key}",
                            timeout_seconds=_CHANGE_LEASE_SECONDS)


async def _blocked_by(store, bid: str, tag: str, *, cleared: bool = False) -> bool:
    """Does this memory carry this change's `source_gone` record (open or closed; with
    `cleared`, one saying its body was cleared)?"""
    b = await store.get_including_archive(bid)
    if not b:
        return False
    return any(r.get("kind") == _I.SOURCE_GONE and r.get("change") == tag
               and (not cleared or r.get("cleared"))
               for r in _I.records(b.get("metadata") or {}))


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
    # Once a later change gave the source back, a dream fed by it since is not this
    # change's to clear.
    fed = (lambda rec: False) if ctx.get("superseded") else (
        lambda rec: _dream.fed_by(rec, ctx["sid"], registry))
    hit = 0
    for rec in _dream.load_dreams(store.base_dir):
        path = rec.get("_路径") or ""
        used = set(_dream.ingredient_ids(rec))
        text = json.dumps({k: v for k, v in rec.items() if not k.startswith("_")},
                          ensure_ascii=False)
        if (used & ids) or fed(rec) or ctx["words"].hit(text):
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
    if slices is None or ctx.get("superseded"):
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
    """(the entries' bodies, the words of the texts written from the material), read
    before anything is cleared: the body, the name, the summary, the sunk original. Tags,
    subjects and aliases are left out — they are labels shared across the library, and a
    generic one would clear dreams and lookups that never held this material."""
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
        path = store._sunk_orig_path(bid)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                sunk = f.read()
            bodies.append(sunk)
            texts.append(sunk)
    return bodies, Words(texts)


# ------------------------------------------------------------
# A run's lines, registered by the host (POST /api/v2/source/lines)
# ------------------------------------------------------------

LINES_MAX = 2000                # lines in one run, the same cap as one slicing batch
_LINES_KEYS = {"source", "revision", "lines"}


def _lines_request(body) -> tuple[_src.SourceId, Optional[str], list[str], Optional[dict]]:
    """The body -> (run, watermark, line ids, per-line revisions or None). Raises
    ValueError on anything but the agreed shape."""
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object")
    extra = sorted(set(map(str, body)) - _LINES_KEYS)
    if extra:
        raise ValueError(f"unknown fields {extra}; send {sorted(_LINES_KEYS)}")
    source = body.get("source")
    if isinstance(source, dict):
        if "revision" in source or "span" in source:
            raise ValueError("source names the run only; its watermark goes in revision")
        sid = _src.record_id(_src._normalize_record(source, where=""))
    elif isinstance(source, str):
        sid, at = _src.SourceId.parse(source)
        if at is not None:
            raise ValueError("source names the run only; its watermark goes in revision")
    else:
        raise ValueError("source is {system, instance, container, id, through} or its "
                         "string form")
    if not sid.through:
        raise ValueError("source is a run: id is its first line and through its last")
    revision = _src._opt_text(body.get("revision"))
    if revision is not None:
        _src._normalize_record({**_identity_fields(sid), "revision": revision}, where="")
    rows = body.get("lines")
    if not isinstance(rows, list) or not 2 <= len(rows) <= LINES_MAX:
        raise ValueError(f"lines lists the run's lines in order, 2 to {LINES_MAX} of them")
    ids: list[str] = []
    revisions: dict = {}
    said = False
    for i, row in enumerate(rows):
        if isinstance(row, str):
            row = {"id": row}
        if not isinstance(row, dict) or set(map(str, row)) - {"id", "revision"}:
            raise ValueError(f"lines[{i}] is a line id or {{id, revision?}}")
        try:
            rec = _src._normalize_record({**_identity_fields(sid), "id": row.get("id"),
                                          "revision": row.get("revision")}, where=f"[{i}]")
        except _src.SourceRecordError as e:
            raise ValueError(f"lines[{i}]: {e}") from None
        if rec["id"] in ids:
            raise ValueError(f"lines[{i}]: {rec['id']} appears twice")
        ids.append(rec["id"])
        if "revision" in row:
            revisions[rec["id"]] = rec["revision"]
            said = True
    if ids[0] != sid.id or ids[-1] != sid.through:
        raise ValueError("lines start at the run's first line (id) and end at its last "
                         "(through)")
    return sid, revision, ids, (revisions if said else None)


def _identity_fields(sid: _src.SourceId) -> dict:
    return {"system": sid.system, "instance": sid.instance, "container": sid.container,
            "id": sid.id}


def authority_refusal(registry, hosts, host, sid: _src.SourceId) -> str:
    """Why `host` may not send a change for `sid`, or "": NO_AUTHORITY when no single host
    is the change authority for it, NOT_AUTHORITY when another host is. A run reaches
    every line it holds (`SourceRegistry.state_of`), so for a run the sender has to be the
    authority for every one of its lines too (`SourceRegistry.lines_of`) — a line no deeper
    place declares is the container's, whose authority covers the run itself; a line two
    hosts declare at the same depth has none. `hosts` None = a deployment of `host` alone."""
    if hosts is None:
        return ""
    for identity in [sid] + (registry.lines_of(sid) if sid.through else []):
        authority = hosts.authority_for(identity)
        if authority is None:
            return NO_AUTHORITY
        if authority.name != host.name:
            return NOT_AUTHORITY
    return ""


def registration_refusal(hosts, host, where: dict, ids: list[str]) -> str:
    """Why `host` may not register these lines of a container (a run's lines, a slicing
    batch), or "": NOT_AUTHORITY when the container or one of the lines has a declared
    change authority that is another host. What a run holds decides what a change for it
    reaches, so only the authority for the material puts lines into its runs; material
    nobody is declared the authority of may be registered by any host within its ceiling.
    `hosts` None = a deployment of `host` alone."""
    if hosts is None or host is None:
        return ""
    base = _src.SourceId(str(where["system"]), str(where["instance"]),
                         str(where["container"]), "\0")
    for identity in [base.piece(i) for i in ids]:
        authority = hosts.authority_for(identity)
        if authority is not None and authority.name != host.name:
            return NOT_AUTHORITY
    return ""


async def handle_lines(store, body, host, *, hosts=None) -> tuple[int, dict]:
    """`POST /api/v2/source/lines`: a host registers which lines a run holds, in order,
    without handing their text over for slicing:

        {"source": {"system", "instance", "container", "id": first, "through": last} | "…",
         "revision": "<the host's watermark for this delivery>" | null,
         "lines": ["m_0010", {"id": "m_0011", "revision": "e1"}, …, "m_0030"]}
     -> {"status": "recorded" | "known" | "conflict" | "forbidden", "source": "…",
         "lines": n, "revision": …, "note"?}

    The same registration as a slicing batch's (core/_sources.SourceRegistry.record_order,
    `_sources/line_orders.jsonl`). A line with `revision` says which revision of it was
    delivered under the watermark; a run record whose own `revision` is that watermark
    adopted those revisions. `conflict`: the watermark already gives one of these lines
    another revision; nothing is recorded. `forbidden`: the run lies past the host's
    `max_grant` (`note: exceeds_max_grant`), or one of its lines has a declared change
    authority that is another host (`note: not_change_authority`, `registration_refusal`;
    `hosts` is the deployment's table, None = a deployment of `host` alone). Like source
    changes, 400 for a malformed body, 403 for a caller that is not a host, every outcome
    a 200."""
    from .bucket_manager import _filesystem_turn      # lazy: bucket_manager imports _sources
    from .scope import Host

    if not isinstance(host, Host):
        return 403, {"error": "a run's lines come from a host's credential "
                              "(x-loci-hook-token), not from the panel"}
    try:
        sid, revision, ids, revisions = _lines_request(body)
    except (ValueError, _src.SourceRecordError) as e:
        return 400, {"error": str(e)}
    out = {"source": sid.to_string(), "lines": len(ids), "revision": revision}
    if host.max_grant is not None and not (
            any(p.covers(sid) for p in host.max_grant)
            or all(any(p.covers(sid.piece(i)) for p in host.max_grant) for i in ids)):
        return 200, {"status": _src.FORBIDDEN, **out, "note": "exceeds_max_grant",
                     "exceeds": _src.Place.coerce(sid).label()}
    where = {"system": sid.system, "instance": sid.instance, "container": sid.container}
    why = registration_refusal(hosts, host, where, ids)
    if why:
        return 200, {"status": _src.FORBIDDEN, **out, "note": why}
    async with _filesystem_turn(store.base_dir, "source-registry"):
        got = store.sources.record_order(where, ids, batch_id=f"lines:{host.name}",
                                         revision=revision, revisions=revisions)
    if got not in (_src.RECORDED, _src.KNOWN):
        return 200, {"status": _src.CONFLICT, **out, "note": got}
    if revisions is not None and revision is None:
        # Kept, but no run record can name this delivery: its lines' revisions stay unknown.
        out["note"] = "revisions_without_watermark"
    return 200, {"status": got, **out}


# ------------------------------------------------------------
# Holds: a host's unordered word that a source is gone
# ------------------------------------------------------------

_SETTLING = (_src.WITHDRAWN, _src.DELETED, "restored")


async def hold(store, identity, said: str, host: str = "") -> list[str]:
    """A host serving a source's original said it is `said` (withdrawn or deleted) while
    the registry records no such change. Nothing is cleared and the registry's state is
    not written — the answer was not ordered — but nothing resting on the source may be
    used until the ordered change settles it: the registry holds the source
    (core/_sources.SourceRegistry.hold) and every memory resting on it, and everything
    derived from those, gets an open `source_held` record the read gate honours on every
    road but 依据变了的. Returns the memories held (empty when the registry already records
    the source withdrawn or deleted)."""
    registry = store.sources
    sid = (identity if isinstance(identity, _src.SourceId)
           else _src.SourceId.parse(str(identity))[0])
    if registry.state_of(sid) in CLEARING:
        return []
    row = registry.hold(sid, said, host)
    entries = await _src.memories_of(store, sid)
    derived = await _derived(store, entries)
    rec = {"kind": _I.SOURCE_HELD, "of": sid.to_string(), "by": said,
           "at": _w.now().isoformat(timespec="seconds"),
           "change": f"held:{row.get('host') or ''}:{row.get('after_seq')}:{sid}"}
    for bid in entries + derived:
        await store.add_invalidation_record(bid, dict(rec))
    logger.info("source %s held on %s's word that it is %s (%d memories)", sid,
                host or "a host", said, len(entries) + len(derived))
    return entries + derived


async def _settle_holds(store, ids: list[str], stamp: str, by: str) -> None:
    """Close the open `source_held` records on these memories whose hold the registry no
    longer has open (an ordered change settled it)."""
    registry = store.sources
    for bid in dict.fromkeys(ids):
        b = await store.get_including_archive(bid)
        if not b:
            continue
        for r in _I.open_records(b.get("metadata") or {}, _I.SOURCE_HELD):
            of = str(r.get("of") or "")
            try:
                still = registry.held_of(of) is not None
            except _src.SourceRecordError:
                still = False
            if not still:
                await store.close_invalidation_records(bid, _I.SOURCE_HELD, of, stamp, by=by)


async def _await_review(store, bid: str, of: str, stamp: str, change: str) -> bool:
    """A source restored: a memory derived from what rested on it trades its open
    `source_gone` record about `of` for an open `source_restored` record. The ground came
    back, but what was derived from it while it was gone was never looked at again, so it
    does not come back by itself — it waits in 依据变了的 (and reads by id, for the review)
    until the model confirms or rewrites it. The new record is written before the old one is closed, so a resend after
    a crash in between finds the work half done and finishes it. True when the memory waits
    on this change's record afterwards."""
    b = await store.get_including_archive(bid)
    if not b:
        return False
    meta = b.get("metadata") or {}
    gone = [r for r in _I.open_records(meta, _I.SOURCE_GONE)
            if str(r.get("of") or "") == of and not r.get("cleared")]
    waiting = any(r.get("change") == change
                  for r in _I.open_records(meta, _I.SOURCE_RESTORED))
    if not gone:
        return waiting
    await store.add_invalidation_record(bid, {"kind": _I.SOURCE_RESTORED, "of": of,
                                              "by": "restored", "at": stamp,
                                              "change": change})
    await store.close_invalidation_records(bid, _I.SOURCE_GONE, of, stamp, by=change)
    return True


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


async def _for_host(store, host, reply: dict) -> dict:
    """A host with a ceiling never sees the ledger's own numbers (core/_ledger.py): its
    receipt names the change's line by `applied_cursor` instead of `applied_seq`, and its
    `entries` and `derived_pending` name only the memories it may reconcile — every source
    behind them within its ceiling, as `/changes` shows them (core/_ledger.visible_ids).
    The rest were reached and handled all the same; their ids are not its to see."""
    if not _ledger.host_view(host):
        return reply
    seq = reply.pop("applied_seq", None)
    if isinstance(seq, int):
        reply["applied_cursor"] = _ledger.cursor_of(store.ledger_mirror, seq, host.name)
    named = list(reply.get("entries") or []) + list(reply.get("derived_pending") or [])
    if named:
        seen = await _ledger.visible_ids(store, host, named)
        for key in ("entries", "derived_pending"):
            reply[key] = [b for b in reply.get(key) or [] if b in seen]
    return reply


async def handle(store, body, host, *, dehydrator=None, hosts=None) -> tuple[int, dict]:
    """One change from `host` (a core.scope.Host). Returns (HTTP status, reply): 400 for a
    malformed body, 403 when the caller is not a host; every outcome of a well-formed
    change — applied, duplicate, conflict, stale, forbidden, unknown_source — is a 200
    whose `status` says which. `hosts` is the deployment's table (core/scope.Hosts), which
    names each source's change authority; None = a deployment of `host` alone."""
    status, reply = await _handle(store, body, host, dehydrator=dehydrator, hosts=hosts)
    return status, ((await _for_host(store, host, reply)) if status == 200 else reply)


async def _handle(store, body, host, *, dehydrator=None, hosts=None) -> tuple[int, dict]:
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
    if host.max_grant is not None and not store.sources.reaches(host.max_grant, sid):
        return 200, _reply(change, _src.FORBIDDEN, note="exceeds_max_grant",
                           exceeds=_src.Place.coerce(sid).label())
    # One order per source: only its declared change authority sends its changes.
    why = authority_refusal(store.sources, hosts, host, sid)
    if why:
        return 200, _reply(change, _src.FORBIDDEN, note=why)
    registry = store.sources
    # One send of a change at a time: a second one waits, then finds the first's progress.
    async with _change_turn(store, _src.SourceRegistry.change_key(host.name,
                                                                  change["change_id"])):
        out = await registry.apply_change(record, may_restore=host.may_restore,
                                          host=host.name)
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
        if outcome == _src.DUPLICATE and prog is not None and _finished(prog):
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
            status = (_src.APPLIED if prior.get("outcome") == _src.APPLIED
                      else prior.get("outcome"))
        return 200, await _carry_out(store, host, change, sid, prior, prog, status,
                                     dehydrator=dehydrator)


async def _carry_out(store, host, change: dict, sid, prior: dict, prog: Optional[dict],
                     status: str, *, dehydrator=None) -> dict:
    """Write the change's ledger line, block, clear, review, settle — or carry on from
    where an earlier send of it stopped. An earlier send's progress names the memories it
    found, and a resend works on those, never on what rests on the source now: after a
    later change gave the source back, a memory written since stands on a source that is
    active. Once a later change has settled the source (`SourceRegistry.settled_after`), a
    resend no longer blocks anything: it finishes the places only for the memories this
    change had already blocked."""
    kind, state = change["kind"], str(prior.get("state") or _src.ACTIVE)
    note = prior.get("note") or ""
    if kind == "restored" and prior.get("previous") in CLEARING:
        note = "redeliver"          # what was cleared does not come back with the state
    key = _src.SourceRegistry.change_key(host.name, change["change_id"])
    tag = key.replace("\x00", ":")
    clearing = kind in CLEARING and state in CLEARING
    resumed = prog is not None and "entries" in prog
    if resumed:
        entries = [str(x) for x in prog.get("entries") or []]
        derived = [str(x) for x in prog.get("derived") or []] if clearing else []
    else:
        entries = await _src.memories_of(store, sid)
        derived = await _derived(store, entries) if clearing else []
    superseded = clearing and store.sources.settled_after(change["source"],
                                                          prior.get("seq"))
    if superseded:
        entries = [b for b in entries if await _blocked_by(store, b, tag, cleared=True)]
        derived = [b for b in derived if await _blocked_by(store, b, tag)]
    if prog is None:
        prog = {"key": key, "host": host.name, "change_id": change["change_id"],
                "applied_seq": None, "places": {p: (PENDING if clearing else NONE)
                                                for p in PLACES},
                "settled": kind not in _SETTLING}
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
               "change": tag}
        if cleared:
            rec["cleared"] = True
        return rec

    if clearing:
        # Blocked first, cleared after; once superseded, what was blocked stays as it is.
        if not superseded:
            for bid in derived:
                await store.add_invalidation_record(bid, gone())
            for bid in entries:
                await store.add_invalidation_record(bid, gone(cleared=True))
        bodies, words = await _words(store, entries)
        ctx = {"entries": entries, "derived": derived, "sid": sid, "gone": gone,
               "bodies": bodies, "words": words, "dehydrator": dehydrator,
               "superseded": superseded}
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
    reached = list(derived)
    if kind == "restored" and state == _src.ACTIVE:
        reached = await _derived(store, entries)
        derived = [bid for bid in reached
                   if await _await_review(store, bid, change["source"], stamp, tag)]
        prog["derived"] = derived
    if kind in _SETTLING:
        # The ordered word has come: what a host's unordered word held is settled by it.
        await _settle_holds(store, entries + reached, stamp, tag)
    # Saved only now: a send that broke off before this point is carried on by a resend
    # (`_finished`), so the review and the settled holds are always written.
    prog["settled"] = True
    _save(store, prog)
    return _reply(change, status, state=state, blocked=state in CLEARING,
                  applied_seq=prog["applied_seq"], entries=entries,
                  derived_pending=derived, cleanup=dict(prog["places"]),
                  note=note)
