"""
========================================
core/profile.py — the single contract shared by the awakening and the profile page
========================================

The rules behind "the note at the door" and "something suddenly comes back" live here,
once: this contract does not belong to breath alone — the profile page
(`web/loci_reads.py`) and the awakening (`tools/breath/awaken.py`) have to read the same
rules, and both import them from here. It sits with the other engine pieces
(`_fold` / `_rooms` / `_when`) so nothing invites the assumption that it is breath's
private property.

On top of this contract, these are all decided on the **read** side,
with no new persisted field (the one tag convention below excepted):
① **The three kinds of want-clock**: the kind is not a stored field, it is inferred from
   how `when` was filled in — empty = `等触发` (waiting for a trigger), a duration mark
   `<N>[dwmy]` = `有量级` (has a magnitude), `YYYY-MM-DD` = `有期限` (has a deadline).
② **Only the oldest one gets asked as a question**: the `heavy` list is still returned
   in full, plus one extra `heavy_question_id` — the id of the one that has been
   **hanging longest** (not the heaviest). The render layer uses it to decide which one
   turns into a question and which ones stay statements.
③ **A notification that a fact was edited from the panel**: alongside `event_pool()`
   there is `edited_by_user()`, which scans for the edit tag plus "not yet folded away"
   (`_F.is_covered()`). That is the entire notification mechanism: the tag is both the
   mark and the notice, and no separate notification table was opened.
④ **Holds** (`core/_holds.py`): an entry with a live hold on it (either level) stays off
   ⏰ reminders, 🫀 weighing on me and "something suddenly comes back"; a hold entry is
   never an item of its own on those roads — it belongs with the entry it is hung on.

⑤ **Awake / asleep and breath's blocks** (`is_accessible`, `prospective`, `involuntary`):
   below `edited_by_user`. Awake is computed on every read and never stored; 惦记的事 is
   chosen from what is awake, 忽然想起 mostly from what sleeps. `door_note`'s reminders
   and weighing-on-me lists stay for the profile page until it is redrawn.
⑥ **The panel's surface and trace pages** (`awake_pool`, `hanging`): the awake pool with
   every reason it is awake, and what still hangs open with the buttons that close it.
   The words both skins show are made here once: `reason_words` (why a 惦记的事 line is
   there now — breath's text and the panel say the same words) and `AWAKE_WORDS`.

Which entries each block may show is the gate's (`core/visibility.py`): every block asks
`visible_for()` with its own road — `prospective` for 惦记的事, `review` for its one held
line, `remind` for the profile page's ⏰ / 🫀, `door` for the rules and the profile page,
`sudden` for 忽然想起, `edited` for the panel's corrections — and what stays here is each
block's own business (an EVENT room, a want, a date). A clock time later today holds an
entry back on every breath road (the gate's `later_today`); `due_now` is the moment it
comes due.

Exports: door_note(all_buckets, now) / event_pool(all_buckets, now=None) /
         edited_by_user(all_buckets) / BreathSettings / breath_settings(config) /
         RECENT_WINDOW_DAYS / days_words(n) / near_days(n) /
         due_day(meta, today) / awake_reasons(meta, now, …) / is_accessible(meta, now, …) /
         is_open_promise(meta) / written_days_ago(meta, today) / due_now(meta, now) /
         prospective(all_buckets, now, …) / involuntary(all_buckets, now, …) /
         label_of(e) / entry_label(meta, content) / short_id(bucket_id) / owed_names(bound) /
         reason_words(reason) / AWAKE_WORDS / awake_words(key, meta, today, …) /
         awake_pool(all_buckets, now, …) / hanging(all_buckets, now, …) / DONE · DROP ·
         WITHDRAW
========================================
"""

import random
import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta

from utils import get_ai_name, get_owner_name, is_closed, is_telic

from . import _fold as _F         # anything covered stops surfacing on its own
from . import _holds as _H        # a live hold keeps its entry off the three roads
from . import _invalidation as _I  # a panel correction looked at and kept leaves 依据变了的
from . import _when as _w          # "today" on the local calendar
from . import names as _names     # the names table: which spellings stand for the AI
from ._muse import is_scene_word  # 忽然想起 links on the same scene words muse clusters on
# is_mind_room is deliberately not imported: the door does not filter rules by room
# (see the long note further down)
from ._rooms import is_event_room
from . import visibility as _V    # the one gate: what may be put in front of the model

_PROFILE_TAG = "__档案事实__"
_BIGEVENT_TAG = "__大event__"
_REMIND_DAYS = 30

# ------------------------------------------------------------
# The three kinds of want-clock
# ------------------------------------------------------------
# The tag written when a fact is edited from the panel gets its own constant here,
# next to _PROFILE_TAG / _BIGEVENT_TAG, so that the two ends (the write side in
# web/loci.py and the read side here) cannot drift apart by each spelling the string
# out for themselves.
# The tag a memory gets when a PERSON corrected it, as opposed to the assistant
# revising its own view. Nothing on disk carried the old value, and every reference
# goes through this constant, so the wording is free to be neutral.
_EDITED_BY_USER_TAG = "人改的"

# Duration mark: `<N><unit>`, with no prefix symbol — an earlier `~` prefix was cut,
# because a symbol that carries no meaning does not earn its place.
# It cannot be confused with the date form `\d{4}-\d{2}-\d{2}`: a duration mark contains
# no dash, so the two are unambiguous by construction.
_DURATION_RE = re.compile(r"^(\d+)([dwmy])$")
_DURATION_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def _magnitude_days(w: str) -> float | None:
    """Duration mark -> roughly how many days. None if unrecognised, i.e. not the
    `有量级` kind at all."""
    m = _DURATION_RE.match(w)
    if not m:
        return None
    n = int(m.group(1))
    return n * _DURATION_UNIT_DAYS[m.group(2)]


def _magnitude_loudness(ratio: float) -> str:
    """The curve belonging to the `有量级` kind: days held divided by days of magnitude.

    Two calibration points, frozen into the smoke test:
    "within this week" (7 days) held for 10 days -> ratio=1.43 -> should nag;
    "within this year" (365 days) held for 10 days -> ratio=0.027 -> should not.
    The thresholds themselves are a proposal reverse-engineered from those two points,
    not sacred numbers. If they turn out to feel wrong in use, move them.
    """
    if ratio < 0.7:
        return "far"
    if ratio < 1.0:
        return "near"
    if ratio < 1.75:
        return "soon"
    return "now"


def _want_clock(meta: dict, created, now: datetime) -> tuple[str, str, str]:
    """Sort one want into `有期限` / `有量级` / `等触发` / `旧数据待复核` (deadline /
    magnitude / waiting for a trigger / old data needing review), and work out how loudly
    that kind should speak.

    Returns (clock, loud, note): `clock` is for internals and debugging only, `note` is a
    one-line explanation meant for human eyes (when non-empty it should be surfaced by
    the display layer — today only the `旧数据待复核` kind ever produces one).

    🔴 This only handles wants that have **already landed in the "weighing on me" pool**
    (i.e. overdue, or with no future `when`). A deadline that has not arrived yet goes
    through the existing `reminders` branch in `door_note()` below, and is not judged
    twice here.
    """
    w = str(meta.get("when") or "").strip()
    if not w:
        return "等触发", "far", ""

    mag = _magnitude_days(w)
    if mag is not None:
        held = (now.date() - created.date()).days if created else 0
        ratio = (held / mag) if mag else 0.0
        return "有量级", _magnitude_loudness(ratio), ""

    m = re.match(r"(\d{4}-\d{2}-\d{2})", w)
    if m:
        try:
            when_date = _w.parse_date(m.group(1))
        except ValueError:
            return "旧数据待复核", "far", "没定期限 —— when 的格式认不出来，它不会自己催你"
        # A minefield in older data, confirmed against a real store: when `when` equals
        # `created`, nine times out of ten it is a placeholder from someone dropping
        # "today" into `when` in passing, not a genuine deadline.
        # Structurally there is no way to tell "genuinely due that day" from "a placeholder
        # filled in by mistake", so this errs on the conservative side: better to miss a nag
        # than to nag wrongly. (In the spirit of not handing memory over to the system to
        # operate on.)
        if created and when_date.date() == created.date():
            return "旧数据待复核", "far", "没定期限 —— 存的那天顺手填成了 when，它不会自己催你"
        # Reaching this point means the want is overdue and still unclosed: anything not
        # overdue is intercepted by the reminders branch and never enters the heavy pool.
        # The anchor becomes "how many days overdue", and the curve simply reuses the
        # existing `_held_loudness` — the smallest possible change. If "late" turns out to
        # deserve a curve of its own, that gets split out then.
        overdue = max(0, (now.date() - when_date.date()).days)
        return "有期限", _held_loudness(overdue), ""

    return "旧数据待复核", "far", "没定期限 —— when 的格式认不出来，它不会自己催你"


def _f_weight(x) -> float:
    """Read weight as a float, falling back to 0.5 when unreadable. **A real 0 must
    survive** (see the "weighing on me" note below)."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.5


def _reminder_loudness(days: int) -> str:
    """⏰ The nearer it is, the louder. **The thresholds live in exactly one place** and
    both skins read them from here."""
    return "now" if days == 0 else "soon" if days <= 3 else "near" if days <= 14 else "far"


def _held_loudness(held: int) -> str:
    """🫀 The longer it hangs, the louder. What this nags about is "are you actually going
    to do it", not "the date is coming up"."""
    return "now" if held >= 60 else "soon" if held >= 30 else "near" if held >= 7 else "far"


def event_pool(all_buckets: list, now: datetime | None = None, *, scope=None) -> list[dict]:
    """The pool behind "something suddenly comes back": events that are visible, live in
    an EVENT room, have **not been covered**, are not held (a hold entry, or an entry
    with a hold live on it at `now`, local today when omitted), and were not put out of
    mind with `dont_surface`.

    🔴 Five gates; miss any one of them and it fails silently:
    ① `is_event_room()` accepts both old and new room names. A hand-written
       `.find("/EVENT/") > 0` would miss the new name `EVENT/SELF`, which contains no
       `/EVENT/` at all, so the pool would **go silently empty** with no error.
    ② Machinery (the name page, periods) is not a memory.
    ③ **Covered entries stay out of this pool.** Coming across something suddenly is a
       chance encounter, whereas anything already folded has been given a name — it
       should appear inside recall as that sentence, not tap me on the shoulder again as
       a loose entry. An old version (`superseded_by`) stays out for the same reason.
    ④ **Held entries stay out**, at either level: a hold asked for it not to come up
       for now. The hold entry itself stays out too; it rides with what it is hung on.
    ⑤ **`dont_surface` stays out**: it says "do not bring this up by yourself", and
       coming across something suddenly is the purest case of that.
    ③④⑤ and the entry's state are the gate's `sudden` road (`core/visibility.py`).
    ⚠️ A future threshold engine's candidate pool has to pass the same gate:
       **this is the anchor point for it.**
    """
    now = now or _w.now()
    holds = _H.hold_index(all_buckets)
    pool: list[dict] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not _V.timeline_kind(meta) or not is_event_room(meta.get("room")):
            continue
        if not _V.visible_for(meta, scope, road=_V.SUDDEN, now=now, holds=holds):
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid:
            continue
        pool.append({"id": bid, "meta": meta, "content": str(b.get("content") or "")})
    return pool


def door_note(all_buckets: list, now: datetime, *, scope=None) -> dict:
    """Name + rules + ⏰ reminders + 🫀 what is weighing on me + the list of periods —
    **one pass over the store, one set of rules.**

    Returns {"facts": [...], "facts_covered": [...], "rules": [...], "reminders": [...],
             "heavy": [...], "big": [...], "entries": [...]}, with the raw `meta` /
    `content` carried on each element (`facts_covered` carries only {"id", "by"}: a
    profile page that has been replaced, and what replaced it). Rendering (the text
    skin, the JSON skin) is each caller's own business: **not one judgement is made in
    the render layer.**
    """
    facts: list[dict] = []        # live profile pages, earliest first; the door warns if >1
    facts_covered: list[dict] = []  # profile pages something has replaced: {"id", "by"}
    rules: list[dict] = []
    reminders: list[dict] = []
    heavy: list[dict] = []        # weighing on me: wants with no date, or a date that passed unresolved
    big: list[dict] = []          # periods (big events) — absent from the awakening, listed on the profile page
    entries: list[dict] = []      # visible memories by recall's definition (for the random pick / the earliest one)
    holds = _H.hold_index(all_buckets)

    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        content = str(b.get("content") or "")
        tags = [str(t) for t in (meta.get("tags") or [])]

        if _PROFILE_TAG in tags:
            # 🔴 A page that has been re-versioned or folded keeps its tag on disk, and
            #    being older it would sort ahead of its successor for good — the door
            #    would go on showing the old text with nothing to say it is old. Only a
            #    page nothing covers is the door. The covered ones come back separately,
            #    so that an empty cell can say which page went and what replaced it.
            page = _V.visible_for(meta, scope, road=_V.DOOR)
            if page:
                facts.append({"id": bid, "created": str(meta.get("created") or ""),
                              "content": content})
            elif page.reasons == (_V.COVERED,):
                facts_covered.append({"id": bid, "by": _F.covers_of(meta)})
            continue
        if _BIGEVENT_TAG in tags:
            # Periods do not appear in the awakening, but the profile page lists a line
            # for each, so they are collected here (resolved ones are not listed).
            if str(meta.get("status") or "") != "resolved":
                big.append({"id": bid, "meta": meta, "content": content})
            continue

        # Reminders: `when` within the next 30 days (wants and ordinary events alike).
        # Nothing closed (resolved/abandoned), deliberately forgotten, or superseded
        # gets a reminder; nor does a hold, or anything a live hold is hung on. The same
        # test keeps both out of "weighing on me" below.
        _status = str(meta.get("status") or "")
        _telic = is_telic(meta)
        _remindable = (not is_closed(meta)  # only `status` marks an ending; the old booleans stay read-only for compatibility
                       and _V.visible_for(meta, scope, road=_V.REMIND, now=now,
                                          holds=holds).shown)
        w = str(meta.get("when") or "") if _remindable else ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", w)
        _reminded = False
        if m:
            try:
                d = datetime.fromisoformat(m.group(1))
                days = (d.date() - now.date()).days
                # "It is today" is reserved for wants — something you want to happen
                # rings when its day arrives. An event that already happened carrying
                # today's `when` is a record, not a reminder; otherwise every single
                # entry stored today would shout "that's today!" and push the real
                # reminders out of all three slots. (Which is exactly what happened.)
                if 0 <= days <= _REMIND_DAYS and (days > 0 or _telic):
                    reminders.append({"id": bid, "meta": meta, "content": content,
                                      "days": days, "when": m.group(1),
                                      "status": _status, "telic": _telic,
                                      "loud": _reminder_loudness(days)})
                    _reminded = True
            except ValueError:
                pass

        # Weighing on me: a want with **no date at all**, or one whose **date passed
        # without it being closed**. A want has only two endings, both of which have to
        # be marked by hand; nothing closes itself.
        # 🔴 There is no automatic closing (that would quietly erase things left
        #    undone). Instead **the number of days it has hung is pushed into view**:
        #    on day 40 with still nothing done, that number asks the question by itself.
        if _telic and _remindable and not _reminded:
            _c = _w.parse_stamp(meta.get("created"))
            # ⚠️ Not `float(meta.get("weight") or 0.5)`: in Python `0.0 or 0.5` is 0.5,
            #    so **an entry whose weight is genuinely 0 would be ranked as 0.5** and
            #    keep pressing in plain view.
            #    Only a missing field or an empty string counts as 0.5; **a real 0 is 0**.
            _wt = meta.get("weight")
            _held = (now.date() - _c.date()).days if _c else 0
            # The three want-clocks only change how "how loud" is computed; the meaning
            # of held/weight is untouched.
            _clock, _loud, _note = _want_clock(meta, _c, now)
            _asked = str(meta.get("last_asked") or "")
            heavy.append({"id": bid, "meta": meta, "content": content,
                          "weight": 0.5 if _wt in (None, "") else _f_weight(_wt),
                          "held": _held, "loud": _loud,
                          "clock": _clock, "clock_note": _note,
                          "last_asked": _asked})

        if not _V.on_timeline(meta, scope):
            continue
        # A rule is **something pinned**. That is the whole test.
        #
        # 🔴 No second filter ("and the room has to be MIND, or the body has to say 'rule
        #    of conduct'"): **pinning something IS a judgement, already made.** Overruling
        #    it with a room amounts to letting an old bulk migration, which mapped rooms by
        #    name and understands nothing about content, **overturn a judgement made
        #    deliberately today** — a pinned note that happens to sit in EVENT/SELF would
        #    show **not a single character at the door**, with no error and no warning.
        #    **A silent filter is worse than a rejection**: a rejection gets fixed, while
        #    silence leaves you believing the thing is there. It also means the mess in the
        #    room field does not block the door and can be cleaned up at leisure.
        # 🔴 **A superseded or covered version is not a rule**: an old version is off the
        #    timeline already, and a covered one is kept off by the `door` road —
        #    otherwise the door would display a rule I have already changed my mind about.
        if meta.get("pinned") and _V.visible_for(meta, scope, road=_V.DOOR):
            rules.append({"id": bid, "meta": meta, "content": content})
        entries.append({"id": bid, "meta": meta, "content": content})

    facts.sort(key=lambda f: f["created"])
    reminders.sort(key=lambda r: r["days"])
    # Heaviest first; ties broken by whichever has hung longer. This ordering is for the
    # "list the whole thing" half and was left as it was.
    heavy.sort(key=lambda h: (-h["weight"], -h["held"]))
    # One statement becomes a question, and **only the one that has hung longest** —
    # "longest" meaning the clock itself (held), not the weight-first ordering used for
    # the list above. "Which comes first" is two different questions here, so this is
    # computed separately rather than by reordering `heavy` or truncating it.
    heavy_question_id = (max(heavy, key=lambda h: h["held"])["id"] if heavy else "")
    return {"facts": facts, "facts_covered": facts_covered, "rules": rules,
            "reminders": reminders, "heavy": heavy, "big": big, "entries": entries,
            "heavy_question_id": heavy_question_id}


def edited_by_user(all_buckets: list, *, scope=None) -> list[dict]:
    """The notification pool for "a fact was edited from the panel": events carrying the
    `_EDITED_BY_USER_TAG` tag that have **not been folded away**.

    That is the entire notification mechanism: the tag is simultaneously the mark that an
    edit happened and the test for "not yet looked at" (`_F.is_covered()`). Agreeing with
    an edit means folding it, after which it drops out of this pool by itself; disagreeing
    leaves it sitting here until the disagreement has actually been talked through and
    something is done about it (folded, or deliberately left alone).
    Reviewing these in batches is not implemented here, but this pool is already the
    substrate for it — a future batch view in muse would draw from exactly this.
    Keeping one as it is, without folding it, is the confirm gesture on trace
    (core/_invalidation.py): the correction stays on disk with its tag, and leaves the pool.
    """
    pool: list[dict] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        tags = [str(t) for t in (meta.get("tags") or [])]
        if _EDITED_BY_USER_TAG not in tags or _I.edit_confirmed(meta):
            continue
        if not _V.on_timeline(meta, scope) or not _V.visible_for(meta, scope, road=_V.EDITED):
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid:
            continue
        pool.append({"id": bid, "meta": meta, "content": str(b.get("content") or "")})
    pool.sort(key=lambda e: str(e["meta"].get("created") or ""))
    return pool


# ============================================================
# Awake / asleep, and breath's blocks
# ============================================================

def short_id(bucket_id: str) -> str:
    """The 6-character handle breath prints (a readable id stays whole)."""
    return bucket_id[:6] if re.fullmatch(r"[0-9a-f]{12}", bucket_id) else bucket_id


# What `bound` holds for the AI when nothing better is known: the placeholder, and the
# first-person words a bound list may still carry from before they were normalised
# (core/names.normalize_bound turns 我 / 自己 into the AI's name).
_AI_PLACEHOLDERS = frozenset({"AI", "我", "自己"})


def owed_names(bound) -> str:
    """Who owes it, as the AI reads it: a bound name that denotes the AI (the placeholder,
    the configured AI name, or a spelling the names table files under it) is 「我」, every
    other name as stored; 「我、小林」. "" when nobody is bound. Text only: the stored names
    and every JSON skin keep the names as they are."""
    ai = get_ai_name()
    mine = set(_AI_PLACEHOLDERS) | {ai, _names.canonical(ai) or ai}
    out: list[str] = []
    for raw in bound or []:
        n = str(raw or "").strip()
        if not n:
            continue
        shown = "我" if (n in mine or (_names.canonical(n) or n) in mine) else n
        if shown not in out:
            out.append(shown)
    return "、".join(out)


def label_of(e: dict) -> str:
    """The display text for a memory `{"meta", "content"}`: gist > name (timestamp
    stripped) > the start of the body. Every one of them is text written down at storage
    time. recall shows it whole; entry_label below is the same rule cut to one line."""
    meta = e["meta"]
    s = str(meta.get("summary") or "").strip()
    if s:
        return s
    name = re.sub(r"^[\d\- :]+", "", str(meta.get("name") or "")).strip()
    if name:
        return name
    return re.sub(r"\s+", " ", e["content"])[:40]


def entry_label(meta: dict, content: str) -> str:
    """What a line shows of an entry: its summary, else its name without the timestamp,
    else the start of its body — all written when the entry went in."""
    s = str(meta.get("summary") or "").strip()
    if not s:
        name = re.sub(r"^[\d\- :]+", "", str(meta.get("name") or "")).strip()
        s = name or re.sub(r"\s+", " ", content or "")[:40]
    return s[:60]


@dataclass(frozen=True)
class BreathSettings:
    """The numbers breath's reading rests on. Every one is a first guess from the plan,
    to be tried against a real library; the host's config overrides each
    (`breath_settings`, keys in `_SETTING_KEYS`)."""
    date_days: int = _REMIND_DAYS     # a date this many days ahead keeps an entry awake
    recent_days: int = 3              # written this recently: awake
    cue_days: int = 7                 # a strong-reminder card delivered this recently: awake
    prospective_lines: int = 5        # 惦记的事 shows this many; the rest are counted
    hanging_days: int = 30            # an undated promise hanging this long is asked 「还算数吗」
    promise_questions: int = 2        # 「像是答应过的」 questions under the list
    backfill_mark_days: int = 3       # a backfilled date says 「补的」 for its first days listed
    involuntary_lines: int = 2        # 忽然想起: one linked to the last few days, the rest random
    invalidation_lines: int = 5       # 依据变了的 shows this many; the rest are counted
    recent_window_days: int = 3       # 近三天: recall's overview over this many days, one card


_SETTING_KEYS = {
    "date_days": "awake_date_days",
    "recent_days": "awake_recent_days",
    "cue_days": "awake_cue_days",
    "prospective_lines": "prospective_lines",
    "hanging_days": "prospective_hanging_days",
    "promise_questions": "prospective_promise_questions",
    "backfill_mark_days": "prospective_backfill_mark_days",
    "involuntary_lines": "involuntary_lines",
    "invalidation_lines": "invalidation_lines",
    "recent_window_days": "breath_recent_days",
}

# breath's recent block covers at least a day and at most a month: the card is one cell
# of recall's overview, and past a month it says little about "the last few days".
# web/config_api.py takes `breath_recent_days` within the same range.
RECENT_WINDOW_DAYS = (1, 30)
_SETTING_RANGES = {"recent_window_days": RECENT_WINDOW_DAYS}


def breath_settings(config) -> BreathSettings:
    """`surfacing.<key>` from the host's config; the default for a key that is absent or
    unreadable, never below 0, and kept within its range for a key that has one
    (`_SETTING_RANGES`)."""
    sf = (config or {}).get("surfacing") or {}
    values = {}
    for f in fields(BreathSettings):
        raw = sf.get(_SETTING_KEYS[f.name], f.default)
        lo, hi = _SETTING_RANGES.get(f.name, (0, None))
        try:
            val = max(lo, int(raw))
        except (TypeError, ValueError):
            val = f.default
        values[f.name] = val if hi is None else min(hi, val)
    return BreathSettings(**values)


_CN_DIGITS = "零一二三四五六七八九"


def days_words(n: int) -> str:
    """A count of days as the words around it read: Chinese numerals up to thirty (两 for
    2: 这两天, 近两天), the digits with a space on each side beyond (近 45 天)."""
    n = int(n)
    if n == 2:
        return "两"
    if 0 <= n <= 30:
        tens, ones = divmod(n, 10)
        if tens == 0:
            return _CN_DIGITS[ones]
        head = "" if tens == 1 else _CN_DIGITS[tens]
        return f"{head}十{_CN_DIGITS[ones] if ones else ''}"
    return f" {n} "


def near_days(n: int) -> str:
    """「近三天」 for a window of n days: breath's recent block title, and the awake
    reason for something written that recently."""
    return f"近{days_words(n)}天"


_YEARLY = "FREQ=YEARLY"


def _local_day(value) -> date | None:
    stamp = _w.parse_stamp(value)
    return stamp.date() if stamp else None


def written_days_ago(meta: dict, today: date) -> int | None:
    """How many local days before `today` the entry was written (its `created`); 0 for
    today, None when it has no readable `created`."""
    created = _local_day(meta.get("created"))
    return (today - created).days if created else None


def _next_yearly(d: date, today: date) -> date:
    """A yearly date's occurrence on or after `today` (29 February is the 28th in a common
    year)."""
    for year in (today.year, today.year + 1):
        try:
            day = d.replace(year=year)
        except ValueError:
            day = date(year, 2, 28)
        if day >= today:
            return day
    return d


def due_day(meta: dict, today: date) -> date | None:
    """The local day an entry is due: its `when`; a yearly one's next occurrence on or
    after `today`; something wanted given a length ("3w") its created day plus that length.
    None when it has no date — and for a hold or a period, whose days say when they hold or
    what they span, not when anything is due."""
    tags = [str(t) for t in (meta.get("tags") or [])]
    if _H.is_hold(meta) or _BIGEVENT_TAG in tags:
        return None
    w = str(meta.get("when") or "").strip()
    if not w:
        return None
    if is_telic(meta):
        length = _magnitude_days(w)
        if length is not None:
            created = _local_day(meta.get("created"))
            return created + timedelta(days=int(length)) if created else None
    d = _local_day(w)
    if d is None:
        return None
    if str(meta.get("recurrence") or "").strip().upper() == _YEARLY:
        return _next_yearly(d, today)
    return d


# Why an entry is awake — `awake_reasons()`; one is enough.
PROMISED = "promised"   # telic, someone bound by it, not closed
DATED = "dated"         # due within `date_days`, or past due and still wanted
RECENT = "recent"       # written within `recent_days`
CUED = "cued"           # a strong-reminder card delivered within `cue_days`
HOLD = "hold"           # a hold that holds today

# The words each reason is said with on the panel. A date that has passed says how long
# ago instead (`awake_words`).
# `recent` names the window `awake_recent_days` sets (`awake_words`: 「近五天写的」);
# these are its words at the default.
AWAKE_WORDS = {PROMISED: "答应了没做", DATED: "日子快到了", RECENT: "近三天写的",
               CUED: "刚被线索碰上", HOLD: "自己是条子"}


def awake_words(key: str, meta: dict, today: date, *,
                settings: BreathSettings | None = None) -> str:
    """The words for one awake reason of this entry: `AWAKE_WORDS`, except a date already
    past, which says 「过了 N 天」, and `recent`, which names `settings.recent_days`."""
    if key == DATED:
        due = due_day(meta, today)
        if due is not None and due < today:
            return f"过了 {(today - due).days} 天"
    if key == RECENT:
        return f"{near_days((settings or BreathSettings()).recent_days)}写的"
    return AWAKE_WORDS.get(key, key)


def is_open_promise(meta: dict) -> bool:
    """Promised and not closed: something wanted that someone is bound by (`bound`), not
    resolved or abandoned, live, and the current version. The `promised` reason for being
    awake, and what recall lifts to the top of a search."""
    return (is_telic(meta) and bool(meta.get("bound")) and not is_closed(meta)
            and _V.state_of(meta) == _V.LIVE
            and not str(meta.get("superseded_by") or "").strip())


def awake_reasons(meta: dict, now: datetime, *, settings: BreathSettings | None = None,
                  delivered_at=None) -> tuple[str, ...]:
    """Which of the five conditions keep this entry awake at `now` (empty = asleep).

    Awake is about how easily an entry is noticed, never about how often it was looked
    up: being found by recall wakes nothing (recall never touches), or the more it was
    searched the hotter it would get. An entry archived, deleted or replaced by a newer
    version is not awake at all. `dont_surface` is not asleep — it closes the roads that
    come up by themselves, and that is the gate's (core/visibility.py). Awake is separate
    from decay: asleep is "not yet its time", not "forgotten".

    `delivered_at`: the card ledger's lookup, `bucket_id -> datetime | None` (a callable
    or a mapping) — when a strong-reminder card for this entry last reached the model, as
    the host confirmed (core/_cue_ledger.CueLedger.delivered_at). Without one, condition 4
    never holds.
    """
    s = settings or BreathSettings()
    if _V.state_of(meta) != _V.LIVE or str(meta.get("superseded_by") or "").strip():
        return ()
    today = now.date()
    closed = is_closed(meta)
    telic = is_telic(meta)
    out: list[str] = []
    if is_open_promise(meta):
        out.append(PROMISED)
    due = due_day(meta, today)
    if due is not None and not closed:
        days = (due - today).days
        if 0 <= days <= s.date_days or (days < 0 and telic):
            out.append(DATED)
    created = _w.parse_stamp(meta.get("created"))
    if created is not None and now - created <= timedelta(days=s.recent_days):
        out.append(RECENT)
    if delivered_at is not None:
        bid = str(meta.get("id") or "")
        seen = delivered_at(bid) if callable(delivered_at) else delivered_at.get(bid)
        if seen is not None and now - seen <= timedelta(days=s.cue_days):
            out.append(CUED)
    if _H.hold_is_live(meta, now):
        out.append(HOLD)
    return tuple(out)


def is_accessible(meta: dict, now: datetime, *, settings: BreathSettings | None = None,
                  delivered_at=None) -> bool:
    """Awake (`awake_reasons` names at least one condition) or asleep. Computed, never
    stored."""
    return bool(awake_reasons(meta, now, settings=settings, delivered_at=delivered_at))


def due_now(meta: dict, now: datetime) -> bool:
    """Open, current, and the clock time its `when` names today has come — the moment the
    gate stops holding it back (`later_today`, core/visibility.py).

    In a window already open when that moment passes, breath has been read; the entry
    reaches the model as a "time is up" card with the owner's next message (core/_cue.py)."""
    moment = _V.clock_moment(meta)
    if (moment is None or is_closed(meta) or _V.state_of(meta) != _V.LIVE
            or str(meta.get("superseded_by") or "").strip()):
        return False
    local = _w.to_local(now)
    return moment.date() == local.date() and moment <= local


# ------------------------------------------------------------
# 惦记的事 (prospective)
# ------------------------------------------------------------
# One list covering both ⏰ reminders and 🫀 weighing on me, chosen
# from what is awake. Every line says why it is here now, and there are only two kinds of
# reason:
#   dated    N days left / today / N days overdue. A want, something heard about the
#            future, a yearly day, a hold's review day.
#   undated  a promise (someone bound) with no date: asked 「要不要定个时间或条件」, or
#            once it has hung `hanging_days`, 「还算数吗」.
# Order: dated first — overdue at the very top by how far overdue (not by weight), then
# the nearest; undated after, the longest hanging first. Past `prospective_lines` nothing
# is dropped: the rest are counted on one line.
# Not on the list: a telic waiting on a `cue` with no date (it waits for the strong-
# reminder card); a want nobody owes and nothing dates (asleep); anything a live hold is
# on, and anything whose clock time today has not come yet (the `prospective` road),
# except the one question a `defer`'s review day asks.

# The words for each loudness of a dated reason. Which loudness a date has is
# `_reminder_loudness` (nearer is louder; overdue is louder still).
_LOUD_WORDS = {"overdue": "过了 {ago} 天", "now": "就是今天", "soon": "马上（还有 {days} 天）",
               "near": "快到了（还有 {days} 天）", "far": "记着（还有 {days} 天）"}


def reason_words(reason: dict) -> str:
    """Why a 惦记的事 line is there now, in words: 「快到了（还有 4 天）」 for a date,
    「挂了 40 天——还算数吗？」 / 「答应了，没定时间——要不要定个时间或条件？」 for an undated
    promise. breath's text puts its mark in front of these words; the panel shows them
    as they are."""
    if reason.get("kind") == "dated":
        days = int(reason.get("days") or 0)
        return _LOUD_WORDS[reason["loud"]].format(days=days, ago=-days)
    if reason.get("ask") == "still_counts":
        return f"挂了 {reason.get('held', 0)} 天——还算数吗？"
    return "答应了，没定时间——要不要定个时间或条件？"


def _waits_on_cue(meta: dict) -> bool:
    cue = meta.get("cue")
    return isinstance(cue, dict) and bool(str(cue.get("condition") or "").strip())


def _backfill_marked(meta: dict, today: date, s: BreathSettings) -> bool:
    """Say 「补的」 while a date the backfill filled in is new on the list: for the first
    `backfill_mark_days` from the day the entry first came onto it (its created day, or
    `date_days` before the date it had then). Derived, nothing stored: a converted date
    that came out wrong has to be seen when it first shows, not every day after."""
    if "when" not in [str(f) for f in (meta.get("backfilled") or [])]:
        return False
    created = _local_day(meta.get("created"))
    if created is None:
        return False
    first_due = due_day(meta, created)
    first_listed = max(created, first_due - timedelta(days=s.date_days)) if first_due else created
    return 0 <= (today - first_listed).days < s.backfill_mark_days


def _current(by_id: dict, bid: str) -> dict | None:
    """The newest version of an entry in the store, following `superseded_by`."""
    seen: set[str] = set()
    row = by_id.get(bid)
    while row is not None:
        nxt = str((row.get("metadata") or {}).get("superseded_by") or "").strip()
        if not nxt or nxt in seen or nxt not in by_id:
            return row
        seen.add(nxt)
        row = by_id[nxt]
    return None


def _review_line(hold: dict, content: str, by_id: dict, holds, now: datetime,
                 scope=None) -> dict | None:
    """A `defer` hold whose review day has come and that has not been asked about since:
    one line, the question with the original and the hold together. Answering is the
    model's (it closes the hold or gives it a date); asked once is the hold's `last_asked`,
    stamped when breath hands the question out (tools/breath/awaken.stamp_asked)."""
    if hold.get("hold") != "defer" or not _H.hold_is_live(hold, now):
        return None
    review = _w.parse_date_or_none(str(hold.get("review_after") or ""))
    today = now.date()
    if review is None or review.date() > today:
        return None
    asked = _local_day(hold.get("last_asked"))
    if asked is not None and asked >= review.date():
        return None
    target = _current(by_id, str(hold.get("exception_of") or "").strip())
    if target is None:
        return None
    tmeta = target.get("metadata") or {}
    if is_closed(tmeta):
        return None
    if not (_V.visible_for(hold, scope, road=_V.REVIEW, now=now, holds=holds)
            and _V.visible_for(tmeta, scope, road=_V.REVIEW, now=now, holds=holds)):
        return None
    tid = str(tmeta.get("id") or target.get("id") or "")
    hid = str(hold.get("id") or "")
    return {"id": tid, "short": short_id(tid),
            "text": entry_label(tmeta, str(target.get("content") or "")),
            "kind": "review", "bound": list(tmeta.get("bound") or []),
            "weight": 0.5 if tmeta.get("weight") in (None, "") else _f_weight(tmeta.get("weight")),
            "reason": {"kind": "dated", "days": (review.date() - today).days,
                       "date": review.date().isoformat(),
                       "loud": "now" if review.date() == today else "overdue"},
            "hold": {"id": hid, "short": short_id(hid), "text": entry_label(hold, content)}}


def prospective(all_buckets: list, now: datetime, *, settings: BreathSettings | None = None,
                delivered_at=None, scope=None) -> dict:
    """惦记的事: {"items": the lines shown, in order; "more": how many did not fit;
    "questions": the 「像是答应过的」 questions under them}.

    item: {id, short, text, kind: "dated" | "undated" | "review", bound: [...],
           weight (how heavily a want sits, for a host's own use; None when not wanted),
           reason: {kind: "dated", days, date, loud, yearly?, length?, backfilled?}
                 | {kind: "undated", held, ask: "set_time" | "still_counts"},
           hold?: {id, short, text}}   (review only: the hold the question is about)
    `days` is negative when overdue. Weight orders nothing here. question: {id, short,
    text}."""
    s = settings or BreathSettings()
    today = now.date()
    holds = _H.hold_index(all_buckets)
    by_id = {str((b.get("metadata") or {}).get("id") or b.get("id") or ""): b
             for b in all_buckets}
    dated: list[dict] = []
    undated: list[dict] = []
    questions: list[tuple[str, dict]] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        content = str(b.get("content") or "")
        if not bid or not _V.timeline_kind(meta):
            continue
        if _H.is_hold(meta):
            line = _review_line(meta, content, by_id, holds, now, scope)
            if line:
                dated.append(line)
            continue
        if is_closed(meta) or not _V.visible_for(meta, scope, road=_V.PROSPECTIVE, now=now,
                                                 holds=holds):
            continue
        telic = is_telic(meta)
        _wt = meta.get("weight")
        base = {"id": bid, "short": short_id(bid), "text": entry_label(meta, content),
                "bound": list(meta.get("bound") or []),
                "weight": (0.5 if _wt in (None, "") else _f_weight(_wt)) if telic else None}
        # The backfill read a promise the main model did not mark: a question, asked once
        # (its `last_asked`, stamped when breath hands it out). Answering is the model's.
        if not telic and meta.get("looks_like_promise") and not meta.get("last_asked"):
            questions.append((str(meta.get("created") or ""),
                              {k: base[k] for k in ("id", "short", "text")}))
        if not is_accessible(meta, now, settings=s, delivered_at=delivered_at):
            continue
        due = due_day(meta, today)
        if due is not None:
            days = (due - today).days
            yearly = str(meta.get("recurrence") or "").strip().upper() == _YEARLY
            if days > s.date_days:
                continue
            if not telic:
                # Something that happened carries a date in the past, and an entry written
                # today about today is a record of it: neither is a reminder.
                if days < 0 or (days == 0 and not yearly
                                and _local_day(meta.get("created")) == today):
                    continue
            reason = {"kind": "dated", "days": days, "date": due.isoformat(),
                      "loud": "overdue" if days < 0 else _reminder_loudness(days)}
            if yearly:
                reason["yearly"] = True
            w = str(meta.get("when") or "").strip()
            if telic and _magnitude_days(w) is not None:
                reason["length"] = w
            if _backfill_marked(meta, today, s):
                reason["backfilled"] = True
            dated.append({**base, "kind": "dated", "reason": reason})
        elif telic and meta.get("bound") and not _waits_on_cue(meta):
            held = written_days_ago(meta, today) or 0
            undated.append({**base, "kind": "undated",
                            "reason": {"kind": "undated", "held": held,
                                       "ask": ("still_counts" if held >= s.hanging_days
                                               else "set_time")}})
    dated.sort(key=lambda i: (i["reason"]["days"] >= 0, i["reason"]["days"]))
    undated.sort(key=lambda i: -i["reason"]["held"])
    ordered = dated + undated
    shown = ordered[:s.prospective_lines]
    questions.sort(key=lambda q: q[0], reverse=True)
    return {"items": shown, "more": len(ordered) - len(shown),
            "questions": [q for _created, q in questions[:s.promise_questions]]}


# ------------------------------------------------------------
# 忽然想起 (involuntary)
# ------------------------------------------------------------
# Old things coming back unbidden: one linked to the last few days — an older event
# sharing a name (`subjects`) or a scene word with something written in them — and the
# rest at random; with nothing to link, all at random. Each says how it came up. Drawn
# from `event_pool` (the `sudden` road: nothing held, nothing covered, nothing put out of
# mind), minus what is wanted — a want coming up is 惦记的事's to decide, and a light one
# nobody owes is asleep — and minus what the last few days already show. Coming up is not
# use: nothing here is touched or warmed.

def _link_words(meta: dict, skip: set[str]) -> list[str]:
    words = [str(n).strip() for n in (meta.get("subjects") or [])]
    words += [str(t).strip() for t in (meta.get("tags") or [])
              if is_scene_word(t) and str(t).strip() != _EDITED_BY_USER_TAG]
    return [w for w in dict.fromkeys(words) if w and w not in skip]


def involuntary(all_buckets: list, now: datetime, *, settings: BreathSettings | None = None,
                rng=None, scope=None) -> list[dict]:
    """忽然想起: [{id, short, text, how: "linked" | "random", via: the shared word or None,
    why: 「因为最近提到…」 / 「随手翻到的」}].

    The two names on nearly everything — mine and the owner's — link nothing, so they are
    not linking words."""
    s = settings or BreathSettings()
    rng = rng or random
    cut = now - timedelta(days=s.recent_days)
    skip = {n for n in (get_ai_name(), get_owner_name()) if n}

    def created(meta: dict):
        return _w.parse_stamp(meta.get("created"))

    recent: list[str] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        c = created(meta)
        if c is not None and c >= cut and _V.on_timeline(meta, scope):
            recent.extend(_link_words(meta, skip))
    recent_words = list(dict.fromkeys(recent))
    pool = [e for e in event_pool(all_buckets, now, scope=scope)
            if not is_telic(e["meta"]) and (created(e["meta"]) or now) < cut]

    def line(e: dict, via: str | None) -> dict:
        return {"id": e["id"], "short": short_id(e["id"]),
                "text": entry_label(e["meta"], e["content"]),
                "how": "linked" if via else "random", "via": via,
                "why": f"因为最近提到{via}" if via else "随手翻到的"}

    picks: list[dict] = []
    if s.involuntary_lines > 0 and recent_words:
        linked = []
        for e in pool:
            own = set(_link_words(e["meta"], skip))
            shared = next((w for w in recent_words if w in own), None)
            if shared:
                linked.append((e, shared))
        if linked:
            e, via = rng.choice(linked)
            picks.append(line(e, via))
    taken = {p["id"] for p in picks}
    rest = [e for e in pool if e["id"] not in taken]
    n = max(0, s.involuntary_lines - len(picks))
    picks.extend(line(e, None) for e in rng.sample(rest, min(n, len(rest))))
    return picks


# ------------------------------------------------------------
# The panel's surface page: the awake pool, every reason on each entry
# ------------------------------------------------------------

def _stamp(meta: dict) -> str | None:
    """The entry's `created` as a local ISO time, None when unreadable."""
    created = _w.parse_stamp(meta.get("created"))
    return created.isoformat(timespec="seconds") if created else None


def awake_pool(all_buckets: list, now: datetime, *, settings: BreathSettings | None = None,
               delivered_at=None, scope=None, reason: str = "") -> list[dict]:
    """Everything awake at `now`, each with every reason it is awake in words:
    [{id, short, text, date, at, reasons: [{key, text}]}]. `date` is the day it is due,
    else the local day it was written; `at` is when it was written (local). `reason`
    keeps only entries awake for that one key.

    Which entries count is the `list` road's (live, current, timeline kinds, the read
    scope); awake is `awake_reasons`. Order: entries awake for a date first, the nearest
    date first (past or ahead), then the rest newest written first."""
    s = settings or BreathSettings()
    today = now.date()
    dated: list[tuple] = []
    rest: list[tuple] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or not _V.timeline_kind(meta) or not _V.visible_for(meta, scope, road=_V.LIST):
            continue
        keys = awake_reasons(meta, now, settings=s, delivered_at=delivered_at)
        if not keys or (reason and reason not in keys):
            continue
        due = due_day(meta, today)
        written = _local_day(meta.get("created"))
        shown = due or written
        row = {"id": bid, "short": short_id(bid),
               "text": entry_label(meta, str(b.get("content") or "")),
               "date": shown.isoformat() if shown else None, "at": _stamp(meta),
               "reasons": [{"key": k, "text": awake_words(k, meta, today, settings=s)}
                           for k in keys]}
        if DATED in keys and due is not None:
            dated.append((abs((due - today).days), due, row))
        else:
            rest.append((row["at"] or "", row))
    dated.sort(key=lambda t: (t[0], t[1]))
    rest.sort(key=lambda t: t[0], reverse=True)
    return [row for *_k, row in dated] + [row for _at, row in rest]


# ------------------------------------------------------------
# The panel's trace page: what still hangs open, and the buttons that close it
# ------------------------------------------------------------
# Three kinds, each one something that can be finished or taken back: a promise not yet
# closed (`is_open_promise`), a live hold (`_holds.hold_is_live`), and an entry waiting on a
# cue's condition that is not closed. A want nobody owes, a yearly day, something merely
# new are not here. Each row says why it still hangs, and which buttons it takes:
#   done      a promise: finished           (trace status="resolved", closed_by="user")
#   drop      a promise: not doing it       (trace status="abandoned", closed_by="user")
#   withdraw  a hold: lift it — closing the hold brings the agreement back
#             (status="resolved", closed_by="user"); a cue: take the condition off (cue="")
# The route recomputes the row before a button acts, so only an id on this list, with an
# action its row offers, closes anything.

DONE = "done"
DROP = "drop"
WITHDRAW = "withdraw"

_HOLD_WORDS = {"defer": "先别催", "avoid": "别碰"}


def _md(d: date) -> str:
    return d.strftime("%m-%d")


def _promise_words(meta: dict, today: date) -> str:
    due = due_day(meta, today)
    if due is None:
        held = written_days_ago(meta, today) or 0
        return f"答应了，没定时间（挂了 {held} 天）"
    days = (due - today).days
    if days > 0:
        return f"还有 {days} 天"
    return "就是今天" if days == 0 else f"过了 {-days} 天"


def _hold_words(meta: dict) -> str:
    end = _H.hold_end(meta)
    if end is not None:
        return f"条子到 {_md(end)}"
    review = _w.parse_date_or_none(str(meta.get("review_after") or ""))
    if review is not None:
        return f"条子，{_md(review.date())} 问一次还放不放"
    return "条子，没写到哪天"


def _cue_condition(meta: dict) -> str:
    cue = meta.get("cue")
    return str(cue.get("condition") or "").strip() if isinstance(cue, dict) else ""


def hanging(all_buckets: list, now: datetime, *, settings: BreathSettings | None = None,
            delivered_at=None, scope=None) -> dict:
    """{"surface": rows awake, "deep": rows asleep}; each half newest written first.

    row: {id, short, text, kind: "promise" | "hold" | "cue", why_words, actions: [...],
          at, on? (a hold: the id it is hung on), hold? / hold_words? (a hold's level),
          condition? (a cue)}
    An entry is one row: a hold is a hold whatever else it carries; a promise waiting on a
    cue is a promise whose row also offers `withdraw` (the cue comes off, the promise
    stays). Which entries count is the `list` road's (live, current, the read scope); a
    scoped read gets a hold's `on` only when it may read that entry ("" otherwise)."""
    s = settings or BreathSettings()
    today = now.date()
    out: dict[str, list[tuple]] = {"surface": [], "deep": []}
    from .scope import narrows
    narrowed = narrows(scope)
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or not _V.timeline_kind(meta) or not _V.visible_for(meta, scope, road=_V.LIST):
            continue
        condition = "" if is_closed(meta) else _cue_condition(meta)
        row = {"id": bid, "short": short_id(bid),
               "text": entry_label(meta, str(b.get("content") or "")), "at": _stamp(meta)}
        if _H.is_hold(meta):
            if not _H.hold_is_live(meta, now):
                continue
            level = str(meta.get("hold") or "")
            on = str(meta.get("exception_of") or "").strip()
            # A scoped read names only what it may read: a hold on an entry out of scope
            # is listed without the id it hangs on.
            if on and narrowed and not scope.permits_id(on):
                on = ""
            row.update(kind="hold", on=on,
                       hold=level, hold_words=_HOLD_WORDS.get(level, level),
                       why_words=_hold_words(meta), actions=[WITHDRAW])
        elif is_open_promise(meta):
            words = _promise_words(meta, today)
            actions = [DONE, DROP]
            if condition:
                words += f"；在等「{condition}」"
                actions.append(WITHDRAW)
                row["condition"] = condition
            row.update(kind="promise", why_words=words, actions=actions)
        elif condition:
            row.update(kind="cue", condition=condition, why_words=f"在等「{condition}」",
                       actions=[WITHDRAW])
        else:
            continue
        half = ("surface" if is_accessible(meta, now, settings=s, delivered_at=delivered_at)
                else "deep")
        out[half].append((row["at"] or "", row))
    return {half: [row for _at, row in sorted(rows, key=lambda t: t[0], reverse=True)]
            for half, rows in out.items()}
