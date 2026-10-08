# -*- coding: utf-8 -*-
"""
server_tools/muse.py — muse: look at what keeps coming back, without a target

The face a client sees: the signature, the description, and the one call that
forwards to tools/muse/ through server_call._with_notice. server.py mounts it
(mcp.tool()) and then runs adapt(mcp).
"""

import logging
from typing import Annotated

from pydantic import Field as _PydField

from tools import muse as _t_muse      # musing: the threshold engine's second instance
from server_call import _with_notice

logger = logging.getLogger("loci_brain")


async def muse(
    cluster: Annotated[int, _PydField(description=(
        "Read cluster N in full, word for word. Leave it out to see only which "
        "clusters and hints are there."
    ))] = 0,
    not_same: Annotated[list, _PydField(description=(
        "The set that turned out not to be one thing. Real bucket_ids. Recorded, so "
        "the same set is not raised again; a changed set comes back."
    ))] = [],
) -> str:
    """Muse: find which entries are about the same thing, and lay them out in front of you.

    It points; it does not write. The line that gathers them is always yours (use fold).

    Two steps:
    · muse()           see which clusters and which hints are there. Evidence and counts
                       only, no memory text.
    · muse(cluster=N)  read cluster N in full, word for word, then decide for yourself
                       whether to fold it.

    Everything laid out comes with its evidence, and nothing without evidence is shown:
    · For realizations, evidence is ordered by how hard it is: valence/arousal coordinates,
      then from links, then semantic similarity (the vector is only a first pass, and
      anything it brought in is marked as such). Time plays no part; realizations do not go
      by calendar.
    · For events, what it looks for is which stretch of days has no name yet: a scene word
      that appears densely inside a stretch and not outside it; the vector centroid jumping
      between one time window and the next; or a stretch that falls into no period at all.

    Anything just written is left out, and nothing still unfolding is pointed at.

    Use this when you have been told that a few clusters are waiting.

    If you look and decide they are not one thing after all, muse(not_same=[id, id]) puts
    that on record and the same set is not raised again. Change the set, by one entry either
    way, and it comes back.

    Do not use this tool when:
    · You are looking for something. Use recall."""
    return await _with_notice(
        _t_muse.dispatch(cluster=int(cluster or 0), not_same=not_same),
        op="muse",
        args={"cluster": cluster, "not_same": not_same},
    )


# Same as above. muse never had a parameter renamed, but one typo (`clusters=`,
# `not_same_ids=`) is silently ignored just the same — and a tool whose whole purpose is
# "I just want to look" must not lie: it hands back the default screen without a word, and
# that screen looks exactly like the one that was asked for.
def adapt(mcp) -> None:
    """What server.py runs right after mounting muse (see the comment above)."""
    try:
        _muse_tool = mcp._tool_manager.get_tool("muse")
        if _muse_tool is None:
            raise RuntimeError("registered muse tool is missing")
        _muse_arg_model = _muse_tool.fn_metadata.arg_model
        _muse_arg_model.model_config["extra"] = "forbid"
        _muse_arg_model.model_rebuild(force=True)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _muse_strict_exc:
        logger.warning("muse strict-argument adapter unavailable: %s", _muse_strict_exc)
