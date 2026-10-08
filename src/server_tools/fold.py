# -*- coding: utf-8 -*-
"""
server_tools/fold.py — fold: draw one circle over several entries (a gist, a period, a scene)

The face a client sees: the signature, the description, and the one call that
forwards to tools/fold/ through server_call._with_notice. server.py mounts it
(mcp.tool()) and then runs adapt(mcp).
"""

import logging
from typing import Annotated, Optional

from pydantic import Field as _PydField

from tools import fold as _t_fold      # one action, three ways of drawing the circle (regrow is its n=1 case)
from server_call import _with_notice

logger = logging.getLogger("loci_brain")


async def fold(
    text: Annotated[str, _PydField(description=(
        "The line itself. Always yours to write, stored exactly as written."
    ))],
    room: Annotated[str, _PydField(description=(
        "One of the four rooms. Naming a stretch of days (with when) takes an EVENT "
        "room; gathering realizations (with folds) takes a MIND room."
    ))] = "",
    v: Annotated[float, _PydField(description=(
        "valence, 0~1. Required, and yours to set."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "arousal, 0~1. Required, and yours to set."
    ))] = -1,
    # On the tool surface this parameter is `folds`, matching the tool's own metaphor:
    # folding, not covering.
    # WARNING: underneath, and **on disk**, it is still cover / covered_by. The storage
    #    fields are deliberately not renamed — renaming them would mean migrating the entire
    #    store. This only changes the name the model sees to the right one.
    folds: Annotated[list, _PydField(description=(
        "The entries to fold up. Real bucket_ids. Realizations only.\n"
        "⛔ There is no folding a group of events: to mark off a stretch of days use\n"
        "   when, and to follow one thread across time use recall with a query."
    ))] = [],
    when: Annotated[str, _PydField(description=(
        '"start..end", for naming a stretch of days. Leave the end open while it is\n'
        'still running: "2026-07-31..".\n'
        "Give this or folds, never both."
    ))] = "",
    # The public parameter name is "from", as in grow and as the spec requires. `from` is a
    # Python keyword, so the signature spells it from_ and pydantic's public
    # validation_alias catches it.
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "What this line grew out of, at most 64.\n"
        "⚠️ Not the same thing as folds:\n"
        "   from   what it grew out of. Those entries go on surfacing normally.\n"
        "   folds  what it covers. Those entries stop surfacing on their own.\n"
        "Both can be given at once."
    ))] = [],
    test_data: Annotated[bool, _PydField(description=(
        "Marks the entry as test data. Do not pass it in normal use."
    ))] = False,
    sources: Annotated[Optional[list | dict | str], _PydField(description=(
        "The host's own material this line came from, as in grow: "
        "[{system, instance, container, id, …}]."
    ))] = None,
) -> str:
    """Fold entries up under one line you write yourself.

    Nothing underneath is lost. Folded entries stay searchable, stay reachable by drilling
    in, and stay readable word for word by their id. They simply stop taking up a line of
    their own when you are looking back.

    When to use:
    · After muse, looking at what it laid out, you can see those really are one thing
    · A stretch of days is over and you want to give it a name

    Two ways to fold, told apart by which parameter you give:

    · folds=[several ids] with room set to one of the MIND rooms
      Gathers realizations that are about the same thing, and that feel alike, under the one
      line you write. The ones you name stop surfacing on their own and give way to it.

    · when="start..end" with room set to one of the EVENT rooms
      Gives a stretch of days a name. That is a period, and it holds nothing but its name
      and its range: not one memory is pinned down by it. Who belongs to a period is worked
      out from the dates every time it is read, so anything written down later falls into
      place on its own, and periods can overlap and sit inside one another.

    The line is always yours to write, and it is stored exactly as you wrote it.
    Give folds or when, never both.

    Do not use this tool when:
    · One entry has a newer version. Use regrow.

    Example — several realizations under one line:
      fold(folds=["a1b2c3d4e5f6", "b2c3d4e5f6a1", "c3d4e5f6a1b2"],
           room="MIND/TRAITS",
           text="The moment I get impatient I start making her decisions for her.",
           v=0.4, a=0.6)

    Example — giving a stretch of days a name:
      fold(when="2026-08-15..2026-08-18", room="EVENT/SELF",
           text="The stretch where we moved the memory system onto a name of our own.",
           v=0.8, a=0.5)"""
    return await _with_notice(
        _t_fold.dispatch(text=text, room=room, v=v, a=a, cover=folds,
                         when=when, from_=from_, test_data=bool(test_data),
                         sources=sources),
        op="fold",
        args={"text_len": len(text or ""), "room": room, "v": v, "a": a,
              "folds": folds, "when": when, "from": from_,
              "test_data": bool(test_data),
              "sources": len(sources) if isinstance(sources, list) else bool(sources)},
    )


# --- A removed or renamed parameter must be **unrecognized** ---------------------------
# `cover` was renamed to `folds`. FastMCP by default **quietly drops** fields that are not
#    in the schema and then calls the function — which means the old spelling
#    `fold(cover=[...])` **silently folds nothing at all**, without a single word of error.
#    breath, grow, recall and trace got forbid; fold and muse were missed.
#    The error exists to be read: the first call that uses the old spelling finds out it is
#    gone.
def adapt(mcp) -> None:
    """What server.py runs right after mounting fold (see the comment above)."""
    try:
        _fold_tool = mcp._tool_manager.get_tool("fold")
        if _fold_tool is None:
            raise RuntimeError("registered fold tool is missing")
        _fold_arg_model = _fold_tool.fn_metadata.arg_model
        _fold_arg_model.model_config["extra"] = "forbid"
        _fold_arg_model.model_rebuild(force=True)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _fold_strict_exc:
        logger.warning("fold strict-argument adapter unavailable: %s", _fold_strict_exc)
