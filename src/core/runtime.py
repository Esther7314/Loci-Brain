"""
========================================
core/runtime.py — the runtime context shared by the tools and the engine pieces
========================================

This file solves one engineering problem: every tool submodule, and the engine
pieces under core/ that run on the live library (fold, muse, dream, big events),
need access to config / bucket_mgr / dehydrator / decay_engine /
embedding_engine / logger — the global objects server.py creates — but none of
them may import server.py back (that would be a circular import).

The approach: once server.py has initialised every component it calls init(...)
to push the references in; every reader then does
`from core import runtime as rt` and reads `rt.bucket_mgr` at call time.
It lives in core/ because core reads it too, and it imports nothing, so any
layer can import it at module top.

Key behaviour:
- A lightweight container holding references to the shared objects
- init() writes once; readers only read afterwards, never write

What this file deliberately does not do:
- Creates no objects, loads no configuration, initialises no logging
- No thread-safety guards: the write happens once, during server.py startup

Exports: init() / config / bucket_mgr / dehydrator / decay_engine /
         embedding_engine / import_engine / logger / fire_webhook
========================================
"""

from typing import Any, Awaitable, Callable, Optional

# --- Shared object references, injected by server.py at startup via init(...) ---
config: Any = None
bucket_mgr: Any = None
dehydrator: Any = None
decay_engine: Any = None
embedding_engine: Any = None
embedding_outbox: Any = None
import_engine: Any = None
logger: Any = None

# --- Shared helper callbacks (also injected by server.py, to avoid a back-import) ---
fire_webhook: Optional[Callable[[str, dict], Awaitable[None]]] = None


def init(**kwargs: Any) -> None:
    """Called once by server.py, after every component exists, to write the
    references onto this module's globals.
    A test fixture may call this again to override individual fields; the effect
    is the same as monkeypatching."""
    g = globals()
    for k, v in kwargs.items():
        g[k] = v
