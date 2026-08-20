"""
========================================
core/profile.py — the single contract shared by the awakening and the profile page
========================================

The rules behind "the note at the door" and "something suddenly comes back" used to be
implemented twice in parallel (once in `tools/breath/awaken.py` for the awakening, once
in `web/loci.py` for the profile page). They were merged into one copy, which then moved
here: this contract does not belong to breath alone — the profile page and the awakening
have to read the same rules, and living under `tools/breath/` invited the assumption that
it was breath's private property. It now sits with the other engine pieces
(`_fold` / `_rooms` / `_when`), and both `tools/breath/awaken.py` and `web/loci.py`
import it, so there is exactly one copy of the rules.

⚠️ **Moved, not rewritten**: `door_note()` and `event_pool()`, along with every comment
explaining their rules, came across word for word; only the import paths followed the
new home.

Three things were later added on top of this contract, all decided on the **read** side,
with no new persisted field (the one tag convention below excepted):
① **The three kinds of want-clock**: the kind is not a stored field, it is inferred from
   how `when` was filled in — empty = `等触发` (waiting for a trigger), a duration mark
   `<N>[dwmy]` = `有量级` (has a magnitude), `YYYY-MM-DD` = `有期限` (has a deadline).
② **Only the oldest one gets asked as a question**: the `heavy` list is still returned
   in full, plus one extra `heavy_question_id` — the id of the one that has been
   **hanging longest** (not the heaviest). The render layer uses it to decide which one
   turns into a question and which ones stay statements.
③ **A notification that a fact was edited from the panel**: alongside `event_pool()`
   there is `edited_by_her()`, which scans for the edit tag plus "not yet folded away"
   (`_F.is_covered()`). That is the entire notification mechanism: the tag is both the
   mark and the notice, and no separate notification table was opened.

Exports: door_note(all_buckets, now) / event_pool(all_buckets) / edited_by_her(all_buckets)
========================================
"""

import re
from datetime import datetime

from utils import is_closed

from . import _fold as _F         # anything covered stops surfacing on its own
from . import _when as _w          # "today" on the local calendar
# is_mind_room is deliberately no longer imported: the secondary "a rule has to live in
# MIND" filter at the door was taken out (see the long note further down)
from ._rooms import is_event_room
from tools.recall.core import _visible  # core->tools backward edge: see the note atop core/__init__.py

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
_EDITED_BY_HER_TAG = "她改的"

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


def event_pool(all_buckets: list) -> list[dict]:
    """The pool behind "something suddenly comes back": events that are visible, live in
    an EVENT room, and have **not been covered**.

    🔴 Three gates; miss any one of them and it fails silently:
    ① `is_event_room()` accepts both old and new room names. Both sides used to write
       `.find("/EVENT/") > 0` themselves, and the new name `EVENT/SELF` contains no
       `/EVENT/` at all, so the pool would **go silently empty** with no error.
    ② Machinery (the name page, periods) is not a memory.
    ③ **Covered entries stay out of this pool.** Coming across something suddenly is a
       chance encounter, whereas anything already folded has been given a name — it
       should appear inside recall as that sentence, not tap me on the shoulder again as
       a loose entry.
       ⚠️ `is_covered()` also covers superseded versions (`superseded_by`) now; that half
       used to be excluded wholesale by `_visible()`, and both are handled by this one
       gate today.
    ⚠️ A future threshold engine's candidate pool has to pass the same gate:
       **this is the anchor point for it.**
    """
    pool: list[dict] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        tags = [str(t) for t in (meta.get("tags") or [])]
        if _PROFILE_TAG in tags or _BIGEVENT_TAG in tags:
            continue
        if not _visible(meta) or _F.is_covered(meta):
            continue
        if not is_event_room(meta.get("room")):
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid:
            continue
        pool.append({"id": bid, "meta": meta, "content": str(b.get("content") or "")})
    return pool


def door_note(all_buckets: list, now: datetime) -> dict:
    """Name + rules + ⏰ reminders + 🫀 what is weighing on me + the list of periods —
    **one pass over the store, one set of rules.**

    Returns {"facts": [...], "rules": [...], "reminders": [...], "heavy": [...],
             "big": [...], "entries": [...]}, with the raw `meta` / `content` carried on
    each element. Rendering (the text skin, the JSON skin) is each caller's own business:
    **not one judgement is made in the render layer.**
    """
    facts: list[dict] = []        # collect (created, content), keep the earliest; warn if >1
    rules: list[dict] = []
    reminders: list[dict] = []
    heavy: list[dict] = []        # weighing on me: wants with no date, or a date that passed unresolved
    big: list[dict] = []          # periods (big events) — absent from the awakening, listed on the profile page
    entries: list[dict] = []      # visible memories by recall's definition (for the random pick / the earliest one)

    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        content = str(b.get("content") or "")
        tags = [str(t) for t in (meta.get("tags") or [])]

        if _PROFILE_TAG in tags:
            facts.append({"id": bid, "created": str(meta.get("created") or ""),
                          "content": content})
            continue
        if _BIGEVENT_TAG in tags:
            # Periods do not appear in the awakening, but the profile page lists a line
            # for each, so they are collected here (resolved ones are not listed).
            if str(meta.get("status") or "") != "resolved":
                big.append({"id": bid, "meta": meta, "content": content})
            continue

        # Reminders: `when` within the next 30 days (wants and ordinary events alike).
        # Nothing closed (resolved/abandoned), deliberately forgotten, or superseded
        # gets a reminder.
        _status = str(meta.get("status") or "")
        _remindable = (not is_closed(meta)  # only `status` marks an ending; the old booleans stay read-only for compatibility
                       and not meta.get("dont_surface")
                       and not meta.get("superseded_by"))
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
                if 0 <= days <= _REMIND_DAYS and (days > 0 or _status == "want"):
                    reminders.append({"id": bid, "meta": meta, "content": content,
                                      "days": days, "when": m.group(1),
                                      "status": _status, "loud": _reminder_loudness(days)})
                    _reminded = True
            except ValueError:
                pass

        # Weighing on me: a want with **no date at all**, or one whose **date passed
        # without it being closed**. Neither used to surface anywhere — and a want has
        # only two endings, both of which have to be marked by hand; nothing closes
        # itself.
        # 🔴 No automatic closing was added (that would quietly erase things left
        #    undone). Instead **the number of days it has hung is pushed into view**:
        #    on day 40 with still nothing done, that number asks the question by itself.
        if _status == "want" and _remindable and not _reminded:
            _c = _w.parse_stamp(meta.get("created"))
            # ⚠️ Caught while building dreaming: this used to read
            #    `float(meta.get("weight") or 0.5)`, and in Python `0.0 or 0.5` is 0.5 —
            #    so **an entry whose weight was genuinely zeroed got ranked as 0.5**.
            #    The whole point of dreaming is that "a want that was dreamt about has its
            #    weight cleared = it stops pressing on me", and this falsy fallback ate
            #    that outcome entirely (field cleared, still pressing in plain view).
            #    Only a missing field or an empty string counts as 0.5; **a real 0 is 0**.
            #    (This had once been fixed in awaken only, leaving the profile page still
            #    carrying the bug — the reason both now share this file.)
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

        if not _visible(meta):
            continue
        room = str(meta.get("room") or "")
        # A rule is **something pinned**. That is the whole test.
        #
        # ⚰️ The secondary filter — "and the room has to be MIND, or the first 40
        #    characters of the body have to say 'rule of conduct'" — was **taken out**.
        #    The reason: **pinning something IS a judgement, already made.** Overruling it
        #    with a room amounts to letting an old bulk migration, which mapped rooms by
        #    name and understands nothing about content, **overturn a judgement made
        #    deliberately today.**
        #    That filter really did bite: three pinned notes, kept on purpose after going
        #    through them one by one, showed **not a single character at the door** purely
        #    because they happened to land in EVENT/SELF. No error, no warning, just
        #    absent. **A silent filter is worse than a rejection**: a rejection gets
        #    fixed, while silence leaves you believing the thing is there.
        #    Good side effect: the mess in the room field no longer blocks the door and
        #    can be cleaned up at leisure.
        # 🔴 **A superseded or covered version is not a rule.** `_visible()` used to
        #    exclude `superseded_by` wholesale; that half now belongs to `is_covered()`,
        #    which has to be called explicitly here — otherwise the door would display a
        #    rule that has already been changed my mind about.
        if meta.get("pinned") and not _F.is_covered(meta):
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
    return {"facts": facts, "rules": rules, "reminders": reminders,
            "heavy": heavy, "big": big, "entries": entries,
            "heavy_question_id": heavy_question_id}


def edited_by_her(all_buckets: list) -> list[dict]:
    """The notification pool for "a fact was edited from the panel": events carrying the
    `_EDITED_BY_HER_TAG` tag that have **not been folded away**.

    That is the entire notification mechanism: the tag is simultaneously the mark that an
    edit happened and the test for "not yet looked at" (`_F.is_covered()`). Agreeing with
    an edit means folding it, after which it drops out of this pool by itself; disagreeing
    leaves it sitting here until the disagreement has actually been talked through and
    something is done about it (folded, or deliberately left alone).
    Reviewing these in batches is not implemented here, but this pool is already the
    substrate for it — a future batch view in muse would draw from exactly this.
    """
    pool: list[dict] = []
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        tags = [str(t) for t in (meta.get("tags") or [])]
        if _EDITED_BY_HER_TAG not in tags:
            continue
        if not _visible(meta) or _F.is_covered(meta):
            continue
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid:
            continue
        pool.append({"id": bid, "meta": meta, "content": str(b.get("content") or "")})
    pool.sort(key=lambda e: str(e["meta"].get("created") or ""))
    return pool
