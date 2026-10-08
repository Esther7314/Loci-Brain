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
  sources    a source the memory rests on (its `sources` and what its quoted lines name,
             core/_sources.basis_records) that the registry now reports withdrawn, deleted
             or held (a run: any line inside it), or revised past the revision this memory
             adopted (and not confirmed at that revision). A run's own revision names the
             host's watermark for the delivery it was formed from, and the registration
             under that watermark says which revision of each line was adopted
             (core/_sources.SourceRegistry.run_revisions): a line counts as revised when
             the host's newest announced revision of it is not the adopted one, or when the
             adopted one is unknown and the host has announced any — when the memory was
             written proves nothing. It is named by the line's identity and confirmed per
             line and revision, so a later revision of the same or another line comes up
             again.
  gone       an open `{kind: source_gone, of, by, at, change, cleared?}` record: written
             when the host's withdrawn / deleted change is applied (core/_source_change.py)
             on every memory resting on the source — `cleared: true` there, its body was
             cleared — and on everything derived from those, which stays readable to
             nobody until it is rewritten or put away. A memory whose body was cleared
             keeps its record even if the source is restored later (the text does not come
             back); a derived one's record is closed by the restore and replaced by an open
             `source_restored` record (below).
  restored   an open `{kind: source_restored, of, by, at, change}` record: the source was
             restored, and this memory was derived from what rested on it while it was
             gone. The ground came back, but nothing re-checked what was derived from it,
             so it does not come back by itself: it stays off every road but this block
             and a read by id (core/visibility.py) until it is kept as is (trace
             invalidation="confirmed") or rewritten. Its text is shown here, and a read by
             id shows it whole under `RESTORED_READ_LINE` — the source may be used again,
             and the model reviews it from there.
  held       an open `{kind: source_held, of, by, at, change}` record: a host serving the
             original said the source is withdrawn or deleted before any ordered change did
             (core/_source_change.hold). Read like `gone` — nothing of the memory is used —
             until the ordered change settles it: withdrawn / deleted turn it into `gone`,
             restored closes it. Nothing is cleared on a host's unordered word.
  basis      a source behind a memory this one is derived from (the walk the read gate
             takes to the roots, `read_from_ids` through every state) revised past the
             revision that memory adopted. A derived memory keeps its own understanding
             and provenance — nothing is superseded or rewritten for it — but its ground
             moved, so it comes up to be checked. Carded one layer at a time like an
             overturn: not while a memory between it and the source (the one resting on
             the source included) is live, current, on the timeline and has not dealt with
             that revision itself. Confirmed per line and revision as a `basis_revised`
             record. Only a revision travels this way; a withdrawal or deletion is the
             mandatory `gone` road above, which hides.
  disputed   an open `{kind: disputed, of, by, at, change, note?}` record: the owner said
             on the panel that a MIND entry (the model's own judgement) is wrong, with her
             note when she left one (`DISPUTED`; web/loci_detail.py, 内容错了). Nothing is
             rewritten for her: the model decides whether to rewrite, put away or keep it.
             One hangs at a time (`disputed_open`).

🔴 The body of a memory standing on a withdrawn or deleted source is **never handed back**
   here: only its id, each failed basis and its state, and the sources that are still
   good. With some left, it can be rewritten from those; with none, it can only be put
   away. Asking the model to review it must not put the material it may no longer use back
   in front of it. "Keep as is" is refused for such a memory for the same reason. The read
   gate withholds these memories on every other road (core/visibility.py, `source_gone`).

Which memories the block may show at all is the gate's: the `edited` road for panel
corrections, the `invalidation` road for the rest (core/visibility.py).

Exports: FIELD · OVERTURN · SOURCE_REVISED · BASIS_REVISED · EDITED · DISPUTED ·
         CONFIRMED · CONFIRMED_AT ·
         SOURCE_GONE · SOURCE_HELD · SOURCE_RESTORED · RESTORED_READ_LINE · CLEARED ·
         HELD_WORD · NOTE · NOTE_MAX · NOTE_SHOWN · records ·
         is_open · open_records · gone_records · disputed_open · dispute_record ·
         library_lookup ·
         edit_confirmed · SourceFindings · source_findings · waiting_on_overturn ·
         state_word · why_words · confirm · block
========================================
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

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
SOURCE_HELD = "source_held"
SOURCE_RESTORED = "source_restored"
BASIS_REVISED = "basis_revised"  # a revision behind what it is derived from, confirmed
DISPUTED = "disputed"            # the owner said on the panel that this judgement is wrong
NOTE = "note"                    # a disputed record's words from the owner
NOTE_MAX = 500
CLEARED = "cleared"              # a source_gone record's word once the body was cleared
HELD_WORD = "held"               # a source_held record's word, and a held source's

# The line a read by id puts above an entry carrying an open source_restored record
# (tools/recall/_read_by_id.py, tools/recall/original.py); `{q}` is its id.
RESTORED_READ_LINE = ("⚠️依据的来源撤回或删除过、现在恢复了；这条是从站在它上面的记忆派生的，还没复核，"
                      "复核前别的路上都不出现。看过照留就 trace(bucket_id=\"{q}\", "
                      "invalidation=\"confirmed\")，要改就 regrow 重写，不要了就 trace(bucket_id=\"{q}\", delete=True)。")

_STATE_WORD = {_src.WITHDRAWN: "已撤回", _src.DELETED: "已删除", CLEARED: "正文已清",
               HELD_WORD: "宿主说已撤回或删除，等变化通知确认"}


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


def disputed_open(meta) -> list[dict]:
    """The open `disputed` records: the owner said this judgement is wrong and the model
    has not dealt with it yet."""
    return open_records(meta, DISPUTED)


def dispute_record(bid: str, note: str, at: str) -> dict:
    """The record the panel's 内容错了 hangs on a MIND entry. `change` names the act (the
    panel, when, and a token of its own), so a later dispute after this one is settled is
    a record of its own (the store keeps one record per kind and change)."""
    rec = {"kind": DISPUTED, "of": bid, "by": "user", "at": at,
           "change": f"panel:{at}:{uuid.uuid4().hex[:8]}"}
    note = str(note or "").strip()[:NOTE_MAX]
    if note:
        rec[NOTE] = note
    return rec


def _revision_confirmed(meta, of: str, by: str) -> bool:
    """This line at this revision looked at and kept, as a source of its own or behind
    what it is derived from."""
    return _confirmed(meta, SOURCE_REVISED, of, by) or _confirmed(meta, BASIS_REVISED, of, by)


def library_lookup(registry):
    """id -> metadata (archive included) from the registry's library, or None when the
    registry has none: the walk to what a memory is derived from."""
    read = getattr(getattr(registry, "store", None), "meta_of", None)
    return read if callable(read) else None


def state_word(state: str) -> str:
    return _STATE_WORD.get(state, state)


# How much of the owner's note on a disputed judgement 依据变了的 quotes; a read by id
# shows it whole (tools/recall).
NOTE_SHOWN = 80


def why_words(item: dict) -> list[str]:
    """Why one 依据变了的 item (a `block` item) is there, one phrase per reason, in the
    words breath shows the model after its title (tools/breath/awaken) and the panel shows
    under it. A disputed record's `text` is the owner's note, quoted up to `NOTE_SHOWN`;
    without one, the phrase stops at the day."""
    from .profile import short_id   # lazy: profile imports this module

    why: list[str] = []
    if item.get("edited"):
        why.append("人在面板上改过，你还没看")
    for r in item.get("disputed") or []:
        note = str(r.get("text") or "")
        if len(note) > NOTE_SHOWN:
            note = note[:NOTE_SHOWN] + "…"
        why.append(f"人在面板上说这条不对（{str(r.get('at') or '')[5:10]}）"
                   + (f"：「{note}」" if note else ""))
    for r in item.get("overturned") or []:
        why.append(f"它站着的 {short_id(r['of'])} 被 {short_id(r['by'])} 推翻了（{r['at'][5:10]}）")
    for r in item.get("revised") or []:
        why.append(f"来源 {r['source']} 出了新版本（{r['revision'][:16]}）")
    for r in item.get("basis_revised") or []:
        why.append(f"它站着的 {short_id(r['via'])} 的来源 {r['source']} 出了新版本"
                   f"（{r['revision'][:16]}）")
    for r in item.get("restored") or []:
        why.append(f"来源 {r['source']} 撤回或删除过、现在恢复了，这条是从站在它上面的"
                   "记忆派生的，等你看过才回来")
    if item.get("failed"):
        failed = "、".join(f"{r['source']} {state_word(r['state'])}" for r in item["failed"])
        remaining = item.get("remaining") or []
        left = (f"还剩 {'、'.join(remaining)}：只凭它们重写" if remaining
                else "一条来源都不剩：只能收起来")
        why.append(f"依据 {failed}，正文不给了；{left}")
    return why


@dataclass
class SourceFindings:
    """What the registry says about a memory's sources.

    failed     [(string form, state)] — withdrawn or deleted: may no longer be used
    revised    [(identity string, the newer revision)] — not confirmed at that revision; for
               a run, the identity of the line inside it that was revised
    remaining  [string form] — the sources still good to stand on
    restored   [(string form, when)] — restored after a withdrawal or deletion; this memory
               was derived from what rested on it and waits for review (open
               `source_restored` records)
    upstream   [(identity string, the newer revision, the id of the memory resting on it)]
               — a source behind a memory this one is derived from, revised and not
               confirmed here; filled in only when the caller gives the walk a lookup"""
    failed: list = field(default_factory=list)
    revised: list = field(default_factory=list)
    remaining: list = field(default_factory=list)
    restored: list = field(default_factory=list)
    upstream: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.failed or self.revised or self.restored or self.upstream)


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


def _run_revisions(rec: dict, registry) -> list[tuple[str, str]]:
    """[(line identity, its newer revision)] for the lines of a run whose newest announced
    revision is not the one the record adopted (core/_sources.SourceRegistry.run_revisions)."""
    return [(str(line), newer) for line, newer, _mine in registry.run_revisions(rec)]


def _revisions(rec: dict, registry) -> list[tuple[str, str]]:
    """[(identity, newer revision)] the registry announced past what this record adopted:
    the record itself, or for a run each line inside it."""
    if _src.record_id(rec).through:
        return _run_revisions(rec, registry)
    newer = _newer_revision(rec, registry)
    return [(str(_src.record_id(rec)), newer)] if newer else []


def _own_revisions(meta, registry) -> list[tuple[str, str]]:
    """Every revision announced past what a memory's own sources adopted, confirmed or
    not; a source withdrawn, deleted or held is not counted (that is the `gone` road)."""
    out: list[tuple[str, str]] = []
    for raw in _src.basis_records(meta):
        try:
            [rec] = _src.normalize_sources([raw])
        except (_src.SourceRecordError, ValueError):
            continue
        if registry.read_state(rec) in (_src.WITHDRAWN, _src.DELETED, _src.HELD):
            continue
        out.extend(_revisions(rec, registry))
    return out


def _settled_on(meta, of: str, by: str) -> bool:
    """Has a memory between a derived one and a revised source dealt with that revision —
    regrown, put away, off the timeline, or confirmed it — so what stands on it is next?"""
    return (_V.state_of(meta) != _V.LIVE or bool(str(meta.get("superseded_by") or "").strip())
            or not _V.timeline_kind(meta) or _revision_confirmed(meta, of, by))


class _Upstream:
    """The walk from a memory to what it is derived from, read for revised sources. One
    per call over the library: what each memory's own sources say is read once."""

    def __init__(self, registry, lookup):
        self.registry = registry
        self.lookup = lookup
        self._own: dict[str, list] = {}

    def _own_of(self, bid: str, meta) -> list:
        if bid not in self._own:
            self._own[bid] = _own_revisions(meta, self.registry)
        return self._own[bid]

    def of(self, meta) -> list[tuple[str, str, str]]:
        """[(identity, newer, holder id)] for `meta`: each revision behind something it is
        derived from that it has not confirmed, and that every memory on the way to it
        (the holder included) has settled — the nearest unsettled layer is carded
        first. Each memory is walked once, by the first way reached (`read_from_ids`
        order), so a lineage that joins again costs nothing more."""
        found: dict[tuple[str, str], str] = {}
        seen = {str((meta or {}).get("id") or "")}

        def walk(m, chain: tuple) -> None:
            for pid in read_from_ids(m):
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                pm = self.lookup(pid)
                if not isinstance(pm, dict):
                    continue
                here = chain + (pm,)
                for of, newer in self._own_of(pid, pm):
                    key = (of, newer)
                    if (key in found or _revision_confirmed(meta, of, newer)
                            or not all(_settled_on(a, of, newer) for a in here)):
                        continue
                    found[key] = pid
                walk(pm, here)

        walk(meta or {}, ())
        return [(of, newer, via) for (of, newer), via in found.items()]


def gone_records(meta) -> list[dict]:
    """The open `source_gone` and `source_held` records: a source behind this memory was
    withdrawn or deleted (written when the host's change was applied), or a host serving
    its original said so and the ordered change has not settled it yet."""
    return [r for r in open_records(meta) if r.get("kind") in (SOURCE_GONE, SOURCE_HELD)]


def source_findings(meta, registry, *, lookup=None, upstream: "_Upstream | None" = None
                    ) -> SourceFindings:
    """Each of the memory's source records, read against the registry (a run by every
    line it holds), and every open `source_gone` record (a source behind a memory it
    stands on, or one whose body was cleared) and open `source_restored` record. A record
    that no longer reads as one is skipped (it names nothing the registry could answer
    for). With `lookup` (id -> metadata, archive included) or a walker already built over
    one (`upstream`), the sources behind what it is derived from are read too
    (`SourceFindings.upstream`)."""
    out = SourceFindings()
    gone = gone_records(meta)
    for r in gone:
        state = (CLEARED if r.get("cleared") else
                 HELD_WORD if r.get("kind") == SOURCE_HELD else str(r.get("by") or ""))
        out.failed.append((str(r.get("of") or ""), state))
    failed_keys = {str(r.get("of") or "") for r in gone}
    out.restored = [(str(r.get("of") or ""), str(r.get("at") or ""))
                    for r in open_records(meta, SOURCE_RESTORED)]
    if registry is None:
        return out
    for raw in _src.basis_records(meta):
        try:
            [rec] = _src.normalize_sources([raw])
        except (_src.SourceRecordError, ValueError):
            continue
        text = _src.record_string(rec)
        state = registry.read_state(rec)
        if state in (_src.WITHDRAWN, _src.DELETED, _src.HELD):
            if str(_src.record_id(rec)) not in failed_keys:
                out.failed.append((text, HELD_WORD if state == _src.HELD else state))
            continue
        out.remaining.append(text)
        for of, newer in _revisions(rec, registry):
            if not _confirmed(meta, SOURCE_REVISED, of, newer):
                out.revised.append((of, newer))
    if upstream is None and lookup is not None:
        upstream = _Upstream(registry, lookup)
    if upstream is not None and not out.failed:
        own = set(out.revised)
        out.upstream = [u for u in upstream.of(meta) if u[:2] not in own]
    return out


def waiting_on_overturn(meta) -> bool:
    """Live, current, and carrying an open overturn record: this memory itself still has
    to be dealt with, and what stands on it waits."""
    return (_V.state_of(meta) == _V.LIVE and not str(meta.get("superseded_by") or "").strip()
            and bool(open_records(meta, OVERTURN)))


def confirm(meta, registry, *, edited: bool, today: str, lookup=None) -> tuple[list[dict], str]:
    """The records `trace(invalidation="confirmed")` writes: every open record with
    `confirmed_at` = `today` (a `disputed` one included: looked at, kept), and one
    confirmed record for each source revision, each revision behind what it is derived
    from (`basis_revised`) and a panel correction (`edited`) looked at now. `lookup`
    reads what it is derived from; the registry's library when omitted. Returns (the
    whole new list, refusal). The list is empty when there was nothing to confirm."""
    found = source_findings(meta, registry,
                            lookup=lookup if lookup is not None else library_lookup(registry))
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
    for identity, newer, _via in found.upstream:
        out.append({"kind": BASIS_REVISED, "of": identity, "by": newer, "at": stamp,
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
         revised: [{source, revision}], restored: [{source, at}],
         basis_revised: [{source, revision, via}], disputed: [{at, text}],
         remaining: [source]}

    `text` is None for a memory standing on a withdrawn or deleted source; `remaining` is
    filled in only then (the sources a rewrite may stand on). `basis_revised.via` is the
    memory resting on the revised source; `disputed.text` is the owner's note ("" when
    she left none).

    Under a read scope (`scope`, a core.scope.ScopeView) the gate leaves out what the
    request may not read, and an overturn is told only when the request may read the
    version that overturned it: its id is not named otherwise; a revision behind what it
    is derived from, only when the request may read the memory resting on it."""
    from .profile import edited_by_user, entry_label, short_id  # lazy: profile imports this module

    items: dict[str, dict] = {}

    def item(bid: str, meta: dict, content: str) -> dict:
        if bid not in items:
            items[bid] = {"id": bid, "short": short_id(bid), "text": entry_label(meta, content),
                          "edited": False, "overturned": [], "failed": [], "revised": [],
                          "restored": [], "basis_revised": [], "disputed": [],
                          "remaining": []}
        return items[bid]

    for e in edited_by_user(all_buckets, scope=scope):
        item(e["id"], e["meta"], e["content"])["edited"] = True

    rows = [(str((b.get("metadata") or {}).get("id") or b.get("id") or ""),
             b.get("metadata") or {}, str(b.get("content") or "")) for b in all_buckets]
    waiting = {bid for bid, meta, _c in rows if bid and waiting_on_overturn(meta)}
    upstream = None
    if registry is not None:
        listed = {bid: meta for bid, meta, _c in rows if bid}
        more = library_lookup(registry)

        def lookup(bid: str):
            got = listed.get(bid)
            return got if got is not None or more is None else more(bid)
        upstream = _Upstream(registry, lookup)
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
        found = source_findings(meta, registry, upstream=upstream)
        basis = found.upstream
        if scope is not None:
            basis = [u for u in basis if scope.permits_id(u[2])]
        disputed = disputed_open(meta)
        if not (overturned or found.failed or found.revised or found.restored or basis
                or disputed):
            continue
        it = item(bid, meta, content)
        it["overturned"] = [{"of": str(r.get("of") or ""), "by": str(r.get("by") or ""),
                             "at": str(r.get("at") or "")} for r in overturned]
        it["failed"] = [{"source": s, "state": st} for s, st in found.failed]
        it["revised"] = [{"source": s, "revision": rv} for s, rv in found.revised]
        it["restored"] = [{"source": s, "at": at} for s, at in found.restored]
        it["basis_revised"] = [{"source": s, "revision": rv, "via": via}
                               for s, rv, via in basis]
        # The owner's note rides under `text`: the breath snapshot keeps no text
        # (core/breath_snapshot.skeleton) and reads it again from the entry (`relabel`).
        it["disputed"] = [{"at": str(r.get("at") or ""), "text": str(r.get(NOTE) or "")}
                          for r in disputed]
        if found.failed:
            it["text"] = None
            it["remaining"] = list(found.remaining)
    return list(items.values())
