# -*- coding: utf-8 -*-
"""
server_tools/grow.py — grow: store new memories (events in a batch, or one mind)

The face a client sees: the signature, the description, and the one call that
forwards to tools/grow/ through server_call._with_notice. server.py mounts it
(mcp.tool()) and then runs adapt(mcp).
"""

import logging
from typing import Annotated, Optional

from pydantic import Field as _PydField

from tools import grow as _t_grow
from server_call import _with_notice

logger = logging.getLogger("loci_brain")


async def grow(
    items: Annotated[list, _PydField(description=(
        "A batch of events, each one a dict: {room, text, v, a}, plus optional when. "
        "Events always go here, even a single one — looking back at a stretch of "
        "conversation, more than one thing usually happened. Do not pass it for a mind."
    ))] = [],
    kind: Annotated[str, _PydField(description=(
        'Which kind to store: "event" (something that happened, including something '
        'you want to happen) or "mind" (something you realized). Required. It says '
        'the same thing room says, and both are required: a mismatch means you have '
        'them confused, and it is rejected on the spot — e.g. kind="mind" with '
        'room="EVENT/SELF".'
    ))] = "",
    room: Annotated[str, _PydField(description=(
        "One of the four rooms above. You fill it in yourself; wrong or missing is "
        "rejected on the spot."
    ))] = "",
    text: Annotated[str, _PydField(description=(
        'The body of a single entry. Only kind="mind" uses it — you realize one '
        "thing at a time, so this one is singular."
    ))] = "",
    # The public parameter name is "from", as the spec requires. `from` is a Python keyword,
    # so the signature spells it from_ and pydantic's public validation_alias catches it.
    # This deliberately avoids reaching into FastMCP's private structures.
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "The entries this one grew out of: real bucket_ids, or the host's own id for a "
        "line of the conversation (like m_0931) when it came straight from what was said. "
        "At most 64.\n"
        'Required for kind="mind": a realization does not come from nowhere. If it '
        "genuinely did, say so plainly in the text and point from at whatever events "
        "are nearest. Events may pass it as well (which thought this one came out of), "
        "but do not have to."
    ))] = [],
    v: Annotated[float, _PydField(description=(
        "v: valence, 0~1 — how it felt: 0 bad, 1 good. Required, and yours to set."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "a: arousal, 0~1 — how stirred up you were: 0 calm, 1 intense. Same."
    ))] = -1,
    direction_of_fit: Annotated[str, _PydField(description=(
        '"telic" for something wanted: a promise, a plan, a wish — "after her exam we go '
        'for dessert" (an event), "I want to be more patient with her" (a mind). Leave it '
        "out for a record of what happened or of how you see things."
    ))] = "",
    bound: Annotated[list, _PydField(description=(
        "With telic: who is bound by it. Names, or 我 for yourself and 你 for the person "
        'you talk to. ["我"] you owe it; ["我", "<her name>"] you both agreed; leave it '
        "out if it is only a wish and nobody owes anything. Other pronouns are refused: "
        "write the name."
    ))] = [],
    evidential: Annotated[str, _PydField(description=(
        'Mind only: how you know it. "inference" — there are signs you can point at. '
        '"assumption" — reasoning or common sense, nothing seen. Leave it out when you '
        "simply know."
    ))] = "",
    internally_generated: Annotated[bool, _PydField(description=(
        "True for a dream or something imagined (\"one day we live by the sea\"). It is "
        "stored in EVENT/SELF like anything you lived, and marked so it never reads as "
        "something that really happened."
    ))] = False,
    weight: Annotated[float, _PydField(description=(
        "With telic only: how heavily this sits on you, 0~1. The longer it goes "
        "unresolved the louder it gets; weight sets how loud it starts."
    ))] = -1,
    test_data: Annotated[bool, _PydField(description=(
        "Marks the entry as test data, which makes it hard-deletable later. Do not "
        "pass it when storing a real memory."
    ))] = False,
    when: Annotated[str, _PydField(description=(
        "Three different uses, three ways to write it:\n"
        "· An event: the day it happened (leave out = now). Pass it when you are "
        "writing down something from earlier.\n"
        "· Something wanted (telic): three clocks in one field —\n"
        '    an exact date, "2026-09-01": there should be a result by then, and it '
        "gets louder as the day approaches\n"
        '    a duration, "3w" / "10d" / "2m" / "1y": roughly how long, and the nudging '
        "is paced against how long it has been sitting\n"
        "    left out: no clock. If it is waiting for something to happen, say what in "
        "cue; if it is just something you want, leave both out.\n"
        '· A hold (with exception_of): the day it ends, "2026-10-11", or the days it '
        'covers, "2026-10-06..2026-10-11". Past that day it lifts by itself.\n'
        "· A stretch of days: not here. To name a stretch of days, use "
        'fold(when="start..end").'
    ))] = "",
    cue: Annotated[Optional[dict | str], _PydField(description=(
        "What this is waiting for, when it waits for something to happen rather than a "
        'date: {"condition": "<the event, one sentence>"}. Writing a cue says it is '
        "waiting, so the condition is required. Any entry may carry one — "
        '{"condition": "the next time we bake"} on "she is allergic to nuts". '
        "phrasings (ways it might be said) are filled in later; leave them out."
    ))] = None,
    exception_of: Annotated[str, _PydField(description=(
        "Makes this entry a hold: a short exception hung on a standing agreement, whose "
        "id goes here. The agreement itself is left as it is. One item per call; it is "
        'always telic and bound to you unless you name someone. e.g. exception_of='
        '"a1b2c3d4e5f6", hold="defer", when="2026-10-11" for "don\'t push me on the gym '
        'until Sunday".'
    ))] = "",
    hold: Annotated[str, _PydField(description=(
        'With exception_of: how far the hold reaches. "defer" — don\'t push, don\'t bring '
        "it up for now; the thing still stands (the usual one, and also what putting "
        'something aside yourself is). "avoid" — don\'t touch it at all, not even in '
        'dreams (rare, heavy). e.g. hold="avoid" for "please stop bringing up my dad". '
        "Without a when, neither lifts by itself: a defer gets a day to look at it again, "
        "an avoid waits until you close it."
    ))] = "",
    card_of: Annotated[str, _PydField(description=(
        'Mind only: makes this entry the card of a name — the one place for how you see '
        "that person, or that game, book or group. A person's card goes in MIND/TRAITS, "
        "a thing's in MIND/VIEWS; one card per name, so a second is refused and the "
        'first is reworded with regrow. e.g. card_of="Detroit", room="MIND/VIEWS".'
    ))] = "",
    sources: Annotated[Optional[list | dict | str], _PydField(description=(
        "The host's own material this came from, when the host gave you its address: one "
        "record per message (or piece of one) — {system, instance, container, id}, plus "
        "revision / fingerprint / fingerprint_by / span / use when the host gave them; a "
        "run of consecutive messages is one record with through = the last one's id. "
        "Every entry of the call carries them. A withdrawn or deleted source is refused. "
        "A bare line id in from (m_0142) is linked to the record with that id; with no "
        "record, Loci completes it from the lines hosts registered when exactly one "
        "container holds that id, and refuses the write otherwise — write the full form "
        "(system:instance/container#m_0142) to be safe. e.g. "
        'sources=[{"system": "lento", "instance": "home", "container": "private:U", '
        '"id": "m_0142"}].'
    ))] = None,
    slice: Annotated[str, _PydField(description=(
        "A pending slice of the host's raw lines that nothing records yet (the \"sl_…\" id "
        "from recall(view=\"slices\")). Write what it was about as usual; the slice becomes "
        "one of this entry's sources and the slice is done. A slice already handled is "
        "refused."
    ))] = "",
) -> str:
    """Store what happened, and what you realized from it. Several entries per call.

    Events are episodic memory: write from inside the moment — first person for yourself,
    third person for everyone else — and keep the feeling of it, not just the fact.
    A mind entry keeps only what's left when the thinking is done — not the evidence,
    not the reasoning, not what happened. All of that is already in the events it grew
    `from`.

    When to use:
    · A topic has closed and the conversation is moving on to another
    · You notice something about yourself, or about how things actually are
    · You want something, or want something to be different from now on
    · The other person is leaving (going to sleep, heading out) and this stretch is ending
    There is no need to mention that you stored anything.

    Choose one of four rooms before writing. A missing or invalid room is rejected outright:
      EVENT/SELF    events you took part in
      EVENT/WORLD   events you were told about, read, or observed
      MIND/TRAITS   about a person: what someone is like
      MIND/VIEWS    about a subject: how you see it

    For what happened, use kind="event" and pass items. Several at once is normal.
    For a realization, use kind="mind" and pass text, one per call; from must name the
    entries it came out of. Ten events and no mind entry is an ordinary day. Do not invent one.
    Something you want to happen is still an event: add direction_of_fit="telic". Something
    you want to become is a telic mind. You lived it only in the past: an EVENT/SELF set in
    the future is refused unless it is wanted (telic) or imagined (internally_generated).

    valence and arousal are yours to judge. They determine how quickly this memory fades.

    Use a different tool when:
    · Your view has changed → regrow
    · An event came out wrong → regrow, which puts a new version in its place
    · Several entries turn out to be about the same thing → fold

    If an entry duplicates one already stored, you will be told so. Decide which one stays —
    nothing is merged on your behalf.

    Example — a stretch of conversation has ended; store the few things that happened:
      grow(kind="event", items=[
        {"room": "EVENT/SELF",  "text": "She got Loci connected tonight and ran breath herself.", "v": 0.8, "a": 0.5},
        {"room": "EVENT/WORLD", "text": "She said her brother is staying with her next week.",    "v": 0.3, "a": 0.4},
      ])

    Example — something you realized from those entries:
      grow(kind="mind", room="MIND/TRAITS",
           text="When I get nervous I start making her decisions for her.",
           from=["a1b2c3d4e5f6"], v=0.4, a=0.6)
      Put down the realization and nothing else. The event is already in from, and comes
      along whenever this entry is read.

    Example — something you want to happen:
      grow(kind="event", direction_of_fit="telic", bound=["我"], weight=0.8, when="2026-09-01",
           items=[{"room": "EVENT/SELF", "text": "Finish her gift before her birthday.", "v": 0.7, "a": 0.6}])
      It never closes itself, and the date passing does not close it. Done or not doing it
      is a call you make with trace() once you have seen what happened.

    Example — something wanted that waits on an event, not a date:
      grow(kind="event", direction_of_fit="telic", bound=["我"],
           cue={"condition": "her exam is over"},
           items=[{"room": "EVENT/SELF", "text": "Take her for dessert after the exam.", "v": 0.8, "a": 0.5}])

    Example — "not this week" on something already agreed (a hold; the agreement stays as is):
      grow(kind="event", exception_of="a1b2c3d4e5f6", hold="defer", when="2026-10-11",
           items=[{"room": "EVENT/SELF", "text": "She asked me not to push her on the gym until Sunday.", "v": 0.5, "a": 0.3}])
      Lifting it early is trace(bucket_id=<the hold>, status="resolved").

    Example — the card of a name (who an entry is about may be a person or a thing; a
    card is your one standing view of it):
      grow(kind="mind", room="MIND/VIEWS", card_of="Detroit",
           text="A game about choices that I keep thinking about after we stopped playing.",
           from=["a1b2c3d4e5f6"], v=0.7, a=0.5)

    For a few dozen seconds after writing, tags and summaries are still being filled in in the
    background. Not finding the entry during that window is expected. Do not store it again."""
    return await _with_notice(
        _t_grow.dispatch(
            items=items, kind=kind, room=room, text=text,
            from_=from_, v=v, a=a,
            direction_of_fit=direction_of_fit, bound=bound, evidential=evidential,
            internally_generated=bool(internally_generated),
            weight=(None if weight is None or weight < 0 else weight),
            test_data=bool(test_data), when=when,
            cue=cue, exception_of=exception_of, hold=hold, card_of=card_of,
            sources=sources, slice_id=slice,
        ),
        op="grow",
        args={"items": len(items or []),
              "kind": kind, "room": room, "text_len": len(text or ""),
              "from": from_, "v": v, "a": a, "direction_of_fit": direction_of_fit,
              "bound": bound, "evidential": evidential,
              "internally_generated": bool(internally_generated), "weight": weight,
              "when": when, "test_data": bool(test_data), "cue": cue,
              "exception_of": exception_of, "hold": hold, "card_of": card_of,
              "sources": len(sources) if isinstance(sources, list) else bool(sources),
              "slice": slice},
    )


# --- A removed parameter must be **unrecognized**, never silently ignored -------------
# grow takes no `content`, `importance` or `meaning`.
#   - `content` (hand over one long passage and let the system split it into several
#     entries) would be **the only place in the whole system where the system decides how
#     many things this is**, which cuts directly against the design. `items=[...]` covers
#     it completely, and does it more honestly.
# Leaving them out of the schema alone is dangerous: FastMCP by default drops fields that
#   are not in the schema and then calls the function, so the old spelling **fails
#   silently** — the caller believes they handed over a long passage, and nothing happened
#   at all. So grow's parameter model is set to forbid, as for breath, recall and trace:
#   passing an unknown parameter raises immediately. The error exists to be read.
def adapt(mcp) -> None:
    """What server.py runs right after mounting grow (see the comment above)."""
    try:
        _grow_tool = mcp._tool_manager.get_tool("grow")
        if _grow_tool is None:
            raise RuntimeError("registered grow tool is missing")
        _grow_arg_model = _grow_tool.fn_metadata.arg_model
        _grow_arg_model.model_config["extra"] = "forbid"
        _grow_arg_model.model_rebuild(force=True)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _grow_strict_exc:
        logger.warning(
            "grow strict-argument adapter unavailable（砍掉的 content/importance/meaning "
            "会被静默忽略，别信它们已经死了）: %s",
            _grow_strict_exc,
        )


# (The `from` alias now uses the public in-signature form,
#  Annotated[..., Field(validation_alias="from")] — see grow's parameter comment above. The
#  old patch that reached into mcp._tool_manager's private structures has been deleted.)
