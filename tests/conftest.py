# -*- coding: utf-8 -*-
"""
tests/conftest.py — make the import paths the tests see identical to the real runtime

Inside this repo the modules import one another as `core.xxx` / `tools.xxx` (in the container
everything is launched with `src/` as the root), but pytest runs from the repo root, where
`src/` is not on sys.path. So we insert it at the very front here.

⚠️ Don't work around this by writing `from src.core import ...` instead — that tests a
different import structure, and the tools' `from core import runtime as rt` would then load
a second copy of the module that `src.core._bigevent` reads, a path the real runtime never
takes. Green tests would say nothing about whether production can even start.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
