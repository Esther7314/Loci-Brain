# -*- coding: utf-8 -*-
"""
tests/test_keyed_turn_after_a_kill.py — a keyed turn held by a process that was killed is
free at once.

regrow runs under a keyed turn on the old id. It used to be a lock file with a 180-second
stale rule, so after a kill (a restart in the middle of a regrow) the next regrow of that
entry waited three minutes. The turn is now a lease the operating system drops with the
process.
"""

import asyncio
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from tools import _common
from tools import _runtime as rt

SRC = str(Path(__file__).resolve().parent.parent / "src")

HOLDER = textwrap.dedent("""
    import asyncio, sys, types
    sys.path.insert(0, {src!r})
    from tools import _runtime as rt
    from tools import _common
    rt.bucket_mgr = types.SimpleNamespace(base_dir={base!r})
    rt.config = {{}}

    async def main():
        async with _common._keyed_turn("regrow-abc123abc123"):
            print("held", flush=True)
            await asyncio.sleep(120)
    asyncio.run(main())
""")


def test_a_turn_left_by_a_killed_process_is_free_at_once(tmp_path, monkeypatch):
    holder = subprocess.Popen([sys.executable, "-c", HOLDER.format(src=SRC, base=str(tmp_path))],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
    finally:
        holder.kill()
        holder.wait(timeout=10)
    monkeypatch.setattr(rt, "bucket_mgr", types.SimpleNamespace(base_dir=str(tmp_path)))
    monkeypatch.setattr(rt, "config", {})

    async def take():
        async with _common._keyed_turn("regrow-abc123abc123"):
            return True
    assert asyncio.run(asyncio.wait_for(take(), timeout=5.0))


def test_a_live_holder_still_keeps_the_turn(tmp_path, monkeypatch):
    holder = subprocess.Popen([sys.executable, "-c", HOLDER.format(src=SRC, base=str(tmp_path))],
                              stdout=subprocess.PIPE, text=True)
    monkeypatch.setattr(rt, "bucket_mgr", types.SimpleNamespace(base_dir=str(tmp_path)))
    monkeypatch.setattr(rt, "config", {})

    async def take():
        async with _common._keyed_turn("regrow-abc123abc123"):
            return True
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(take(), timeout=1.0))
    finally:
        holder.kill()
        holder.wait(timeout=10)
