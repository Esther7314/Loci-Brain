# -*- coding: utf-8 -*-
"""
exam/serve.py — start the real Loci MCP server (stdio) with the exam's fake clock.

The runner launches this instead of src/server.py. Everything the server reads comes
from the environment the runner sets:

    LOCI_BUCKETS_DIR    the throwaway library for this item
    LOCI_CONFIG_PATH    the exam config (no model keys, no embeddings)
    EXAM_CLOCK_FILE     where the fake "now" lives (see clock.py)
    EXAM_SEED           seed for random: breath's "suddenly remembered" and dream picks

Nothing else differs from a real start: same tools, same argument checks, same output.
"""

import os
import random
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from exam import clock  # noqa: E402

clock.install(Path(os.environ["EXAM_CLOCK_FILE"]))
random.seed(int(os.environ.get("EXAM_SEED", "0")))

runpy.run_path(str(ROOT / "src" / "server.py"), run_name="__main__")
