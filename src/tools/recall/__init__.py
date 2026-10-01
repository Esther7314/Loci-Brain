"""
========================================
tools/recall/ — remembering
========================================

The first act of "reading": reaching for something. The three gates stack, and
whatever comes back always goes through the same zoom.

    recall(when="上月")                           time gate
    recall(room="MIND/TRAITS")                    room gate (prefixes work, e.g. room="MIND")
    recall(query="跟海有关的那件事")               search (keyword + vector) -> time + score
    recall(query="学代码", view="scene")            cluster by shared scene words (how it got here)
    recall(when="本周", tag="Home")               stack them; tag is a free fourth sieve
    recall(when="上月", slices=1)                   how coarse or fine I want that stretch
    recall(view="slices")                         the host's raw lines, sliced and waiting
                                                  for me to handle (tools/_slices.py)

    ⚠️ Every example above uses a form `_parse_when()` actually accepts. Two of them
       used to be `when="7月底"` and `when="那阵子"`, which it does not: it takes
       48h / 7d / 今天 / 昨天 / 前天 / 本周 / 上周 / 本月 / 上月 / 今年 / 2026-07 /
       2026-07-15 / 起..止, and nothing else. An example that does not run is worse
       than no example — it is the documentation asserting a capability that is absent.

🔪 **`by` was removed**: `by="touched"` (there is no such act as "digesting"
   here) and `by="回看"` (superseded by `slices`). The rule behind it:
   **every parameter must map onto a sentence that actually surfaces in the mind.**

Three density tiers:
    A overview (>=4 cells): two lines per cell — a stats line + what stands out (with id)
    B single cell (1-3 cells): the full card — what I was doing / what it circled /
      how it felt / what jumps out
    C item list (<=20 items): id + gist (🧠 = a piece of thinking), gist only, never the body

Core principle: the output is fragments, not sentences, and **never goes through a
model** — every character is either what was written down when it was stored, or
fixed text from a template (the plain-speech shell).

Exports: dispatch(when, room, tag, query, slices, view) -> str
========================================
"""

from .core import recall_core


async def dispatch(
    when: str = "",
    room: str = "",
    tag: str = "",
    query: str = "",
    slices: int = 0,
    view: str = "",
) -> str:
    try:
        slices = int(slices or 0)
    except (TypeError, ValueError):
        slices = 0
    kwargs = {}
    if 1 <= slices <= 20:
        kwargs["max_cells"] = slices
    return await recall_core(
        when=str(when or ""),
        room=str(room or ""),
        tag=str(tag or ""),
        query=str(query or ""),
        view=str(view or ""),
        **kwargs,
    )
