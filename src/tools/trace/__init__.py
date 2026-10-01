"""
========================================
tools/trace/__init__.py — trace tool entry point
========================================

trace is "I correct or update one memory". Nothing in trace really branches, so
there is a single core.py implementation. This module forwards dispatch under the
call's write key, so a resent trace is answered with the first reply instead of
appending twice (core/_sources.SourceRegistry.run_once).

Exports: dispatch(...) -> str (parameters match the trace tool in server.py)
========================================
"""

import functools

from core import _sources as _src
from .._common import with_write_key
from .core import trace_core


@functools.wraps(trace_core)
async def dispatch(*args, **kwargs) -> str:
    return await with_write_key(_src.current_write_key(),
                                lambda: trace_core(*args, **kwargs), op="trace")
