"""
========================================
tools/__init__.py — the entry point for every MCP tool implementation
========================================

This file makes src/tools a Python package. Each subdirectory is one MCP tool
(breath / hold / grow / trace / anchor / plan / dream), split into separate
files along code paths so each can be read and edited on its own.

Key behaviour:
- Package marker only; no runtime initialisation happens here
- The actual runtime context (config / bucket_mgr / dehydrator ...) is injected
  by server.py after startup through tools._runtime.init(...)

What this file deliberately does not do:
- No submodule imports here, so that starting server.py is not forced to load
  every tool
- No global object references held here

Exports: none (package marker only)
========================================
"""
