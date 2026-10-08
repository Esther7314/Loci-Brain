# -*- coding: utf-8 -*-
"""
tools/recall/_read_by_id.py — a read by id: one entry whole, with everything it links to

recall(query=<id>) is the "click through to the original" door: the body verbatim, its
metadata, its sources (each with its gist; a quoted line is the host's, not a memory),
what it covers, who cites it, and its state said out loud when it is not live. Every
linked entry goes through the gate's `read` road and the request's read scope.

tools/recall/core.py re-exports DREAMT_MARK.
"""

import re

from core import _fold as _F          # fold / gist: what is covered no longer surfaces on its own
from core import visibility as _V     # the one gate: what may be put in front of the model
from core import _bigevent as _big    # a big event: one sentence laid over a stretch of time
from core import _sources as _src     # outside material: source records and their registry state
from core import _usage              # the usage log: what a lookup handed back
from core import runtime as rt
from utils import (HAD_PRIMARY_SOURCE, WAS_DERIVED_FROM, WAS_QUOTED_FROM, WAS_REVISION_OF,
                   panel_actor, read_prov)
from .._common import read_scope

from ._words import _HANDLE_TAG_RE


# What each provenance line is, in the read by id's 「来源:」 block.
_PROV_WORD = {WAS_DERIVED_FROM: "派生", WAS_REVISION_OF: "新版本",
              WAS_QUOTED_FROM: "引原话", HAD_PRIMARY_SOURCE: "一手来源"}

# The registry's state of a source, as the 「来源:」 block says it; active says nothing.
_SOURCE_STATE_WORD = {_src.WITHDRAWN: "⚠️撤回", _src.DELETED: "⚠️删除",
                      _src.HELD: "⚠️宿主说已撤回，等确认",
                      _src.UNREADABLE: "⚠️宿主那边读不到"}


def _source_strings(meta: dict) -> list[str]:
    """The string forms of an entry's source records, each once; a hand-edited record
    that no longer reads as one is skipped."""
    out: list[str] = []
    for rec in meta.get(_src.SOURCES_FIELD) or []:
        try:
            text = _src.record_string(rec)
        except (KeyError, TypeError):
            continue
        if text not in out:
            out.append(text)
    return out


def _source_label(target: str) -> str:
    """A quoted source as the 「来源:」 block names it: its string form, and for a run of
    lines (`…#m_0012..m_0031`) how many lines it spans when the slice store still holds
    the batch it was cut from."""
    slices = getattr(rt.bucket_mgr, "slices", None)
    if slices is None or "#" not in target:
        return target
    try:
        sid, _revision = _src.SourceId.parse(target)
    except _src.SourceRecordError:
        return target
    count = slices.run_length(sid) if sid.through else None
    return f"{target}（{count} 行）" if isinstance(count, int) else target


def _source_mark(target: str) -> str:
    """The registry's word for a quoted source, and a note when the host has revised it
    since (the entry keeps pointing at the revision it was formed on). A bare host id
    has no identity to look up and gets nothing."""
    registry = getattr(rt.bucket_mgr, "sources", None)
    if registry is None or "#" not in target:
        return ""
    try:
        sid, revision = _src.SourceId.parse(target)
    except _src.SourceRecordError:
        return ""
    known = registry.describe(sid)
    state = registry.state_of(sid)          # a run: the worst of its lines
    if not known and state == _src.ACTIVE:
        return ""
    known = known or {"revisions": []}
    marks = [_SOURCE_STATE_WORD[state]] if state in _SOURCE_STATE_WORD else []
    latest = (known["revisions"] or [{}])[-1].get("revision")
    if latest and latest != revision:
        marks.append(f"（宿主那边已改到 @{latest}，这条是按 @{revision or '原版'} 记的）")
    return ("  " + " ".join(marks)) if marks else ""


# What a read by id says first about an entry that is not live. The body still comes out
# whole — a lookup hides nothing — but never as if it were a current memory.
_STATE_LOUD = {
    _V.ARCHIVED: "⚠️在归档区：这条已经沉下去了，不是现在的记忆——下面是它沉下去之前的原文，别当成眼下的事。",
    _V.DELETED: "⚠️在归档区（已删除）：这条被删掉了，不是现在的记忆——下面是它删掉之前的原文，别当成眼下的事。",
}


# What a read by id says of an entry carrying the internally-generated mark (core/_dream
# is_marked), on its own line and on every line linking to it: it happened inside, not out
# there.
DREAMT_MARK = "💭梦或想象"


async def _linked(bucket_id: str, scope_view=None) -> tuple[dict | None, str, str] | None:
    """One entry a read by id links to — a source, what it covers, a period's member, who
    cites it — as its line shows it: (bucket or None, hint, mark).

    The gate's `read` road decides: a linked entry is always a line (its gist), never
    expanded into its body, and one that is not live carries its state out loud. One the
    request's read scope (`scope_view`) does not reach is None: no line at all, not even its id."""
    if scope_view is not None and not scope_view.permits_id(bucket_id):
        return None
    b = await rt.bucket_mgr.get_including_archive(bucket_id)
    if not b:
        return None, "", ""
    meta = b.get("metadata", {}) or {}
    verdict = _V.visible_for(meta, scope_view, road=_V.READ)
    if _V.SOURCE_GONE in verdict.reasons:
        return b, "", "  （依据的来源被撤回或删除了，正文不给）"
    if not verdict:
        return b, "", "  （不在这次能看的范围里）"
    hint = re.sub(r"^[\d\- :]+", "",
                  str(meta.get("summary") or meta.get("name") or "").strip())[:60]
    from core._dream import is_marked
    dreamt = f"  {DREAMT_MARK}" if is_marked(meta, b.get("content") or "") else ""
    waiting = "  （依据的来源恢复了，这条还等着复核）" if _V.source_restored(meta) else ""
    return b, hint, (f"  {verdict.mark}" if verdict.mark else "") + dreamt + waiting


async def read_by_id(q: str) -> str:
    """A read by id: the query is itself a full bucket_id, and this returns that entry's
    verbatim text plus all of its metadata (recall_core sends an id-shaped query here)."""
    # Under a read scope (`scope_view`) everything this read names is asked of it: an entry
    # out of scope reads like one that does not exist (resolve_bucket_id already said
    # so for the entry itself), and a linked one is left out — no line, no id, not
    # counted.
    scope_view = await read_scope()
    b = await rt.bucket_mgr.get_including_archive(q)
    _usage.offer([q], "recall.read")
    if not b:
        return f"查无此桶：{q}（id 形状但不存在——可能已物理删除或打错，不做语义联想）。"
    if b:
        meta = b.get("metadata", {}) or {}
        # A read by id is a lookup: dont_surface hides nothing, and an entry that is
        # not live still comes out whole — with its state said before anything else.
        verdict = _V.visible_for(meta, scope_view, road=_V.READ)
        if verdict.out_of_scope:
            return f"查无此桶：{q}（id 形状但不存在——可能已物理删除或打错，不做语义联想）。"
        if _V.SOURCE_GONE in verdict.reasons:
            return (f"{q} 依据的来源被撤回或删除了，正文不给。它在开口前「依据变了的」里："
                    "只凭还剩的来源重写（regrow），或者收起来（trace delete=True）。")
        if not verdict:
            return f"{q} 不在这次能看的范围里。"

        def seen(bid) -> bool:
            return scope_view is None or scope_view.permits_id(str(bid or ""))
        lines = [f"═ {q} · {str(meta.get('name') or '')}"]
        if _V.source_restored(meta):
            # Shown whole on purpose: the model reviews it from here (core/visibility.py).
            from core._invalidation import RESTORED_READ_LINE
            lines.append(RESTORED_READ_LINE.format(q=q))
        if verdict.mark:
            lines.append(_STATE_LOUD[verdict.state])
        info = []
        for k, label in (("room", "房间"), ("when", "when"), ("valence", "V"),
                         ("arousal", "A"), ("importance", "重"), ("status", "状态")):
            v = meta.get(k)
            if v not in (None, ""):
                info.append(f"{label}:{v}")
        # A tag that points at another entry by its handle (「疑似同件:abc123」) names
        # it: under a read scope only when the request may read it.
        tags_ = [str(t) for t in (meta.get("tags") or []) if not str(t).startswith("__")
                 and (scope_view is None or not (m := _HANDLE_TAG_RE.match(str(t)))
                      or scope_view.permits_handle(m.group(1)))]
        if tags_:
            info.append("标签:" + ",".join(tags_[:6]))
        if str(meta.get("card_of") or "").strip():
            info.append(f"名字卡:{str(meta['card_of']).strip()}")
        # From a dream: the entry's own mark, or an entry it stands on that carries one
        # (a gist or a thought grown from a dream note carries no field of its own).
        from core import _dream
        if _dream.is_marked(meta, b.get("content") or ""):
            info.append(f"{DREAMT_MARK}（内部生成，不是外面发生的事）")
        else:
            dreamt_roots = [r for r in await _dream.dream_roots(meta) if seen(r)]
            if dreamt_roots:
                info.append(f"{DREAMT_MARK}：依据里有梦或想象（{'、'.join(dreamt_roots[:3])}"
                            + (f" 等 {len(dreamt_roots)} 条" if len(dreamt_roots) > 3 else "")
                            + "）")
        # Sources must never be reduced to bare ids: a mind holds only the
        # product of thinking, the events live in its provenance, and reading it
        # has to bring the sources' gists along or the thinking has nothing to
        # stand on. Each line says what kind of source it is; a quoted line is the
        # host's conversation, not a memory, so nothing is looked up for it.
        # A source record of the host's material is shown by its string form, with
        # the registry's word for it when it is no longer simply there.
        src_lines: list[str] = []
        quoted: set[str] = set()
        for line in read_prov(meta):
            fid, word = line["target"], _PROV_WORD[line["rel"]]
            if line["rel"] == WAS_QUOTED_FROM:
                quoted.add(fid)
                src_lines.append(f"  ← {_source_label(fid)}  [{word}] "
                                 "（宿主对话里的原话，不在库里）" + _source_mark(fid))
                continue
            # A source that sank or was deleted still explains the thought, but
            # reading it as current would be wrong: it carries its mark.
            got = await _linked(fid, scope_view)
            if got is None:
                continue
            src, hint, mark = got
            if src:
                src_lines.append(f"  ← {fid}  [{word}] {hint}{mark}")
            else:
                src_lines.append(f"  ← {fid}  [{word}] （查无此桶——源可能被硬删过）")
        for text in _source_strings(meta):
            if text not in quoted:
                src_lines.append(f"  ← {_source_label(text)}  [来源记录] "
                                 "（宿主的材料，不在库里）" + _source_mark(text))
        if meta.get("supersedes") and seen(meta["supersedes"]):
            info.append(f"换掉了:{meta['supersedes']}")
        if meta.get("superseded_by") and seen(meta["superseded_by"]):
            info.append(f"⚠️已被换版:{meta['superseded_by']}（这是旧版）")
        # The other end of drilling down: **who covers this** (possibly
        # several — entries can cross).
        # The re-versioning case (n=1) is already stated by the superseded_by
        # line above and is not repeated here.
        _sup = str(meta.get("superseded_by") or "")
        _cbs = [c for c in _F.covers_of(meta) if c != _sup and seen(c)]
        if _cbs:
            info.append(f"⚠️被 {'、'.join(_cbs)} 盖着（不再独立冒头；搜索和这儿照样看得见）")
        lines.append(" · ".join(info))
        # A basis this memory grew out of was overturned (regrow's overturn marks
        # every descendant). The entry still counts; whoever reads it has to know
        # which ground moved, and to what. One looked at and kept as it is
        # (core/_invalidation.py, `confirmed_at`) is no longer a warning, only a note.
        for rec in (meta.get("invalidation") or []):
            if not isinstance(rec, dict) or rec.get("kind") != "overturn":
                continue
            if not (seen(rec.get("of")) and seen(rec.get("by"))):
                continue
            confirmed = str(rec.get("confirmed_at") or "")
            if confirmed:
                lines.append(f"依据变过，{confirmed[5:10]} 确认照留"
                             f"（{rec.get('of')} 被 {rec.get('by')} 推翻过）。")
            else:
                lines.append(f"⚠️依据变了：{rec.get('of')} 被 {rec.get('by')} 推翻了"
                             f"（{str(rec.get('at') or '')[:10]}）——这条站在它上面，看的时候记着。")
        # The owner said on the panel that this judgement is wrong (core/
        # _invalidation.DISPUTED): her note whole, and the three ways out.
        from core._invalidation import disputed_open, NOTE
        for rec in disputed_open(meta):
            note = str(rec.get(NOTE) or "")
            lines.append(f"⚠️{panel_actor()}在面板上说这条不对（{str(rec.get('at') or '')[:10]}）"
                         + (f"：「{note}」" if note else "")
                         + f"——认同就 regrow 改写；不认同就照留 trace(bucket_id=\"{q}\", "
                           "invalidation=\"confirmed\")，也可以说出来。")
        if src_lines:
            lines.append("来源:")
            lines.extend(src_lines)
            # The host's own words are not in the library; this says where to ask.
            if quoted or _source_strings(meta):
                lines.append(f'  （宿主那边的原话：recall(query="{q}", view="original")）')
            # A quoted line holding only the host's bare id (written before such a
            # line was refused) names no container: a traceability gap, said once.
            bare_quotes = [t for t in quoted if "#" not in t]
            if bare_quotes:
                how_many = "一" if len(bare_quotes) == 1 else f" {len(bare_quotes)} "
                lines.append(f"  ⚠️有{how_many}根引原话的线只写了编号"
                             f"（{'、'.join(sorted(bare_quotes))}），"
                             "追不到来源：原话取不回，来源撤回也够不着它。")
        # **This is unfold, and it is not a separate tool**: what a gist
        # covers is laid out here, one line each (id + gist).
        # Why not a separate tool: the drilling-down action already exists
        # (search by id), and adding an unfold would give one thing two entry
        # points — of which I would only ever remember one.
        # A period (the time-circling form) has no member list to consult — it
        # stores only a name and a span, and its members are **computed live**.
        # Drilling down still works: what currently falls inside the span is
        # laid out here.
        if _big.is_big(meta) and str(meta.get("when") or ""):
            _t0, _t1, _serr = _F.check_span(str(meta.get("when")))
            if not _serr:
                mem = [m for m in await _F.span_members(_t0, _t1) if seen(m)]
                lines.append(f"范围内现在有 {len(mem)} 条（**现场算的**，没记账；"
                             f"下钻：拿下面的 id 再搜）:")
                for mid in mem[:30]:
                    _mb, mhint, mmark = await _linked(mid, scope_view) or (None, "", "")
                    lines.append(f"  ◈ {mid}  {mhint}{mmark}")
                if len(mem) > 30:
                    lines.append(f"  …… 还有 {len(mem) - 30} 条")
        cov = [c for c in _F.cover_ids(meta) if seen(c)]
        if cov:
            lines.append(f"盖着 {len(cov)} 条（下钻：拿下面的 id 再搜）:")
            for cid in cov[:30]:
                cb, chint, cmark = await _linked(cid, scope_view) or (None, "", "")
                cmeta = (cb or {}).get("metadata", {}) or {}
                if not cb:
                    chint = "（查无此桶——可能被硬删过）"
                # With crossing, covered_by is a list: if this gist is still on
                # it, nothing needs marking; only when it is absent (someone
                # edited it by hand) is the current owner marked
                now_by = _F.covers_of(cmeta)
                mark = ("" if (not cb or q in now_by)
                        else f"  ↑现在归 {'、'.join(now_by) or '（没人盖）'}")
                lines.append(f"  ▣ {cid}  {chint}{cmark}{mark}")
            if len(cov) > 30:
                lines.append(f"  …… 还有 {len(cov) - 30} 条")
        # The reverse chain: what thinking has grown out of this entry —
        # being one of another entry's sources is direct evidence of how far
        # something has been digested
        try:
            refs = [r for r in await rt.bucket_mgr.referenced_by(q) if seen(r)]
        except Exception:
            refs = []
        if refs:
            lines.append("被引用（有东西从这条长出来过）:")
            for rid in refs[:6]:
                _rb, rhint, rmark = await _linked(rid, scope_view) or (None, "", "")
                lines.append(f"  → {rid}  {rhint}{rmark}")
        # The body, verbatim and never cut; one that is not live says where it
        # comes from on the rule above it as well.
        lines.append("─" * 30 if not verdict.mark
                     else "─" * 12 + " 归档区里的原文 " + "─" * 12)
        lines.append(str(b.get("content") or ""))
        return "\n".join(lines)
