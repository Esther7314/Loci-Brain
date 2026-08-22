# -*- coding: utf-8 -*-
"""A tool that rejects unknown arguments must also *say* it rejects them.

WHY THIS FILE EXISTS
    Making a tool strict takes three steps, and the third one leaves no trace when you
    skip it. `extra=forbid` plus `model_rebuild()` changes what a call does; it does not
    change the input schema FastMCP cached at registration time. Miss the re-publish and
    you get a tool that refuses a misspelled argument while still advertising that it
    accepts anything — the client only learns the rule by being rejected, which is the
    failure the gate was installed to prevent.

    That is not hypothetical. `trace` was hardened by hand and did all three. A later
    sweep, written so that future tools would be covered automatically, copied the first
    two. Eight of the nine tools shipped with a schema that contradicted their behaviour,
    and nothing was red, because every existing assertion looked at the model config —
    the half that was correct.

    So the assertions here read `tool.parameters` — **what a client is actually shown** —
    and one of them deliberately reproduces the old two-step version to prove it is not
    enough. If someone ever inlines the flip again, that test is the one that goes red.

WHAT THIS DOES NOT CHECK
    That every real tool in server.py goes through harden(). Importing server.py builds
    the whole world (BucketManager, engines, the live data directory), so that check does
    not belong in a unit test. What is checked here is that the one function they all
    call cannot be half-applied.
"""
import pytest
from mcp.server.fastmcp import FastMCP

from core.strict_args import harden, is_strict


def _toy_tool():
    """A freshly registered tool, exactly as FastMCP leaves it."""
    server = FastMCP("strict-args-test")

    @server.tool()
    async def toy(bucket_id: str, name: str = "") -> str:
        return f"{bucket_id}/{name}"

    return server._tool_manager.get_tool("toy")


def test_a_fresh_tool_publishes_a_permissive_schema():
    # The premise of the whole module: FastMCP's default is not strict. If this ever
    # goes green-by-default, harden() is solving a problem that no longer exists and
    # this file should be read again rather than deleted.
    assert is_strict(_toy_tool()) is False


def test_harden_makes_the_published_schema_strict():
    tool = _toy_tool()
    assert harden(tool) is True
    assert is_strict(tool) is True
    assert tool.parameters["additionalProperties"] is False


def test_flipping_the_model_alone_does_not_reach_the_client():
    """The exact two-step version the sweep used to inline. It must not be enough."""
    tool = _toy_tool()
    model = tool.fn_metadata.arg_model
    model.model_config["extra"] = "forbid"
    model.model_rebuild(force=True)

    # The call would now reject unknown arguments...
    assert model.model_config["extra"] == "forbid"
    # ...and the client is still told the opposite.
    assert is_strict(tool) is False, (
        "flipping extra=forbid updated behaviour without updating the published schema — "
        "this is the drift core/strict_args exists to prevent"
    )

    # harden() repairs that state even though the model is already strict, which is why
    # it re-publishes unconditionally instead of returning early.
    assert harden(tool) is False
    assert is_strict(tool) is True


def test_harden_is_idempotent():
    tool = _toy_tool()
    harden(tool)
    schema_after_first = tool.parameters
    assert harden(tool) is False
    assert tool.parameters == schema_after_first


def test_strict_schema_still_describes_the_real_arguments():
    # A gate that hardened the schema by emptying it would pass every assertion above.
    tool = _toy_tool()
    harden(tool)
    assert set(tool.parameters["properties"]) == {"bucket_id", "name"}
    assert tool.parameters["required"] == ["bucket_id"]
