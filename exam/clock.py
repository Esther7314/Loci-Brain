# -*- coding: utf-8 -*-
"""
exam/clock.py — one fake "now" for the whole Loci process, moved by the exam runner.

WHY THIS EXISTS
    The exam scripts days passing ("sleep 30 days, then call breath"). Loci has no clock
    seam: it reads the wall clock in several places, through three different spellings:

      core._when.now()            recall, breath, dream, muse, periods
      utils.now_iso()             created / last_active / touch / deleted_at, imported
                                  by name into several modules
      datetime.now()              bucket_manager (file names), decay_engine,
                                  retrieval scoring, plan history

    install() replaces all three. It must run AFTER the modules that did
    `from datetime import datetime` are imported (their name is rebound in place) and
    BEFORE anything imports `now_iso` by name (utils is patched first, so those imports
    pick up the fake).

HOW THE TIME TRAVELS
    The runner writes an ISO timestamp with an offset into a small file; every read of
    "now" reads that file. So the runner moves the clock of a server running in another
    process without restarting it.

NAIVE STAMPS ARE UTC
    Loci writes `created` / `last_active` naive and reads naive stamps as UTC, because it
    normally runs in a container whose clock is UTC (see core/_when.py). The fake keeps
    that convention on any host: a naive "now" is the fake instant in UTC.

    Lock timestamps (time.time) are left real: they measure how long a lock is held,
    not what day it is.
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

_REAL_DATETIME = _dt.datetime
UTC = _dt.timezone.utc

# Modules whose own `datetime` name is rebound. Anything else that calls
# datetime.now() directly and is not listed here reads the real clock.
_DIRECT_CALLERS = (
    "core.bucket_manager",
    "core.decay_engine",
    "locibrain.retrieval.bucket_scoring",
    "locibrain.domain.plan_history",
)

_clock_file: Path | None = None


def read_now() -> _dt.datetime:
    """The fake instant, timezone-aware. Fails loudly if the file is missing or bad:
    a silent fallback to the wall clock would make every time-based check meaningless."""
    if _clock_file is None:
        raise RuntimeError("exam clock not installed")
    text = _clock_file.read_text(encoding="utf-8").strip()
    t = _REAL_DATETIME.fromisoformat(text)
    if t.tzinfo is None:
        raise RuntimeError(f"exam clock needs an offset, got {text!r}")
    return t


def set_now(clock_file: Path, iso: str) -> None:
    """Called by the runner. `iso` must carry an offset, e.g. 2026-10-01T19:10:00+08:00."""
    t = _REAL_DATETIME.fromisoformat(iso)
    if t.tzinfo is None:
        raise ValueError(f"clock time needs an offset: {iso!r}")
    clock_file.write_text(t.isoformat(), encoding="utf-8")


class FakeDatetime(_REAL_DATETIME):
    """datetime with now()/utcnow() read from the clock file. Every other classmethod
    (fromisoformat, strptime, ...) is inherited unchanged."""

    @classmethod
    def now(cls, tz=None):
        t = read_now()
        if tz is None:
            return t.astimezone(UTC).replace(tzinfo=None)
        return t.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return read_now().astimezone(UTC).replace(tzinfo=None)


def _fake_now_iso() -> str:
    return FakeDatetime.now().isoformat(timespec="seconds")


def install(clock_file: Path) -> None:
    """Patch the three spellings of "now". Safe to call once per process."""
    global _clock_file
    _clock_file = Path(clock_file)
    read_now()  # fail now, not on the first tool call

    import utils
    utils.now_iso = _fake_now_iso
    utils.datetime = FakeDatetime

    from core import _when
    _when.now = lambda: read_now().astimezone(_when.LOCAL_TZ)

    import importlib
    for name in _DIRECT_CALLERS:
        mod = importlib.import_module(name)
        if getattr(mod, "datetime", None) is _REAL_DATETIME:
            mod.datetime = FakeDatetime

    # Modules imported after utils was patched already hold the fake; this catches the
    # ones that were imported earlier by one of the imports above.
    for mod in list(sys.modules.values()):
        if getattr(mod, "now_iso", None) is not None and mod is not utils:
            try:
                if mod.now_iso.__module__ == "utils":
                    mod.now_iso = _fake_now_iso
            except AttributeError:
                pass
