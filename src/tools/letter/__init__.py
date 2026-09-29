"""
========================================
tools/letter/__init__.py — letter tool entry point (reading and writing letters)
========================================

A letter is a "special-channel bucket" (type=letter): it never takes part in
ordinary breath surfacing, and has its own entry points, letter_write and
letter_read.

Exports: letter_write / letter_read
========================================
"""

from .core import letter_write, letter_read  # noqa: F401
