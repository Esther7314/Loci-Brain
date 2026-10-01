"""
========================================
core/visibility.py — the one gate: may this memory be put in front of the model
========================================

Every road that puts a memory in front of the model asks `visible_for()` first. The
rules used to live wherever a road happened to need them — recall's timeline filter,
the quota helpers' "terminal" test, `dont_surface` checked on some roads and forgotten
on others — and a road that forgot one rule leaked quietly: no error, the entry simply
came up. One gate, one table of roads, so a road's rules can be read in one place and a
new road starts from the table instead of from memory.

------------------------------------------------------------
Two kinds of visibility
------------------------------------------------------------
  surfacing  the entry comes up **by itself**: breath's blocks, muse's pools, dream
             material, a dream handed out. `dont_surface`, live holds and covers count
             here, each on the roads that have always honoured it (the table below).
  lookup     the entry is **asked for**: recall's search and time browsing, a read by id.
             `dont_surface` hides nothing here — it governs only what comes up
             by itself. An archived or deleted entry is never shown as if it were live:
             listings leave it out, and a read by id shows it with its state said out
             loud (`Verdict.mark`).

What an entry's state is (`state_of`) is the same on every road:
  live · archived (`type: archived`, sank on purpose) · deleted (`deleted_at` or
  `tombstone`: a soft delete — the file sits in archive/, a read by id still reaches it).

🔴 `dont_surface` on an entry that has been re-versioned is the version chain speaking,
   not a choice: regrow writes it on the old version together with `superseded_by`
   (core/_fold.save_gist). It is read as `superseded`, so a road that lets old versions
   through (a dream handed out, made from the wording of the day) is not closed by it.

------------------------------------------------------------
The roads
------------------------------------------------------------
| road            | kind    | dont_surface | old version | covered | holds        | hold entry | later today |
|-----------------|---------|--------------|-------------|---------|--------------|------------|-------------|
| prospective     | surface | ✔            | ✔           |         | defer, avoid | ✔          | ✔           |
| review          | surface | ✔            | ✔           |         | avoid        |            | ✔           |
| recent          | surface |              | ✔           |         |              |            | ✔           |
| sudden          | surface | ✔            | ✔           | ✔       | defer, avoid | ✔          | ✔           |
| edited          | surface |              |             | ✔       |              |            | ✔           |
| invalidation    | surface |              | ✔           |         |              |            | ✔           |
| door            | surface |              |             | ✔       |              |            |             |
| remind          | surface | ✔            | ✔           |         | defer, avoid | ✔          |             |
| muse            | surface |              |             |         |              |            |             |
| dream           | surface | ✔            | ✔           |         | avoid        | ✔          |             |
| dream_handout   | surface | ✔            |             |         | avoid        |            |             |
| cue             | surface | ✔            | ✔           |         | defer, avoid |            |             |
| list            | lookup  |              | ✔           |         |              |            |             |
| read            | lookup  | (any state shown, with its mark)                                              |

Every road but `read` shows live entries only.

A source withdrawn or deleted closes every road but `invalidation` — `read` included —
to what stood on it: the entries naming the source carry an open `source_gone`
invalidation record from the moment the change is applied, and so does everything derived
from them (core/_source_change.py). Under a read scope the registry says the same thing
again (core/scope.py reads each source's state on every request); without one this record
is how the gate knows. `invalidation` lists such an entry by id and status only, never its
text (core/_invalidation.py).

later today: an open entry whose `when` names a clock time later today (`waits_for_clock`)
stays off until that time has passed — 「今晚回来说面试结果」 written in the morning is not
talked about over lunch. From that moment it is due today like anything else. Only a
clock time waits: a `when` that is a day alone has no hour to wait for and counts from the
morning, and a later day is simply "N days left". Every road that lists an entry on breath
counts it; the entry is still awake (core/profile.is_accessible), and the lookup roads find
it as always. In a window already open when the time comes, the entry reaches the model
through the strong-reminder card with the owner's next message, not through breath
(core/profile.due_now, core/_cue.py).

- prospective: breath's 惦记的事 (core/profile.prospective: its dated and undated lines
  and the 「像是答应过的」 questions).
- review: the one 惦记的事 line that shows a held entry on purpose — a `defer` hold whose
  review day has come, asked about together with what it is hung on. Asked of the hold and
  of the original alike: a `defer` is the thing being asked about, an `avoid` still closes.
- recent: breath's 近三天 — recall's three-day overview (tools/recall/core.
  recall_text_and_data with this road), which is the `list` road's set less what waits
  for a clock time today.
- sudden: 「忽然想起」 (core/profile.event_pool, which breath's involuntary block draws
  from, and the profile page's list of the same name).
- edited: the panel edits in 依据变了的 (core/profile.edited_by_user).
- invalidation: the rest of 依据变了的 — an overturned basis, a source revised, withdrawn
  or deleted (core/_invalidation.py). A basis that changed is told whatever else the entry
  asked for: neither `dont_surface`, a hold nor a cover keeps it off. What the block may
  not hand back — the body of an entry standing on a withdrawn or deleted source — is that
  block's own rule.
- door: the pinned rules and the profile page by the door (door_note). A rule is only
  ever one already on the timeline (`on_timeline`), so old versions never reach it. Rules
  and the name page are not things that come due, so no clock time holds them back.
- remind: the profile page's reminders and weighing-on-me lists (core/profile.door_note):
  the panel, not breath.
- muse: muse's pools (core/_muse.in_pool). Which pool counts a cover is that pool's own
  spec (`POOL_SPECS`), so covers are not a road rule here.
- dream: dream material, both pools (core/_dream.want_pool / unclear_pool). The
  undigested pool's cover rule is muse's dream spec.
- dream_handout: each ingredient a woven dream recorded, when the dream is handed out
  (core/_dream.withheld_ingredients). One that may not be seen withholds the whole dream.
- cue: the strong-reminder cards and name cards that ride with the owner's message
  (core/_cue.py). A hold itself may be carded — one waiting on an event is lifted only by
  the model, after a card — while what a live hold covers is not. A clock time later today
  does not hold a card back: the owner's own words brought the entry up, which is not
  breath's unprompted screen. A memory in 依据变了的 is carded on the `invalidation` road,
  by id and status.
- list: what recall lists and counts — search, time browsing, a period's members, the
  earliest-entry line in breath (`on_timeline`).
- read: recall's read by id — the entry itself and every entry it links to (sources,
  what it covers, a period's members, who cites it). A linked entry is a line with its
  mark, never its body.

Where a road's column is empty it is because that road never honoured the rule, not an
oversight: the gate took each road's rules as they stood and closed only the known
leaks (`dont_surface` on 「忽然想起」 and on the undigested dream pool).

Callers still to come, and the road each will ask:
- stage 6's write tools return memories (回望 / 场景常来): a return is surfacing — they
  will ask with a road of their own added to this table.

------------------------------------------------------------
Read scope
------------------------------------------------------------
`scope` is the request's read scope as a `core.scope.ScopeView` — the host's grant, the
turn's venue and audience, the source registry, and the library for walking to an
entry's roots — or None: the whole library (an open host that sent no scope, a background
job, a direct call). What may be read under a scope is `ScopeView.permits` (the three
conditions, every root of a derived entry, nothing for a memory without sources); this
gate asks it first, on every road and both kinds, and an entry it refuses is `OUT_OF_SCOPE`
— on the `read` road too, where it is not shown with a mark but not shown at all: a lookup
of something out of scope must read like a lookup of something that does not exist.
Anything else is refused as a scope rather than ignored, so a caller cannot believe it is
scoped when it is not.

The gate is cheap on purpose — breath runs it over the whole store: the hold index is
built once by the caller and passed in, and a caller that already knows whether an entry
is covered passes that too.

Exports: SURFACE · LOOKUP · LIVE · ARCHIVED · DELETED · the road names · Verdict ·
         state_of() · source_gone() · clock_moment() · waits_for_clock() · visible_for() ·
         timeline_kind() · on_timeline()
========================================
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from utils import is_closed, parse_bool

from . import _fold as _F
from . import _holds as _H
from . import _when as _w

SURFACE = "surface"
LOOKUP = "lookup"

LIVE = "live"
ARCHIVED = "archived"
DELETED = "deleted"
_ALL_STATES = frozenset({LIVE, ARCHIVED, DELETED})

# What a lookup says out loud about an entry that is not live. Both kinds sit in archive/,
# and the words say so first; a deleted one says it was deleted.
STATE_MARK = {ARCHIVED: "⚠️在归档区", DELETED: "⚠️在归档区（已删除）"}

# Why a road keeps an entry off — `Verdict.reasons`. A state that the road does not show
# is a reason too, under its own name.
DONT_SURFACE = "dont_surface"
SUPERSEDED = "superseded"
COVERED = "covered"
HELD = "held"
HOLD_ENTRY = "hold_entry"
LATER_TODAY = "later_today"
OUT_OF_SCOPE = "out_of_scope"
SOURCE_GONE = "source_gone"

PROSPECTIVE = "prospective"
RECENT = "recent"
REMIND = "remind"
REVIEW = "review"
DOOR = "door"
SUDDEN = "sudden"
EDITED = "edited"
INVALIDATION = "invalidation"
MUSE = "muse"
DREAM = "dream"
DREAM_HANDOUT = "dream_handout"
CUE = "cue"
LIST = "list"
READ = "read"


@dataclass(frozen=True)
class Road:
    """One road's rules. See the table in the module docstring."""
    kind: str
    dont_surface: bool = False      # a deliberate dont_surface keeps it off
    superseded: bool = False        # an old version (`superseded_by` on disk) stays off
    covered: bool = False           # a live cover (`_fold.is_covered`) keeps it off
    holds: frozenset = frozenset()  # the live hold levels that keep the held entry off
    hold_entry: bool = False        # a hold itself stays off: it rides with its target
    later_today: bool = False       # a clock time later today keeps an open entry off until then
    states: frozenset = field(default_factory=lambda: frozenset({LIVE}))


_BOTH_LEVELS = frozenset(_H.HOLD_LEVELS)

ROADS: dict[str, Road] = {
    PROSPECTIVE: Road(SURFACE, dont_surface=True, superseded=True, holds=_BOTH_LEVELS,
                      hold_entry=True, later_today=True),
    REVIEW: Road(SURFACE, dont_surface=True, superseded=True, holds=frozenset({"avoid"}),
                 later_today=True),
    RECENT: Road(SURFACE, superseded=True, later_today=True),
    SUDDEN: Road(SURFACE, dont_surface=True, superseded=True, covered=True,
                 holds=_BOTH_LEVELS, hold_entry=True, later_today=True),
    EDITED: Road(SURFACE, covered=True, later_today=True),
    INVALIDATION: Road(SURFACE, superseded=True, later_today=True),
    DOOR: Road(SURFACE, covered=True),
    REMIND: Road(SURFACE, dont_surface=True, superseded=True, holds=_BOTH_LEVELS,
                 hold_entry=True),
    MUSE: Road(SURFACE),
    DREAM: Road(SURFACE, dont_surface=True, superseded=True, holds=frozenset({"avoid"}),
                hold_entry=True),
    DREAM_HANDOUT: Road(SURFACE, dont_surface=True, holds=frozenset({"avoid"})),
    CUE: Road(SURFACE, dont_surface=True, superseded=True, holds=_BOTH_LEVELS),
    LIST: Road(LOOKUP, superseded=True),
    READ: Road(LOOKUP, states=_ALL_STATES),
}


@dataclass(frozen=True)
class Verdict:
    """What the gate said. Truthy when the road may show the entry.

    `state` is always filled in. On the `read` road an entry that is not live is still
    shown — with `mark` said out loud, and as a linked line never expanded."""
    shown: bool
    state: str
    reasons: tuple = ()

    def __bool__(self) -> bool:
        return self.shown

    @property
    def mark(self) -> str:
        return STATE_MARK.get(self.state, "")

    @property
    def out_of_scope(self) -> bool:
        """The request may not read it at all: say nothing of it, not even that it exists."""
        return OUT_OF_SCOPE in self.reasons


def _meta_of(row) -> dict:
    """Accept a store bucket ({"metadata": …}) or a bare meta."""
    if isinstance(row, dict) and isinstance(row.get("metadata"), dict):
        return row["metadata"]
    return row if isinstance(row, dict) else {}


def state_of(meta) -> str:
    """live / archived / deleted. A soft delete wins over the archived type: both sit in
    archive/, and "deleted" is the stronger thing to say."""
    m = _meta_of(meta)
    if m.get("deleted_at") or parse_bool(m.get("tombstone"), default=False):
        return DELETED
    if str(m.get("type") or "").strip().lower() == "archived":
        return ARCHIVED
    return LIVE


def _superseded(m: dict) -> bool:
    return bool(str(m.get("superseded_by") or "").strip())


# The invalidation record a withdrawn or deleted source leaves on what stood on it
# (core/_invalidation.SOURCE_GONE; spelled here because that module imports this one).
_SOURCE_GONE_KIND = "source_gone"


def source_gone(meta) -> bool:
    """Does the entry carry an open record that a source standing behind it was withdrawn
    or deleted? Written when the host's change is applied, on the entries naming the
    source and on everything derived from them (core/_source_change.py)."""
    raw = _meta_of(meta).get("invalidation") or []
    if not isinstance(raw, list):
        return False
    return any(isinstance(r, dict) and r.get("kind") == _SOURCE_GONE_KIND
               and not str(r.get("confirmed_at") or "").strip() for r in raw)


def _faded(m: dict) -> bool:
    """A deliberate dont_surface: on an old version it is the chain's, read as superseded."""
    return parse_bool(m.get("dont_surface"), default=False) and not _superseded(m)


# A `when` that names a clock time: the day, then a T or a space and hh:mm.
_CLOCK_WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def clock_moment(meta) -> datetime | None:
    """The local moment an entry's `when` names when it carries a clock time; None for a
    day alone, a span, a length, or anything unreadable."""
    w = str(_meta_of(meta).get("when") or "").strip()
    if not _CLOCK_WHEN.match(w) or ".." in w:
        return None
    return _w.parse_stamp(w)


def waits_for_clock(meta, now: datetime) -> bool:
    """Open, and its `when` names a clock time later today: the `later_today` column."""
    m = _meta_of(meta)
    moment = clock_moment(m)
    if moment is None or is_closed(m):
        return False
    local_now = _w.to_local(now)
    return moment.date() == local_now.date() and moment > local_now


def visible_for(meta, scope=None, *, road: str,
                now: datetime | None = None, holds: "_H.HoldIndex | None" = None,
                covered: bool | None = None) -> Verdict:
    """May `road` put this entry in front of the model?

    meta     the entry's metadata, or the store bucket carrying it.
    scope    the request's read scope (a `core.scope.ScopeView`), or None for the whole
             library.
    road     one of `ROADS`; its kind (surfacing or lookup) comes with it.
    now      the moment holds and clock times are read against (local now when omitted).
    holds    the hold index (`_holds.hold_index`) over the store — required on a road that
             counts holds, built once by the caller.
    covered  whether the entry is covered, when the caller already knows (muse's Items
             carry it); computed otherwise, and only on a road that counts covers.
    """
    if scope is not None and not callable(getattr(scope, "permits", None)):
        raise ValueError(f"scope must be a core.scope.ScopeView or None, got {type(scope).__name__}")
    try:
        r = ROADS[road]
    except KeyError:
        raise ValueError(f"no such road: {road!r} (roads: {', '.join(ROADS)})") from None
    m = _meta_of(meta)
    state = state_of(m)
    if scope is not None and not scope.permits(m):
        return Verdict(shown=False, state=state, reasons=(OUT_OF_SCOPE,))
    reasons: list[str] = []
    if road != INVALIDATION and source_gone(m):
        reasons.append(SOURCE_GONE)
    if state not in r.states:
        reasons.append(state)
    if r.superseded and _superseded(m):
        reasons.append(SUPERSEDED)
    if r.dont_surface and _faded(m):
        reasons.append(DONT_SURFACE)
    if r.covered and (covered if covered is not None else _F.is_covered(m)):
        reasons.append(COVERED)
    if r.hold_entry and _H.is_hold(m):
        reasons.append(HOLD_ENTRY)
    if r.holds:
        if holds is None:
            raise ValueError(f"road {road!r} counts holds: pass the hold index")
        if _H.is_held(m, now or _w.now(), holds) in r.holds:
            reasons.append(HELD)
    if r.later_today and waits_for_clock(m, now or _w.now()):
        reasons.append(LATER_TODAY)
    return Verdict(shown=not reasons, state=state, reasons=tuple(reasons))


def timeline_kind(meta) -> bool:
    """Is this the kind of entry that sits on the timeline at all — a memory, not machinery?

    An old `i` entry stays out until it is merged into MIND (it has a room then); seeds
    are a coordinate system, not memories; the note by the door and periods are tooling. This says nothing about state — that is `visible_for`'s."""
    m = _meta_of(meta)
    t = str(m.get("type") or "")
    if t == "i" and not m.get("room"):
        return False
    if (m.get("domain") or [""])[0] == "seed":
        return False
    tags = [str(x) for x in (m.get("tags") or [])]
    return "__档案事实__" not in tags and "__大event__" not in tags


def on_timeline(meta, scope=None) -> bool:
    """What recall lists and counts: a timeline kind, shown on the `list` road (under the
    request's read scope, when there is one).

    🔴 **A version superseded by regrow does not count towards the number of entries**
       (and therefore does not appear in the browse view). Re-versioning an event = "I
       remembered it wrong" — the thing happened once, and the mistaken version is not a
       second thing; re-versioning a mind = "I used to think that and no longer do" — a
       changed view is not a second act of thinking. Two reasons, one conclusion: **an
       entry that has been re-versioned is still one entry**.
       ⚠️ The old version has not disappeared: a read by id still returns it verbatim,
       and both ends of the version chain are listed there. It simply no longer
       occupies a line or a place in the count.
       ⚠️ **Entries folded away by fold still count** (several real memories collected
       together, not earlier versions of one entry) — a cover is not a `list` rule.
    """
    return timeline_kind(meta) and visible_for(meta, scope, road=LIST).shown
