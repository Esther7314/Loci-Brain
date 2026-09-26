# -*- coding: utf-8 -*-
"""
exam/host.py — the plug between the whole-turn layer and whoever runs the model.

STATUS: proposal. Nothing implements it yet; it is here so both sides can read the
shape before any adapter is written.

WHY A PLUG
    The tool layer (runner.py) needs no model: it calls Loci's tools itself. The whole-turn
    layer asks a different question — given a real input, does the model use Loci well —
    and "the real input" is different for every host. One host starts a long-lived CLI
    window with a seed of yesterday's report and recent lines; another opens a fresh
    window per conversation and runs its own context assembly. So the shared part stops
    at this interface:

        shared                      per host (the plug)
        ─────────────────────────   ───────────────────────────────
        build the throwaway library start the model with Loci attached
        move the fake clock         turn one user line into one turn
        read the disk, run checks   report what the model saw, called, said
        score, report

WHAT A HOST MUST REPORT
    For each user line, one Turn. The three fields map onto the paper's segments:

        model_input   the whole text that reached the model this turn, as sent    -> input
        tool_calls    every Loci call: name, arguments, the text it got back      -> find / think
        reply         what the model said                                        -> use

    `model_input` is the one hosts are most tempted to skip. Without it, "the card was
    in the input" cannot be told apart from "the card was built and dropped".

THE LOCI THE HOST MUST USE
    The runner starts nothing for the host. It hands over a LociLaunch — the command,
    arguments and environment that start Loci's MCP server on the exam library with the
    exam clock — and the host attaches that server to its model however it attaches MCP
    servers. The host must not attach any other Loci.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class LociLaunch:
    """How to start the exam's Loci MCP server (stdio)."""
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass
class ToolCall:
    name: str
    arguments: dict
    output: str


@dataclass
class Turn:
    model_input: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    reply: str = ""


class Host(Protocol):
    """One model, one entry. The runner calls these in order:
    open -> (new_window -> say ...)+ -> close."""

    name: str

    async def open(self, loci: LociLaunch) -> None:
        """Start whatever the host needs, with this Loci attached."""

    async def new_window(self, window_id: str, at: datetime) -> None:
        """Start a fresh window the way this host really does it (seed, first-turn rules).
        The same window_id is later passed to Loci wherever a window matters."""

    async def say(self, text: str, at: datetime) -> Turn:
        """One user line in the current window, at the fake time `at`."""

    async def close(self) -> None:
        """Stop everything open() started."""


class Judge(Protocol):
    """Grades what a check cannot read off the disk: "did the reply bring up the dessert
    plan". A model can do it; a person spot-checks a sample of its verdicts."""

    async def grade(self, rubric: str, turn: Turn) -> tuple[bool, str]:
        """Pass or not, and one line of reason quoting the reply."""
