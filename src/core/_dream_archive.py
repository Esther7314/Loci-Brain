"""
========================================
core/_dream_archive.py — the panel's copy of each dream, kept three natural days
========================================

A dream is forgotten for real (core/_dream.py): its whole text is stripped at waking
(`degrade_on_wake`) and its file is deleted when it has faded (`sweep_expired`). The
panel's dream page still shows each dream of the last three natural days — the whole
text, its state, and the weaver's thread candidates with whether one was kept — so a
copy is written beside the lifecycle, for the panel alone.

🔴 The model never reads this copy. No tool, cue, breath, recall, dream handout, host
   route or export reads this folder: for him the dream is gone the way it always was.
   The panel's `GET /api/loci/dreams` (web/loci_activity.py, never on HOOK_PATHS) is its
   one reader; the dream engine (core/_dream.py) writes it, the source change
   (core/_source_change.py, `dream_records`) clears its words, and the decay cycle and
   the dream's own lazy sweep delete what has aged out. A test lists those modules and
   fails on any other that reaches it.

When it is written — the moments the dream's text would otherwise be lost:

    weave      the dream is saved: the copy is written whole, state `waiting`
               (inside core/_dream._commit, under the ingredients' leases, so a source
               change either blocked them first or finds the copy when it clears)
    wake       `degrade_on_wake` strips the whole text: state `fading`, `degraded_at`
    sweep      `sweep_expired` deletes the file: state `gone`, `gone_at`

A dream on disk from before this copy existed gets one at whichever of these comes first,
with the text that is left by then (`whole` says whether it is the whole version).

How long: three natural days on the local calendar, counted by the night it was woven on
(`night`, the local date of `织于`) — today, yesterday and the day before. Older copies are
deleted by `sweep`, which the decay cycle runs daily (core/decay_engine.py) and the dream's
own lazy sweep runs on waking; the page leaves out any that has aged out but is still on
disk.

A source withdrawn or deleted clears the words of every copy it reaches — the dream's
ingredients, the sources behind its quote share, or its text holding the cleared words
— and of the copy of every dream file it removed (`clear`): the text and the candidates
are blanked, `cleared` says so, and the card stays with its night and state.

One file per dream: `<buckets>/_state/dream_archive/<id>.json`, written whole each time
under one lease for the folder.

    {"id", "night", "woven_at", "state", "degraded_at", "gone_at", "text", "whole",
     "nightmare", "candidates": [{"meet", "recall"}], "material": {stream: [ids]},
     "sources": [string forms], "cleared", "cleared_at"}

Exports: DIR · KEEP_DAYS · WAITING · FADING · GONE · STATE_WORDS · CLEARED_WORDS ·
         archive_dir · load · keep · sweep · clear · ingredient_ids · as_dream ·
         kept_candidates · panel_view
========================================
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import _when as _w
from . import runtime as rt
from .ledger_mirror import file_lease
from .paging import page, past

DIR = os.path.join("_state", "dream_archive")
KEEP_DAYS = 3

WAITING, FADING, GONE = "waiting", "fading", "gone"
_ORDER = (WAITING, FADING, GONE)          # forward only, as the dream itself
STATE_WORDS = {WAITING: "还没送到", FADING: "送到了·在散", GONE: "散了"}
CLEARED_WORDS = "依据的来源撤回或删除了，梦里的字清掉了"

# The streams of a dream's `素材` that hold memory ids (core/_dream.INGREDIENT_STREAMS).
_STREAMS = ("压在心头", "想不明白", "冷档案", "原话")


def archive_dir(base_dir: str | None = None) -> str:
    bd = base_dir or str((rt.config or {}).get("buckets_dir") or ".")
    return os.path.join(bd, DIR)


def _lease(base_dir: str | None):
    folder = Path(archive_dir(base_dir))
    folder.mkdir(parents=True, exist_ok=True)
    return file_lease(folder / ".lock")


def _path(base_dir: str | None, dream_id: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_-]", "_", str(dream_id))[:64]
    return os.path.join(archive_dir(base_dir), f"{safe}.json")


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("id") else None


def _write(base_dir: str | None, rec: dict) -> None:
    path = _path(base_dir, rec["id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load(base_dir: str | None = None) -> list[dict]:
    """Every copy on disk, newest woven first; `_path` is the reader's own."""
    folder = archive_dir(base_dir)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = _read(os.path.join(folder, name))
        if rec is not None:
            rec["_path"] = os.path.join(folder, name)
            out.append(rec)
    out.sort(key=lambda r: str(r.get("woven_at") or ""), reverse=True)
    return out


def _night(woven_at) -> str | None:
    stamp = _w.parse_stamp(woven_at)
    return _w.to_local(stamp).date().isoformat() if stamp is not None else None


def _copy_of(dream: dict) -> dict:
    """A new copy from a live dream record (core/_dream.weave's shape)."""
    whole = str(dream.get("完整") or "").strip()
    candidates = [{"meet": str(c.get("碰到") or ""), "recall": str(c.get("想起") or "")}
                  for c in dream.get("线索候选") or [] if isinstance(c, dict)]
    material = dream.get("素材") or {}
    return {
        "id": str(dream.get("id") or ""),
        "night": _night(dream.get("织于")),
        "woven_at": dream.get("织于"),
        "state": WAITING,
        "degraded_at": None,
        "gone_at": None,
        "text": whole or str(dream.get("碎片") or ""),
        "whole": bool(whole),
        "nightmare": bool(dream.get("nightmare")),
        "candidates": candidates,
        "material": {k: [str(i) for i in material.get(k) or []] for k in _STREAMS},
        "sources": [str(s) for s in dream.get("来源") or []],
        "cleared": False,
        "cleared_at": None,
    }


def keep(dream: dict, *, state: str, at: datetime | None = None,
         base_dir: str | None = None) -> None:
    """Write the copy of `dream` (a live record), or move an existing one forward to
    `state` (never back). A copy whose words were cleared is never given them again."""
    if state not in _ORDER or not dream.get("id"):
        return
    stamp = (at or _w.now()).isoformat(timespec="seconds")
    with _lease(base_dir):
        path = _path(base_dir, str(dream["id"]))
        rec = _read(path) or _copy_of(dream)
        if _ORDER.index(state) > _ORDER.index(rec.get("state") or WAITING):
            rec["state"] = state
        if state == FADING and not rec.get("degraded_at"):
            rec["degraded_at"] = stamp
        if state == GONE and not rec.get("gone_at"):
            rec["gone_at"] = stamp
        _write(base_dir, rec)


def _aged_out(rec: dict, today: datetime) -> bool:
    """Woven before the last three natural days (a copy with no readable night is too)."""
    night = _w.parse_date_or_none(rec.get("night") or "")
    first = today - timedelta(days=KEEP_DAYS - 1)
    return night is None or night.date() < first.date()


def _today(now: datetime | None) -> datetime:
    return _w.to_local(now or _w.now()).replace(hour=0, minute=0, second=0, microsecond=0)


def sweep(base_dir: str | None = None, now: datetime | None = None) -> list[str]:
    """Delete the copies woven before the last three natural days; their ids."""
    today = _today(now)
    gone: list[str] = []
    if not os.path.isdir(archive_dir(base_dir)):
        return gone
    with _lease(base_dir):
        for rec in load(base_dir):
            if not _aged_out(rec, today):
                continue
            try:
                os.remove(rec["_path"])
            except OSError as e:
                rt.logger.warning("[dream] 面板的梦存档删不掉 %s: %s", rec["_path"], e)
                continue
            gone.append(str(rec.get("id") or ""))
    return gone


def ingredient_ids(rec: dict) -> list[str]:
    """The memory ids the copy's dream was woven from, every stream."""
    material = rec.get("material") or {}
    return [str(i) for k in _STREAMS for i in material.get(k) or []]


def as_dream(rec: dict) -> dict:
    """The copy in the live record's shape, for core/_dream's readers of material and
    sources (`ingredient_ids`, `fed_by`)."""
    return {"素材": dict(rec.get("material") or {}), "来源": list(rec.get("sources") or [])}


def clear(base_dir: str | None, reaches, removed=()) -> int:
    """Blank the words of every copy `reaches(rec)` says the change reaches, and of the
    copy of each dream file the change removed (`removed`, their ids, which also go to
    `gone`). Returns how many copies held words and hold none now; a copy cleared before
    is not counted again."""
    removed = {str(i) for i in removed}
    if not os.path.isdir(archive_dir(base_dir)):
        return 0
    n = 0
    stamp = _w.now().isoformat(timespec="seconds")
    with _lease(base_dir):
        for rec in load(base_dir):
            rid = str(rec.get("id") or "")
            if rid not in removed and not reaches(rec):
                continue
            rec.pop("_path", None)
            changed = False
            if rid in removed and rec.get("state") != GONE:
                rec["state"] = GONE
                rec["gone_at"] = rec.get("gone_at") or stamp
                changed = True
            if not rec.get("cleared"):
                rec.update({"text": None, "candidates": [], "cleared": True,
                            "cleared_at": stamp})
                n += 1
                changed = True
            if changed:
                _write(base_dir, rec)
    return n


# ------------------------------------------------------------
# The panel's page
# ------------------------------------------------------------

_EDGE = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’「」『』《》()（）]+")


def _norm(s) -> str:
    return _EDGE.sub("", str(s or ""))


def _matches(meet: str, cue: dict) -> bool:
    """Does a cue (`{condition, phrasings}`) say what the candidate meets? The weaver's
    「碰到」 is what the hint asks to be written as the condition: either holds the other
    (a one-character cue is too little to say so)."""
    m = _norm(meet)
    if not m:
        return False
    for said in [cue.get("condition")] + list(cue.get("phrasings") or []):
        s = _norm(said)
        if s and (m in s or (len(s) >= 2 and s in m)):
            return True
    return False


def _cues_written_since(events, since: datetime) -> set[str]:
    """The entries the ledger shows a cue written on at or after `since`: created with
    one, or a write that touched it."""
    out: set[str] = set()
    for event in events or []:
        at = _w.parse_stamp(event.get("recorded_at"))
        if at is None or at < since:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        etype = str(event.get("event_type") or "")
        if etype == "TraceCreated":
            fields = payload.get("fields") or []
        elif etype == "TraceUpdated":
            fields = payload.get("changed_fields") or []
        else:
            continue
        if "cue" in [str(f) for f in fields]:
            out.add(str(event.get("trace_id") or ""))
    return out


def kept_candidates(rec: dict, all_buckets: list, events, scope=None) -> list[dict]:
    """The copy's thread candidates, each with `kept`: an entry the panel lists now (the
    `list` road under `scope`, the request's view: live, current, nothing it rests on
    withdrawn, deleted or held) carries a cue saying what the candidate meets, written
    after the dream was woven; and `ids`, those entries, newest written first — the ones
    the panel opens."""
    from . import visibility as _V
    candidates = [c for c in rec.get("candidates") or [] if isinstance(c, dict)]
    if not candidates:
        return []
    woven = _w.parse_stamp(rec.get("woven_at"))
    written = _cues_written_since(events, woven) if woven is not None else set()
    cues: list[tuple[str, str, dict]] = []
    for b in all_buckets or []:
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        cue = meta.get("cue")
        if (bid in written and isinstance(cue, dict)
                and _V.visible_for(meta, scope, road=_V.LIST).shown):
            cues.append((str(meta.get("created") or ""), bid, cue))
    cues.sort(key=lambda x: x[0], reverse=True)
    out = []
    for c in candidates:
        ids = [bid for _at, bid, cue in cues if _matches(c.get("meet") or "", cue)]
        out.append({"meet": c.get("meet") or "", "recall": c.get("recall") or "",
                    "kept": bool(ids), "ids": ids})
    return out


def _row(rec: dict, all_buckets: list, events, scope=None) -> dict:
    cleared = bool(rec.get("cleared"))
    state = rec.get("state") if rec.get("state") in _ORDER else WAITING
    return {"id": rec.get("id"), "night": rec.get("night"), "state": state,
            "state_words": CLEARED_WORDS if cleared else STATE_WORDS[state],
            "text": None if cleared else rec.get("text"),
            "whole": bool(rec.get("whole")) and not cleared,
            "nightmare": bool(rec.get("nightmare")),
            "woven_at": _stamp(rec.get("woven_at")),
            "degraded_at": _stamp(rec.get("degraded_at")),
            "gone_at": _stamp(rec.get("gone_at")),
            "cleared": cleared,
            "kept": [] if cleared else kept_candidates(rec, all_buckets, events, scope)}


def _stamp(value) -> str | None:
    at = _w.parse_stamp(value)
    return _w.to_local(at).isoformat(timespec="seconds") if at is not None else None


def panel_view(records: list[dict], all_buckets: list, events, *, now: datetime,
               offset: int, limit: int, as_of: datetime, scope=None) -> dict:
    """The dream page: one row per copy of the last three natural days, newest first,
    paged. A copy woven after `as_of` is left out (a dream since the first page).
    `events`: the ledger lines (for `kept`), or a callable giving them — read only when a
    copy has candidates to judge. `scope`: the request's view (the registry's word on
    what the entries carrying a kept thread rest on)."""
    today = _today(now)
    rows = [r for r in records or []
            if not _aged_out(r, today) and not past(_w.parse_stamp(r.get("woven_at")), as_of)]
    rows.sort(key=lambda r: str(r.get("woven_at") or ""), reverse=True)
    if callable(events):
        events = events() if any(r.get("candidates") and not r.get("cleared")
                                 for r in rows) else []
    events = list(events or [])
    return page([_row(r, all_buckets, events, scope) for r in rows], offset, limit, as_of)
