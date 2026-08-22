# -*- coding: utf-8 -*-
"""Reject unknown tool arguments — and say so in the schema, not only at call time.

WHY THIS FILE EXISTS
    Pydantic's default is `extra=ignore`: a misspelled argument is dropped in silence and
    the call goes through as if it had never been passed. For `trace` that turns an
    intended edit into a bucket-id-only no-op, so a gate was installed there by hand, and
    it did three things: set `extra=forbid`, rebuild the model, and — the part that is
    easy to forget — **write the rebuilt schema back onto the registered tool**.

    That third step is not decoration. FastMCP caches a tool's public input schema at
    registration time, so flipping `extra` afterwards changes what the call does without
    changing what the client is told. The client then discovers the rule by having a call
    rejected, which is exactly the failure the gate was meant to prevent.

    Later a sweep was added so that any tool added in the future would get the gate
    automatically. The sweep copied the first two steps and dropped the third. Result:
    nine tools rejected unknown arguments at runtime while eight of them still published
    a schema that said unknown arguments were fine. The one tool that was correct was the
    hand-written one.

    So the whole point of this module is that the three steps live in one place and are
    applied by one function. A second caller cannot copy two of them.

WHAT THIS DOES NOT CHECK
    Whether the arguments themselves are sensible. This only makes "this name is not an
    argument of this tool" visible up front instead of after a failed call.
"""


def harden(tool):
    """Make `tool` reject unknown arguments, and keep its published schema in sync.

    Idempotent: a tool that is already strict still gets its cached schema refreshed,
    because "already forbidden" and "already published as forbidden" are two different
    facts and only the first one is cheap to check.

    Returns True when this call was the one that flipped the model, False when the model
    was already strict (the schema is refreshed either way).
    """
    model = tool.fn_metadata.arg_model
    newly_installed = model.model_config.get("extra") != "forbid"
    if newly_installed:
        model.model_config["extra"] = "forbid"
        model.model_rebuild(force=True)
    # Always re-publish: the model can be strict while the cached schema still says
    # otherwise, which is the exact state this module exists to make impossible.
    tool.parameters = model.model_json_schema()
    return newly_installed


def is_strict(tool) -> bool:
    """True when the tool's **published** schema forbids unknown arguments.

    Deliberately reads `tool.parameters` — what a client is actually shown — rather than
    the model config, because that is the half that was wrong for eight tools.
    """
    return bool(tool.parameters.get("additionalProperties") is False)
