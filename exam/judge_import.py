# -*- coding: utf-8 -*-
"""
exam/judge_import.py — bring outside verdicts back into a whole-turn report.

    python exam/judge_import.py exam/out/turn-<time> verdicts.txt

When no judge is configured, turn.py writes judge-packet.md: every ungraded rubric with
the reply it is about. Anyone can grade it — a person, or a model in any app — as long
as the answer is one JSON object per line:

    {"key": "I7#2/run1", "pass": false, "reason": "..."}

Lines that are not JSON are skipped, so an answer pasted with some chatter around it
still imports. A key that matches nothing is reported, never silently dropped. Verdicts
land only on runs that were still ungraded; nothing already decided is overwritten.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from exam.turn import CheckRun, CheckSummary, TurnItemResult, judge_packet, save  # noqa: E402

KEY = re.compile(r"^(?P<item>.+)#(?P<check>\d+)/run(?P<run>\d+)$")


def load(out: Path) -> tuple[list[TurnItemResult], dict]:
    raw = json.loads((out / "results.json").read_text(encoding="utf-8"))
    results = []
    for r in raw:
        checks = [CheckSummary(**{**c, "runs": [CheckRun(**x) for x in c["runs"]]})
                  for c in r["checks"]]
        results.append(TurnItemResult(**{**r, "checks": checks}))
    return results, json.loads((out / "meta.json").read_text(encoding="utf-8"))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    out, verdict_file = Path(sys.argv[1]), Path(sys.argv[2])
    results, meta = load(out)
    by_id = {r.item_id: r for r in results}
    applied, unmatched = 0, []
    for line in verdict_file.read_text(encoding="utf-8").splitlines():
        line = line.strip().strip(",")
        if not line.startswith("{"):
            continue
        try:
            v = json.loads(line)
            m = KEY.match(str(v["key"]))
            r = by_id[m["item"]]
            run = r.checks[int(m["check"]) - 1].runs[int(m["run"]) - 1]
        except Exception:
            unmatched.append(line[:120])
            continue
        if run.ok is None:
            run.ok = bool(v["pass"])
            run.evidence = f"graded outside: {v.get('reason', '')}"
            applied += 1
    meta["judge"] = f"{meta.get('judge', '')} + outside verdicts ({verdict_file.name})"
    save(out, results, meta)
    left = judge_packet(results)
    (out / "judge-packet.md").write_text(left, encoding="utf-8") if left else \
        (out / "judge-packet.md").unlink(missing_ok=True)
    print(f"applied {applied} verdicts; unmatched lines: {len(unmatched)}")
    for u in unmatched:
        print(f"  ? {u}")
    print(f"report: {out / 'report.md'}" + ("" if not left else " (some rubrics still ungraded)"))


if __name__ == "__main__":
    main()
