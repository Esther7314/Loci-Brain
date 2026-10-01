"""
========================================
tools/trace/__init__.py — trace tool entry point
========================================

trace is "I correct or update one memory". Nothing in trace really branches, so
there is a single core.py implementation. This module forwards dispatch under the
call's write key, so a resent trace is answered with the first reply instead of
appending twice (core/_sources.SourceRegistry.run_once).

A pending slice of the host's raw lines (`slice="sl_…"`) is handled here before
core.py sees the call (tools/_slices.py): with bucket_id the slice is appended to
that memory's sources as one record; `slice_span` re-cuts it and `drop_slice` drops it, both
without a bucket.

Exports: dispatch(...) -> str (parameters match the trace tool in server.py)
========================================
"""

import functools
import inspect

from core import _sources as _src
from utils import parse_bool
from .._common import with_write_key
from .._slices import trace_slice
from .core import trace_core


async def _trace(*args, slice_id: str = "", drop_slice: bool = False, slice_span: str = "",
                 **kwargs) -> str:
    if not str(slice_id or "").strip():
        if parse_bool(drop_slice, default=False) or str(slice_span or "").strip():
            return 'drop_slice / slice_span 要跟 slice="切片id" 一起用。本次什么都没改。'
        return await trace_core(*args, **kwargs)
    kwargs = dict(inspect.signature(trace_core).bind_partial(*args, **kwargs).arguments)
    return await trace_slice(slice_id, drop=parse_bool(drop_slice, default=False),
                             span=slice_span, kwargs=kwargs, trace_core=trace_core)


@functools.wraps(trace_core)
async def dispatch(*args, **kwargs) -> str:
    return await with_write_key(_src.current_write_key(),
                                lambda: _trace(*args, **kwargs), op="trace")
