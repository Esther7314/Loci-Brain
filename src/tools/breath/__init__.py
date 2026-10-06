"""
========================================
tools/breath/__init__.py — breath tool entry point
========================================

breath is "I open my eyes and see what I remember". **No parameters, no
branches**: one act, one screen, always the same screen.

- awaken.py: that screen (the note by the door · reminders · the middle stretch ·
  something that just came to mind)

Key behaviour:
- dispatch() does exactly three things: record an op, make sure the forgetting
  engine is running, render the waking screen
- No bucket fetching or LLM calls happen here

What this file deliberately does not do:
- No permission checks; the MCP caller is assumed to be the model itself

Exports: dispatch() -> str

⚰️ **The whole parameterised retrieval layer underneath was deleted.**
   It used to route on parameters into five branches: `catalog` (catalogue mode) ·
   `feel` (the feel channel) · `importance` (pull by importance) · `surface` (the
   old surfacing) · `search` (keyword + vector retrieval), plus the `_verbatim.py`
   they shared — six files, 1110 lines.
   🔴 **Once breath's nine tool-facing parameters were cut, not one of those five
   had an entry point left** — `server.py` always calls `_t_breath.dispatch()`
   with no arguments, and the tool schema force-empties its parameters with
   `extra="forbid"`, so nothing can be pushed in from outside either.
   **Leave a road with no entrance and the next person reading this code (me)
   will assume it is still alive.**
   Finding things is `recall`'s job — that is the retriever; breath only opens
   its eyes.
   (Every reference was checked one by one before deleting: the five branches
    were called only from this file, `_verbatim` only by those four, `awaken`
    depended on none of them, and every smoke test calls `breath({})`.)
========================================
"""

from .. import _runtime as rt
from .awaken import surface_awaken


async def dispatch() -> str:
    if rt.mark_op:
        rt.mark_op("breath")
    await rt.decay_engine.ensure_started()
    return await surface_awaken()
