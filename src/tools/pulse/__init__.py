"""
========================================
tools/pulse/__init__.py — pulse tool entry point
========================================

iter 2.0 introduced anchor (the coordinate-frame bucket). anchor and release
are a matched pair of switches; pulse is the whole-system overview, and since
the tree is organised per tool it lives here too, where it reads naturally.

Exports: pulse
========================================
"""

from .core import pulse  # noqa: F401
