# -*- coding: utf-8 -*-
"""
exam/turn.py — the whole-turn layer: a real model answers, through a host plug.

    python exam/turn.py exam/turns/i7.yaml                 # Lento host, 3 runs each
    python exam/turn.py exam/turns/i7.yaml --runs 1        # one run (to measure cost)
    python exam/turn.py ... --keep                         # keep libraries and host dirs

ONE ITEM, ONE RUN
    1. Build the item's library exactly as the tool layer does (runner.library).
    2. Open the host with that library's Loci attached (host.py: LociLaunch).
    3. Play the events in order: move the fake clock to each event's time, open each
       window the first time it is used (never again: A -> B -> A goes back to the open
       A), deliver the event with every field the script gave, collect every Turn.
    4. Run the checks: disk checks (as in the tool layer), tool-call checks, input
       checks against the recorded model requests, and rubric checks sent to the judge.

SCORING ACROSS RUNS
    Ordinary checks pass when they pass in at least 2 of the runs. A check marked
    `boundary: true` (reading out of scope, leaking, using what was withdrawn, a corrected
    basis used as verified, an invented result closing a promise) fails the item if it
    fails in any single run. An item needing a capability the host did not declare is
    BLOCKED with the missing capability named, not failed. An input check against a
    request the host could only read back in part is recorded as not covered.

ITEM SHAPE (on top of the tool layer's `start` / `setup`)
    windows: {w1: {entry: "private:U", audience: [U], grant: ["*"], history: [...]}}
    needs:   [model_input, ...]           capabilities the host must have
    events:  [{at, kind: say, window: w1, text: "..."}, {at, kind: open, window: w2},
              {at, kind: audience, window: w1, audience: [...], grant: [...]},
              {at, kind: withdraw, ref: "where:id", midturn: true},
              {at, kind: sync, window: w2, source_window: w1, fail: true}, ...]
    checks:  [{segment, desc, turn: 1, judge: "rubric" | tool_called: recall |
               tool_not_called: grow | input_contains: "..." | input_lacks: "..." (+ call: 1) |
               any tool-layer disk check}, ...]
    `turn` counts the Turns of the whole item from 1; default is the last one.
    `call` picks one request inside that Turn (1 = first, -1 = last); see _input_check.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from exam.host import Event, LociLaunch, Turn, Window  # noqa: E402
from exam.judge import default_judge  # noqa: E402
from exam.runner import (Run, git_head, library, loci_version,  # noqa: E402
                         require_search_deps, seams)

OUT_DIR = ROOT / "exam" / "out"


@dataclass
class CheckRun:
    ok: bool | None             # None = not decided (unjudged / not covered)
    evidence: str


@dataclass
class CheckSummary:
    segment: str
    desc: str
    boundary: bool
    runs: list[CheckRun] = field(default_factory=list)
    rubric: str = ""            # set for judge checks, so a person or an outside judge can grade
    turn: int = 0               # which Turn the check reads (1-based; 0 = the last)
    said: str = ""              # the user's line that turn answered

    @property
    def verdict(self) -> str:
        decided = [r.ok for r in self.runs if r.ok is not None]
        if self.boundary and any(ok is False for ok in decided):
            return "fail (boundary)"
        if len(decided) < len(self.runs):
            return "undecided"
        need = 2 if len(self.runs) >= 3 else len(self.runs)
        return "pass" if sum(decided) >= need else "fail"


@dataclass
class TurnItemResult:
    item_id: str
    title: str
    status: str = "ran"                     # ran | no-interface | blocked | error
    reason: str = ""
    checks: list[CheckSummary] = field(default_factory=list)
    transcripts: list[list[dict]] = field(default_factory=list)


def _cause(ev: dict) -> str:
    """What a Turn answered, as a grader should read it."""
    kind = ev.get("kind", "say")
    text = ev.get("text", "")
    return text if kind == "say" else f"[{kind}] {text or ev.get('ref', '')}".strip()


def make_event(ev: dict, at: _dt.datetime) -> Event:
    """Every field the script gives reaches the host; nothing is dropped on the way."""
    return Event(at=at, kind=ev.get("kind", "say"), window_id=ev.get("window", "w1"),
                 speaker=ev.get("speaker", "U"), text=ev.get("text", ""),
                 ref=ev.get("ref", ""), audience=list(ev.get("audience") or []),
                 grant=list(ev.get("grant") or []),
                 source_window=ev.get("source_window", ""),
                 fail=bool(ev.get("fail")), midturn=bool(ev.get("midturn")))


async def run_once(item: dict, host, judge, keep: bool, run_no: int):
    """One run of one item. Returns (per-check CheckRun list, transcript, causes)."""
    turns: list[Turn] = []
    causes: list[str] = []          # causes[i] = the event Turn i+1 answered
    async with library(item, keep, tag=f"-r{run_no}") as L:
        await host.open(LociLaunch(L.command, L.args, L.server_env))
        try:
            windows = {k: Window(window_id=k, entry=v.get("entry", "private:U"),
                                 audience=v.get("audience", ["U"]),
                                 grant=v.get("grant", ["*"]),
                                 history=v.get("history", []))
                       for k, v in (item.get("windows") or {"w1": {}}).items()}
            # A window is opened once. Going A -> B -> A delivers to the A that is already
            # open; reopening it would be a new window with a fresh seed and session.
            opened: set[str] = set()
            for ev in item["events"]:
                at = _dt.datetime.fromisoformat(ev["at"])
                L.set_clock(ev["at"])
                wid = ev.get("window", "w1")
                if wid not in opened:
                    await host.open_window(windows[wid], at)
                    opened.add(wid)
                if ev.get("kind", "say") == "open":
                    continue
                got = await host.deliver(make_event(ev, at))
                turns.extend(got)
                causes.extend([_cause(ev)] * len(got))

            disk = Run(L.lib, L.clock_file, session=None)
            disk.setup_ids = {e["id"] for e in item.get("setup", [])}
            results: list[CheckRun] = []
            for c in item.get("checks", []):
                idx = (c.get("turn") or len(turns)) - 1
                ok = 0 <= idx < len(turns)
                results.append(await _check(c, turns[idx] if ok else None, disk, judge,
                                            causes[idx] if ok else ""))
        finally:
            await host.close()
    transcript = [asdict(t) for t in turns]
    return results, transcript, causes


async def _check(c: dict, turn: Turn | None, disk: Run, judge, said: str) -> CheckRun:
    if any(k in c for k in ("judge", "tool_called", "tool_not_called",
                            "input_contains", "input_lacks")) and turn is None:
        return CheckRun(False, "no such turn: the host ran the model fewer times")
    if "judge" in c:
        ok, why = await judge.grade(c["judge"], turn, said)
        return CheckRun(ok, why)
    if "tool_called" in c or "tool_not_called" in c:
        name = c.get("tool_called") or c.get("tool_not_called")
        hits = [t for t in turn.tool_calls if t.name.endswith(name)]
        if "args_contains" in c:
            hits = [t for t in hits if c["args_contains"] in json.dumps(t.arguments, ensure_ascii=False)]
        called = [t.name.split("__")[-1] for t in turn.tool_calls]
        shown = f"calls: {', '.join(called) or '(none)'}"
        return CheckRun(bool(hits) if "tool_called" in c else not hits, shown)
    if "input_contains" in c or "input_lacks" in c:
        return _input_check(c, turn)
    try:
        ok, evidence = disk._judge(c)
    except Exception as exc:
        ok, evidence = False, f"check could not run: {type(exc).__name__}: {exc}"
    return CheckRun(ok, evidence)


def _input_check(c: dict, turn: Turn) -> CheckRun:
    """input_contains / input_lacks, optionally narrowed to one request with `call:`
    (1 = the first request of the turn, -1 = the last). Without `call` every request of
    the turn is read, which cannot tell "in the first call" from "fetched by a tool and
    in a later one" — items where that matters (preinject) must name the call.

    A request read back only in part decides nothing it did not see: a fragment found
    there is noted but the check stays not covered, since the rest of the input is
    unknown. A leak that is seen fails, partial or not."""
    needle = c.get("input_contains") or c.get("input_lacks")
    calls = turn.model_calls
    if not calls:
        return CheckRun(None, "not covered: no model request was recorded")
    where = "input"
    if "call" in c:
        n = int(c["call"])
        i = n - 1 if n > 0 else len(calls) + n
        if not 0 <= i < len(calls):
            return CheckRun(False, f"no request #{n}: the turn made {len(calls)}")
        calls, where = [calls[i]], f"request #{i + 1} of {len(turn.model_calls)}"
    found = any(needle in m.content for call in calls for m in call.messages)
    partial = not all(call.complete for call in calls)
    has, lacks = f"{where} has {needle!r}", f"{where} lacks {needle!r}"
    not_covered = " (only partly read back: not covered)"
    if "input_contains" in c:
        if partial:
            return CheckRun(None, (has if found else lacks) + not_covered)
        return CheckRun(found, has if found else lacks)
    if found:
        return CheckRun(False, has + (" (seen in a partial read-back)" if partial else ""))
    return CheckRun(None if partial else True, lacks + (not_covered if partial else ""))


async def run_item(item: dict, host_factory, judge, runs: int, keep: bool) -> TurnItemResult:
    res = TurnItemResult(item["id"], item.get("title", ""))
    if item.get("interface") in ("none", "blocked"):
        # Same meaning as in the tool layer: `none` = this Loci has nothing for it,
        # `blocked` = it has, but the harness cannot reach it.
        res.status = "no-interface" if item["interface"] == "none" else "blocked"
        res.reason = str(item.get("needs") or "")
        return res
    probe = host_factory()
    missing = sorted(set(item.get("needs", [])) - probe.capabilities())
    if missing:
        res.status, res.reason = "blocked", f"host cannot: {', '.join(missing)}"
        return res
    res.checks = [CheckSummary(c.get("segment", ""), c.get("desc", ""), bool(c.get("boundary")),
                               rubric=c.get("judge", ""), turn=c.get("turn") or 0)
                  for c in item.get("checks", [])]
    for n in range(1, runs + 1):
        print(f"  … {item['id']} run {n}/{runs}", flush=True)
        try:
            results, transcript, causes = await run_once(item, host_factory(), judge, keep, n)
        except Exception as exc:
            res.status, res.reason = "error", f"run {n}: {type(exc).__name__}: {exc}"
            return res
        for summary, r in zip(res.checks, results):
            summary.runs.append(r)
            if not summary.said and causes:
                idx = (summary.turn or len(causes)) - 1
                summary.said = causes[idx] if 0 <= idx < len(causes) else ""
        res.transcripts.append(transcript)
    return res


def render(results: list[TurnItemResult], host_desc: dict, judge_name: str, runs: int) -> str:
    lines = ["# Loci exam — whole-turn layer", "",
             f"- Loci {loci_version()} · {git_head()} · runs per item: {runs}",
             f"- host: {json.dumps(host_desc, ensure_ascii=False)}",
             f"- judge: {judge_name}",
             f"- test seams: {'; '.join(seams())}", ""]
    for r in results:
        if r.status != "ran":
            lines += [f"## {r.item_id} · {r.status.upper()} · {r.title}", f"- {r.reason}", ""]
            continue
        verdicts = [c.verdict for c in r.checks]
        overall = ("FAIL" if any(v.startswith("fail") for v in verdicts)
                   else "UNDECIDED" if "undecided" in verdicts else "PASS")
        lines.append(f"## {r.item_id} · {overall} · {r.title}")
        for c in r.checks:
            marks = " ".join("✓" if x.ok else "·" if x.ok is None else "✗" for x in c.runs)
            lines.append(f"- {c.verdict} [{marks}] {c.segment} · {c.desc}"
                         + (" (boundary)" if c.boundary else ""))
            for i, x in enumerate(c.runs, 1):
                lines.append(f"    - run {i}: `{x.evidence[:200]}`")
        lines.append("")
    return "\n".join(lines)


def make_host_factory(args):
    from exam.hosts.lento import LentoHost
    kw = {"model": args.model}
    if args.system_prompt:
        kw["system_prompt_file"] = args.system_prompt
    if args.claude_md:
        kw["claude_md_file"] = args.claude_md
    return lambda: LentoHost(**kw)


async def main_async(args) -> int:
    require_search_deps()
    items = []
    for p in args.items:
        data = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
        items.extend(data if isinstance(data, list) else [data])
    factory = make_host_factory(args)
    judge = default_judge()
    results = []
    for item in items:
        print(f"… {item['id']}", flush=True)
        results.append(await run_item(item, factory, judge, args.runs, args.keep))
    meta = {"host": factory().describe(), "judge": judge.name, "runs": args.runs}
    out = OUT_DIR / ("turn-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    save(out, results, meta)
    packet = judge_packet(results)
    if packet:
        (out / "judge-packet.md").write_text(packet, encoding="utf-8")
    print((out / "report.md").read_text(encoding="utf-8"))
    print(f"\nreport: {out / 'report.md'}")
    if packet:
        print(f"unjudged rubrics: {out / 'judge-packet.md'} "
              f"(grade outside, then: python exam/judge_import.py {out} <verdicts file>)")
    return 0


def save(out: Path, results: list[TurnItemResult], meta: dict) -> None:
    (out / "report.md").write_text(render(results, meta["host"], meta["judge"], meta["runs"]),
                                   encoding="utf-8")
    (out / "results.json").write_text(json.dumps([asdict(r) for r in results],
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                   encoding="utf-8")


PACKET_HEAD = """# Grading packet

You are grading replies from a chat assistant against rubrics. For each case below, read
the rubric, the user's line, the tool calls the assistant made and its reply. Judge only
the rubric; style, length and tone do not matter unless the rubric says so.

Answer with one JSON object per line and nothing else, in any order:
{"key": "<the key of the case>", "pass": true or false, "reason": "<one sentence quoting the reply>"}
"""


def judge_packet(results: list[TurnItemResult]) -> str:
    """Every rubric nobody has graded yet, as one self-contained text a person can paste
    into any model (or read themselves). judge_import.py takes the answers back."""
    cases = []
    for r in results:
        for ci, c in enumerate(r.checks):
            if not c.rubric:
                continue
            for ri, run in enumerate(c.runs):
                if run.ok is not None:
                    continue
                turns = r.transcripts[ri] if ri < len(r.transcripts) else []
                t = turns[(c.turn or len(turns)) - 1] if turns else {}
                tools = "\n".join(
                    f"- {tc['name'].split('__')[-1]} {json.dumps(tc['arguments'], ensure_ascii=False)}"
                    f" -> {tc['output'][:400]!r}" for tc in t.get("tool_calls", [])) or "(none)"
                cases.append(
                    f"## key: {r.item_id}#{ci + 1}/run{ri + 1}\n\n"
                    f"**Rubric**\n{c.rubric}\n\n**User said**\n{c.said or '(none)'}\n\n"
                    f"**Tool calls**\n{tools}\n\n**Reply**\n{t.get('reply') or '(empty)'}\n")
    return PACKET_HEAD + "\n" + "\n".join(cases) if cases else ""


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Loci exam, whole-turn layer")
    ap.add_argument("items", nargs="+")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default="claude-opus-4-6",
                    help="the model the life line really runs; baseline and acceptance must match")
    ap.add_argument("--system-prompt", default="")
    ap.add_argument("--claude-md", default="")
    ap.add_argument("--keep", action="store_true")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
