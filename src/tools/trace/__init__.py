"""
========================================
tools/trace/__init__.py — trace tool entry point
========================================

trace is "I correct or update one memory". Nothing in trace really branches, so
there is a single core.py implementation. This module only forwards dispatch.

Exports: dispatch(...) -> str (parameters match the trace tool in server.py)
========================================
"""

from .core import trace_core as dispatch  # noqa: F401
