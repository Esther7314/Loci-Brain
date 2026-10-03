"""
========================================
core/_case_recall.py — 场景常来: a scene that keeps coming back
========================================

Plan part three, two. Some scenes come almost every day, and every time they are met from
scratch: what was learnt the last few times does not come up by itself. When a new event's
body carries a scene word that the library has on MIN_DAYS or more distinct days of the last
WINDOW_DAYS, grow's return asks once —
「X 最近常出现（14 天里 6 天），前几次有没有经验值得留下？」 — and the model writes a cue
(「下次碰到 X，回去看 Y 那次」) or a line of experience, or skips it.

It is not muse's word burst: a burst is a word that suddenly comes much more often, and a
scene that is always there is exactly what a burst skips as "how things are".

------------------------------------------------------------
How a word is counted
------------------------------------------------------------
  · the words are the library's scene tags (core/_muse.is_scene_word): a tag is a word
    that stands literally in its own body. The new entry's tags are not backfilled yet when
    the return is written, so the library's tags are matched against the new body, by the
    strong reminder's rules (core/_cue.Message.find: Latin whole-word, a CJK word of two
    characters only on word edges)
  · a day is the day the entry happened (`when`, else the day it was written), local time;
    only events that happened count (not a want, not a dream or an imagining)
  · only what the gate lets this caller see counts (core/visibility, road `case_recall`,
    under the read scope), and only those are named

------------------------------------------------------------
When it is not asked
------------------------------------------------------------
  · the same word was asked about in the last QUIET_DAYS (kept in `_state/ASKED_FILE`:
    word -> day asked, pruned as it is written, so it stays a handful of lines)
  · a live cue the caller may read already hangs on the word (its condition or a phrasing
    carries it): the experience is already written as a cue
  · more than one word qualifies: only the one on the most days is asked; the rest wait
    for their next write

Nothing runs between writes: the count is taken from the library at the moment of the
write, and the asked file is the only thing written.

Exports: WINDOW_DAYS · MIN_DAYS · QUIET_DAYS · ASKED_FILE · ROAD · Question · ask() ·
         render() · record_asked() · record_shown()
========================================
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from utils import is_closed, is_telic, parse_bool

from . import _cue
from . import _holds as _H
from . import _when as _w
from . import visibility as _V
from ._muse import is_scene_word
from ._rooms import is_event_room
from .profile import short_id

logger = logging.getLogger("loci_brain.case_recall")

WINDOW_DAYS = 14
MIN_DAYS = 5
QUIET_DAYS = 14
ASKED_FILE = "case_recall_asked.json"
ROAD = "write.case_recall"          # the usage log's road for the entries a question named
_NAMED_MAX = 3                      # how many earlier entries the question names
_MIN_CJK_WORD = 2

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class Question:
    word: str
    days: int
    earlier: list[tuple[str, date]] = field(default_factory=list)   # (id, day), newest first


def _meta(b) -> dict:
    return (b or {}).get("metadata") or {}


def _day_of(meta: dict) -> Optional[date]:
    """The local day the entry happened: a `when` that names one day (with or without a
    clock time), else the day it was written. A span, a length or nothing readable falls back
    to the day it was written."""
    when = str(meta.get("when") or "").strip()
    if _DAY_RE.match(when) and ".." not in when:
        t = _w.parse_stamp(when)
        if t is not None:
            return _w.to_local(t).date()
    t = _w.parse_stamp(meta.get("created"))
    return _w.to_local(t).date() if t is not None else None


def _counts(event: bool, meta: dict) -> bool:
    return (event and not is_telic(meta)
            and not parse_bool(meta.get("internally_generated"), default=False))


def _cued_words(buckets: list, scope=None) -> list[str]:
    """The normalised conditions and phrasings of every live, open cue the read scope may
    read: a cue out of scope does not keep the question back, or whether it is asked would
    say that cue exists."""
    out: list[str] = []
    for b in buckets:
        m = _meta(b)
        cue = m.get("cue")
        if not isinstance(cue, dict) or not str(cue.get("condition") or "").strip():
            continue
        if is_closed(m) or _V.state_of(m) != _V.LIVE or str(m.get("superseded_by") or "").strip():
            continue
        if scope is not None and not scope.permits(m):
            continue
        out += [_cue.norm(p) for p in [cue.get("condition"), *(cue.get("phrasings") or [])] if p]
    return out


def _asked_path(buckets_dir: str) -> str:
    return os.path.join(str(buckets_dir or "."), "_state", ASKED_FILE)


def load_asked(buckets_dir: str) -> dict[str, str]:
    """{normalised word: the day it was last asked about, YYYY-MM-DD}. Unreadable is empty:
    the worst that costs is one question asked again."""
    try:
        with open(_asked_path(buckets_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    asked = data.get("asked") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in asked.items()} if isinstance(asked, dict) else {}


def _recently_asked(asked: dict[str, str], word: str, today: date) -> bool:
    try:
        day = date.fromisoformat(asked.get(word, ""))
    except ValueError:
        return False
    return (today - day).days < QUIET_DAYS


def record_asked(buckets_dir: str, word: str, today: date) -> None:
    """Remember that `word` was asked about today, dropping what is past the quiet period.
    Written whole and replaced; two windows asking at the same moment can lose one line,
    which only means one more question later."""
    key = _cue.norm(word)
    asked = {k: v for k, v in load_asked(buckets_dir).items()
             if _recently_asked({k: v}, k, today)}
    asked[key] = today.isoformat()
    path = _asked_path(buckets_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "asked": asked}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("case recall: could not remember 「%s」 was asked: %s", word, e)


def ask(buckets: list, texts: list[str], *, buckets_dir: str, scope=None,
        now: Optional[datetime] = None) -> Optional[Question]:
    """The one scene question these new bodies earn, or None. `buckets` is the library as it
    stood before the write; nothing is written here (record_asked does that, once the
    question is handed out)."""
    texts = [t for t in texts if str(t or "").strip()]
    if not texts:
        return None
    now = now or _w.now()
    today = _w.to_local(now).date()
    first = today - timedelta(days=WINDOW_DAYS - 1)
    holds = _H.hold_index(buckets)
    days: dict[str, dict[date, str]] = {}       # word -> {day: newest id that day}
    spelled: dict[str, str] = {}
    created: dict[str, str] = {}
    for b in buckets:
        m = _meta(b)
        bid = str(m.get("id") or (b or {}).get("id") or "")
        if not bid or not _counts(is_event_room(m.get("room")), m):
            continue
        day = _day_of(m)
        if day is None or not first <= day <= today:
            continue
        words = [str(t).strip() for t in m.get("tags") or [] if is_scene_word(t)]
        if not words:
            continue
        if not _V.visible_for(m, scope, road=_V.CASE_RECALL, now=now, holds=holds):
            continue
        created[bid] = str(m.get("created") or "")
        for w in words:
            key = _cue.norm(w)
            spelled.setdefault(key, w)
            seen = days.setdefault(key, {})
            if day not in seen or created[bid] > created.get(seen[day], ""):
                seen[day] = bid
    qualifying = {k: v for k, v in days.items() if len(v) >= MIN_DAYS}
    if not qualifying:
        return None
    asked = load_asked(buckets_dir)
    cued = _cued_words(buckets, scope)
    messages = [_cue.Message(t) for t in texts]
    best: Optional[tuple[int, int, str]] = None
    for key, by_day in qualifying.items():
        if _recently_asked(asked, key, today) or any(key in c for c in cued):
            continue
        if not any(msg.find(key, min_cjk=_MIN_CJK_WORD, edges=len(key) <= 2) for msg in messages):
            continue
        rank = (len(by_day), len(key), key)
        if best is None or rank > best:
            best = rank
    if best is None:
        return None
    key = best[2]
    by_day = qualifying[key]
    earlier = sorted(((bid, d) for d, bid in by_day.items()), key=lambda p: p[1], reverse=True)
    return Question(spelled[key], len(by_day), earlier[:_NAMED_MAX])


def render(q: Question) -> str:
    """The line a write's return carries for the question."""
    named = "、".join(f"{short_id(bid)}（{d.strftime('%m-%d')}）" for bid, d in q.earlier)
    return (f"🔂 「{q.word}」最近常出现（{WINDOW_DAYS} 天里 {q.days} 天），"
            f"前几次有没有经验值得留下？最近几次：{named}。"
            f"有就写成线索（cue：下次碰到「{q.word}」回去看哪次）或一句经验，没有就跳过。")


def record_shown(store, q: Question) -> None:
    """The usage log's `shown` line for the earlier entries a question named."""
    usage = getattr(store, "usage", None)
    if usage is None:
        return
    try:
        from . import _usage
        usage.record(_usage.SHOWN, [bid for bid, _d in q.earlier], ROAD)
    except Exception as e:  # noqa: BLE001 - the log is a record, not part of the write
        logger.warning("case recall: usage log not written: %s", e)
