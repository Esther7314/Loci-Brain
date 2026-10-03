"""
========================================
tools/regrow/ — a new version of one memory
========================================

The same memory has a new version (A -> A'): the new one takes over, the old one
stays on file and no longer surfaces alongside it.

🔴 **This entry point is `fold`'s n=1 special case**: underneath it runs the same
code in `tools/_fold.py` (the new bucket gets `cover=[old id]`, the old one gets
`covered_by`), and neither the version chain nor anything said to the caller
changed. The difference between "I changed my mind" and "I summed several up"
lives **in the body text**, not in the gesture.

The boundary with the other two acts (never mix them):
- grow(kind="mind"): **distil something new** out of several event/mind entries
  -> one more item in the pool
- **regrow**: the same memory **grows a new version** -> the new one takes over,
  the old sinks but is still there
- trace: correct or delete. ⚠️ Its content parameter **overwrites the body
  directly** — never use it for a version change

🔴 **The caller says what kind of change it is** (`mode`, required; nothing is written
without it):
- `supplement`: the old version was right as far as it went. Whatever grew out of it
  (`prov` pointing at it) still stands and is not touched.
- `overturn`: the old version was wrong. Everything that grew out of it, and out of
  that, gets an `invalidation` record (`{kind: "overturn", of: old id, by: new id, at}`)
  appended — the walk follows `referenced_by` layer by layer. The descendants keep
  surfacing as before; the mark is shown wherever one is read by id, and breath's
  「依据变了的」 block reads it.

Four things fixed during review:
- The whole flow is wrapped in a _keyed_turn on the old id (a cross-process
  lock): two concurrent regrows of the same entry can no longer fork it
- Archived buckets are rejected outright (update() will not write an archived
  bucket, so forcing it would leave half a chain — trace restore first)
- Sources appended via from go through grow's _normalize_from (a memory has to
  exist, the host's line ids are quoted); when the chain is full they go in one
  at a time and however many fit, fit
- Backfill self-healing: backfill_sweep also recognises source_tool=regrow (that
  change lives over in rooms_path)

The return also says which old views the new version runs into (回望,
tools/_write_returns.py); its own lineage — what it stands on, the versions it replaces,
what grew out of the old version — is not among them.

The old version's `sources` come across with its standing; new ones (`sources=`) are
checked like any write's and added. A source withdrawn or deleted since the old version
was written is left behind, quoted line and all, and the receipt says so.

Exports: dispatch(bucket_id, text, v, a, from_, mode, sources) -> str · MODES
========================================
"""

from core import _fold as _F           # fold's bones: regrow is its n=1 case
from core import _reconsolidation as _R
from core import _sources as _src
from .. import _runtime as rt
from core._bigevent import is_big as _is_big
from .._common import _keyed_turn, read_scope, resolve_bucket_id, with_write_key
# is_mind_room is no longer used to keep events out (that gate was removed; see
# the epitaph inside regrow below)
# from core._rooms import is_mind_room
from utils import PROV_MAX_LINES, WAS_REVISION_OF, now_iso, prov_targets, read_prov
from ..grow.rooms_path import _normalize_from, check_sources
from .._write_returns import noticed

MODES = ("supplement", "overturn")
_MODE_WORD = {"supplement": "补充", "overturn": "推翻"}
_MODE_HELP = ('mode 必填：这次换版是补充还是推翻？mode="supplement"——旧版没说错，只是说少了'
              '，从它长出来的东西照旧；mode="overturn"——旧版说错了，从它长出来的东西依据'
              '变了，会被逐条标上。没说清楚之前什么都不写。')


async def _mark_overturned(old_id: str, new_id: str) -> tuple[list[str], list[str]]:
    """Append one `invalidation` record to every memory that grew out of `old_id`,
    and out of those, layer by layer (`referenced_by`, the reverse of `prov`).
    Returns (marked, could not write). The walk never enters the new version or a
    memory it has already seen, so a `prov` loop ends.

    Each record is appended to the list as it is on disk, under that entry's lock, so two
    overturns reaching one descendant at once both leave their record. A descendant that
    fails — a write refused or raising — is reported as not marked and the walk goes on:
    the new version is already written, so a retry would be refused, and the receipt is
    the only place left to say which ones still lack the record."""
    record = {"kind": "overturn", "of": old_id, "by": new_id, "at": now_iso()}

    def append(meta: dict) -> dict:
        return {"invalidation": list(meta.get("invalidation") or []) + [record]}

    seen = {old_id, new_id}
    frontier = [old_id]
    marked: list[str] = []
    failed: list[str] = []
    while frontier:
        next_layer: list[str] = []
        for parent in frontier:
            try:
                children = await rt.bucket_mgr.referenced_by(parent)
            except Exception as e:  # noqa: BLE001 - reported below as the walk stopping short
                rt.logger.warning(f"regrow overturn: {parent} 的下游没列出来: {e}")
                failed.append(parent)
                continue
            for cid in children:
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                next_layer.append(cid)
                try:
                    ok = await rt.bucket_mgr.update(cid, revise=append)
                except Exception as e:  # noqa: BLE001 - one descendant must not stop the rest
                    rt.logger.warning(f"regrow overturn: {cid} 的记号没写上: {e}")
                    ok = False
                (marked if ok else failed).append(cid)
        frontier = next_layer
    return marked, failed


def _carry_sources(old_meta: dict) -> tuple[list[dict], list[str]]:
    """The old version's source records a new version may stand on, and the string forms
    of those it may not. A source withdrawn, deleted or held since the old version was
    written is not carried (nor its quoted line), nor a run whose lines were never
    registered: a new version is a new write, and the registry refuses that material to
    every write. A record that no longer reads as one is left behind the same way."""
    registry = getattr(rt.bucket_mgr, "sources", None)
    kept: list[dict] = []
    dropped: list[str] = []
    for raw in old_meta.get(_src.SOURCES_FIELD) or []:
        try:
            [rec] = _src.normalize_sources([raw])
        except (_src.SourceRecordError, ValueError):
            continue
        sid = _src.record_id(rec)
        state = registry.state_of(sid) if registry is not None else _src.ACTIVE
        if (state in (_src.WITHDRAWN, _src.DELETED, _src.HELD)
                or (registry is not None and not registry.order_known(sid))):
            dropped.append(_src.record_string(rec))
        else:
            kept.append(rec)
    return kept, dropped


async def dispatch(bucket_id: str = "", text: str = "", v=-1, a=-1, from_=None,
                   mode: str = "", sources=None) -> str:
    # One write call, one write key (core/_sources.SourceRegistry.run_once).
    return await with_write_key(
        _src.current_write_key(),
        lambda: _regrow(bucket_id=bucket_id, text=text, v=v, a=a, from_=from_, mode=mode,
                        sources=sources),
        op="regrow")


async def _regrow(bucket_id: str = "", text: str = "", v=-1, a=-1, from_=None,
                  mode: str = "", sources=None) -> str:
    bucket_id = str(bucket_id or "").strip()
    text = str(text or "")  # stored verbatim: never strip the body
    mode = str(mode or "").strip().lower()
    if not bucket_id:
        return "regrow 要换谁的版本？传旧版的 bucket_id。"
    if not text.strip():
        return "text 不能为空——新版的完整正文（不是补丁，是整条重写后的样子）。"
    if mode not in MODES:
        return _MODE_HELP if not mode else f"mode 只认 supplement / overturn（收到 {mode!r}）。{_MODE_HELP}"

    # v/a follows the same rule as mind: I set them myself, never outsourced
    try:
        v = float(v)
        a = float(a)
    except (TypeError, ValueError):
        return "v/a 必填：新版此刻的坐标是你自己打的（0~1）。"
    if not (0 <= v <= 1 and 0 <= a <= 1):
        return f"v/a 必须在 0~1 之间（收到 v={v}, a={a}）。"

    # The handle breath prints is enough to name the old version. Resolved before
    # the lock, so the lock key and every message below carry the full id.
    bucket_id, id_err = await resolve_bucket_id(bucket_id)
    if id_err:
        return id_err

    # Appended sources: resolved, checked and typed by the one normaliser
    extra, from_err = await _normalize_from(from_)
    if from_err:
        return from_err
    # New sources are checked like any write's; the old version's come across below.
    new_sources, extra, source_notes, sources_err = await check_sources(
        sources, extra, exclude={bucket_id})
    if sources_err:
        return sources_err

    # ---- check -> create -> write both link directions, all inside a
    # cross-process lock on the old id ----
    async with _keyed_turn(f"regrow-{bucket_id}"):
        old = await rt.bucket_mgr.get(bucket_id)
        if not old:
            arch = await rt.bucket_mgr.get_including_archive(bucket_id)
            if arch:
                # update() will not write an archived bucket; forcing it would
                # leave half a chain behind
                return (f"{bucket_id} 在归档区。先 trace(bucket_id=\"{bucket_id}\", restore=True) "
                        "把它捞回来，再 regrow。")
            return f"找不到 {bucket_id}。"
        old_meta = old.get("metadata", {}) or {}
        # The room is inherited from the old version and **there is no opening
        # here to change it**. The axis: regrow changes the content itself, trace
        # changes metadata. A room is metadata — it does not affect what this
        # memory says, and changing it is more like correction fluid than a new
        # version. Moving rooms goes through trace(bucket_id=..., room=...).
        # ⚰️ This function briefly accepted `room`, on the grounds that "moving
        #    is part of re-versioning". That is one field with two entry points —
        #    exactly the disease killed off deliberately elsewhere in this system.
        room = str(old_meta.get("room") or "")
        is_big = _is_big(old_meta)
        # 🔴 There used to be a gate here that only let through thinking (the two
        #    MIND rooms) and periods; ordinary events were kept out, on the
        #    grounds that "what happened should not be rewritten". **It was
        #    removed.**
        #
        #    Not because that principle is wrong, but because **this gate was not
        #    what protected it**: regrow never edits the original anyway. The new
        #    version takes over and the old one stays on file (superseded_by links
        #    them, and a direct id lookup still returns it verbatim).
        #    The real cost was "one thought, three different gestures" —
        #      the thinking changed -> regrow · the event was misremembered ->
        #      store a correction alongside · I remembered wrong -> fold one over
        #    — which cannot be explained, and jams you on the spot the moment you
        #    sit down to write the tool description.
        #
        #    After the removal: **regrow = this entry has a new version** (used
        #    for thinking, events and periods alike), **fold = fold it up**
        #    (several collapsed into one sentence / naming a stretch of days).
        #    Two tools, two sentences, no overlap left — and with it goes the
        #    question of whether regrow and fold do the same thing.
        #
        #    If "I once remembered this wrong" matters in itself, it deserves to
        #    be a real piece of thinking ("when I am tired I mix up dates"), not
        #    to be implied by two events sitting side by side. Insight belongs
        #    with insight, facts with facts.
        if old_meta.get("superseded_by"):
            return (f"{bucket_id} 已经被 {old_meta.get('superseded_by')} 换过版了——"
                    "在最新版上 regrow，别从旧版分叉。")

        # Where the new version came from: the old version's sources and quotes come
        # across, then the new ones one at a time — however many fit, fit — and one
        # wasRevisionOf line names the old version, with its place kept so a full chain
        # can never push it out. The old version's own revision line is its link in the
        # chain, not something it came from, so it is not carried: each version names
        # only the one it replaced. The chain fields (supersedes / superseded_by) are
        # written by save_gist as always; the revision line is the typed record beside
        # them.
        # The old version's source records come across with their quoted lines, the new
        # ones after them; a source the registry now refuses stays behind with its line.
        kept_sources, dropped_sources = _carry_sources(old_meta)
        try:
            merged_sources = _src.normalize_sources(kept_sources + list(new_sources))
        except _src.SourceRecordError as e:
            return f"sources 不对：{e.zh}。"
        revision = {"rel": WAS_REVISION_OF, "target": bucket_id}
        carried = [ln for ln in read_prov(old_meta)
                   if ln["rel"] != WAS_REVISION_OF and ln["target"] not in dropped_sources]
        lines = list({(ln["rel"], ln["target"]): ln
                      for ln in carried + list(extra or [])}.values())
        room_left = PROV_MAX_LINES - 1
        dropped = [ln["target"] for ln in lines[room_left:]]
        prov = lines[:room_left] + [revision]

        # Re-versioning a period: the marker and the span both have to come
        # along, or the new version stops being a period — **the span is copied
        # verbatim from the old one**, and it goes in at creation because save_gist
        # reads a `when` as "this is a period".
        # An event's or a want's `when` is inherited too, but by save_gist itself,
        # together with the rest of the old version's standing (status, weight,
        # subjects, tags…) — passing it here would turn the event into a period.
        # There is no opening to change time here either: `when` is "where this
        # hangs in time", metadata rather than content — changing a period's span
        # or filling in an event's date both go through trace(bucket_id=..., when=...).
        # 📌 Periods used to be the exception here ("the span is half its body, so
        #    it counts as content"). That exception was dropped too: a point and a
        #    span are the same kind of thing, both "where it hangs". A period's
        #    actual content is **the name**.
        new_when = str(old_meta.get("when") or "") if is_big else ""

        is_test = bool(old_meta.get("provenance", {}).get("kind") == "test"
                       if isinstance(old_meta.get("provenance"), dict) else False)
        # ---- The write itself is handed to fold's bones (tools/_fold.py) ----
        # 🔴 regrow is fold's **n=1 special case**: the new bucket gets
        #    cover=[old id], the old one covered_by=new id, and the version chain
        #    (supersedes/superseded_by/dont_surface) is unchanged.
        #    The difference between "I changed my mind" and "I summed several up"
        #    is in the body, not in the gesture — so the same code runs
        #    underneath, and this entry point only preserves the old signature
        #    and the old wording.
        # Re-versioning a period **only changes text/v/a/when**: a period is a
        # pure naming layer, with no member list to inherit and no span to
        # re-parse — `cover` holds nothing but the old version (the version chain).
        # ⚠️ `save_gist` clears cover as soon as it sees a when, so the version
        #    chain is written through `supersedes=`, not through cover (which is
        #    why report["cover"] below is necessarily empty for a period — do not
        #    report counts from it).
        cover = [bucket_id]
        if is_big and new_when:
            _t0, _t1, span_err = _F.check_span(new_when)
            if span_err:
                return span_err
        # The library as it stood before the new version, for the return's look-back: the
        # old views it may run into, without the new version itself.
        try:
            library = await rt.bucket_mgr.list_all(include_archive=False)
        except Exception as e:  # noqa: BLE001 - without it the return only skips the look-back
            rt.logger.warning(f"regrow: library not listed, no look-back this time: {e}")
            library = None
        new_id, report = await _F.save_gist(
            text, room, v, a, cover, when=new_when if is_big else "",
            prov=prov, sources=merged_sources, supersedes=bucket_id, test_data=is_test)

        # Overturned: whatever grew out of the old version is told so, still under the
        # lock on the old id, so the marks land before anyone else can re-version it.
        # A supplement touches nothing downstream.
        marked: list[str] = []
        unmarked: list[str] = []
        if mode == "overturn":
            marked, unmarked = await _mark_overturned(bucket_id, new_id)

    try:
        await rt.bucket_mgr.touch_many(prov_targets(prov), road="regrow")  # a new version = remembering its sources again
    except Exception:
        pass

    # Every descendant is marked; the receipt names and counts only those the request may
    # read (a read scope, core/scope.py).
    view = await read_scope()
    if view is not None:
        marked = [m for m in marked if view.permits_id(m)]
        unmarked = [m for m in unmarked if view.permits_id(m)]
    mark = "◈时期" if is_big else "🌱regrow"
    span = f" {new_when}" if is_big and new_when else ""
    out = (f"{mark}·{_MODE_WORD[mode]} {bucket_id} → {new_id}  {room}{span}"
           "（旧版留档不浮现，id 直查仍能看）")
    if mode == "overturn":
        if marked:
            out += (f"\n依据变了的：从旧版长出来的 {len(marked)} 条已标上"
                    f"（{', '.join(marked[:6])}{'…' if len(marked) > 6 else ''}）"
                    "——它们照常冒头，id 直查能看见这行记号。")
        else:
            out += "\n（没有东西从旧版长出来过，不用标。）"
        if unmarked:
            out += f"\n⚠️ 这几条的记号没写上：{', '.join(unmarked[:6])}——把这条报给AI查。"
    if is_big and new_when:
        # A period only changes its name and its edges — no member list moves
        # with it. Count who falls inside the span right now, purely to give a
        # sense of scale.
        _t0, _t1, _e = _F.check_span(new_when)
        if not _e:
            members = [m for m in await _F.span_members(_t0, _t1)
                       if view is None or view.permits_id(m)]
            out += (f"\n（范围内现在有 {len(members)} 条——"
                    "**现场数的**，不落盘；时期只起名字，一条都没被压住。）")
    if dropped:
        out += f"\n⚠️ 来源链满：这几个没挂上 {', '.join(dropped)}（继承链优先）"
    if dropped_sources:
        out += (f"\n新版没带上这几条来源：{', '.join(dropped_sources)}"
                "——宿主那边已经撤回或删除了（或说过撤回、正等确认，或是一段没交过有哪几行的），"
                "旧版留档照旧挂着。")
    out += "".join("\n" + note for note in source_notes)
    if report["链没写全"]:
        out += "\n⚠️ 版本链没写全（supersedes/superseded_by 有一半失败）——把这条报给AI查"
    tail = _F.format_report(report)
    if tail:
        out += "\n" + tail
    # Old views the new version runs into. Its own lineage is not one: what it stands on,
    # the versions it replaces, and what grew out of the version it replaces.
    notes = await noticed(library, [(new_id, text)],
                          skip=_R.lineage([bucket_id, *prov_targets(prov)], library or [],
                                          down_from=[bucket_id]))
    return out + "".join("\n" + line for line in notes)
