# -*- coding: utf-8 -*-
"""
server_tools/recall.py — recall: reach for something (time / room / tag gates, or a query)

The face a client sees: the signature, the description, and the one call that
forwards to tools/recall/ through server_call._with_notice. server.py mounts it
(mcp.tool()) and then runs adapt(mcp).
"""

import logging
from typing import Annotated

from pydantic import Field as _PydField

from tools import recall as _t_recall
from server_call import _with_notice

logger = logging.getLogger("loci_brain")


async def recall(
    when: Annotated[str, _PydField(description=(
        "A stretch of time. Understood forms: 48h / 7d / today / yesterday / "
        "day before yesterday / this week / last week / this month / last month / "
        "this year / 2026-07 / 2026-07-15 / start..end.\n"
        "The Chinese spellings (今天 / 昨天 / 前天 / 本周 / 上周 / 本月 / 上月 / 今年) "
        "name exactly the same stretches and are equally accepted.\n"
        "It only narrows where to look. It does not change the shape of what comes "
        "back. That is decided by whether there is a query."
    ))] = "",
    room: Annotated[str, _PydField(description=(
        "Which room. A prefix is enough: EVENT (both event rooms) / MIND (both mind "
        "rooms) / EVENT/SELF / EVENT/WORLD / MIND/TRAITS / MIND/VIEWS."
    ))] = "",
    tag: Annotated[str, _PydField(description=(
        'A tag, matched by containment: "床" also finds "床上" and "床头".\n'
        "Tags are words lifted from the text when the memory was written, so a tag has "
        "always appeared in the original."
    ))] = "",
    query: Annotated[str, _PydField(description=(
        "What you are looking for. One or two words, ideally the words that were used "
        "at the time. Give it and entries come back matched and scored; leave it out "
        "and the stretch comes back laid out by time.\n"
        "A full bucket_id can also be passed here to read that one entry verbatim."
    ))] = "",
    slices: Annotated[int, _PydField(description=(
        "1~20: how coarse or how fine you want this stretch. Leave it out and it is "
        "chosen from the span. slices=1 collapses the whole stretch into one card; "
        "slices=20 cuts it fine enough to see the distribution.\n"
        "Only meaningful without a query."
    ))] = 0,
    view: Annotated[str, _PydField(description=(
        'Three values. "scene": groups the entries that matched into clusters sharing the '
        "same scene words, with the rest hanging under a representative. This is how a "
        "single thread reads across time. Needs a query: without one nothing has "
        "matched, so there is nothing to cluster.\n"
        "\"slices\", on its own: the host's raw lines of a day, cut into slices and "
        "waiting for you — each with its span, one line of what it is, and the entries "
        "from that day that look like they already record it. Handle each one: not "
        'recorded → grow(..., slice="sl_…"); recorded → trace(bucket_id=…, '
        'slice="sl_…"); cut wrong → trace(slice="sl_…", slice_span=…) or drop_slice=True.\n'
        '"original", with query set to one entry\'s id: asks the host for the original '
        "material that entry was formed from (the host keeps it; Loci keeps only where it "
        "is). The first line says whether the host gave it, could not reach it for now "
        "(the entry's own text stands in), or no longer allows it (then nothing of the "
        "entry is shown). Use it when the exact words matter; it goes over the network, so "
        "not for every read.\n"
        '"original" also reads an imported conversation Loci keeps itself: query set to its '
        'source string ("import:imp_…/c0001#l0003..l0020") reads that stretch, query set to '
        "a few words searches every imported line for them."
    ))] = "",
) -> str:
    """Look back through memories that are already stored.

    Four filters. Give at least one:
      when    a stretch of time
      room    which room
      tag     a tag
      query   words to search on

    query is what you are looking for; the other three are where to look. What comes back
    depends on whether you give a query at all:

    · With a query: entries are matched and scored; hits that grew from the same root are
      one line, promises still open come first, the rest newest first. Anything below the line
      is not thrown away: it collapses into a single line telling you how many were held
      back, the highest score among them, and what the oldest one was about.
      Use one or two words, and use the words that were actually written at the time.
      A long phrase gets averaged out in the vector and finds less, not more.

    · Without a query: the stretch is laid out by time instead. The last few days come back
      entry by entry; anything older collapses into a sentence with two or three
      representatives.
      This is the one to use when you don't know what you are looking for and just want to
      see what was there.

    Not seeing something this way does not mean it is gone. Memories that have not been
    thought about in a long time stop surfacing on their own, but they are still there and
    still searchable.

    When to use:
    · The user refers back to something: "that thing I told you about last time…"
    · The user says what they want
    · You suspect this has come up before
    · What is being said now might hang on an older condition (a plan, a promise, a limit,
      something said before): go and look; general knowledge only tells you what to look
      for, never fills in a fact about this person

    If two attempts turn up nothing, stop and tell the user plainly that you cannot find it.
    Do not keep rewording the search. That is how you end up inventing an answer.

    Example — look through a stretch of time:
      recall(when="last week")

    Example — find one thing:
      recall(query="青岛")

    Example — find one thing inside a stretch of time (where to look + what to look for):
      recall(when="上月", query="青岛")

    Example — read one entry word for word:
      recall(query="a1b2c3d4e5f6")
      Passing a full bucket_id as the query returns that entry verbatim, along with its
      metadata, where it came from, and what has cited it.

    Example — the host's original words behind one entry:
      recall(query="a1b2c3d4e5f6", view="original")"""
    return await _with_notice(
        _t_recall.dispatch(when=when, room=room, tag=tag, query=query,
                           slices=int(slices or 0), view=view),
        op="recall",
        args={"when": when, "room": room, "tag": tag,
              "query_len": len(query or ""), "slices": slices, "view": view},
    )


# --- A removed parameter must be **unrecognized**, never silently ignored --------------
# FastMCP by default **quietly drops** fields that are not in the schema and then calls the
# function. So an old spelling like `by="..."` degrades silently into the default view —
# the caller believes they are looking at the full, uncollapsed list and are handed a
# collapsed screen instead, **with no signal whatsoever**: a knob that does not exist
# still appearing to do something.
# As with breath's compatibility adapter just above, recall's parameter model is set to
# forbid, so an unknown or misspelled parameter raises immediately.
# **The error exists to be read**: wherever a manual teaches `by=`, the first call that
# spells it that way finds out it does not exist.
def adapt(mcp) -> None:
    """What server.py runs right after mounting recall (see the comment above)."""
    try:
        _recall_tool = mcp._tool_manager.get_tool("recall")
        if _recall_tool is None:
            raise RuntimeError("registered recall tool is missing")
        _recall_arg_model = _recall_tool.fn_metadata.arg_model
        _recall_arg_model.model_config["extra"] = "forbid"
        _recall_arg_model.model_rebuild(force=True)
    except (AttributeError, RuntimeError, TypeError, ValueError) as _recall_strict_exc:
        logger.warning(
            "recall strict-argument adapter unavailable（砍掉的参数会被静默忽略，"
            "别信 by= 已经死了）: %s",
            _recall_strict_exc,
        )
