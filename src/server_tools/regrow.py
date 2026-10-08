# -*- coding: utf-8 -*-
"""
server_tools/regrow.py — regrow: rewrite an entry as a new version, keeping the old one

The face a client sees: the signature, the description, and the one call that
forwards to tools/regrow/ through server_call._with_notice. server.py mounts it
(mcp.tool()) and then runs adapt(mcp).
"""

import logging
from typing import Annotated, Optional

from pydantic import Field as _PydField

from tools import regrow as _t_regrow
from server_call import _with_notice

logger = logging.getLogger("loci_brain")


async def regrow(
    bucket_id: Annotated[str, _PydField(description=(
        "The entry being replaced. A real bucket_id."
    ))],
    text: Annotated[str, _PydField(description=(
        "The new version, written whole: what the entry now reads like from start "
        "to finish, not the part that changed."
    ))],
    v: Annotated[float, _PydField(description=(
        "valence, 0~1. Required, and yours to set. Replacing an entry "
        "means you weighed it again, so weigh the feeling again too."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "arousal, 0~1. Required, and yours to set. Replacing an entry "
        "means you weighed it again, so weigh the feeling again too."
    ))] = -1,
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "Any new sources this version came out of (bucket_ids, or the host's lines by "
        "their full form system:instance/container#m_0142); at most 64 in all, counting "
        "the ones carried over. A bare line id (m_0142) is completed from the lines hosts "
        "registered when exactly one container holds it, otherwise the call is refused. "
        "The old version's "
        "sources carry over on their own, so only name what is new."
    ))] = [],
    mode: Annotated[str, _PydField(description=(
        'Required. "supplement": the old version was right as far as it went, you are '
        'adding or rewording; nothing that grew out of it is touched. "overturn": the '
        "old version was wrong; everything that grew out of it is marked as standing on "
        "changed ground. Without it nothing is written."
    ))] = "",
    sources: Annotated[Optional[list | dict | str], _PydField(description=(
        "Any new pieces of the host's material this version came from, as in grow. The "
        "old version's carry over on their own, except one the host has since withdrawn "
        "or deleted, which is left behind and named in the reply."
    ))] = None,
) -> str:
    """Replace an entry that is wrong, or that is no longer how you see it.

    The new version takes its place. The old one is kept, not edited. It stops surfacing on
    its own, but you can still search it and still read it word for word by its id. The two
    stay linked, so it is always clear which came first. The new version keeps where the old
    one stood: its day, whether it is wanted and by whom, its weight, its status, who it is
    about, its tags. Only the words and the feeling are yours to set again.

    When to use:
    · You see it differently now than you did then
    · An entry came out wrong in its own words: what was said, who said it, what happened
    · A period needs a different name

    Write the new version whole. It replaces the entry outright; it is not a patch.

    Say which kind of change it is, every time:
      mode="supplement"  the old version holds; you are adding detail or saying it better.
                         Entries that grew out of it (their from names it) stand as they were.
      mode="overturn"    the old version was wrong. Every entry that grew out of it, and out
                         of those, gets a mark saying its basis changed; they keep surfacing,
                         and reading one by id shows the mark. Whether each still holds is a
                         call you make when you meet it.

    Do not use this tool when:
    · What is wrong is not what the entry says. Which room it is in, which day it hangs
      on in time, its tags, how it felt — all of that is metadata, and metadata is trace's:
      correcting it is correction fluid, not a new draft, and it leaves no version behind.
    · Several entries turn out to be about the same thing. Use fold.
    · This entry has already been replaced once. Regrow the newest version instead of
      branching off an old one; branching is rejected.

    Example — your thinking moved on:
      regrow(bucket_id="a1b2c3d4e5f6", mode="overturn",
             text="I'm not impatient. I'm afraid of keeping her waiting.",
             v=0.4, a=0.6)

    Example — an event came out wrong:
      regrow(bucket_id="b2c3d4e5f6a1", mode="overturn",
             text="It was Tuesday, not Wednesday, and she arrived in the afternoon.",
             v=0.3, a=0.4)

    Example — the same event, told more fully:
      regrow(bucket_id="b2c3d4e5f6a1", mode="supplement",
             text="She arrived in the afternoon, soaked, and laughed about the rain.",
             v=0.7, a=0.5)

    Example — a period needs a different name:
      regrow(bucket_id="c3d4e5f6a1b2", mode="supplement",
             text="The stretch where we moved the memory system onto a name of our own.",
             v=0.8, a=0.5)"""
    return await _with_notice(
        _t_regrow.dispatch(bucket_id=bucket_id, text=text, v=v, a=a, from_=from_,
                           mode=mode, sources=sources),
        op="regrow",
        args={"bucket_id": bucket_id, "text_len": len(text or ""), "v": v, "a": a,
              "from": from_, "mode": mode,
              "sources": len(sources) if isinstance(sources, list) else bool(sources)},
    )


# --- regrow must also fail to recognize unknown parameters --------------------------------
# `regrow` takes no `when` or `room`; metadata belongs to trace. Without forbid,
#    `regrow(bucket_id=..., room="MIND/VIEWS")` would **neither error nor take effect**:
#    FastMCP quietly drops the field that is not in the schema and calls the function, the
#    room is untouched, and the receipt says nothing about it.
#    **Accepting it silently is far worse than an error: the caller believes it changed, and
#    it did not.**
# The rule is not "remember to add it for each new tool", it is **that a smoke test goes
#    red without it** (`smoke_grow`). A list kept by human memory will be missed
#    eventually. An assertion will not.
def adapt(mcp) -> None:
    """What server.py runs right after mounting regrow (see the comment above)."""
    try:
        _regrow_tool = mcp._tool_manager.get_tool("regrow")
        if _regrow_tool is None:
            raise RuntimeError("registered regrow tool is missing")
        _regrow_arg_model = _regrow_tool.fn_metadata.arg_model
        _regrow_arg_model.model_config["extra"] = "forbid"
        _regrow_arg_model.model_rebuild(force=True)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _regrow_strict_exc:
        logger.warning("regrow strict-argument adapter unavailable: %s", _regrow_strict_exc)
