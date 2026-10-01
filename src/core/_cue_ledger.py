"""
========================================
core/_cue_ledger.py — the card ledger: what each window was offered, and what really
reached its model
========================================

The strong-reminder route (`POST /api/v2/cue`, core/_cue.py) hands cards to a host. A card
counts as delivered only when the host says it really went into the model's input (plan
二·七: Loci records what it gave, the host records what was loaded). Delivered cards are
not handed to the same window again at the same version; a host whose window was
compacted says which cards are no longer in the input, and those can be handed again.

Everything is keyed by (host, window): window ids are the host's own and opaque, and two
hosts never share one. A card's key is `<entry id>@<version>` (a name with no card entry:
`name:<name>@<version>`), the version being the entry's content fingerprint
(core/_cue.fingerprint) — an entry that changes is a new key, so it is carded again.

One append-only file, `<buckets>/_cue/ledger.jsonl`, one line per event:

    {"op": "open",    "at", "host", "window", "baseline": [keys]}
    {"op": "offer",   "at", "host", "window", "turn", "cards": [{card, id, kind, why}]}
    {"op": "deliver", "at", "host", "window", "turn", "cards": [{card, id, at?}]}
    {"op": "drop",    "at", "host", "window", "cards": [keys]}
    {"op": "clear",   "at", "host", "window"}
    {"op": "seen",    "at", "ids": {id: at}}          (compaction only)
    {"op": "gen",     "gen": "<uuid>"}                (first line after a compaction)

  open     the first time a window asks for cards. `at` is when the window is known to
           have been open (a clock time that passes later is the card's, not breath's);
           `baseline` the 依据变了的 items that existed then, which that window's breath
           already listed.
  offer    the cards one cue call returned, with why each was picked: the turn's record
           of what was handed out. Nothing is delivered by being offered.
  deliver  the host's word that these cards reached the model's input.
  drop     the host's word that these delivered cards are no longer in the input.
  clear    the whole window's input is gone: every delivered card and the baseline.

The state is replayed from the file, incrementally (only the bytes appended since the
last read), so every process on the library sees every other's writes; appends hold the
library's cross-process file lease. Past `COMPACT_LINES` lines the file is rewritten to
the state it describes (offers older than `OFFER_KEEP_DAYS` dropped, deliveries kept for
`SEEN_KEEP_DAYS` as the awake record); a reader notices the rewrite by its new `gen`.

Exports: CueLedger · WindowState · LEDGER_DIR · LEDGER_FILE
========================================
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from locibrain.eventsourcing.ledger_mirror import file_lease

from . import _when as _w

logger = logging.getLogger("loci_brain.cue_ledger")

LEDGER_DIR = "_cue"
LEDGER_FILE = "ledger.jsonl"
COMPACT_LINES = 20000
OFFER_KEEP_DAYS = 7
SEEN_KEEP_DAYS = 60
_LOCK_TIMEOUT = 5.0


@dataclass
class WindowState:
    """One window as the ledger knows it.

    opened     when it first asked for cards
    baseline   the 依据变了的 keys it had when it opened (its breath listed them)
    delivered  {card key: {"at", "turn", "id"}} — in the model's input now
    offers     {turn: [card dicts]} — what each turn was handed"""
    opened: datetime
    baseline: set = field(default_factory=set)
    delivered: dict = field(default_factory=dict)
    offers: dict = field(default_factory=dict)
    offer_at: dict = field(default_factory=dict)

    def offered_keys(self) -> set:
        return {c["card"] for cards in self.offers.values() for c in cards}


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class CueLedger:
    """The card ledger of one library."""

    def __init__(self, base_dir):
        self.path = Path(base_dir) / LEDGER_DIR / LEDGER_FILE
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._guard = threading.RLock()
        self._compacted_lines = 0
        self._reset()

    # ---------- replay ----------

    def _reset(self) -> None:
        self._windows: dict[tuple[str, str], WindowState] = {}
        self._seen: dict[str, datetime] = {}
        self._offset = 0
        self._gen = ""
        self._lines = 0

    def _first_gen(self) -> str:
        try:
            with self.path.open("rb") as f:
                head = f.readline()
        except OSError:
            return ""
        try:
            row = json.loads(head.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        return str(row.get("gen") or "") if isinstance(row, dict) and row.get("op") == "gen" else ""

    def refresh(self) -> None:
        """Read what was appended since the last read; start over after a rewrite."""
        with self._guard:
            try:
                size = self.path.stat().st_size
            except OSError:
                if self._offset:
                    self._reset()
                return
            if size < self._offset or self._first_gen() != self._gen:
                self._reset()
            if size == self._offset:
                return
            with self.path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read(size - self._offset)
            end = chunk.rfind(b"\n")
            if end < 0:
                return
            for raw in chunk[:end].split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(row, dict):
                    self._apply(row)
                    self._lines += 1
            self._offset += end + 1

    def _apply(self, row: dict) -> None:
        op = row.get("op")
        if op == "gen":
            self._gen = str(row.get("gen") or "")
            return
        at = _w.parse_stamp(row.get("at")) or _w.now()
        if op == "seen":
            for bid, when in (row.get("ids") or {}).items():
                t = _w.parse_stamp(when)
                if t is not None:
                    self._note_seen(str(bid), t)
            return
        key = (str(row.get("host") or ""), str(row.get("window") or ""))
        if not key[1]:
            return
        if op == "open":
            if key not in self._windows:
                self._windows[key] = WindowState(
                    opened=at, baseline={str(k) for k in row.get("baseline") or []})
            return
        win = self._windows.get(key)
        if win is None:                       # a line from before its open row: open it here
            win = self._windows[key] = WindowState(opened=at)
        if op == "offer":
            turn = str(row.get("turn") or "")
            cards = [c for c in row.get("cards") or [] if isinstance(c, dict) and c.get("card")]
            win.offers.setdefault(turn, [])
            have = {c["card"] for c in win.offers[turn]}
            win.offers[turn].extend(c for c in cards if c["card"] not in have)
            win.offer_at[turn] = at
        elif op == "deliver":
            for c in row.get("cards") or []:
                if not isinstance(c, dict) or not c.get("card"):
                    continue
                when = _w.parse_stamp(c.get("at")) or at
                bid = str(c.get("id") or "")
                win.delivered[str(c["card"])] = {"at": when, "turn": c.get("turn") or row.get("turn"),
                                                 "id": bid}
                if bid:
                    self._note_seen(bid, when)
        elif op == "drop":
            for k in row.get("cards") or []:
                win.delivered.pop(str(k), None)
        elif op == "clear":
            win.delivered.clear()
            win.baseline.clear()

    def _note_seen(self, bid: str, when: datetime) -> None:
        prev = self._seen.get(bid)
        if prev is None or when > prev:
            self._seen[bid] = when

    # ---------- reads ----------

    def window(self, host: str, window: str) -> Optional[WindowState]:
        self.refresh()
        with self._guard:
            return self._windows.get((host, window))

    def is_delivered(self, host: str, window: str, card: str) -> bool:
        win = self.window(host, window)
        return bool(win and card in win.delivered)

    def delivered_at(self, bucket_id: str) -> Optional[datetime]:
        """When a card for this entry last reached a model, in any window of any host —
        the `cued` reason for being awake (core/profile.awake_reasons). A card struck
        later still reached the model when it did."""
        self.refresh()
        with self._guard:
            return self._seen.get(str(bucket_id))

    # ---------- writes ----------

    def _append(self, rows: Iterable[dict]) -> None:
        """Append rows under the lease, then read them (and anything else new) back."""
        data = b"".join((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                        for r in rows)
        if not data:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lease(self.lock_path, timeout=_LOCK_TIMEOUT):
            self._append_locked(data)

    def _append_locked(self, data: bytes) -> None:
        with self.path.open("ab") as f:
            f.write(data)
        self.refresh()
        # A state that is itself large is not rewritten on every append: only once the
        # file has doubled past what the last rewrite left.
        if self._lines > max(COMPACT_LINES, 2 * self._compacted_lines):
            self._compact_locked()

    def open_window(self, host: str, window: str, baseline) -> tuple[WindowState, bool]:
        """The window's state, opening it first when it is new. `baseline` is called for
        the keys only when the window is new. Returns (state, opened just now)."""
        win = self.window(host, window)
        if win is not None:
            return win, False
        keys = sorted({str(k) for k in (baseline() if callable(baseline) else baseline or [])})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lease(self.lock_path, timeout=_LOCK_TIMEOUT):
            self.refresh()
            with self._guard:
                win = self._windows.get((host, window))
            if win is not None:            # another caller opened it first
                return win, False
            row = {"op": "open", "at": _stamp(_w.now()), "host": host, "window": window,
                   "baseline": keys}
            self._append_locked(
                (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            with self._guard:
                return self._windows[(host, window)], True

    def offer(self, host: str, window: str, turn: str, cards: list[dict]) -> None:
        """Record what one cue call handed this window (nothing when it handed nothing)."""
        if not cards:
            return
        self._append([{"op": "offer", "at": _stamp(_w.now()), "host": host, "window": window,
                       "turn": turn,
                       "cards": [{k: c.get(k) for k in ("card", "id", "kind", "why")}
                                 for c in cards]}])

    def deliver(self, host: str, window: str, *, turn: Optional[str] = None,
                cards: Optional[list[str]] = None) -> tuple[list[str], list[str]]:
        """The host's word that cards reached the model: every card offered to this window
        in `turn`, and/or the named `cards`. A card this window was never offered is not
        recorded. Returns (delivered, unknown)."""
        win = self.window(host, window)
        if win is None:
            return [], sorted({str(c) for c in cards or []})
        offered: dict[str, dict] = {}
        for t, offs in win.offers.items():
            for c in offs:
                offered.setdefault(c["card"], {**c, "turn": t})
        wanted: list[str] = []
        if turn is not None:
            wanted += [c["card"] for c in win.offers.get(turn, [])]
        wanted += [str(c) for c in cards or []]
        wanted = list(dict.fromkeys(wanted))
        known = [k for k in wanted if k in offered]
        unknown = [k for k in wanted if k not in offered]
        if known:
            self._append([{"op": "deliver", "at": _stamp(_w.now()), "host": host,
                           "window": window, "turn": turn,
                           "cards": [{"card": k, "id": offered[k].get("id") or "",
                                      "turn": offered[k].get("turn")} for k in known]}])
        return known, unknown

    def drop(self, host: str, window: str, *, cards: Optional[list[str]] = None,
             turns: Optional[list[str]] = None, everything: bool = False) -> list[str]:
        """The host's word that cards are no longer in the model's input: the named ones,
        those delivered from the named turns, or (`everything`) the whole window, baseline
        included. Returns the keys struck."""
        win = self.window(host, window)
        if win is None:
            return []
        if everything:
            gone = sorted(win.delivered)
            self._append([{"op": "clear", "at": _stamp(_w.now()), "host": host,
                           "window": window}])
            return gone
        want = {str(c) for c in cards or []}
        turn_set = {str(t) for t in turns or []}
        gone = sorted(k for k, d in win.delivered.items()
                      if k in want or (turn_set and str(d.get("turn") or "") in turn_set))
        if gone:
            self._append([{"op": "drop", "at": _stamp(_w.now()), "host": host,
                           "window": window, "cards": gone}])
        return gone

    # ---------- compaction ----------

    def _compact_locked(self) -> None:
        """Rewrite the file to the state it describes. Called holding the lease."""
        now = _w.now()
        offer_cut = now - timedelta(days=OFFER_KEEP_DAYS)
        seen_cut = now - timedelta(days=SEEN_KEEP_DAYS)
        rows: list[dict] = [{"op": "gen", "gen": uuid.uuid4().hex}]
        with self._guard:
            seen = {bid: _stamp(t) for bid, t in self._seen.items() if t >= seen_cut}
            if seen:
                rows.append({"op": "seen", "at": _stamp(now), "ids": seen})
            for (host, window), win in self._windows.items():
                rows.append({"op": "open", "at": _stamp(win.opened), "host": host,
                             "window": window, "baseline": sorted(win.baseline)})
                for turn, cards in win.offers.items():
                    at = win.offer_at.get(turn) or win.opened
                    if at >= offer_cut:
                        rows.append({"op": "offer", "at": _stamp(at), "host": host,
                                     "window": window, "turn": turn, "cards": cards})
                if win.delivered:
                    rows.append({"op": "deliver", "at": _stamp(now), "host": host,
                                 "window": window, "turn": None,
                                 "cards": [{"card": k, "id": d.get("id") or "",
                                            "turn": d.get("turn"), "at": _stamp(d["at"])}
                                           for k, d in win.delivered.items()]})
        tmp = self.path.with_name(self.path.name + ".rewrite")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, self.path)
        with self._guard:
            self._reset()
        self.refresh()
        self._compacted_lines = self._lines
