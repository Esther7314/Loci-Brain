"""
========================================
core/_usage.py — the usage log: what Loci handed out, and what was used as a source
========================================

Three things are recorded by code, each as one line of `<buckets>/_usage/usage.jsonl`:

    shown    put in front of the model by itself — breath's blocks
    found    handed back to a lookup — recall's search, browsing, a read by id
    source   used as a source — a write stood on it (grow's from, regrow, fold): the
             one use that refreshes decay (BucketManager.touch)

    {"at": "2026-10-01T12:00:00+00:00", "kind": "shown", "road": "breath.recent",
     "ids": ["3f9c1a2b7d40"], "host": "life-line", "key": "life-line:t-1001#2"}
    {"at": "...", "kind": "found", "road": "recall.search", "ids": ["3f9c1a2b7d40"],
     "query": "看牙", "gates": {"when": "", "room": "", "tag": ""}}

`shown` and `source` lines hold ids, the kind and the road only. A `found` line is one
lookup call: the ids it listed (none, when it found nothing) and, for the owner's review
of how searching goes, the query as the model typed it and the other gates. That text is
the panel's alone: no host-facing route reads this file, and `/api/v2/changes` reads the
ledger, not this. `host` is the calling host, `key` the turn's write key when the host
sent one.

What is recorded is what went out — the ids whose handle is in the text handed back
(`ids_in`), never the candidate set — so under a read scope it is exactly what that
scope let through. It is what Loci gave, not what the model took in: whether a block
really reached the model's input is the host's to record (plan 二·七). Being shown or
found is not use; only `source` is.

The log is not the ledger: the ledger's seq numbers mean "the library changed", and
reading a memory changes nothing. Lines older than `usage.retain_days` (config, default
30) are dropped, at most once an hour per process. A source withdrawn or deleted takes
the query off every lookup that listed what it cleared or typed its words (`scrub`). A
failed write only logs.

A lookup's candidates reach the tool that hands its text out through `offering()` /
`offer()`: the lookup offers what it collected, the tool keeps what its text shows.

Exports: SHOWN · FOUND · SOURCE · KINDS · DEFAULT_RETAIN_DAYS · UsageLog · ids_in ·
         offering · offer
========================================
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from locibrain.eventsourcing.ledger_mirror import file_lease

logger = logging.getLogger("loci_brain.usage")

SHOWN, FOUND, SOURCE = "shown", "found", "source"
KINDS = (SHOWN, FOUND, SOURCE)
USAGE_DIR = "_usage"
USAGE_FILE = "usage.jsonl"
DEFAULT_RETAIN_DAYS = 30
_PRUNE_EVERY_SECONDS = 3600.0
_QUERY_MAX = 400
_GATE_KEYS = ("when", "room", "tag", "view")


def ids_in(candidates: Iterable[str], text: str, short: int = 6) -> list[str]:
    """The candidates whose handle (the first `short` characters, as lines print them)
    is in `text`, in candidate order."""
    out: list[str] = []
    for bid in candidates:
        b = str(bid or "")
        if b and (b in text or b[:short] in text):
            out.append(b)
    return list(dict.fromkeys(out))


_OFFERED: contextvars.ContextVar = contextvars.ContextVar("loci_usage_offered", default=None)


@contextmanager
def offering():
    """Collect what a lookup offers inside the block: yields {"road": str, "ids": list}."""
    got: dict = {"road": "", "ids": []}
    token = _OFFERED.set(got)
    try:
        yield got
    finally:
        _OFFERED.reset(token)


def offer(ids: Iterable[str], road: str) -> None:
    """A lookup's candidates, for the tool collecting them (nothing outside `offering`)."""
    got = _OFFERED.get()
    if got is not None:
        got["road"] = got["road"] or road
        got["ids"].extend(str(i) for i in ids if i)


def _caller() -> tuple[str, str]:
    from . import _sources as _src
    from . import scope as _scope
    req = _scope.current_request()
    host = req.host.name if (req is not None and req.host is not None) else ""
    return host, _src.current_write_key() or ""


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


class UsageLog:
    """The usage log of one library."""

    def __init__(self, base_dir, retain_days: Optional[int] = None):
        self.path = Path(base_dir) / USAGE_DIR / USAGE_FILE
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            days = int(retain_days) if retain_days is not None else DEFAULT_RETAIN_DAYS
        except (TypeError, ValueError):
            days = DEFAULT_RETAIN_DAYS
        self.retain_days = max(1, days)
        self._pruned_at = 0.0

    def record(self, kind: str, ids: Iterable[str], road: str, *,
               query: Optional[str] = None, gates: Optional[dict] = None) -> None:
        """Append one line. `shown` and `source` with no ids write nothing; `found` always
        writes (a search that listed nothing is part of how searching goes). `query` and
        `gates` are taken on `found` lines only."""
        if kind not in KINDS:
            raise ValueError(f"usage kind must be one of {KINDS}")
        ids = [str(i) for i in dict.fromkeys(ids) if i]
        if not ids and kind != FOUND:
            return
        host, key = _caller()
        row: dict = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "kind": kind, "road": str(road), "ids": ids}
        if kind == FOUND:
            if query is not None:
                row["query"] = str(query)[:_QUERY_MAX]
            if gates:
                row["gates"] = {k: str(gates.get(k) or "") for k in _GATE_KEYS if k in gates}
        if host:
            row["host"] = host
        if key:
            row["key"] = key
        data = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lease(self.lock_path, timeout=5.0):
                with self.path.open("ab") as f:
                    f.write(data)
        except (OSError, TimeoutError) as e:
            logger.warning("usage log write failed (%s %s): %s", kind, road, e)
            return
        if time.monotonic() - self._pruned_at > _PRUNE_EVERY_SECONDS:
            self.prune()

    def read(self) -> list[dict]:
        """Every line, oldest first (torn lines skipped)."""
        return _read(self.path)

    def _rewrite(self, keep) -> int:
        """Replace the file with the lines `keep` returns (a line, or None to drop it).
        Returns how many lines changed or went."""
        with file_lease(self.lock_path, timeout=5.0):
            rows = _read(self.path)
            out, changed = [], 0
            for row in rows:
                new = keep(row)
                if new is None or new != row:
                    changed += 1
                if new is not None:
                    out.append(new)
            if not changed:
                return 0
            tmp = self.path.with_name(self.path.name + ".rewrite")
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                for row in out:
                    f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            tmp.replace(self.path)
            return changed

    def prune(self) -> int:
        """Drop lines older than the retention."""
        self._pruned_at = time.monotonic()
        if not self.path.exists():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retain_days)

        def keep(row: dict):
            try:
                at = datetime.fromisoformat(str(row.get("at")))
            except ValueError:
                return row
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return row if at >= cutoff else None
        try:
            return self._rewrite(keep)
        except (OSError, TimeoutError) as e:
            logger.warning("usage log prune failed: %s", e)
            return 0

    def scrub(self, ids: Iterable[str], holds_words=None) -> int:
        """Take the query and gates off every lookup that listed one of `ids` or whose
        query `holds_words` (a predicate on the query). Returns how many lines changed."""
        wanted = {str(i) for i in ids if i}
        if not self.path.exists() or not (wanted or holds_words):
            return 0

        def keep(row: dict):
            if "query" not in row and "gates" not in row:
                return row
            q = str(row.get("query") or "")
            hit = bool(wanted & set(map(str, row.get("ids") or []))) or bool(
                holds_words is not None and holds_words(q))
            if not hit:
                return row
            return {k: v for k, v in row.items() if k not in ("query", "gates")}
        return self._rewrite(keep)
