"""
========================================
tools/_runtime.py — the runtime context shared by every tool module
========================================

This file solves one engineering problem: after the split, every tool submodule
needs access to config / bucket_mgr / dehydrator / decay_engine /
embedding_engine / logger — the global objects server.py creates — but a
submodule must not import server.py back (that would be a circular import).

The approach: once server.py has initialised every component it calls init(...)
to push the references in; every tool module then does
`from . import _runtime as rt` and reads `rt.bucket_mgr`.

Key behaviour:
- A lightweight container holding references to the shared objects
- init() writes once; tool modules only read afterwards, never write

What this file deliberately does not do:
- Creates no objects, loads no configuration, initialises no logging
- No thread-safety guards: the write happens once, during server.py startup

Exports: init() / config / bucket_mgr / dehydrator / decay_engine /
         embedding_engine / import_engine / logger / fire_webhook / mark_op
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
mark_op: Optional[Callable[..., None]] = None


def init(**kwargs: Any) -> None:
    """Called once by server.py, after every component exists, to write the
    references onto this module's globals.
    A test fixture may call this again to override individual fields; the effect
    is the same as monkeypatching."""
    g = globals()
    for k, v in kwargs.items():
        g[k] = v
