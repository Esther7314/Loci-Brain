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
       using Loci's own BucketManager so the files look exactly like real ones. An
       item's `slices:` batch (a day of a host's raw lines) is then handed to Loci's
       own intake, with the slicing written in the item standing in for the side model.
    3. Start the real MCP server (exam/serve.py) over stdio with the fake clock. An item's
       `side_model:` ({phrase in the body: answer}) answers for the backfill's side model.
    4. Walk the `steps`: move the clock, call tools (a call may carry what a host puts on
       its request: `host:` its credential, `scope:` its Loci-Scope, `turn:` / `write_key:`
       its Loci-Turn; an item's `hosts:` is the deployment's hosts table), wait
       (`wait: seconds`, for background work such as the backfill), take snapshots, run
       checks. Host routes that are not tools run in this process against the same
       library, as a second entry point does: `source_change:` (what POST
       /api/v2/source/change runs), `changes:` (GET /api/v2/changes), and the strong
       reminder's `cue:` / `cue_delivered:` / `cue_dropped:` (POST /api/v2/cue and its
       two acknowledgements). `concurrent:` runs tool calls at once with writer
       processes beside them. An item's `names:` is written as the names table.
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
from contextlib import asynccontextmanager
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
# a remote model), no auth on stdio, decay checks far apart so the background decay
# pass never lands in the middle of an item.
EXAM_CONFIG = {
    "transport": "stdio",
    "log_level": "WARNING",
    "mcp_require_auth": False,
    "dehydration": {"api_key": "", "base_url": "", "model": ""},
    "embedding": {"enabled": False},
    "decay": {"check_interval_hours": 100000},
}

# Embeddings are off unless EXAM_EMBED_URL names a local Ollama (…/v1). Real search
# weighs vectors 2.5 against BM25's 1.5; with vectors off the best BM25 hit scores 37.5
# against recall's line of 35, so little more than the top hit survives. A baseline
# meant to match a library that has vectors has to run with them.
EMBED_URL_ENV = "EXAM_EMBED_URL"
EMBED_MODEL_ENV = "EXAM_EMBED_MODEL"


def exam_config() -> dict:
    cfg = json.loads(json.dumps(EXAM_CONFIG))
    url = os.environ.get(EMBED_URL_ENV, "").strip()
    if url:
        cfg["embedding"] = {
            "enabled": True,
            "api_format": "ollama",
            "base_url": url,
            "model": os.environ.get(EMBED_MODEL_ENV, "").strip() or "bge-m3",
            "timeout_seconds": 60,
        }
    return cfg


def hosts_config(hosts: dict) -> tuple[dict, dict]:
    """An item's `hosts:` ({name: {token, scope_mode?, max_grant?, may_restore?}}) as the
    deployment writes it: the config names an environment variable per host, and the
    server's environment holds the credential."""
    table, env = {}, {}
    for name, spec in hosts.items():
        var = "EXAM_HOST_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", str(name)).upper()
        env[var] = str(spec["token"])
        table[name] = {"token_env": var, **{k: v for k, v in spec.items() if k != "token"}}
    return table, env


def embeddings_label() -> str:
    cfg = exam_config()["embedding"]
    if not cfg.get("enabled"):
        return "embeddings off"
    return f"embeddings {cfg['model']} via local Ollama {cfg['base_url']}"


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
    status: str                     # ran | no-interface | blocked | error
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


async def seed(lib: Path, clock_file: Path, entries: list[dict],
               batch: dict | None = None, extra: dict | None = None) -> None:
    """Write setup entries with Loci's own BucketManager, each at its own fake time (an
    entry with `sink: true` is sunk after its fields are written, its original going to
    archive/原文); then hand `batch` (an item's `slices:`) to the slicing intake. `extra`
    holds the item's `dreams:` ([{id, text, ingredients}], saved as woven dream records)
    and `dehydration_cache:` ([{text, summary}], cached under the text by Loci's own
    dehydrator)."""
    from utils import WAS_DERIVED_FROM, load_config
    from core.bucket_manager import BucketManager
    from core.embedding_engine import EmbeddingEngine

    config = load_config()
    engine = EmbeddingEngine(config)
    if config.get("embedding", {}).get("enabled") and not engine.enabled:
        raise RuntimeError(f"embeddings asked for ({os.environ.get(EMBED_URL_ENV)}) "
                           "but the engine did not start")
    # No outbox is attached, so each create() embeds inline before returning: every
    # setup entry has its vector before the server starts.
    bm = BucketManager(config, embedding_engine=engine)
    for e in entries:
        clock.set_now(clock_file, e["at"])
        bid = await bm.create(
            e["text"],
            tags=e.get("tags", []),
            valence=e.get("v", 0.5),
            arousal=e.get("a", 0.3),
            name=e.get("name"),
            prov=[{"rel": WAS_DERIVED_FROM, "target": i} for i in e.get("from", [])],
            room=e.get("room", ""),
            when=e.get("when", ""),
            subjects=e.get("subjects"),
            bucket_id_override=e["id"],
            pinned=e.get("pinned", False),
            sources=e.get("sources"),
        )
        if bid != e["id"]:
            raise RuntimeError(f"setup wanted id {e['id']}, got {bid}")
        fields = e.get("fields") or {}
        if fields:
            ok = await bm.update(bid, **fields)
            if not ok:
                raise RuntimeError(f"setup could not write fields {fields} on {bid}")
        if e.get("sink") and not await bm.sink_bucket(bid):
            raise RuntimeError(f"setup could not sink {bid} (it needs a summary)")
    if batch:
        await seed_slices(bm, batch)
    extra = extra or {}
    if extra.get("dreams"):
        from core import _dream
        for d in extra["dreams"]:
            _dream.save_record({"id": d["id"], "织于": d.get("at", ""), "起算点": d.get("at", ""),
                                "回想次数": 0, "轮次": 0, "碎片": d["text"][:20],
                                "完整": d["text"], "完整字数": len(d["text"]),
                                "v": 0.5, "a": 0.3, "nightmare": False,
                                "素材": {"压在心头": list(d.get("ingredients") or []),
                                       "想不明白": [], "几个词": []}},
                               str(lib))
    if extra.get("dehydration_cache"):
        from core.dehydrator import Dehydrator
        dh = Dehydrator(config)
        try:
            for row in extra["dehydration_cache"]:
                dh._set_cached_summary(row["text"], row["summary"])
        finally:
            dh.close()


async def seed_slices(bm, batch: dict) -> None:
    """The host's intake of raw lines (core/_slicer.take_batch, what POST /api/v2/slices
    runs), with the item's `cut` answering for the side model: the exam has no model key,
    and what is under test is what the main model does with the slices. The guesses are
    Loci's own (embeddings, when on)."""
    from core import _slicer

    cut = json.dumps({"slices": batch["cut"]}, ensure_ascii=False)

    async def side_model(system: str, user: str) -> str:
        return cut
    body = {k: v for k, v in batch.items() if k != "cut"}
    await _slicer.take_batch(bm, body, model=side_model)


def breath_section(text: str, title: str) -> str:
    """The body of one ═══ title ═══ block of breath's output ('' if the block is absent).
    Checks aim at a block because the same entry can show up in several: a promise that
    has fallen out of the reminders may still appear under "suddenly remembered"."""
    m = re.search(rf"═+ {re.escape(title)}[^\n]*═+\n(.*?)(?=\n═+ |\n📍|\Z)", text, re.S)
    return m.group(1) if m else ""


# ───────────────────────── steps ─────────────────────────

# One writer process of a `concurrent:` step: another entry point creating entries
# through Loci's own BucketManager on the same library.
_WRITER = """
import asyncio, sys
sys.path.insert(0, {src!r})
from utils import load_config
from core.bucket_manager import BucketManager

async def main():
    bm = BucketManager(load_config())
    for i in range({each}):
        await bm.create("{text}（进程 {k} 第 %d 条）" % i, room="EVENT/WORLD")

asyncio.run(main())
"""

class Run:
    def __init__(self, lib: Path, clock_file: Path, session, hosts: dict | None = None,
                 server_env: dict | None = None):
        self.lib = lib
        self.clock_file = clock_file
        self.session = session
        self.hosts = hosts or {}
        self.server_env = server_env or {}
        self.vars: dict[str, str] = {}
        self.outputs: dict[str, str] = {}
        self.snapshots: dict[str, dict] = {}
        self.checks: list[CheckResult] = []
        self.setup_ids: set[str] = set()    # the item's own fixtures, for `new: true`

    def sub(self, value: Any) -> Any:
        """Replace $name with a captured value, anywhere in strings, lists and dicts;
        ${name:6} is its first six characters (the handle tools print)."""
        if isinstance(value, str):
            value = re.sub(r"\$\{(\w+):(\d+)\}",
                           lambda m: (self.vars[m.group(1)][:int(m.group(2))]
                                      if m.group(1) in self.vars else m.group(0)), value)
            return re.sub(r"\$(\w+)", lambda m: self.vars.get(m.group(1), m.group(0)), value)
        if isinstance(value, list):
            return [self.sub(v) for v in value]
        if isinstance(value, dict):
            return {k: self.sub(v) for k, v in value.items()}
        return value

    def headers(self, step: dict) -> dict:
        """What a host would put on this call's HTTP request: `host:` its credential
        (x-loci-hook-token), `scope:` its Loci-Scope (an object, sent as JSON; a string is
        sent as it is, for a malformed one), `turn:` or `write_key:` its Loci-Turn."""
        out = {}
        if step.get("host"):
            out["x-loci-hook-token"] = str(self.sub(step["host"]))
        if "scope" in step:
            scope = self.sub(step["scope"])
            out["Loci-Scope"] = (scope if isinstance(scope, str)
                                 else json.dumps(scope, ensure_ascii=False))
        turn = step.get("turn") or step.get("write_key")
        if turn:
            out["Loci-Turn"] = str(self.sub(turn))
        return out

    async def call(self, step: dict) -> None:
        tool = step["call"]
        args = self.sub(step.get("args", {}))
        # The headers ride in _meta; exam/serve.py makes them the request the call carries.
        headers = self.headers(step)
        meta = {"loci_headers": headers} if headers else None
        try:
            res = await self.session.call_tool(tool, args, meta=meta)
            text = "\n".join(getattr(c, "text", "") for c in res.content)
            if res.isError:
                text = "[tool error] " + text
        except Exception as exc:  # a rejected call is a result, not a crash
            text = f"[call failed] {type(exc).__name__}: {exc}"
        await asyncio.sleep(SETTLE_SECONDS)
        self._keep(step, step.get("as") or tool, text)

    def _keep(self, step: dict, name: str, text: str) -> None:
        self.outputs[name] = text
        for var, pattern in (step.get("capture") or {}).items():
            m = re.search(pattern, text)
            if m:
                self.vars[var] = m.group(1)

    def _host(self, token: str):
        """The host a credential names, read from the item's hosts table the way the
        server reads it (core/scope.load_hosts); the legacy open host when the item has
        no table."""
        from core import scope as _scope
        table, env = hosts_config(self.hosts) if self.hosts else ({}, {})
        hs = _scope.load_hosts({"hosts": table} if table else {}, env, legacy_token=token)
        return hs.by_token(token) if token else hs.default

    def _store(self):
        from utils import load_config
        from core.bucket_manager import BucketManager
        return BucketManager(load_config())

    async def source_change(self, step: dict) -> None:
        """What POST /api/v2/source/change runs (core/_source_change.handle), in this
        process, on the same library: the reply as JSON text under `as`."""
        from utils import load_config
        from core import _source_change
        from core.dehydrator import Dehydrator
        body = self.sub(step["source_change"])
        host = self._host(str(self.sub(step.get("host") or "")))
        # The route hands the server's dehydrator over (its cache keys); this is the same.
        dehydrator = Dehydrator(load_config())
        try:
            status, out = await _source_change.handle(self._store(), body, host,
                                                      dehydrator=dehydrator)
        finally:
            dehydrator.close()
        self._keep(step, step.get("as") or "source_change",
                   json.dumps({"http": status, **out}, ensure_ascii=False))
        await asyncio.sleep(SETTLE_SECONDS + 1.0)   # the server notices changed files

    def _request(self, step: dict):
        """The request a host's call carries, resolved the way the hook guard resolves it
        (core/scope.RequestScope): `host:` its credential, `scope:` its Loci-Scope."""
        from core import scope as _scope
        host = self._host(str(self.sub(step.get("host") or "")))
        header = None
        if "scope" in step:
            scope = self.sub(step["scope"])
            header = scope if isinstance(scope, str) else json.dumps(scope, ensure_ascii=False)
        return _scope.RequestScope.resolve(host, header)

    async def cue(self, step: dict) -> None:
        """What POST /api/v2/cue and its two acknowledgements run (core/_cue.handle_cue /
        handle_delivered / handle_dropped), in this process, on the same library: the reply
        as JSON text under `as`."""
        from core import _cue
        kind = next(k for k in ("cue", "cue_delivered", "cue_dropped") if k in step)
        handler = {"cue": _cue.handle_cue, "cue_delivered": _cue.handle_delivered,
                   "cue_dropped": _cue.handle_dropped}[kind]
        status, out = await handler(self._store(), self.sub(step[kind]), self._request(step))
        self._keep(step, step.get("as") or kind,
                   json.dumps({"http": status, **out}, ensure_ascii=False))

    async def changes(self, step: dict) -> None:
        """What GET /api/v2/changes runs (core/_ledger.changes_since): JSON text."""
        from core import _ledger
        spec = self.sub(step["changes"]) or {}
        host = self._host(str(self.sub(step.get("host") or "")))
        out = await _ledger.changes_since(self._store(), host, int(spec.get("since", 0)),
                                          int(spec.get("limit", _ledger.CHANGES_LIMIT)))
        self._keep(step, step.get("as") or "changes", json.dumps(out, ensure_ascii=False))

    async def concurrent(self, step: dict) -> None:
        """Several windows writing at once: the `calls` go to the server together, and
        `processes` writer processes each create `each` entries straight through Loci's
        BucketManager on the same library at the same moment."""
        spec = step["concurrent"]
        procs = []
        n, each = int(spec.get("processes", 0)), int(spec.get("each", 1))
        for k in range(n):
            code = _WRITER.format(src=str(ROOT / "src"), each=each, k=k,
                                  text=str(spec.get("text", "并发写入")))
            procs.append(await asyncio.create_subprocess_exec(
                sys.executable, "-c", code, env=self.server_env))
        await asyncio.gather(*(self.call(c) for c in spec.get("calls") or []))
        for p in procs:
            if await p.wait() != 0:
                raise RuntimeError("a writer process failed")

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
            if "lacks" in c:
                return str(self.sub(c["lacks"])) not in str(val), shown
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
            if "lines_with" in c:
                # Every line naming `lines_with` also says `each_has` (and there is at least
                # one): an entry that may show up only in one kind of line, such as a held
                # promise that appears only inside the question about its hold.
                needle, want = self.sub(c["lines_with"]), self.sub(c["each_has"])
                hits = [ln for ln in text.splitlines() if needle in ln]
                bad = [ln for ln in hits if want not in ln]
                if not hits:
                    return False, f"{c['output']}: no line has {needle!r}"
                if bad:
                    return False, f"{c['output']}: a line with {needle!r} lacks {want!r}: {bad[0][:120]}"
                return True, f"{c['output']}: {len(hits)} line(s) with {needle!r}, each has {want!r}"
            if "in_order" in c:
                # Each needle appears, the first of each after the first of the one before.
                needles = [self.sub(n) for n in c["in_order"]]
                at = [text.find(n) for n in needles]
                ok = all(a >= 0 for a in at) and at == sorted(at)
                return ok, f"{c['output']}: " + ", ".join(f"{n}@{a}" for n, a in zip(needles, at))
            if c.get("ok"):
                bad = text.startswith("[tool error]") or text.startswith("[call failed]")
                return not bad, first
            if c.get("rejected"):
                bad = text.startswith("[tool error]") or text.startswith("[call failed]")
                return bad, first
        if "usage" in c:
            # The usage log (core/_usage.py): a line of this kind naming the id (and, when
            # given, on a road starting with `road`).
            spec = self.sub(c["usage"])
            from core._usage import UsageLog
            rows = UsageLog(self.lib).read()
            hits = [r for r in rows if r.get("kind") == spec["kind"]
                    and spec["id"] in (r.get("ids") or [])
                    and str(r.get("road", "")).startswith(spec.get("road", ""))]
            return bool(hits), (f"{len(hits)} {spec['kind']} line(s) for {spec['id']}: "
                                + "; ".join(r.get("road", "") for r in hits[:4]))
        if "lib_lacks" in c:
            # Grep-level: no file under the library holds the text (the server's own log
            # directory aside: logs are not a store).
            needle = self.sub(c["lib_lacks"]).encode("utf-8")
            hits = []
            for p in self.lib.rglob("*"):
                if p.is_file() and ".logs" not in p.relative_to(self.lib).parts:
                    if needle in p.read_bytes():
                        hits.append(str(p.relative_to(self.lib)))
            return not hits, ("no file holds it" if not hits else "held by: " + ", ".join(hits))
        if "vector" in c:
            bid = self.sub(c["vector"])
            db = self.lib / "embeddings.db"
            have = False
            if db.exists():
                import sqlite3
                with sqlite3.connect(db) as conn:
                    try:
                        have = conn.execute("SELECT 1 FROM embeddings WHERE bucket_id = ?",
                                            (bid,)).fetchone() is not None
                    except sqlite3.OperationalError:
                        have = False
            want = not c.get("absent")
            return have == want, f"{bid} vector {'present' if have else 'absent'}"
        if "ledger" in c:
            # The ledger itself: its numbers distinct and gapless, and how many lines of a type.
            spec = c["ledger"]
            events = []
            path = self.lib / "_ledger" / "events.jsonl"
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        events.append(json.loads(line))
            seqs = [int(e.get("seq") or 0) for e in events]
            notes = [f"{len(events)} lines, seq {min(seqs, default=0)}..{max(seqs, default=0)}"]
            ok = True
            if spec.get("unique_seq"):
                dup = len(seqs) - len(set(seqs))
                gap = sorted(seqs) != list(range(1, len(seqs) + 1))
                ok = ok and not dup and not gap
                notes.append(f"{dup} repeated, {'gaps' if gap else 'no gaps'}")
            for etype, want in (spec.get("type_count") or {}).items():
                got = sum(1 for e in events if e.get("event_type") == etype)
                ok = ok and got == int(want)
                notes.append(f"{etype} {got} (want {want})")
            return ok, "; ".join(notes)
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
        if "entry" in c or "no_entry" in c:
            return self._entries(c)
        if "any_of" in c or "all_of" in c:
            parts = [self._judge(p) for p in (c.get("any_of") or c.get("all_of"))]
            ok = any(p[0] for p in parts) if "any_of" in c else all(p[0] for p in parts)
            return ok, " | ".join(("✓ " if p[0] else "✗ ") + p[1] for p in parts)
        raise ValueError(f"unknown check: {sorted(c)}")

    def _entries(self, c: dict) -> tuple[bool, str]:
        """Entries found by what they are, not by id: the ones the model wrote itself.

        entry: {spec}      passes when at least `min` (default 1) and at most `max` live
                           entries match; `as: var` keeps the first match's id
        no_entry: {spec}   passes when none match

        spec keys, all optional, all must hold:
          room         room starts with this ("MIND" matches MIND/TRAITS)
          subjects_has one subject contains this
          body_has     body (name included) contains each of these
          body_any     body contains at least one of these
          body_lacks   body contains none of these
          from_has     its sources (read_from_ids) include each of these ids
          from_room    one of those sources has a room starting with this
          fields       frontmatter values, compared as text ("" = absent)
          new          true = not one of the item's setup entries
        Superseded versions are skipped unless `versions: all`."""
        from core.bucket_manager import read_from_ids

        spec = self.sub(c.get("entry") or c.get("no_entry"))
        as_list = lambda v: v if isinstance(v, list) else [v]  # noqa: E731
        rooms: dict[str, str] = {}
        rows = []
        for path in self.lib.rglob("*.md"):
            m = re.search(r"_([0-9a-f]{12})\.md$", path.name)
            if not m:
                continue
            post = frontmatter.load(path)
            meta, bid = dict(post.metadata), m.group(1)
            rooms[bid] = str(meta.get("room") or "")
            rows.append((bid, meta, f"{meta.get('name') or ''}\n{post.content}"))

        def match(bid: str, meta: dict, body: str) -> bool:
            if meta.get("superseded_by") and spec.get("versions") != "all":
                return False
            if "room" in spec and not rooms[bid].startswith(spec["room"]):
                return False
            if "subjects_has" in spec and not any(
                    spec["subjects_has"] in str(s) for s in meta.get("subjects") or []):
                return False
            if any(n not in body for n in as_list(spec.get("body_has", []))):
                return False
            if "body_any" in spec and not any(n in body for n in as_list(spec["body_any"])):
                return False
            if any(n in body for n in as_list(spec.get("body_lacks", []))):
                return False
            froms = set(read_from_ids(meta))
            if any(i not in froms for i in as_list(spec.get("from_has", []))):
                return False
            if "from_room" in spec and not any(
                    rooms.get(i, "").startswith(spec["from_room"]) for i in froms):
                return False
            for k, want in (spec.get("fields") or {}).items():
                if str(meta.get(k) or "") != str(want):
                    return False
            if spec.get("new") and bid in self.setup_ids:
                return False
            return True

        hits = [bid for bid, meta, body in rows if match(bid, meta, body)]
        shown = ", ".join(f"{h} ({rooms[h]})" for h in hits[:5]) or "none"
        if "no_entry" in c:
            return not hits, f"matching entries: {shown}"
        if hits and c.get("as"):
            self.vars[c["as"]] = hits[0]
        lo, hi = int(c.get("min", 1)), c.get("max")
        ok = len(hits) >= lo and (hi is None or len(hits) <= int(hi))
        return ok, f"{len(hits)} matching: {shown}"


@dataclass
class Library:
    """One throwaway library, seeded and ready. `launch` starts Loci's MCP server on it."""
    lib: Path
    clock_file: Path
    server_env: dict

    @property
    def command(self) -> str:
        return sys.executable

    @property
    def args(self) -> list[str]:
        return [str(ROOT / "exam" / "serve.py")]

    def set_clock(self, iso: str) -> None:
        clock.set_now(self.clock_file, iso)


@asynccontextmanager
async def library(item: dict, keep: bool, tag: str = ""):
    """Build the item's library (setup entries at their own fake times), leave the clock
    at `start`, yield it, then delete it unless `keep`."""
    lib = Path(tempfile.mkdtemp(prefix=f"loci-exam-{item['id']}{tag}-"))
    clock_file = lib.parent / f"{lib.name}.clock"
    config_file = lib.parent / f"{lib.name}.config.yaml"
    config, host_env = exam_config(), {}
    if item.get("hosts"):
        config["hosts"], host_env = hosts_config(item["hosts"])
    config_file.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    side_model_file = lib.parent / f"{lib.name}.side_model.json"
    env = dict(os.environ)
    env.update(host_env)
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
    if item.get("side_model"):
        side_model_file.write_text(json.dumps(item["side_model"], ensure_ascii=False),
                                   encoding="utf-8")
        env["EXAM_SIDE_MODEL_FILE"] = str(side_model_file)
    for k in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LOCI_DEHYDRATION_API_KEY",
              "LOCI_EMBEDDING_API_KEY", "GEMINI_API_KEY"):
        env.pop(k, None)
    saved_env = {k: os.environ.get(k) for k in ("LOCI_BUCKETS_DIR", "LOCI_CONFIG_PATH")}
    if item.get("names"):
        # The names table, where Loci looks for it in the data volume.
        (lib / "aliases.yaml").write_text(
            yaml.safe_dump(item["names"], allow_unicode=True, sort_keys=False), encoding="utf-8")
    env.pop("LOCI_ALIAS_TABLE", None)
    saved_env["LOCI_ALIAS_TABLE"] = os.environ.pop("LOCI_ALIAS_TABLE", None)
    try:
        os.environ["LOCI_BUCKETS_DIR"] = str(lib)
        os.environ["LOCI_CONFIG_PATH"] = str(config_file)
        clock.set_now(clock_file, item["start"])
        clock.install(clock_file)
        await seed(lib, clock_file, item.get("setup", []), item.get("slices"),
                   {k: item.get(k) for k in ("dreams", "dehydration_cache")})
        clock.set_now(clock_file, item["start"])
        yield Library(lib, clock_file, env)
    finally:
        clock.uninstall()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if not keep:
            shutil.rmtree(lib, ignore_errors=True)
            for suffix in (".clock", ".config.yaml", ".server.log", ".side_model.json"):
                (lib.parent / f"{lib.name}{suffix}").unlink(missing_ok=True)


async def run_item(item: dict, keep: bool) -> ItemResult:
    res = ItemResult(item["id"], item.get("title", ""), "ran")
    if item.get("interface") in ("none", "blocked"):
        res.status = "no-interface" if item["interface"] == "none" else "blocked"
        res.needs = item.get("needs", "")
        return res

    try:
        async with library(item, keep) as L:
            if keep:
                res.error = f"[library kept at {L.lib}]"
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(command=L.command, args=L.args,
                                           env=L.server_env, cwd=str(L.lib))
            errlog = open(L.lib.parent / f"{L.lib.name}.server.log", "w", encoding="utf-8")
            try:
                async with stdio_client(params, errlog=errlog) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        run = Run(L.lib, L.clock_file, session, item.get("hosts"),
                                  L.server_env)
                        run.setup_ids = {e["id"] for e in item.get("setup", [])}
                        for step in item.get("steps", []):
                            if "at" in step:
                                L.set_clock(step["at"])
                            elif "call" in step:
                                await run.call(step)
                            elif "source_change" in step:
                                await run.source_change(step)
                            elif "changes" in step:
                                await run.changes(step)
                            elif {"cue", "cue_delivered", "cue_dropped"} & set(step):
                                await run.cue(step)
                            elif "concurrent" in step:
                                await run.concurrent(step)
                            elif "wait" in step:
                                await asyncio.sleep(float(step["wait"]))
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
        res.status = "error"
        res.error = (res.error + " " if res.error else "") + f"{type(exc).__name__}: {exc}"
    return res


# ───────────────────────── report ─────────────────────────

# The only test seams this runner adds to the version under test. A baseline has to say
# them out loud: anything more than this and it is no longer the old version's score.
def seams() -> tuple[str, ...]:
    return ("fake clock (exam/clock.py: core._when.now, utils.now_iso and its by-name "
            "imports, utils.datetime (utc_now), datetime.now in bucket_manager)",
            "random seeded per item", "no model keys",
            embeddings_label(),
            "first BM25 build before the first search (exam/serve.py; live Loci builds "
            "it in the background)",
            "stdio, not HTTP: a call step's host credential, Loci-Scope and Loci-Turn ride in "
            "_meta and become the request the call carries, its host named by the "
            "middleware's own identify_host (exam/serve.py); the HTTP transport and "
            "MCPAuthMiddleware are not run here",
            "an item's slices: batch goes through Loci's intake in setup, its cut standing in "
            "for the side model (exam/runner.py seed_slices)",
            "an item's side_model: answers the backfill's side-model call, picked by a phrase "
            "in the body (exam/serve.py; no item without one is affected)",
            "source_change: and changes: steps run what POST /api/v2/source/change and GET "
            "/api/v2/changes run (core/_source_change.handle, core/_ledger.changes_since) in "
            "the runner's process on the same library, a second entry point beside the "
            "server; the HTTP layer and the hook guard are tests/test_source_change.py's",
            "cue:, cue_delivered: and cue_dropped: steps run what POST /api/v2/cue and its two "
            "acknowledgements run (core/_cue.handle_*) the same way, the host credential and "
            "Loci-Scope resolved as the hook guard resolves them; the HTTP layer is "
            "tests/test_cue.py's",
            "an item's names: is written to the library as aliases.yaml before setup",
            "an item's dreams: and dehydration_cache: are written in setup through Loci's own "
            "_dream.save_record and Dehydrator cache")


def require_search_deps() -> None:
    """Without rank_bm25 / jieba, Loci drops BM25 without a word and, with embeddings
    off, search only matches the whole query as a substring. Stop before scoring that."""
    try:
        import jieba  # noqa: F401
        import rank_bm25  # noqa: F401
    except ImportError as exc:
        sys.exit(f"exam: {exc.name} is missing in {sys.executable} — "
                 "pip install rank-bm25 jieba (see requirements.txt)")


def loci_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def git_head() -> str:
    try:
        import subprocess
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "?"


def render(results: list[ItemResult]) -> str:
    lines = ["# Loci exam — tool layer", "",
             f"- Loci {loci_version()} · {git_head()} · LOCI_TZ="
             f"{os.environ.get('LOCI_TZ') or 'Asia/Shanghai (default)'}",
             f"- test seams: {'; '.join(seams())}", ""]
    ran = [r for r in results if r.status == "ran"]
    lines.append(f"items: {len(results)} · ran: {len(ran)} · passed: "
                 f"{sum(r.passed for r in ran)} · no interface: "
                 f"{sum(r.status == 'no-interface' for r in results)} · blocked: "
                 f"{sum(r.status == 'blocked' for r in results)} · errors: "
                 f"{sum(r.status == 'error' for r in results)}")
    lines.append("")
    for r in results:
        mark = {"ran": "PASS" if r.passed else "FAIL", "no-interface": "NO INTERFACE",
                "blocked": "BLOCKED", "error": "ERROR"}[r.status]
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
    require_search_deps()
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
