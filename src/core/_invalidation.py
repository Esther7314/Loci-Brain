"""
========================================
core/_invalidation.py — 依据变了的: what a memory stood on changed under it
========================================

A memory stands on things: the memories it grew out of (`prov`), the host's material it
was formed from (`sources`), and — for a correction written from the panel — a person's
word. When one of those changes, the memory is not wrong by itself, but whoever reads it
next has to know which ground moved. breath's 「依据变了的」 block lists those memories
until each has been dealt with, in one of three ways the owner decided (10-01):

  rewrite      regrow the memory: the new version carries no mark (regrow never carries
               `invalidation` across, core/_fold._CARRIED_FIELDS), the old one is an old
               version and leaves every road.
  put away     archive it (trace delete=True).
  keep as is   trace(bucket_id=…, invalidation="confirmed"): looked at today, the basis
               changed but this stands. Every open record gains `confirmed_at` (the day);
               a source revision or a panel edit seen and kept is recorded the same way, as
               a record already confirmed. A confirmed record no longer counts as open
               anywhere.

What puts a memory in the block:

  edited     a correction a person wrote on the panel (the 人改的 tag, core/profile.
             edited_by_user) that has not been folded or confirmed.
  overturn   an open `{kind: overturn, of, by, at}` record: regrow(mode="overturn")
             appends one to every descendant of the overturned version at once — trust is
             withdrawn from the whole line immediately — but the block cards them **one
             layer at a time**: a memory is carded only once none of the memories it stands
             on is itself still waiting (live, current, an open overturn record). A parent
             confirmed, regrown or archived has been dealt with, and its children come up
             next. Looking at a grandchild before its parent is settled would be judging it
             on ground that may still move.
  sources    a source the registry (core/_sources.SourceRegistry) now reports withdrawn or
             deleted (a run: any line inside it), or revised past the revision this memory
             recorded (and not confirmed at that revision). A run records one revision and
             one fingerprint for all its lines, which say nothing of one line's version, so
             a line inside it counts as revised when the host's latest `revised` for that
             line was applied at or after the memory was written (`created`); it is named
             by the line's identity and confirmed per line and revision, so a later
             revision of the same or another line comes up again.
  gone       an open `{kind: source_gone, of, by, at, change, cleared?}` record: written
             when the host's withdrawn / deleted change is applied (core/_source_change.py)
             on every memory naming the source — `cleared: true` there, its body was
             cleared — and on everything derived from those, which stays readable to
             nobody until it is rewritten or put away. A memory whose body was cleared
             keeps its record even if the source is restored later (the text does not come
             back); a derived one's record is closed by the restore.

🔴 The body of a memory standing on a withdrawn or deleted source is **never handed back**
   here: only its id, each failed basis and its state, and the sources that are still
   good. With some left, it can be rewritten from those; with none, it can only be put
   away. Asking the model to review it must not put the material it may no longer use back
   in front of it. "Keep as is" is refused for such a memory for the same reason. The read
   gate withholds these memories on every other road (core/visibility.py, `source_gone`).

Which memories the block may show at all is the gate's: the `edited` road for panel
corrections, the `invalidation` road for the rest (core/visibility.py).

Exports: FIELD · OVERTURN · SOURCE_REVISED · EDITED · CONFIRMED · CONFIRMED_AT ·
         SOURCE_GONE · CLEARED · records · is_open · open_records · gone_records ·
         edit_confirmed · SourceFindings · source_findings · waiting_on_overturn ·
         state_word · confirm · block
========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from utils import now_iso, read_from_ids

from . import _sources as _src
from . import visibility as _V

FIELD = "invalidation"
OVERTURN = "overturn"
SOURCE_REVISED = "source_revised"
EDITED = "edited"
CONFIRMED = "confirmed"          # the one value trace's `invalidation=` takes
CONFIRMED_AT = "confirmed_at"
SOURCE_GONE = "source_gone"
CLEARED = "cleared"              # a source_gone record's word once the body was cleared

_STATE_WORD = {_src.WITHDRAWN: "已撤回", _src.DELETED: "已删除", CLEARED: "正文已清"}


def records(meta) -> list[dict]:
    """The memory's invalidation records, as stored (dicts only)."""
    raw = (meta or {}).get(FIELD) or []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def is_open(rec: dict) -> bool:
    return not str(rec.get(CONFIRMED_AT) or "").strip()


def open_records(meta, kind: str | None = None) -> list[dict]:
    """The records not yet confirmed, of one kind when `kind` is given."""
    return [r for r in records(meta) if is_open(r) and (kind is None or r.get("kind") == kind)]


def _confirmed(meta, kind: str, of: str, by: str | None = None) -> bool:
    return any(r.get("kind") == kind and str(r.get("of") or "") == of
               and (by is None or str(r.get("by") or "") == by) and not is_open(r)
               for r in records(meta))


def edit_confirmed(meta) -> bool:
    """A panel correction looked at and kept as it is."""
    return _confirmed(meta, EDITED, str((meta or {}).get("id") or ""))


def state_word(state: str) -> str:
    return _STATE_WORD.get(state, state)


@dataclass
class SourceFindings:
    """What the registry says about a memory's sources.

    failed     [(string form, state)] — withdrawn or deleted: may no longer be used
    revised    [(identity string, the newer revision)] — not confirmed at that revision; for
               a run, the identity of the line inside it that was revised
    remaining  [string form] — the sources still good to stand on"""
    failed: list = field(default_factory=list)
    revised: list = field(default_factory=list)
    remaining: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.failed or self.revised)


def _newer_revision(rec: dict, registry) -> str:
    """The revision (or fingerprint) the host announced past the one this record holds,
    or ""."""
    revisions = registry.revisions_of(_src.record_id(rec))
    if not revisions:
        return ""
    latest = revisions[-1]
    if latest.get("revision"):
        return str(latest["revision"]) if latest["revision"] != rec.get("revision") else ""
    if latest.get("fingerprint"):
        return str(latest["fingerprint"]) if latest["fingerprint"] != rec.get("fingerprint") else ""
    return ""


def _stamp(value):
    """A stored stamp as an aware UTC datetime (naive stamps are UTC), or None."""
    try:
        dt = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _run_revisions(meta: dict, rec: dict, registry) -> list[tuple[str, str]]:
    """[(line identity, its newer revision)] for the lines a run holds (the host's order
    when known, else its first and last line): each line whose latest announced revision
    was applied at or after the memory was written. A memory with no readable `created`
    counts every announced revision."""
    since = _stamp((meta or {}).get("created"))
    out = []
    for line in registry.lines_of(_src.record_id(rec)):
        revisions = registry.revisions_of(line)
        if not revisions:
            continue
        latest = revisions[-1]
        at = _stamp(latest.get("recorded_at"))
        if since is not None and (at is None or at < since):
            continue
        newer = str(latest.get("revision") or latest.get("fingerprint") or "")
        if newer:
            out.append((str(line), newer))
    return out


def gone_records(meta) -> list[dict]:
    """The open `source_gone` records: a source behind this memory was withdrawn or
    deleted, written on it when the host's change was applied."""
    return open_records(meta, SOURCE_GONE)


def source_findings(meta, registry) -> SourceFindings:
    """Each of the memory's source records, read against the registry (a run by every
    line it holds), and every open `source_gone` record (a source behind a memory it
    stands on, or one whose body was cleared). A record that no longer reads as one is
    skipped (it names nothing the registry could answer for)."""
    out = SourceFindings()
    gone = gone_records(meta)
    for r in gone:
        state = str(r.get("by") or "")
        out.failed.append((str(r.get("of") or ""), CLEARED if r.get("cleared") else state))
    failed_keys = {str(r.get("of") or "") for r in gone}
    if registry is None:
        return out
    for raw in (meta or {}).get(_src.SOURCES_FIELD) or []:
        try:
            [rec] = _src.normalize_sources([raw])
        except (_src.SourceRecordError, ValueError):
            continue
        text = _src.record_string(rec)
        state = registry.read_state(rec)
        if state in (_src.WITHDRAWN, _src.DELETED):
            if str(_src.record_id(rec)) not in failed_keys:
                out.failed.append((text, state))
            continue
        out.remaining.append(text)
        identity = str(_src.record_id(rec))
        if _src.record_id(rec).through:
            changed = _run_revisions(meta, rec, registry)
        else:
            newer = _newer_revision(rec, registry)
            changed = [(identity, newer)] if newer else []
        for of, newer in changed:
            if not _confirmed(meta, SOURCE_REVISED, of, newer):
                out.revised.append((of, newer))
    return out


def waiting_on_overturn(meta) -> bool:
    """Live, current, and carrying an open overturn record: this memory itself still has
    to be dealt with, and what stands on it waits."""
    return (_V.state_of(meta) == _V.LIVE and not str(meta.get("superseded_by") or "").strip()
            and bool(open_records(meta, OVERTURN)))


def confirm(meta, registry, *, edited: bool, today: str) -> tuple[list[dict], str]:
    """The records `trace(invalidation="confirmed")` writes: every open record with
    `confirmed_at` = `today`, and one confirmed record for each source revision and for a
    panel correction (`edited`) looked at now. Returns (the whole new list, refusal). The
    list is empty when there was nothing to confirm."""
    found = source_findings(meta, registry)
    if found.failed:
        failed = "、".join(f"{s}（{state_word(st)}）" for s, st in found.failed)
        tail = ("只凭还剩的来源重写（regrow），或者收起来（trace delete=True）"
                if found.remaining else "一条来源都不剩，只能收起来（trace delete=True）")
        return [], f"这条站着的来源已经不能用了：{failed}。照留不行——{tail}。"
    bid = str((meta or {}).get("id") or "")
    stamp = now_iso()
    changed = False
    out: list[dict] = []
    for rec in records(meta):
        if is_open(rec):
            rec = {**rec, CONFIRMED_AT: today}
            changed = True
        out.append(rec)
    for identity, newer in found.revised:
        out.append({"kind": SOURCE_REVISED, "of": identity, "by": newer, "at": stamp,
                    CONFIRMED_AT: today})
        changed = True
    if edited and not edit_confirmed(meta):
        out.append({"kind": EDITED, "of": bid, "by": "", "at": stamp, CONFIRMED_AT: today})
        changed = True
    return (out if changed else []), ""


def block(all_buckets: list, registry, *, scope=None) -> list[dict]:
    """依据变了的, in order: panel corrections (oldest first), then the rest in store
    order. One item per memory, every reason it is here on it:

        {id, short, text, edited, overturned: [{of, by, at}], failed: [{source, state}],
         revised: [{source, revision}], remaining: [source]}

    `text` is None for a memory standing on a withdrawn or deleted source; `remaining` is
    filled in only then (the sources a rewrite may stand on).

    Under a read scope (`scope`, a core.scope.ScopeView) the gate leaves out what the
    request may not read, and an overturn is told only when the request may read the
    version that overturned it: its id is not named otherwise."""
    from .profile import edited_by_user, entry_label, short_id  # lazy: profile imports this module

    items: dict[str, dict] = {}

    def item(bid: str, meta: dict, content: str) -> dict:
        if bid not in items:
            items[bid] = {"id": bid, "short": short_id(bid), "text": entry_label(meta, content),
                          "edited": False, "overturned": [], "failed": [], "revised": [],
                          "remaining": []}
        return items[bid]

    for e in edited_by_user(all_buckets, scope=scope):
        item(e["id"], e["meta"], e["content"])["edited"] = True

    rows = [(str((b.get("metadata") or {}).get("id") or b.get("id") or ""),
             b.get("metadata") or {}, str(b.get("content") or "")) for b in all_buckets]
    waiting = {bid for bid, meta, _c in rows if bid and waiting_on_overturn(meta)}
    for bid, meta, content in rows:
        if (not bid or not _V.timeline_kind(meta)
                or not _V.visible_for(meta, scope, road=_V.INVALIDATION)):
            continue
        overturned = open_records(meta, OVERTURN)
        if scope is not None:
            overturned = [r for r in overturned
                          if scope.permits_id(str(r.get("by") or ""))
                          and scope.permits_id(str(r.get("of") or ""))]
        if overturned and any(src in waiting for src in read_from_ids(meta)):
            overturned = []          # what it stands on is still waiting: that comes first
        found = source_findings(meta, registry)
        if not overturned and not found:
            continue
        it = item(bid, meta, content)
        it["overturned"] = [{"of": str(r.get("of") or ""), "by": str(r.get("by") or ""),
                             "at": str(r.get("at") or "")} for r in overturned]
        it["failed"] = [{"source": s, "state": st} for s, st in found.failed]
        it["revised"] = [{"source": s, "revision": rv} for s, rv in found.revised]
        if found.failed:
            it["text"] = None
            it["remaining"] = list(found.remaining)
    return list(items.values())
