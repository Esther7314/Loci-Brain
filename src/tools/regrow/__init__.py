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

Four things fixed during review:
- The whole flow is wrapped in a _keyed_turn on the old id (a cross-process
  lock): two concurrent regrows of the same entry can no longer fork it
- Archived buckets are rejected outright (update() will not write an archived
  bucket, so forcing it would leave half a chain — trace restore first)
- Sources appended via from are each checked for existence; when the chain is
  full they go in one at a time and however many fit, fit
- Backfill self-healing: backfill_sweep also recognises source_tool=regrow (that
  change lives over in rooms_path)

Exports: dispatch(bucket_id, text, v, a, from_) -> str
========================================
"""

from core import _fold as _F           # fold's bones: regrow is its n=1 case
from .. import _runtime as rt
from core._bigevent import is_big as _is_big
from .._common import _keyed_turn
# is_mind_room is no longer used to keep events out (that gate was removed; see
# the epitaph inside regrow below)
# from core._rooms import is_mind_room
from utils import read_from_ids
from ..grow.rooms_path import _normalize_from

_CHAIN_LIMIT = 64  # the underlying ceiling on triggered_by


async def dispatch(bucket_id: str = "", text: str = "", v=-1, a=-1, from_=None) -> str:
    bucket_id = str(bucket_id or "").strip()
    text = str(text or "")  # stored verbatim: never strip the body
    if not bucket_id:
        return "regrow 要换谁的版本？传旧版的 bucket_id。"
    if not text.strip():
        return "text 不能为空——新版的完整正文（不是补丁，是整条重写后的样子）。"

    # v/a follows the same rule as mind: I set them myself, never outsourced
    try:
        v = float(v)
        a = float(a)
    except (TypeError, ValueError):
        return "v/a 必填：新版此刻的坐标是你自己打的（0~1）。"
    if not (0 <= v <= 1 and 0 <= a <= 1):
        return f"v/a 必须在 0~1 之间（收到 v={v}, a={a}）。"

    # Appended sources: normalise first, then check each one exists
    extra, from_err = _normalize_from(from_)
    if from_err:
        return from_err
    if extra:
        missing = []
        for fid in extra:
            if not await rt.bucket_mgr.get_including_archive(fid):
                missing.append(fid)
        if missing:
            return f"from 里这些 id 不存在：{', '.join(missing)}。"

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

        # The source chain: inherit the old chain, then append new sources one at
        # a time — however many fit, fit
        inherited = read_from_ids(old_meta)   # from wins; triggered_by is the compatible fallback
        sources = list(dict.fromkeys(inherited))
        dropped: list[str] = []
        for fid in (extra or []):
            if fid in sources:
                continue
            if len(",".join(sources + [fid])) <= _CHAIN_LIMIT:
                sources.append(fid)
            else:
                dropped.append(fid)

        # Re-versioning a period: the marker and the span both have to come
        # along, or the new version stops being a period — **the span is copied
        # verbatim from the old one**.
        # Time is inherited from the old version, and there is no opening here
        # either: `when` is "where this hangs in time", metadata rather than
        # content — changing a period's span or filling in an event's date both
        # go through trace(bucket_id=..., when=...).
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
        new_id, report = await _F.save_gist(
            text, room, v, a, cover, when=new_when if is_big else "",
            from_ids=sources, supersedes=bucket_id, test_data=is_test)

    try:
        await rt.bucket_mgr.touch_many(sources)  # a new version = remembering its sources again
    except Exception:
        pass

    mark = "◈时期" if is_big else "🌱regrow"
    span = f" {new_when}" if is_big and new_when else ""
    out = f"{mark} {bucket_id} → {new_id}  {room}{span}（旧版留档不浮现，id 直查仍能看）"
    if is_big and new_when:
        # A period only changes its name and its edges — no member list moves
        # with it. Count who falls inside the span right now, purely to give a
        # sense of scale.
        _t0, _t1, _e = _F.check_span(new_when)
        if not _e:
            out += (f"\n（范围内现在有 {len(await _F.span_members(_t0, _t1))} 条——"
                    "**现场数的**，不落盘；时期只起名字，一条都没被压住。）")
    if dropped:
        out += f"\n⚠️ 来源链满：这几个没挂上 {', '.join(dropped)}（继承链优先）"
    if report["链没写全"]:
        out += "\n⚠️ 版本链没写全（supersedes/superseded_by 有一半失败）——把这条报给AI查"
    tail = _F.format_report(report)
    if tail:
        out += "\n" + tail
    return out
