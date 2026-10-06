"""
========================================
tools/grow/_sources_check.py — what an entry was formed from: `from` and `sources`
========================================

_normalize_from turns `from` into provenance lines; check_sources takes the `sources`
records against them and the registry. grow, regrow, fold and trace all come through
here (through tools/grow/rooms_path), so this is the one place that decides what a source
is. _same_entry is grow_event's "this body is already stored" test: whether pointing at
the stored entry would lose any line, record or cue of the call.

Exports: _normalize_from(from_ids, missing_hint) · check_sources(sources, prov, exclude)
========================================
"""

import re as _re

from .. import _runtime as rt
from .._common import read_scope, resolve_bucket_ids
from core import _sources as _src
from core import scope as _scope
from utils import (PROV_MAX_LINES, PROV_TARGET_MAX, WAS_DERIVED_FROM, WAS_QUOTED_FROM,
                   is_bucket_id, is_closed, is_telic, read_prov)


# ------------------------------------------------------------
# Outside material: `sources`
# ------------------------------------------------------------
# A record names one piece of the host's material (core/_sources.py). Each record on an
# entry has a wasQuotedFrom line naming it by its string form, so prov and sources say the
# same thing. A bare host id in `from` (m_0142) is that line before its record is known:
# when the same call carries a record with that id the line is linked to it. When none
# does, Loci does not guess the container: the id is completed only from what Loci itself
# was told - the line orders hosts registered (`SourceRegistry.lines_named`: exactly one
# container the caller may reach holds the id), else the request's own host
# (`_host_container`: a ceiling of one container, and a write key of that host) - and the
# completed record carries `completed_from` and the receipt says so. Several reachable
# containers holding the id refuse the write naming them; nothing to complete with
# refuses it naming the id, the same words whether or not a container the caller may not
# reach holds it. A line naming no source would be one reads cannot fetch and a
# withdrawal cannot reach. Lines an entry already has are not refused (trace
# appending sources, a new version's carried-over lines). A full string form in `from`
# with no record of its own gets the record it spells out. Whether a source may be used at
# all is the registry's call (check_writable): withdrawn and deleted are refused, unreadable
# is taken with a note, and a grant set by the request layer limits a turn to its own.

def _registry():
    return getattr(rt.bucket_mgr, "sources", None)


def _host_container():
    """The one container a bare host id of this call may be completed with, or None. Only
    the request's own host says it, never the model: a host whose ceiling (`max_grant`) is
    exactly one place naming a container (system, instance and container, no piece), and
    whose write key (`<host>:<turn>#<ordinal>`, set by the request layer) is this call's.
    An open host, a host reaching several places or a whole instance, and a call without a
    write key complete nothing."""
    req = _scope.current_request()
    host = req.host if req is not None and not req.refused else None
    grant = getattr(host, "max_grant", None)
    if not grant or len(grant) != 1:
        return None
    place = grant[0]
    if place.container is None or place.id is not None or place.through is not None:
        return None
    key = _src.current_write_key() or ""
    return place if key.startswith(f"{host.name}:") else None


def _reachable(registry, sid) -> bool:
    """May this call stand on the line at all: within its host's ceiling (`max_grant`)
    and its turn's grant, when it has them."""
    req = _scope.current_request()
    if req is not None and req.refused:
        return False
    ceiling = getattr(getattr(req, "host", None), "max_grant", None)
    if ceiling is not None and not registry.reaches(ceiling, sid):
        return False
    grant = _src.current_grant()
    return grant is None or registry.granted(grant, sid)


def _complete_bare(line_id: str) -> tuple[dict | None, str, str]:
    """A bare host id no record of the call carries -> (completed record, receipt note,
    refusal). First the registry's line orders, counting only containers this call may
    reach (one completes; several refuse, naming them); then the host's one container;
    (None, "", "") when there is nothing to complete with."""
    registry = _registry()
    if registry is not None:
        found = [sid for sid in registry.lines_named(line_id) if _reachable(registry, sid)]
        if len(found) > 1:
            where = "、".join(f"{x.system}:{x.instance}/{x.container}" for x in found)
            return None, "", (f"编号 {line_id} 在 {len(found)} 个容器里都登记过（{where}），"
                              f"写全是哪一个：from 里写 system:instance/container#{line_id}。"
                              "这次什么都没写。")
        if found:
            one = found[0]
            rec = _completed_record(_src.Place(one.system, one.instance, one.container),
                                    line_id, "registry")
            if rec is not None:
                return rec, (f"from 里的 {line_id} 只写了编号，按 Loci 登记过的那一处补全成 "
                             f"{_src.record_string(rec)}。"), ""
    place = _host_container()
    rec = _completed_record(place, line_id, "host_scope") if place is not None else None
    if rec is not None:
        return rec, (f"from 里的 {line_id} 只写了编号，按这个宿主唯一的容器补全成 "
                     f"{_src.record_string(rec)}。"), ""
    return None, "", ""


def _completed_record(place, line_id: str, how: str):
    """The record a bare id completed with this container names, marked with how it was
    completed, or None when the id cannot stand in a source's identity (it would not read
    back as itself)."""
    raw = {"system": place.system, "instance": place.instance, "container": place.container,
           "id": line_id, "completed_from": how}
    try:
        [rec] = _src.normalize_sources([raw])
        sid, _revision = _src.SourceId.parse(_src.record_string(rec))
    except (_src.SourceRecordError, ValueError):
        return None
    return rec if sid == _src.record_id(rec) else None


def _bare_refusal(ids: list[str]) -> str:
    named = "、".join(ids)
    example = f"system:instance/container#{ids[0]}"
    return (f"引原话那根线只写了编号 {named}，没写系统和容器，Loci 不猜：写全再来"
            f"（from 里写 {example}，或在 sources 里带上它那条记录）。这次什么都没写。")


def _same(stored, given: dict) -> str:
    """"delivery" when the two records name the same piece as delivered (identity plus
    fingerprint, or a named revision); "reference" when they only name the same piece —
    neither carries a fingerprint or a revision, so nothing can be said of its content;
    "" otherwise."""
    try:
        if not isinstance(stored, dict):
            return ""
        if _src.same_delivery(stored, given):
            return "delivery"
        bare = all(not r.get("fingerprint") and r.get("revision") in (None, "")
                   for r in (stored, given))
        return "reference" if bare and _src.same_reference(stored, given) else ""
    except (KeyError, TypeError):
        return ""


def _same_entry(meta: dict, item: dict, *, telic: bool, prov, records, cue) -> bool:
    """Whether a stored entry with the same body is this write already, so its id can be
    returned instead of a new one: nothing the call brings may be lost by that. It has to
    be live and current (not replaced by a newer version), wanted the same way and still
    open (a new want never comes back as a closed one), in the same room on the same day,
    and already carry every `from` line, every source record and the cue of this call."""
    if meta.get("deleted_at") or meta.get("superseded_by"):
        return False
    if is_telic(meta) != telic or is_closed(meta):
        return False
    if str(meta.get("room") or "") != item["room"]:
        return False
    if str(meta.get("when") or "").strip() != item["when"]:
        return False
    have = {(ln["rel"], ln["target"]) for ln in read_prov(meta)}
    if any((ln["rel"], ln["target"]) not in have for ln in prov or []):
        return False
    stored = list(meta.get(_src.SOURCES_FIELD) or [])
    if any(not any(_same(s, r) for s in stored) for r in records or []):
        return False
    return not cue or meta.get("cue") == cue


async def _already_recorded(records: list[dict], exclude: set) -> list[str]:
    """One hint per live, current memory that already carries one of `records`: the same
    delivery (same identity and fingerprint, or the same named revision), or — when
    neither record has a fingerprint or a revision — the same piece, which says it was
    referenced and nothing about the content. A hint only: two memories from one message
    are often two different things, so nothing is blocked. Under a read scope a memory
    the request may not read is not named."""
    if not records:
        return []
    view = await read_scope()
    hits: dict[str, tuple[str, str]] = {}
    for b in await rt.bucket_mgr.list_all(include_archive=False):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or bid in exclude or meta.get("deleted_at") or meta.get("superseded_by"):
            continue
        if view is not None and not view.permits(meta):
            continue
        for stored in meta.get(_src.SOURCES_FIELD) or []:
            found = next(((r, how) for r in records if (how := _same(stored, r))), None)
            if found is not None:
                hits.setdefault(bid, (_src.record_string(found[0]), found[1]))
                break
    return [(f"这条来源已经记过 {bid}，看一眼是不是同一件（{text}）。" if how == "delivery"
             else f"{bid} 也引过同一条来源（{text}）；没有指纹也没有版本号，"
                  "只知道引的是同一条，内容一不一样说不准。")
            for bid, (text, how) in hits.items()]


async def check_sources(raw, prov: list[dict] | None, exclude=(), *,
                        from_call: bool = True,
                        ) -> tuple[list[dict], list[dict] | None, list[str], str]:
    """The `sources` argument and the provenance lines `from` produced -> (records to
    store, provenance lines with every record's quoted line, notes for the receipt,
    refusal). `exclude` are the entries this write replaces or edits, left out of the
    already-recorded hint. Nothing given and nothing to link: (``[]``, prov, [], "").

    `from_call` says `prov` is this call's own `from`: a bare id there that no record of
    the call carries is completed (`_complete_bare`: the registry's line orders, then the
    host's one container) or refused. False (trace appending to an entry) means it is what the entry already has:
    a string form there is not made into a record again, and a bare id that names no new
    record, or several, stays as it is."""
    try:
        records = _src.normalize_sources(_src.coerce_sources_arg(raw))
    except _src.SourceRecordError as e:
        return [], prov, [], (f"sources 不对：{e.zh}。每条写 {{system, instance, container, "
                              "id}，可选 through / revision / fingerprint / fingerprint_by / "
                              "span / use。")
    if any("completed_from" in r for r in records):
        return [], prov, [], ("sources 不对：completed_from 是 Loci 自己补全编号时记的，"
                              "不用写；只写 {system, instance, container, id} 和宿主给的字段。")
    lines = list(prov or [])
    strings = [_src.record_string(r) for r in records]
    bare: list[int] = []        # this call's bare ids that no record of the call carries
    for i, line in enumerate(lines):
        if line["rel"] != WAS_QUOTED_FROM:
            continue
        target = line["target"]
        if "#" in target:
            if from_call and target not in strings:
                try:
                    sid, revision = _src.SourceId.parse(target)
                except _src.SourceRecordError as e:
                    return [], prov, [], f"from 里「{target[:40]}」像来源的全称，但{e.zh}。"
                records.append({"system": sid.system, "instance": sid.instance,
                                "container": sid.container, "id": sid.id,
                                **({"through": sid.through} if sid.through else {}),
                                "revision": revision, "fingerprint": None,
                                "fingerprint_by": None, "use": None})
                strings.append(target)
            continue
        named = list(dict.fromkeys(s for r, s in zip(records, strings) if r["id"] == target))
        if len(named) > 1 and from_call:
            return [], prov, [], (f"from 里的 {target} 对得上好几条来源（{'、'.join(named)}）"
                                  "——from 里写那一条的全称。")
        if len(named) == 1:
            lines[i] = {"rel": WAS_QUOTED_FROM, "target": named[0]}
        elif not named and from_call:
            bare.append(i)
    completed: list[str] = []
    made: list[tuple[int, dict]] = []
    unresolved: list[str] = []
    for i in bare:
        rec, note, refusal = _complete_bare(lines[i]["target"])
        if refusal:
            return [], prov, [], refusal
        if rec is None:
            unresolved.append(lines[i]["target"])
        else:
            made.append((i, rec))
            completed.append(note)
    if unresolved:
        return [], prov, [], _bare_refusal(list(dict.fromkeys(unresolved)))
    for i, rec in made:
        text = _src.record_string(rec)
        lines[i] = {"rel": WAS_QUOTED_FROM, "target": text}
        if text not in strings:
            records.append(rec)
            strings.append(text)
    if not records:
        return [], prov, [], ""
    if len(records) > _src.SOURCES_MAX:
        return [], prov, [], (f"sources 最多 {_src.SOURCES_MAX} 条（收到 {len(records)} 条）"
                              "——拆开分别存。")
    registry = _registry()
    notes: list[str] = []
    if registry is not None:
        refusal, notes = registry.check_writable(records, _src.current_grant())
        if refusal:
            return [], prov, [], refusal
    lines += [{"rel": WAS_QUOTED_FROM, "target": s} for s in strings]
    lines = list({(ln["rel"], ln["target"]): ln for ln in lines}.values())
    if len(lines) > PROV_MAX_LINES:
        return [], prov, [], (f"from 加 sources 一共 {len(lines)} 条来源，超过 {PROV_MAX_LINES}"
                              "——一条记忆挂不了这么多来源，拆开分别存。")
    notes = completed + notes + await _already_recorded(records, set(exclude))
    return records, lines, notes, ""


# ------------------------------------------------------------
# `from`
# ------------------------------------------------------------

async def _check_quoted_source(target: str) -> str:
    """Whether a target from outside the library may be quoted as a source; returns the
    refusal, or "" to accept. Every write tool that takes `from` comes through
    _normalize_from, so this is the one place that decides it. A target is either a
    source's string form (`lento:home/private:U#m_0142`, or `…#m_0142..m_0160` for a run
    of lines; `#` marks it), which has to parse and pass the registry's own check of a
    source a memory is written from (`SourceRegistry.check_writable`, under this turn's
    grant: outside the grant one answer whatever the source's state, inside it withdrawn,
    deleted and held refused), or the host's bare id for a line (`m_0931`), whose identity
    is not known yet: shape only here - one token, short enough to store whole; whether
    it gets an identity (a record of the call, the registry, the host's one container) or
    refuses the write is check_sources'."""
    if _re.search(r"\s", target):
        return (f"from 里「{target[:40]}」不像 id（带空格）——填 bucket_id，"
                "或宿主那句话自己的 id（如 m_0931）。")
    if len(target) > PROV_TARGET_MAX:
        return (f"from 里 {target[:40]}… 有 {len(target)} 字符，超过 {PROV_TARGET_MAX} 上限"
                "——不截断，换成它的短 id。")
    if "#" in target:
        try:
            sid, _revision = _src.SourceId.parse(target)
        except _src.SourceRecordError as e:
            return (f"from 里「{target[:40]}」像来源的全称，但{e.zh}。全称写成 "
                    "system:instance/container#id（有版本号再加 @版本）。")
        registry = _registry()
        if registry is not None:
            record = {"system": sid.system, "instance": sid.instance,
                      "container": sid.container, "id": sid.id,
                      **({"through": sid.through} if sid.through else {})}
            refusal, _notes = registry.check_writable([record], _src.current_grant())
            if refusal:
                return refusal
    return ""


async def _normalize_from(from_ids, missing_hint: str = "") -> tuple[list[dict] | None, str]:
    """Turn the `from` argument into provenance lines. Returns (lines, error
    message); lines=None means nothing was passed. grow, regrow and fold all take
    `from` through here, so this is the one place that decides what a source is:

    - a short handle resolves to the full id it names (the same resolver every
      write tool uses), so what lands on disk is always the full id;
    - a memory's id (12 hex, or a `feel_…` id) has to exist, archive included, and
      becomes a wasDerivedFrom line;
    - anything else that is an entry of the library all the same (seeded test data) is
      a memory too; otherwise it is the host's own id for a line of the conversation
      (`m_0931`) and becomes a wasQuotedFrom line once _check_quoted_source lets it
      through (check_sources then completes or refuses a bare one).

    More than PROV_MAX_LINES distinct sources is refused outright, never cut.
    `missing_hint` is appended to the "these ids do not exist" refusal."""
    if from_ids is None:
        return None, ""
    if isinstance(from_ids, str):
        from_ids = [s.strip() for s in from_ids.split(",") if s.strip()]
    if not isinstance(from_ids, list):
        return None, "from 要传 id 列表。"
    ids = list(dict.fromkeys(str(s).strip() for s in from_ids if str(s).strip()))
    if not ids:
        return None, ""
    if len(ids) > PROV_MAX_LINES:
        return None, (f"from 最多 {PROV_MAX_LINES} 条（收到 {len(ids)} 条）"
                      "——一条记忆挂不了这么多来源，拆开分别存。")
    ids, id_err = await resolve_bucket_ids(ids, "from")
    if id_err:
        return None, id_err
    lines: list[dict] = []
    missing: list[str] = []
    for fid in dict.fromkeys(ids):
        if is_bucket_id(fid):
            if not await rt.bucket_mgr.get_including_archive(fid):
                missing.append(fid)
            lines.append({"rel": WAS_DERIVED_FROM, "target": fid})
            continue
        # An id of another shape that is nonetheless an entry of this library (seeded
        # test data) is a memory, not the host's line id.
        if await rt.bucket_mgr.get_including_archive(fid):
            lines.append({"rel": WAS_DERIVED_FROM, "target": fid})
            continue
        quote_err = await _check_quoted_source(fid)
        if quote_err:
            return None, quote_err
        lines.append({"rel": WAS_QUOTED_FROM, "target": fid})
    if missing:
        return None, f"from 里这些 id 不存在：{', '.join(missing)}。{missing_hint}"
    return lines, ""
