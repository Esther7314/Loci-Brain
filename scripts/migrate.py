# -*- coding: utf-8 -*-
"""
migrate.py — bring a library to the version this code expects (core/schema.py).

Stop the server first: this rewrites memory files underneath it.

    python scripts/migrate.py --buckets /path/to/buckets            # dry run: what would change
    python scripts/migrate.py --buckets /path/to/buckets --apply    # back up, then migrate

Without --buckets it uses LOCI_BUCKETS_DIR. The backup is a zip of the whole library
(minus config.yaml and earlier backups) under <buckets>/_backups/, written before the
first file is touched.
"""

import argparse
import io
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core import schema  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--buckets", default=os.environ.get("LOCI_BUCKETS_DIR", ""))
    ap.add_argument("--apply", action="store_true", help="write (after a full backup)")
    args = ap.parse_args()
    if not args.buckets or not Path(args.buckets).is_dir():
        print(f"buckets directory not found: {args.buckets or '(not given)'}")
        return 2

    report = schema.migrate(args.buckets, apply=args.apply)
    if report["from"] is None:
        print("New library with no memories: nothing to migrate.")
        return 0
    if not report["steps"]:
        print(f"Library is already version {report['from']}.")
        return 0

    print(f"Library version {report['from']} -> {report['to']}"
          + ("" if args.apply else "  (dry run, nothing written)"))
    for s in report["steps"]:
        fields = ", ".join(f"{k} {n}" for k, n in sorted(s["fields"].items())) or "nothing"
        print(f"  step {s['from']} -> {s['to']}: {s['files']} files ({fields})")
    if args.apply:
        print(f"Backup: {report['backup']}")
    else:
        print("Run again with --apply to write. A full backup is made first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
