# -*- coding: utf-8 -*-
"""
exam/runner.py — run exam items against a real Loci server in a throwaway library.

    python exam/runner.py                      # every item under exam/items/
    python exam/runner.py exam/items/b4*.yaml  # some items
    python exam/runner.py --keep               # keep the libraries for a look afterwards

WHAT ONE ITEM DOES
    1. Make an empty temp directory; that is the whole library. No real library is
       ever opened: the server only sees LOCI_BUCKETS_DIR, which points here.
    2. Write the item's `setup` entries straight onto disk (fixed ids, fixed times),
       using Loci's own BucketManager so the files look exactly like real ones.
    3. Start the real MCP server (exam/serve.py) over stdio with the fake clock.
    4. Walk the `steps`: move the clock, call tools, take snapshots, run checks.
    5. Each check records pass / fail and one line of evidence read back from disk or
       from the tool's own output.

WHY THROUGH THE REAL SERVER
    The tool layer is meant to see what a model sees: the same tool names, the same
    strict argument checks, the same text back. Calling Python functions directly would
    test something slightly different, and "passes in the harness, fails for the model"
    is the gap this exam exists to close.

ITEMS THE CURRENT VERSION HAS NO INTERFACE FOR
    An item may declare `interface: none` with a `needs` line. It is not run; the report
    lists it as "no interface in this version" together with what the next version has to
    expose. This keeps the baseline honest: absent is reported as absent, not as a pass
    or a crash.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from exam import clock  # noqa: E402

ITEMS_DIR = ROOT / "exam" / "items"
OUT_DIR = ROOT / "exam" / "out"
SETTLE_SECONDS = 0.4

# The exam config: no model keys (nothing leaves the machine, results do not depend on
# a remote model), embeddings off, no auth on stdio, decay checks far apart so the
# background decay pass never lands in the middle of an item.
EXAM_CONFIG = {
    "transport": "stdio",
    "log_level": "WARNING",
    "mcp_require_auth": False,
    "dehydration": {"api_key": "", "base_url": "", "model": ""},
    "embedding": {"enabled": False},
    "decay": {"check_interval_hours": 100000},
}


# ───────────────────────── results ─────────────────────────

@dataclass
class CheckResult:
    segment: str
    desc: str
    ok: bool
    evidence: str


@dataclass
class ItemResult:
    item_id: str
    title: str
    status: str                     # ran | no-interface | error
    checks: list[CheckResult] = field(default_factory=list)
    needs: str = ""
    error: str = ""
    outputs: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "ran" and all(c.ok for c in self.checks)


# ───────────────────────── library ─────────────────────────

def find_entry_file(lib: Path, bucket_id: str) -> Path | None:
    hits = sorted(lib.rglob(f"*_{bucket_id}.md"))
    return hits[0] if hits else None


def read_entry(lib: Path, bucket_id: str) -> tuple[dict, str] | None:
    path = find_entry_file(lib, bucket_id)
    if path is None:
        return None
    post = frontmatter.load(path)
    meta = dict(post.metadata)
    meta["_path"] = str(path.relative_to(lib))
    return meta, post.content


async def seed(lib: Path, clock_file: Path, entries: list[dict]) -> None:
    """Write setup entries with Loci's own BucketManager, each at its own fake time."""
    from utils import load_config
    from core.bucket_manager import BucketManager

    bm = BucketManager(load_config())
    for e in entries:
        clock.set_now(clock_file, e["at"])
        bid = await bm.create(
            e["text"],
            tags=e.get("tags", []),
            valence=e.get("v", 0.5),
            arousal=e.get("a", 0.3),
            name=e.get("name"),
            from_ids=",".join(e.get("from", [])),
            room=e.get("room", ""),
            when=e.get("when", ""),
            subjects=e.get("subjects"),
            bucket_id_override=e["id"],
            pinned=e.get("pinned", False),
        )
        if bid != e["id"]:
            raise RuntimeError(f"setup wanted id {e['id']}, got {bid}")
        extra = e.get("fields") or {}
        if extra:
            ok = await bm.update(bid, **extra)
            if not ok:
                raise RuntimeError(f"setup could not write fields {extra} on {bid}")


def breath_section(text: str, title: str) -> str:
    """The body of one ═══ title ═══ block of breath's output ('' if the block is absent).
    Checks aim at a block because the same entry can show up in several: a promise that
    has fallen out of the reminders may still appear under "suddenly remembered"."""
    m = re.search(rf"═+ {re.escape(title)}[^\n]*═+\n(.*?)(?=\n═+ |\n📍|\Z)", text, re.S)
    return m.group(1) if m else ""


# ───────────────────────── steps ─────────────────────────

class Run:
    def __init__(self, lib: Path, clock_file: Path, session):
        self.lib = lib
        self.clock_file = clock_file
        self.session = session
        self.vars: dict[str, str] = {}
        self.outputs: dict[str, str] = {}
        self.snapshots: dict[str, dict] = {}
        self.checks: list[CheckResult] = []

    def sub(self, value: Any) -> Any:
        """Replace $name with a captured value, anywhere in strings, lists and dicts."""
        if isinstance(value, str):
            return re.sub(r"\$(\w+)", lambda m: self.vars.get(m.group(1), m.group(0)), value)
        if isinstance(value, list):
            return [self.sub(v) for v in value]
        if isinstance(value, dict):
            return {k: self.sub(v) for k, v in value.items()}
        return value

    async def call(self, step: dict) -> None:
        tool = step["call"]
        args = self.sub(step.get("args", {}))
        try:
            res = await self.session.call_tool(tool, args)
            text = "\n".join(getattr(c, "text", "") for c in res.content)
            if res.isError:
                text = "[tool error] " + text
        except Exception as exc:  # a rejected call is a result, not a crash
            text = f"[call failed] {type(exc).__name__}: {exc}"
        await asyncio.sleep(SETTLE_SECONDS)
        name = step.get("as") or tool
        self.outputs[name] = text
        for var, pattern in (step.get("capture") or {}).items():
            m = re.search(pattern, text)
            if m:
                self.vars[var] = m.group(1)

    def snapshot(self, step: dict) -> None:
        bid = self.sub(step["snapshot"])
        got = read_entry(self.lib, bid)
        self.snapshots[step.get("as") or bid] = dict(got[0]) if got else {}

    def follow_versions(self, step: dict) -> None:
        """$var = the newest version of an entry, walking superseded_by."""
        bid = self.sub(step["newest"])
        seen = set()
        while bid and bid not in seen:
            seen.add(bid)
            got = read_entry(self.lib, bid)
            nxt = got[0].get("superseded_by") if got else None
            if not nxt:
                break
            bid = str(nxt)
        self.vars[step["as"]] = bid

    def check(self, step: dict) -> None:
        c = step["check"]
        segment = c.get("segment", "")
        desc = c.get("desc", "")
        try:
            ok, evidence = self._judge(c)
        except Exception as exc:
            ok, evidence = False, f"check could not run: {type(exc).__name__}: {exc}"
        self.checks.append(CheckResult(segment, desc, ok, evidence))

    def _judge(self, c: dict) -> tuple[bool, str]:
        if "field" in c:
            bid = self.sub(c["id"])
            got = read_entry(self.lib, bid)
            if got is None:
                return False, f"{bid}: no such entry on disk"
            meta, _body = got
            key = c["field"]
            present = key in meta and meta[key] not in (None, "", [])
            val = meta.get(key)
            shown = f"{bid}.{key} = {val!r}" if present else f"{bid}.{key} absent"
            if "equals" in c:
                return present and str(val) == str(self.sub(c["equals"])), shown
            if "contains" in c:
                return present and str(self.sub(c["contains"])) in str(val), shown
            if c.get("absent"):
                return not present, shown
            return present, shown
        if "body_contains" in c or "body_lacks" in c:
            bid = self.sub(c["id"])
            got = read_entry(self.lib, bid)
            if got is None:
                return False, f"{bid}: no such entry on disk"
            body = got[1]
            if "body_contains" in c:
                needle = c["body_contains"]
                return needle in body, f"{bid} body {'has' if needle in body else 'lacks'} {needle!r}"
            needle = c["body_lacks"]
            return needle not in body, f"{bid} body {'has' if needle in body else 'lacks'} {needle!r}"
        if "output" in c:
            text = self.outputs.get(c["output"], "")
            if "section" in c:
                text = breath_section(text, c["section"])
            first = text.strip().splitlines()[0][:160] if text.strip() else "(empty)"
            if "contains" in c:
                needle = self.sub(c["contains"])
                return needle in text, f"{c['output']} {'has' if needle in text else 'lacks'} {needle!r}"
            if "lacks" in c:
                needle = self.sub(c["lacks"])
                if needle not in text:
                    return True, f"{c['output']} lacks {needle!r}"
                where = [t for t in re.findall(r"═+ ([^═\n]+?) ?═+", text)
                         if needle in breath_section(text, t.strip())]
                return False, (f"{c['output']} has {needle!r}"
                               + (f" (in: {', '.join(where)})" if where else ""))
            if c.get("ok"):
                bad = text.startswith("[tool error]") or text.startswith("[call failed]")
                return not bad, first
            if c.get("rejected"):
                bad = text.startswith("[tool error]") or text.startswith("[call failed]")
                return bad, first
        if "var" in c:
            val = self.vars.get(c["var"], "")
            want = str(self.sub(c["equals"]))
            return val == want, f"${c['var']} = {val!r} (want {want!r})"
        if "unchanged" in c:
            bid = self.sub(c["unchanged"])
            before = self.snapshots.get(c["since"], {})
            got = read_entry(self.lib, bid)
            after = got[0] if got else {}
            diffs = [f"{k}: {before.get(k)!r} -> {after.get(k)!r}"
                     for k in c["keys"] if before.get(k) != after.get(k)]
            return not diffs, "; ".join(diffs) if diffs else f"{', '.join(c['keys'])} unchanged"
        raise ValueError(f"unknown check: {sorted(c)}")


async def run_item(item: dict, keep: bool) -> ItemResult:
    res = ItemResult(item["id"], item.get("title", ""), "ran")
    if item.get("interface") == "none":
        res.status, res.needs = "no-interface", item.get("needs", "")
        return res

    lib = Path(tempfile.mkdtemp(prefix=f"loci-exam-{item['id']}-"))
    clock_file = lib.parent / f"{lib.name}.clock"
    config_file = lib.parent / f"{lib.name}.config.yaml"
    config_file.write_text(yaml.safe_dump(EXAM_CONFIG), encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "LOCI_BUCKETS_DIR": str(lib),
        "LOCI_CONFIG_PATH": str(config_file),
        "LOCI_TRANSPORT": "stdio",
        "LOCI_MCP_REQUIRE_AUTH": "false",
        "LOCI_LOG_DIR": str(lib / ".logs"),
        "EXAM_CLOCK_FILE": str(clock_file),
        "EXAM_SEED": str(item.get("seed", 0)),
        "PYTHONIOENCODING": "utf-8",
    })
    for k in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LOCI_DEHYDRATION_API_KEY",
              "LOCI_EMBEDDING_API_KEY", "GEMINI_API_KEY"):
        env.pop(k, None)
    saved_env = {k: os.environ.get(k) for k in ("LOCI_BUCKETS_DIR", "LOCI_CONFIG_PATH")}
    try:
        os.environ["LOCI_BUCKETS_DIR"] = str(lib)
        os.environ["LOCI_CONFIG_PATH"] = str(config_file)
        clock.set_now(clock_file, item["start"])
        clock.install(clock_file)
        await seed(lib, clock_file, item.get("setup", []))
        clock.set_now(clock_file, item["start"])

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT / "exam" / "serve.py")],
            env=env,
            cwd=str(lib),
        )
        errlog = open(lib.parent / f"{lib.name}.server.log", "w", encoding="utf-8")
        try:
            async with stdio_client(params, errlog=errlog) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    run = Run(lib, clock_file, session)
                    for step in item.get("steps", []):
                        if "at" in step:
                            clock.set_now(clock_file, step["at"])
                        elif "call" in step:
                            await run.call(step)
                        elif "snapshot" in step:
                            run.snapshot(step)
                        elif "newest" in step:
                            run.follow_versions(step)
                        elif "check" in step:
                            run.check(step)
                        else:
                            raise ValueError(f"unknown step: {sorted(step)}")
                    res.checks = run.checks
                    res.outputs = run.outputs
        finally:
            errlog.close()
    except Exception as exc:
        res.status, res.error = "error", f"{type(exc).__name__}: {exc}"
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if keep:
            res.error = (res.error + " " if res.error else "") + f"[library kept at {lib}]"
        else:
            shutil.rmtree(lib, ignore_errors=True)
            for suffix in (".clock", ".config.yaml", ".server.log"):
                (lib.parent / f"{lib.name}{suffix}").unlink(missing_ok=True)
    return res


# ───────────────────────── report ─────────────────────────

def render(results: list[ItemResult]) -> str:
    lines = ["# Loci exam — tool layer", ""]
    ran = [r for r in results if r.status == "ran"]
    lines.append(f"items: {len(results)} · ran: {len(ran)} · passed: "
                 f"{sum(r.passed for r in ran)} · no interface: "
                 f"{sum(r.status == 'no-interface' for r in results)} · errors: "
                 f"{sum(r.status == 'error' for r in results)}")
    lines.append("")
    for r in results:
        mark = {"ran": "PASS" if r.passed else "FAIL",
                "no-interface": "NO INTERFACE", "error": "ERROR"}[r.status]
        lines.append(f"## {r.item_id} · {mark} · {r.title}")
        if r.needs:
            lines.append(f"- needs: {r.needs}")
        if r.error:
            lines.append(f"- {r.error}")
        for c in r.checks:
            lines.append(f"- [{'x' if c.ok else ' '}] {c.segment} · {c.desc} — `{c.evidence}`")
        lines.append("")
    return "\n".join(lines)


def load_items(patterns: list[str]) -> list[dict]:
    paths: list[Path] = []
    if patterns:
        for p in patterns:
            paths.extend(sorted(Path().glob(p)) or [Path(p)])
    else:
        paths = sorted(ITEMS_DIR.glob("*.yaml"))
    items = []
    for p in paths:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        items.extend(data if isinstance(data, list) else [data])
    return items


async def main_async(args) -> int:
    results = []
    for item in load_items(args.items):
        print(f"… {item['id']}", flush=True)
        results.append(await run_item(item, args.keep))
    report = render(results)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUT_DIR / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "results.json").write_text(json.dumps(
        [{**r.__dict__, "checks": [c.__dict__ for c in r.checks]} for r in results],
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)
    print(f"\nreport: {out / 'report.md'}")
    return 0


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("items", nargs="*", help="item files (default: exam/items/*.yaml)")
    ap.add_argument("--keep", action="store_true", help="keep each temp library")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
