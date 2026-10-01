"""The append-only ledger of memory mutations (`<buckets>/_ledger/events.jsonl`).

One JSON line per event, numbered by `seq`. Several processes write the same file (the
HTTP server, stdio MCP processes, the panel, maintenance scripts), so every append runs
under one lease shared by all of them: `<ledger>.lock`, an OS lock on its first byte
(msvcrt on Windows, flock elsewhere), held only for the append; the kernel drops it when a
process dies.

Under the lease the next number is one past the highest seq among the whole lines in the
last 64 KB of the file — the newest lines, whoever wrote them — and never below the
highest seq in the whole file, which each writer reads once, on its first append (older
lines out of order cannot pull a number back). A write opens the ledger once and reads its
tail, whatever the ledger's size.

`rewrite()` replaces the file whole under the same lease (write a temporary file, then
rename it over the ledger): a reader sees the old file or the new one, never half of
either, and seq numbers and line order are kept.

Inside one process a thread lock per path is taken first, because an flock is per open
file and a POSIX record lock would not exclude threads of the same process.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, Optional


LEDGER_SCHEMA_VERSION = 1
LEDGER_ROLE = "mirror"
LOCK_TIMEOUT_SECONDS = 30.0

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _THREAD_LOCKS[key] = threading.Lock()
        return lock


@contextmanager
def file_lease(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Hold an exclusive lease on `lock_path` across threads and processes. Raises
    TimeoutError when it cannot be had within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    tlock = _thread_lock(lock_path)
    if not tlock.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"timed out waiting for {lock_path}")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # A byte-range lock on Windows needs the byte to exist.
            if os.fstat(fd).st_size == 0:
                try:
                    os.write(fd, b"\0")
                except OSError:
                    pass
            while True:
                try:
                    if os.name == "nt":  # pragma: no branch - platform-specific
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - exercised in the container
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {lock_path}") from None
                    time.sleep(0.002)
            try:
                yield
            finally:
                try:
                    if os.name == "nt":  # pragma: no branch - platform-specific
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover - exercised in the container
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)
    finally:
        tlock.release()


class LedgerMirror:
    """Append-only JSONL ledger beside the Markdown files (not their source of truth)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._floor: Optional[int] = None       # the whole file's highest seq, read once

    # ---------- writing ----------

    def append_event(
        self,
        *,
        event_type: str,
        trace_id: str,
        trace_kind: str,
        payload: dict[str, Any] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        event = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_role": LEDGER_ROLE,
            "canonical": False,
            "event_type": str(event_type),
            "trace_id": str(trace_id),
            "trace_kind": str(trace_kind),
            "body_hash": _hash_body(body),
            "payload": _json_safe(payload or {}),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lease(self.lock_path):
            if self._floor is None:
                self._floor = self.latest_seq()
            with self.path.open("a+b") as f:
                f.seek(0, os.SEEK_END)
                newest, ends_clean = _tail(f, f.tell())
                seq = max(newest, self._floor) + 1
                event["seq"] = seq
                event["recorded_at"] = datetime.now(timezone.utc).isoformat()
                data = (json.dumps(event, ensure_ascii=False, sort_keys=True)
                        + "\n").encode("utf-8")
                if not ends_clean:
                    data = b"\n" + data
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self._floor = seq
        return event

    def rewrite(self, transform: Callable[[dict[str, Any]], Optional[dict[str, Any]]]) -> int:
        """Replace the file whole: each valid line goes through `transform`, which returns
        the line to keep in its place (None keeps it as it is). Torn and blank lines are
        dropped. Returns how many lines changed; nothing is written when none did."""
        with file_lease(self.lock_path):
            if not self.path.exists():
                return 0
            out: list[bytes] = []
            changed = 0
            with self.path.open("rb") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    new = transform(event)
                    if new is not None and new != event:
                        changed += 1
                        event = new
                    out.append((json.dumps(event, ensure_ascii=False, sort_keys=True)
                                + "\n").encode("utf-8"))
            if not changed:
                return 0
            tmp = self.path.with_name(self.path.name + ".rewrite")
            with tmp.open("wb") as f:
                f.writelines(out)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return changed

    # ---------- reading ----------

    def latest_seq(self) -> int:
        latest = 0
        for event in self.iter_events():
            latest = max(latest, _seq_of(event))
        return latest

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def iter_since(self, since: int) -> Iterator[dict[str, Any]]:
        """The events numbered above `since`, in file order. A line is parsed only when
        its seq is past `since`: the number is read off the line's tail first."""
        if not self.path.exists():
            return
        with self.path.open("rb") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                quick = _tail_seq(line)
                if quick is not None and quick <= since:
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict) and _seq_of(event) > since:
                    yield event

    def verify_integrity(self) -> dict[str, Any]:
        valid_events = 0
        invalid_lines: list[int] = []
        latest_seq = 0
        schema_versions: set[int] = set()
        if not self.path.exists():
            return {
                "ok": True,
                "path": str(self.path),
                "ledger_role": LEDGER_ROLE,
                "canonical": False,
                "valid_events": 0,
                "invalid_lines": [],
                "latest_seq": 0,
                "schema_versions": [],
            }

        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines.append(lineno)
                    continue
                valid_events += 1
                try:
                    latest_seq = max(latest_seq, int(event.get("seq", 0)))
                except (TypeError, ValueError):
                    invalid_lines.append(lineno)
                try:
                    schema_versions.add(int(event.get("schema_version")))
                except (TypeError, ValueError):
                    invalid_lines.append(lineno)

        return {
            "ok": not invalid_lines,
            "path": str(self.path),
            "ledger_role": LEDGER_ROLE,
            "canonical": False,
            "valid_events": valid_events,
            "invalid_lines": invalid_lines,
            "latest_seq": latest_seq,
            "schema_versions": sorted(schema_versions),
        }


_TAIL_BYTES = 64 * 1024


def _tail(f, size: int) -> tuple[int, bool]:
    """(the highest seq among the whole lines in the last _TAIL_BYTES of an open file,
    whether the file ends on a line break). A torn last line counts for nothing."""
    if size == 0:
        return 0, True
    start = max(0, size - _TAIL_BYTES)
    f.seek(start)
    chunk = f.read(size - start)
    f.seek(0, os.SEEK_END)
    ends_clean = chunk.endswith(b"\n")
    lines = chunk.split(b"\n")
    if start > 0:
        lines = lines[1:]           # the first piece may be the end of a longer line
    if not ends_clean:
        lines = lines[:-1]          # a torn last line
    newest = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        quick = _tail_seq(line)
        if quick is None:
            try:
                quick = _seq_of(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                continue
        newest = max(newest, quick)
    return newest, ends_clean


def _seq_of(event: dict[str, Any]) -> int:
    try:
        return int(event.get("seq", 0))
    except (TypeError, ValueError):
        return 0


_SEQ_KEY = b'"seq": '


def _tail_seq(line: bytes) -> Optional[int]:
    """The seq of a line written with sorted keys, read without parsing the whole line;
    None when it cannot be told this way (the caller parses the line)."""
    at = line.rfind(_SEQ_KEY)
    if at < 0:
        return None
    digits = bytearray()
    for b in line[at + len(_SEQ_KEY):]:
        if 48 <= b <= 57:
            digits.append(b)
        else:
            break
    return int(digits) if digits else None


def _hash_body(body: str) -> str:
    digest = hashlib.sha256(str(body).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
