# -*- coding: utf-8 -*-
"""
server_tools/breath.py — breath: wake up (one screen, no arguments), then the silent dream upkeep

The face a client sees: the signature, the description, and the one call that
forwards to tools/breath/ through server_call._with_notice. server.py mounts it
(mcp.tool()) and then runs adapt(mcp).
"""

import asyncio
import logging

from tools import breath as _t_breath
from server_call import _resolve_call, _with_notice

# Dreaming, the threshold engine's third instance: **it has no MCP tool surface** — dreams
# are delivered through the bridge.
# It is imported here only for the silent hook on waking: sweep the expired ones, and weave
# if the backlog is over the line.
# A name collision here makes the hook **silently do nothing**: one line in the log saying
#    the module has no such attribute, while a smoke test for "do not weave below the
#    threshold" stays green because it never runs at all. **That is exactly how a green
#    light lies.**
from core import _dream as _dream_engine

logger = logging.getLogger("loci_brain")


async def breath() -> str:
    # breath takes no parameters (its schema on the tool surface is forcibly emptied, see
    #    the adapter below) and has no parameterized retrieval underneath.
    #    Finding things is recall's job. breath only wakes up: one action, one screen.
    """Wake up. Call this once before you say anything. It takes no arguments.

    It gives you the one screen you should see on waking, in five parts:
    · 核心         names, what you call each other, and the principles you have pinned.
                   What earns a place here: things there is no time to go and look up.
    · 惦记的事     what you want or owe and what is coming, each line saying why it is
                   here now: how many days are left (or how long overdue), or that a
                   promise has no time yet, or has hung so long it may not count any more.
                   Questions to answer sit under it: a hold whose review day has come, a
                   line that reads like a promise you never marked.
    · 近三天       the last few days collapsed into a single card, in plain words rather
                   than machine readings. Three days unless set otherwise; the title
                   names the window (近七天 covers seven).
    · 忽然想起     two older things coming back on their own — one because something from
                   the last few days shares a name or a scene with it, one at random.
    · 依据变了的   entries whose ground moved: corrected by a person on the panel, or
                   standing on something that was overturned, revised or withdrawn.
                   Rewrite (regrow), put away (trace delete=True), or keep as is
                   (trace invalidation="confirmed").
    Anything else is asleep: still there, found by recall, and finding it does not wake it.

    Waking up calls no model. Everything on the screen was written earlier: the rules
    word for word as you wrote them, and the rest assembled from templates and from the
    one-line summary stored alongside each entry — those summaries were written by a
    model when the entry went in, not now. The rules are printed in full: a rule is
    already the short version of itself, and putting a summary of it in front of you
    every morning means reading someone else's paraphrase of your own words.

    The summaries are hooks. When one looks relevant, go and get the original with recall.

    Do not use this tool when:
    · You are looking for something. Use recall. This one only handles waking up."""
    result = await _with_notice(_t_breath.dispatch(), op="breath", args={})
    # --- The nightly auto-weave hook ---
    # **Anything that only happens if someone remembers to do it will not happen.** So
    # weaving hangs off waking up, rather than depending on the model remembering to call it.
    # But **not one word is added to breath** — that is a hard boundary. Two silent things
    #    happen here and nothing else: (1) sweep the dreams whose time is up (delete the
    #    file, leave a trace) and (2) if the backlog is over the line and nothing was woven
    #    today, weave one.
    #    The dream that comes out is **not stuffed into this return value**: how a dream
    #    reaches the conversation, and how it is removed from the context afterwards, is the
    #    bridge's job.
    # It runs on every breath that reads the whole library: dreams are the life line's,
    # woven from everything, so a host waking under a read scope (or refused) does not
    # drive them.
    try:
        req = _resolve_call()[0]
        whole = req is None or req.whole_library
    except Exception:                           # noqa: BLE001 — upkeep is never worth a failed breath
        whole = False
    if whole:
        asyncio.create_task(_dream_upkeep())
    return result


async def _dream_upkeep() -> None:
    """The background beat that follows waking up. **Swallows every exception**: a dream
    that fails to weave must never break breath."""
    try:
        await _dream_engine.maintain()
    except Exception as _dream_exc:  # noqa: BLE001
        logger.warning("织梦挂点失败（不影响 breath）: %s", _dream_exc)


# Keep the advertised schema parameter-free so claude.ai still auto-loads the
# default surfacing tool.  The callable deliberately retains the pre-2.6.8
# signature behind that schema: clients which cached the old tool definition
# may keep sending those arguments after an upgrade, and FastMCP otherwise
# silently drops every unknown field before calling a zero-argument function.
def adapt(mcp) -> None:
    """What server.py runs right after mounting breath (see the comment above)."""
    try:
        _breath_public_tool = mcp._tool_manager.get_tool("breath")
        if _breath_public_tool is None:
            raise RuntimeError("registered breath tool is missing")
        # Unknown/typoed legacy arguments must fail loudly instead of recreating
        # the original bug by degrading a targeted request into default surfacing.
        _breath_arg_model = _breath_public_tool.fn_metadata.arg_model
        _breath_arg_model.model_config["extra"] = "forbid"
        _breath_arg_model.model_rebuild(force=True)
        _breath_public_tool.parameters = {
            "properties": {},
            "title": "breathArguments",
            "type": "object",
        }
    except (AttributeError, RuntimeError, TypeError, ValueError) as _breath_compat_exc:
        logger.warning(
            "breath legacy-argument compatibility adapter unavailable: %s",
            _breath_compat_exc,
        )


# -- Dreaming ---------------------------------------------------------------------------
# The engine is `core/_dream.py`: four material sources -> one independent call ->
# two layers (whole dream plus fragments) -> fragments follow a time-based lifecycle ->
# leave a trace.
# Retrieval does not go through the MCP tool surface: `GET /api/dream/current`
# (web/loci_dream.py) plus the engine functions weave() and current_dream().
# It is not upstream Night-Fall's design (a three-hour latency, delete after four missed
# catches, surface only on resonance, invisible even to its own author once written):
# **not one of those is what this system wants.**
