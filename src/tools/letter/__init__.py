"""
========================================
tools/letter/__init__.py — letter tool entry point (reading and writing letters)
========================================

plan and letter are both "special-channel buckets" (type=plan / type=letter):
they never take part in ordinary breath surfacing, and each has its own entry
point. All three (plan / letter_write / letter_read) live under the plan
subpackage so the special channels can be read as one picture.

Exports: letter_write / letter_read
========================================
"""

from .core import letter_write, letter_read  # noqa: F401
