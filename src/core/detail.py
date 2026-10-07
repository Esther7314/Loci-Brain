"""
========================================
core/detail.py — the detail window: one entry, what it led to, where it came from
========================================

The panel opens one window for any memory clicked anywhere (GET /api/loci/bucket/{id},
/api/loci/lineage/{id}, /api/loci/source/{id}; contract 「面板接口」 §三). The routes list
the store and hand it over; everything the window shows is assembled here, and every
word on it is said here (`*_words` / `text`), so the panel and a host's own page say the
same thing.

  human_tags(meta)       the row of small tags under the title: what kind of statement
                         it is, whose promise, whether it is closed, a hold, an edit.
                         The default (a thetic record) shows nothing.
  date_of(meta)          the one day the window shows: `when` when it names a day, else
                         the local day it was written.
  edit_actions(meta)     which of 字写错了 / 内容错了 / 删除 the window offers; POST
                         /api/loci/entry/fix asks it again before writing. Only a live,
                         current entry is edited (an old version is edited through the
                         version in use, an archived one after restoring it); 内容错了 is
                         for an event only — a MIND entry is the model's own judgement.
  lineage(...)           关联: what came after it (derived entries, the gists covering it,
                         the periods it falls in, its new version) and its signposts (the
                         cue it waits on, the holds live on it). Every linked entry goes
                         through the gate on the read road; one out of the request's scope
                         is left out, one not live carries its state in words.
  source_view(...)       来源: the host sources it was formed from with their registry
                         state and whether the original can be asked for, the memories it
                         stands on, and two sentences — how it is known and whether its
                         ground still holds.
  fetch_original(...)    one source's original, asked of its host (core/_originals.fetch)
                         for a person reading the window: nothing fetched is stored or
                         logged, and no use is recorded (it is not the model reading).
                         Lines Loci holds itself (an import) carry who said them and when;
                         a host's lines carry neither, since the fourth joint's answer has
                         no such fields.
  entry_view(...)        the window's body: the entry verbatim, its metadata, and the
                         fields above. An entry whose text a source change clears
                         (`clearing_due`) is shown as the clearing leaves it, from the
                         moment the registry records the change.

Every timestamp leaves as local ISO 8601 with its offset (core/_when); a day alone as
YYYY-MM-DD.

Exports: TAG_WORDS · HOLD_WORDS · FIX_KINDS · human_tags · date_of · local_stamp ·
         state_words · clearing_due · edit_actions · lineage · related_counts · source_layer_of ·
         source_view · fetch_original · entry_view
========================================
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from utils import is_closed, is_telic, parse_bool, read_from_ids

from . import _bigevent as _B
from . import _fold as _F
from . import _holds as _H
from . import _invalidation as _I
from . import _originals as _O
from . import _sources as _src
from . import _when as _w
from . import visibility as _V
from ._rooms import is_event_room, normalize_room, room_cn
from .profile import _EDITED_BY_USER_TAG, entry_label, owed_names, short_id
from .scope import IMPORT_SYSTEM, LOCI
from .starfield import split_ids

# The words of the tag row, by key. `promised` names who owes it when that is not me;
# `yearly` and `hold` carry their detail.
TAG_WORDS = {
    "self": "亲历", "world": "听说", "telic": "想要", "promised": "我答应的",
    "inference": "推的", "assumption": "猜的", "dream": "梦里来的", "yearly": "每年",
    "resolved": "做完了", "abandoned": "不做了", "hold": "条子", "edited": "人改的",
}
HOLD_WORDS = {"defer": "先别催", "avoid": "别碰"}

TYPO, CONTENT, DELETE = "typo", "content", "delete"
FIX_KINDS = (TYPO, CONTENT, DELETE)

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
_YEARLY = "FREQ=YEARLY"


def _meta(row) -> dict:
    return (row.get("metadata") or {}) if isinstance(row, dict) else {}


def _bid(row) -> str:
    return str(_meta(row).get("id") or (row or {}).get("id") or "")


# ============================================================
# Small pieces shown on every row
# ============================================================

def local_stamp(value) -> str | None:
    """A stored timestamp as local ISO 8601 with its offset; None when unreadable."""
    stamp = _w.parse_stamp(value)
    return stamp.isoformat(timespec="seconds") if stamp else None


def date_of(meta: dict) -> str:
    """The day the window shows: `when` when it names one day (a clock time on it counts
    as that day), else the local day it was written; "" when neither reads."""
    w = str((meta or {}).get("when") or "").strip()
    if w and ".." not in w and _DAY.match(w):
        stamp = _w.parse_stamp(w)
        if stamp:
            return stamp.date().isoformat()
    created = _w.parse_stamp((meta or {}).get("created"))
    return created.date().isoformat() if created else ""


def human_tags(meta: dict) -> list[dict]:
    """The tag row: [{key, text}], in a fixed order. Nothing for a plain record."""
    m = meta or {}
    out: list[dict] = []

    def add(key: str, text: str = "") -> None:
        out.append({"key": key, "text": text or TAG_WORDS[key]})

    room = normalize_room(m.get("room"))
    if room == "EVENT/SELF":
        add("self")
    elif room == "EVENT/WORLD":
        add("world")
    if is_telic(m):
        add("telic")
        owed = owed_names(m.get("bound") or [])
        if owed:
            add("promised", TAG_WORDS["promised"] if owed == "我" else f"{owed}欠着")
    evidential = str(m.get("evidential") or "").strip().lower()
    if evidential in ("inference", "assumption"):
        add(evidential)
    if parse_bool(m.get("internally_generated"), default=False):
        add("dream")
    if str(m.get("recurrence") or "").strip().upper() == _YEARLY:
        w = str(m.get("when") or "").strip()
        add("yearly", f"每年 {w[5:10]}" if _DAY.match(w) else "")
    status = str(m.get("status") or "").strip().lower()
    if status == "abandoned":
        add("abandoned")
    elif status == "resolved" or is_closed(m):
        add("resolved")
    if _H.is_hold(m):
        add("hold", f"条子·{HOLD_WORDS[m['hold']]}")
    if _EDITED_BY_USER_TAG in [str(t) for t in (m.get("tags") or [])]:
        add("edited")
    return out


def state_words(meta: dict) -> str:
    """What a linked entry's line says when it is not a live, current memory; "" when it
    is."""
    state = _V.state_of(meta)
    if state == _V.DELETED:
        return "已删除"
    if state == _V.ARCHIVED:
        return "在归档区"
    if str((meta or {}).get("superseded_by") or "").strip():
        return "旧版"
    if _V.source_gone(meta):
        return "来源撤回了"
    if _V.source_restored(meta):
        return "来源恢复了·待复核"
    return ""


def clearing_due(meta: dict, scope=None) -> bool:
    """Does this entry rest on a withdrawn or deleted source itself (not only through what
    it is derived from), so its text is cleared or about to be? A `source_gone` record
    saying its body is cleared, written before the body is; or, before even that record,
    the registry through the request's view (core/scope.ScopeView.source_blocked). The
    window then shows it as its clearing leaves it (BucketManager.as_cleared)."""
    if any(r.get("cleared") for r in _I.gone_records(meta)):
        return True
    blocked = getattr(scope, "source_blocked", None)
    return callable(blocked) and bool(blocked(meta, roots=False))


def edit_actions(meta: dict) -> list[str]:
    """Which edits the window offers for this entry (see the module header)."""
    m = meta or {}
    if _V.state_of(m) != _V.LIVE or str(m.get("superseded_by") or "").strip():
        return []
    if is_event_room(m.get("room")):
        return [TYPO, CONTENT, DELETE]
    return [TYPO, DELETE]


def _line(row, *, kind: bool = False) -> dict:
    """One linked entry as a line: id, short, its text, and its state in words."""
    m = _meta(row)
    bid = _bid(row)
    out = {"id": bid, "short": short_id(bid),
           "text": entry_label(m, str((row or {}).get("content") or ""))}
    if kind:
        out["kind"] = "event" if is_event_room(m.get("room")) else "mind"
    out["state_words"] = state_words(m)
    return out


def _missing_line(bid: str) -> dict:
    return {"id": bid, "short": short_id(bid), "text": "", "state_words": "找不到了"}


def _in_scope(row, scope) -> bool:
    return not _V.visible_for(_meta(row), scope, road=_V.READ).out_of_scope


# ============================================================
# 关联 — lineage
# ============================================================

def lineage(all_buckets: list, meta: dict, now: datetime, *, scope=None,
            extra: dict | None = None) -> dict:
    """关联 for one entry. `all_buckets` is the live store's listing; `extra` maps the
    ids of entries outside it that this entry names (its new version, what it stands on)
    to their buckets, fetched by the caller. `scope` is the request's ScopeView, or None
    for the whole library."""
    bid = str((meta or {}).get("id") or "")
    by_id = {_bid(b): b for b in all_buckets}
    for k, v in (extra or {}).items():
        by_id.setdefault(k, v)
    sup = str((meta or {}).get("superseded_by") or "").strip()

    derived = [_line(b, kind=True) for b in all_buckets
               if _bid(b) != bid and bid in read_from_ids(_meta(b)) and _in_scope(b, scope)]
    covered_by = [_line(by_id[c]) for c in _F.covers_of(meta)
                  if c != sup and c in by_id and _in_scope(by_id[c], scope)
                  and _V.state_of(_meta(by_id[c])) == _V.LIVE]
    periods = []
    ts = _w.ts_of(meta or {})
    if ts is not None:
        for pmeta, content, pid in _B.covering(all_buckets, ts, ts + timedelta(seconds=1)):
            if pid == bid or _V.visible_for(pmeta, scope, road=_V.READ).out_of_scope:
                continue
            periods.append({"id": pid, "short": short_id(pid),
                            "text": _B.first_line(content) or entry_label(pmeta, content),
                            "span": str(pmeta.get("when") or "")})
    new_version = None
    if sup:
        row = by_id.get(sup)
        if row is None:
            new_version = _missing_line(sup)
        elif _in_scope(row, scope):
            new_version = _line(row)

    cue = None
    raw_cue = (meta or {}).get("cue")
    if isinstance(raw_cue, dict) and str(raw_cue.get("condition") or "").strip():
        cue = {"condition": str(raw_cue.get("condition") or "").strip(),
               "phrasings": [str(p) for p in (raw_cue.get("phrasings") or []) if str(p).strip()]}
    holds = []
    for h in _H.holds_on(bid, now, _H.hold_index(all_buckets)):
        if _V.visible_for(h, scope, road=_V.READ).out_of_scope:
            continue
        hid = str(h.get("id") or "")
        end = _H.hold_end(h)
        row = by_id.get(hid) or {"metadata": h, "content": ""}
        holds.append({"id": hid, "short": short_id(hid),
                      "text": entry_label(h, str(row.get("content") or "")),
                      "hold": h["hold"], "words": HOLD_WORDS[h["hold"]],
                      "until": end.isoformat() if end else None})

    later = {"derived": derived, "covered_by": covered_by, "periods": periods,
             "new_version": new_version}
    signposts = {"cue": cue, "holds": holds}
    out = {"id": bid, "short": short_id(bid), "later": later, "signposts": signposts}
    counts = related_counts(out)
    out["count"] = counts["later"] + counts["signposts"]
    return out


def related_counts(lin: dict) -> dict:
    """The two numbers the window's 关联 line shows, from a lineage dict."""
    later, signs = lin["later"], lin["signposts"]
    n_later = (len(later["derived"]) + len(later["covered_by"]) + len(later["periods"])
               + (1 if later["new_version"] else 0))
    n_signs = (1 if signs["cue"] else 0) + len(signs["holds"])
    return {"later": n_later, "signposts": n_signs}


# ============================================================
# 来源 — sources
# ============================================================

_SOURCE_STATE_WORDS = {
    _src.ACTIVE: "在", _src.UNREADABLE: "宿主那边读不到了", _src.WITHDRAWN: "已撤回",
    _src.DELETED: "已删除", _src.HELD: "宿主说撤回或删了，等确认",
}
_BLOCKED = (_src.WITHDRAWN, _src.DELETED, _src.HELD)


def source_layer_of(meta: dict) -> str:
    """originals (it names host sources) · derived (it stands on other memories) · none."""
    if _O.source_records_of(meta or {}):
        return "originals"
    if read_from_ids(meta):
        return "derived"
    return "none"


def _import_rows(base_dir: str, sid) -> tuple[dict, dict]:
    """(the import batch's meta, its conversation's rows by line id) for an imported
    source; empty when the store does not have them."""
    from .import_memory import ImportStore     # lazy: import_memory imports _originals
    if not base_dir:
        return {}, {}
    store = ImportStore(base_dir)
    meta = store.meta(sid.instance) or {}
    if not meta:
        return {}, {}
    return meta, {str(r.get("id")): r for r in store.lines(sid.instance, sid.container)}


def _original_row(index: int, rec: dict, meta: dict, *, registry, hosts) -> dict:
    """One source as the 来源 layer lists it. `at` is the day the entry was formed from it
    (for an import, the day its first line was said); `span.count` is None for a run whose
    lines the host has not registered."""
    sid = _src.record_id(rec)
    imported = sid.system == IMPORT_SYSTEM
    host = None if imported else _O.host_for(hosts, sid)
    state = registry.read_state(rec) if registry is not None else _src.ACTIVE
    order_known = registry.order_known(sid) if registry is not None else True
    if not sid.through:
        count = 1
    else:
        members = registry.members_of(sid) if registry is not None else None
        count = len(members) if members else None
    at = date_of({"created": meta.get("created")}) or None
    if imported:
        _, rows = _import_rows(getattr(registry, "base_dir", ""), sid)
        first = local_stamp((rows.get(sid.id) or {}).get("at"))
        at = first[:10] if first else at
    reachable = imported or bool(host is not None and getattr(host, "fetch_token", ""))
    return {"index": index, "record": _src.record_string(rec),
            "host": LOCI if imported else (host.name if host is not None else None),
            "container": sid.container,
            "span": {"first": sid.id, "last": sid.through or sid.id, "count": count},
            "at": at, "state": state,
            "state_words": _SOURCE_STATE_WORDS.get(state, state),
            "can_fetch": reachable and order_known and state not in _BLOCKED}


def how_known(meta: dict, originals: list[dict], derived_n: int) -> str:
    """How this entry is known, in one sentence."""
    m = meta or {}
    if parse_bool(m.get("internally_generated"), default=False):
        return "是梦里或想象出来的，不是亲历"
    if originals:
        where = list(dict.fromkeys(
            "导入的聊天记录" if o["host"] == LOCI else (o["host"] or "宿主") for o in originals))
        line = (f"从{'、'.join(where)}里记下的" if where == ["导入的聊天记录"]
                else f"从{'、'.join(where)}那边的对话里记下的")
        if normalize_room(m.get("room")) == "EVENT/WORLD":
            line += "（听来的，不是亲历）"
        return line
    if derived_n:
        evidential = str(m.get("evidential") or "").strip().lower()
        verb = {"inference": "推出来的", "assumption": "猜的"}.get(evidential, "长出来的")
        return f"从 {derived_n} 条记忆{verb}"
    return "没记下是从哪儿来的"


def still_holds(meta: dict, findings, originals: list[dict], derived_from: list[dict]) -> str:
    """Whether the ground under this entry still holds, in one sentence."""
    if findings.failed:
        gone = "、".join(_I.state_word(state) for _s, state in findings.failed)
        left = f"；还剩 {len(findings.remaining)} 条能用" if findings.remaining else "；一条都不剩"
        return f"来源不能用了（{gone}）{left}"
    if findings.restored:
        return "来源撤回过、现在恢复了，这条还没复核"
    if findings.revised:
        return f"宿主那边改过 {len(findings.revised)} 处，这条是按改之前记的"
    if _I.open_records(meta, _I.OVERTURN):
        return "它站着的那条被推翻了，还没复核"
    if originals:
        return "来源还在，没改过"
    if derived_from:
        gone = sum(1 for d in derived_from if d["state_words"])
        return (f"它站着的记忆有 {gone} 条已经不是现在的样子了" if gone
                else "它站着的记忆都还在")
    return "没有来源可核"


def source_view(meta: dict, *, registry, hosts, scope=None, lookup: dict | None = None) -> dict:
    """来源 for one entry. `lookup` maps the ids it stands on to their buckets (any state);
    an id missing there is shown as not found."""
    m = meta or {}
    bid = str(m.get("id") or "")
    records = _O.source_records_of(m)
    originals = [_original_row(i, rec, m, registry=registry, hosts=hosts)
                 for i, rec in enumerate(records)]
    derived_from = []
    for pid in read_from_ids(m):
        row = (lookup or {}).get(pid)
        if row is None:
            derived_from.append({**_missing_line(pid), "kind": ""})
        elif _in_scope(row, scope):
            derived_from.append(_line(row, kind=True))
    findings = _I.source_findings(m, registry)
    return {"id": bid, "short": short_id(bid), "layer": source_layer_of(m),
            "originals": originals,
            "how_known": how_known(m, originals, len(derived_from)),
            "still_holds": still_holds(m, findings, originals, derived_from),
            "derived_from": derived_from}


_OUTCOME_WORDS = {
    _O.GIVEN: "宿主给了", _O.UNAVAILABLE: "原话暂时取不到", _O.NOT_ALLOWED: "原话不许看了",
    _O.NO_HOST: "原话在宿主那边，这儿没配谁给原话",
}
_PARTIAL_WORDS = "宿主只给了一部分"
_WHY_WORDS = {
    _O.UNREACHABLE: "连不上宿主", _O.TIMEOUT: "宿主没在时限里回话", _O.HOST_SAYS: "宿主说眼下拿不到",
    _O.NO_TOKEN: "问这个宿主的钥匙没配上", _O.ALL_MISSING: "这几行宿主眼下都拿不到",
    _O.NOT_FOUND: "找不到这段",
}
_REASON_WORDS = {
    "withdrawn": "撤回了", "deleted": "删了", "out_of_scope": "不在能看的范围里",
    _src.HELD: "宿主说撤回或删了，等确认", _O.ORDER_UNKNOWN: "这一段有哪几行宿主没交过",
    _O.REDIRECT: "宿主要转去别处", _O.TOO_BIG: "宿主的回答太大", _O.MALFORMED: "宿主的回答读不懂",
    _O.UNKNOWN_WORD: "宿主的说法这一版不认识", _O.REQUEST_REFUSED: "这次请求认不出能读什么",
    _O.GONE_HTTP: "宿主的取原文口说它不在了", _O.TLS_FAILED: "宿主的证书没验过",
}
_MISSING_WORDS = {"unavailable": "这一行暂时取不到", "not_found": "这一行宿主那边找不到",
                  "withdrawn": "这一行撤回了", "deleted": "这一行删了",
                  "out_of_scope": "这一行不在能看的范围里"}


def _outcome_words(answer) -> str:
    if answer.partial:
        return _PARTIAL_WORDS
    base = _OUTCOME_WORDS.get(answer.outcome, answer.outcome)
    why = (_REASON_WORDS.get(answer.reason) or (f"HTTP {answer.reason[5:]}"
                                                if answer.reason.startswith("http_") else "")
           if answer.outcome == _O.NOT_ALLOWED else
           _WHY_WORDS.get(answer.why) or (f"HTTP {answer.why[5:]}"
                                          if answer.why.startswith("http_") else ""))
    return f"{base}（{why}）" if why else base


async def fetch_original(meta: dict, index: int, *, store, hosts, registry, settings,
                         request=None) -> dict:
    """The original of the entry's source number `index` (as `source_view` numbers them),
    asked of its host. Raises IndexError for a number it does not have. What a host says
    is gone is held (core/_originals.hold_what_hosts_said), as on every fetch."""
    from .import_memory import speaker_label   # lazy: import_memory imports _originals
    records = _O.source_records_of(meta or {})
    if not 0 <= index < len(records):
        raise IndexError(index)
    rec = records[index]
    answer = await _O.fetch(rec, hosts=hosts, request=request, settings=settings,
                            registry=registry)
    await _O.hold_what_hosts_said([answer], store)
    sid = _src.record_id(rec)
    batch, rows = ({}, {})
    if sid.system == IMPORT_SYSTEM:
        batch, rows = _import_rows(getattr(registry, "base_dir", ""), sid)
    lines = []
    for ln in answer.lines:
        item: dict = {"id": ln.id}
        if ln.missing is not None:
            item["missing"] = ln.missing
            item["missing_words"] = _MISSING_WORDS.get(ln.missing, "这一行没给")
            lines.append(item)
            continue
        text = ln.text or ""
        row = rows.get(ln.id)
        if row is not None:
            who = speaker_label(row.get("role"), bool(batch.get("same_self", True)),
                                str(batch.get("human") or "用户"))
            # The import's own answer prefixes each line with who and when; the window
            # shows those as fields.
            text = text.split("：", 1)[1] if text.startswith(who) and "：" in text else text
            item["who"] = who
            item["at"] = local_stamp(row.get("at"))
        item["text"] = text
        if ln.cut:
            item["cut"] = True
        lines.append(item)
    return {"record": answer.source, "host": answer.host or None, "outcome": answer.outcome,
            "outcome_words": _outcome_words(answer), "partial": answer.partial,
            "lines": lines}


# ============================================================
# The window's body
# ============================================================

def entry_view(bucket: dict, lin: dict, *, scope_line: str) -> dict:
    """The detail window for one bucket: verbatim body, metadata, the tag row, the 关联
    counts (from its lineage dict), the source layer and the edits offered."""
    meta = _meta(bucket)
    bid = str(meta.get("id") or bucket.get("id") or "")
    room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
    state = _V.state_of(meta)
    edit = edit_actions(meta)
    return {
        "id": bid,
        "short": short_id(bid),
        "name": str(meta.get("name") or ""),
        "date": date_of(meta),
        "tags_human": human_tags(meta),
        "summary": str(meta.get("summary") or ""),
        "content": str(bucket.get("content") or ""),     # verbatim, never truncated
        "room": room,
        "room_cn": room_cn(meta.get("room")),
        "when": str(meta.get("when") or ""),
        "created": local_stamp(meta.get("created")) or "",
        "last_active": local_stamp(meta.get("last_active")) or "",
        "valence": meta.get("valence"),
        "arousal": meta.get("arousal"),
        "status": str(meta.get("status") or ""),
        "pinned": bool(meta.get("pinned")),
        "tags": [str(t) for t in (meta.get("tags") or [])],
        # Subjects are a third kind of tag, alongside tags and aliases, never mixed in.
        "subjects": [str(s) for s in (meta.get("subjects") or [])],
        "from": read_from_ids(meta),
        "supersedes": split_ids(meta.get("supersedes")),
        "superseded_by": str(meta.get("superseded_by") or ""),
        "archived": state != _V.LIVE,
        # The `meaning` write path is retired; what is on disk is still shown.
        "meaning": meta.get("meaning") or [],
        "why_remembered": str(meta.get("why_remembered") or ""),
        "related": related_counts(lin),
        "source_layer": source_layer_of(meta),
        "edit": edit,
        "can_edit": CONTENT in edit,
        "scope": scope_line,
    }
