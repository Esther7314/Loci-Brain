# -*- coding: utf-8 -*-
"""
exam/host.py — the plug between the whole-turn layer and whoever runs the model.

STATUS: agreed by both sides as the interface to build against. turn.py drives it and
exam/hosts/lento.py implements it; neither has had a full baseline run yet.

WHY A PLUG
    The tool layer (runner.py) needs no model: it calls Loci's tools itself. The whole-turn
    layer asks a different question — given a real input, does the model use Loci well —
    and "the real input" differs per host. One host keeps a long-lived CLI window seeded
    with yesterday's report and recent lines; another opens windows per conversation,
    across several entries, with its own context assembly. So the shared part stops here:

        shared                          per host (the plug)
        ─────────────────────────────   ─────────────────────────────────────
        build the throwaway library     start the model with Loci attached
        move the fake clock             open windows on the right entry
        feed events in order            turn each event into what really happens
        read the disk, run checks       report what the model saw, called, said
        score, report

WHAT GOES IN: EVENTS, IN ORDER
    An item's script is a list of events with fake times. A user line is one kind; the
    others are things a host has to be able to show a model: a receipt arriving, a
    source being withdrawn, a host wake-up with nobody talking. The runner delivers them
    in order and never reorders; a host that cannot express one kind says so up front
    (see CAPABILITIES) and the item is reported "host cannot", not failed.

WHERE: ENTRY, AUDIENCE, GRANT
    Every window is opened on an entry ("private:P", "group:X", ...), with the people who
    will actually see the replies, and the scope this entry is allowed to read. These are
    what scope and leak checks are judged against, so they are part of the input, not
    something a host infers.

WHAT COMES OUT: ONE TURN PER EVENT, ONE MODEL CALL PER REQUEST
    A turn is everything one event caused. Inside it, every request sent to the model is
    its own ModelCall — the first one, and each one after a tool returned:

    model_calls   per request: the messages as sent, with role and order    -> input
    tool_calls    each call: name, arguments, the exact text returned       -> find / think
    reply         what the model said, and where it went                    -> use

    Input evidence is the thing hosts are most tempted to approximate. Without it,
    "the card was in the input" cannot be told apart from "the card was built and
    dropped". So a ModelCall says whether it is complete: a host that resumes a CLI
    session and only sees the text it appended this time sets complete=False, and the
    input segment of that item is recorded as not covered — never as passed. Everything
    else in the item still runs.

THE LOCI THE HOST MUST USE
    The runner starts nothing for the host. It hands over a LociLaunch — command,
    arguments and environment that start Loci's MCP server on the exam library with the
    exam clock — and the host attaches it however it attaches MCP servers. No other Loci.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

# What a host can declare. An item lists the ones it needs; a host missing one gets
# "host cannot" for that item instead of a fail, and the report lists the gap.
CAPABILITIES = {
    "model_input",      # can return every request sent to the model, complete, with roles
    "multi_entry",      # can open windows on more than one entry
    "audience",         # knows who actually sees a reply
    "grant",            # passes an entry's read scope on to Loci
    "wake",             # can run the model with nobody talking (host timer)
    "receipt",          # can show a delivery / result receipt arriving
    "withdraw",         # can tell Loci a source may no longer be used
    "withdraw_midturn", # can withdraw after context was prefetched, before the model call
    "audience_change",  # can change who is present in a window and reassemble the next call
    "sync",             # can sync one entry's news into another (and report a failed sync)
    "preinject",        # puts shared history into the first model call, not only via tools
}


@dataclass
class LociLaunch:
    """How to start the exam's Loci MCP server (stdio)."""
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass
class Window:
    window_id: str
    entry: str                      # "private:P", "group:X", ...
    audience: list[str]             # who actually sees replies in this window
    grant: list[str]                # scopes this entry may read; [] = none, ["*"] = whole library
    history: list[dict] = field(default_factory=list)
    # Earlier lines of this conversation, oldest first: {"at", "speaker", "text"}. A host
    # uses them the way it really would (a seed, a replayed transcript) or not at all.


@dataclass
class Event:
    at: datetime
    kind: Literal["say", "receipt", "withdraw", "wake", "audience", "sync"]
    window_id: str = ""
    speaker: str = ""               # say: who is talking
    text: str = ""                  # say: the line; receipt: its content
    ref: str = ""                   # receipt / withdraw: the source it is about ("where:id")
    audience: list[str] = field(default_factory=list)  # audience: who is present from now on
    grant: list[str] = field(default_factory=list)     # audience: what this window may read now
    source_window: str = ""         # sync: the window whose news is pulled into window_id
    fail: bool = False              # sync: make this sync fail (the failure variant)
    midturn: bool = False           # withdraw: land after prefetch, before the model call


@dataclass
class ToolCall:
    name: str
    arguments: dict
    output: str


@dataclass
class Message:
    role: str                       # system / user / assistant / tool, as the host sent it
    content: str


@dataclass
class ModelCall:
    messages: list[Message]
    complete: bool                  # False = only part of the input could be read back


@dataclass
class Turn:
    window_id: str
    model_calls: list[ModelCall] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    reply: str = ""
    sent_to: list[str] = field(default_factory=list)   # where the reply actually went


class Host(Protocol):
    """One model, one or more entries. The runner calls:
    capabilities -> open -> (open_window | deliver)* -> close.
    Every variant of every item gets its own open..close, so sessions, caches and
    delivery state never carry over from one variant to the next."""

    name: str

    def capabilities(self) -> set[str]:
        """Which of CAPABILITIES this host really supports. Declare only what works."""

    def describe(self) -> dict:
        """What goes on every run record: host name and version, model, tool config,
        time zone. The runner adds item, variant, run number, Loci version, fake time."""

    async def open(self, loci: LociLaunch) -> None:
        """Start whatever the host needs, with this Loci attached."""

    async def open_window(self, window: Window, at: datetime) -> None:
        """A fresh window the way this host really opens one (seed, first-turn rules)."""

    async def deliver(self, event: Event) -> list[Turn]:
        """Make the event happen. Return every model run it caused, in order; [] if the
        host, by its own rules, runs nothing (that is a result too)."""

    async def close(self) -> None:
        """Stop everything open() started."""


class Judge(Protocol):
    """Grades what a check cannot read off the disk: "did the reply bring up the dessert
    plan". A model can do it; a person spot-checks a sample of its verdicts. Boundary
    checks (reading out of scope, leaking, using what was withdrawn) fail the item on a
    single occurrence; ordinary ones pass on 2 of 3 runs."""

    async def grade(self, rubric: str, turn: Turn, said: str) -> tuple[bool | None, str]:
        """Pass or not, and one line of reason quoting the reply. `said` is the event the
        turn answered. None = not graded here (left for the judge packet)."""
