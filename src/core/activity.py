"""
========================================
core/activity.py — what Loci handed out, read back for the panel: per turn, over the
last three days, and per memory
========================================

Three reads over two logs, neither of which this module writes:

    the card ledger   core/_cue_ledger.py — what each window was offered, and what the
                      host said reached its model or left it again (`events()`)
    the usage log     core/_usage.py — what breath showed, what a lookup found (with the
                      query as the model typed it), what a write stood on

  turns       one window, turn by turn, newest first: the cards the turn was handed and
              what became of each (offered / delivered / dropped), what breath showed and
              what a lookup found in that turn, and the holds live now on any of them.
  timeline    recall's empty-box screen: the last three natural days (local today,
              yesterday, the day before), "我搜 X" lines from the usage log and "你说 X"
              lines from the card ledger, mixed, newest first.
  usage_counts  each memory: how often it was shown, found and stood on since a day,
              the ones shown often and never stood on first.

A card's state is replayed from the ledger's rows in order: offered when a turn's answer
held it, delivered when the host said it was placed, dropped when the host said a
delivered card left the input (by card, by turn, or the whole window cleared).

The usage log does not know windows: a line carries the host and its write key
`<host>:<turn>#<n>` (core/_usage.turn_of), so a turn's shown and found lines are joined
to its cards by (host, turn) — see `_turn_lines`.

What a search typed (`query`) is the panel's alone (core/_usage.py): every read here
takes `panel`, and only a panel read carries it.

Every list pages the way every panel list does (core/paging.py): newest first, and a row
stamped after `as_of` is left out, so rows arriving while someone pages do not shift the
pages. `entry_ref` is the one place a memory becomes a line here: its id, short handle and
label, or — when the read gate would not show it — no text and a state saying why.

Exports: CARD_STATE_WORDS · index · entry_ref · card_replay · turns · timeline_since ·
         timeline · usage_counts
========================================
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from . import _cue
from . import _holds as _H
from . import _usage
from . import scope as _scope
from . import _when as _w
from . import visibility as _V
from .paging import page, past, stamp
from .profile import entry_label, short_id

TIMELINE_DAYS = 3

OFFERED, DELIVERED, DROPPED = "offered", "delivered", "dropped"
CARD_STATE_WORDS = {OFFERED: "递了，还没回话", DELIVERED: "送到了", DROPPED: "丢了"}

_GONE_WORDS = "这条不在了"
_STATE_WORDS = {
    _V.SOURCE_GONE: "来源撤回了，字不给看",
    _V.ARCHIVED: "在归档区",
    _V.DELETED: "删掉了（在归档区）",
}
_SUPERSEDED_WORDS = "有新版本了"


# ── memories as lines ────────────────────────────────────────────────────────

def index(buckets: Iterable[dict]) -> dict[str, tuple[dict, str]]:
    """{id: (meta, body)} over store buckets."""
    out: dict[str, tuple[dict, str]] = {}
    for b in buckets or []:
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if bid:
            out[bid] = (meta, str(b.get("content") or ""))
    return out


def entry_ref(lib: dict, bid: str, scope=None) -> dict:
    """One memory as a line: {id, short, text, state, state_words}. The read gate decides
    whether its label is shown: a memory standing on a withdrawn or deleted source has no
    text here, and one that is gone (or out of the request's scope) says only that."""
    bid = str(bid or "")
    out = {"id": bid, "short": short_id(bid) if bid else "", "text": "", "state": "",
           "state_words": ""}
    got = lib.get(bid)
    if got is None:
        out.update(state="missing", state_words=_GONE_WORDS)
        return out
    meta, body = got
    verdict = _V.visible_for(meta, scope, road=_V.READ)
    if _V.OUT_OF_SCOPE in verdict.reasons:
        out.update(state="missing", state_words=_GONE_WORDS)
        return out
    out["state"] = verdict.state
    if not verdict.shown:
        why = next((r for r in verdict.reasons if r in _STATE_WORDS), verdict.state)
        out["state_words"] = _STATE_WORDS.get(why, _GONE_WORDS)
        return out
    out["text"] = entry_label(meta, body)
    if verdict.state != _V.LIVE:
        out["state_words"] = _STATE_WORDS.get(verdict.state, "")
    elif str(meta.get("superseded_by") or "").strip():
        out["state_words"] = _SUPERSEDED_WORDS
    return out


def _has_original(lib: dict, bid: str) -> bool:
    """Does the memory carry a source record a host could be asked for its original by
    (core/_originals.source_records_of)?"""
    from ._originals import source_records_of
    got = lib.get(bid)
    return bool(got and source_records_of(got[0]))


# ── what became of each card ─────────────────────────────────────────────────

def card_replay(events: list[dict]) -> dict[tuple[str, str], dict[str, dict]]:
    """Replay ledger rows (CueLedger.events, file order) into {(host, window): {turn:
    {"at", "n", "cards": {key: {card, id, kind, why, state}}}}}. A turn's `at` is when it
    was first answered; a turn known only from a delivery (its offer compacted away) is
    dated by that delivery. `n` counts turns in the order the rows first name them, which
    orders turns answered within the same second."""
    out: dict[tuple[str, str], dict[str, dict]] = {}
    live: dict[tuple[str, str], dict[str, str]] = {}      # key -> the turn it was delivered from
    count = [0]

    def record(win: dict, turn: str, at: datetime) -> dict:
        rec = win.get(turn)
        if rec is None:
            count[0] += 1
            rec = win[turn] = {"at": at, "n": count[0], "cards": {}}
        return rec

    for row in events:
        key = (row["host"], row["window"])
        win = out.setdefault(key, {})
        held = live.setdefault(key, {})
        op = row.get("op")
        at = row["at"]
        if op == "offer":
            turn = str(row.get("turn") or "")
            rec = record(win, turn, at)
            for c in list(row.get("cards") or []) + list(row.get("ever") or []):
                if isinstance(c, dict) and c.get("card") and c["card"] not in rec["cards"]:
                    rec["cards"][str(c["card"])] = {
                        "card": str(c["card"]), "id": str(c.get("id") or ""),
                        "kind": str(c.get("kind") or ""), "why": str(c.get("why") or ""),
                        "state": OFFERED}
        elif op == "deliver":
            for c in row.get("cards") or []:
                if not isinstance(c, dict) or not c.get("card"):
                    continue
                k = str(c["card"])
                turn = str(c.get("turn") or row.get("turn") or "")
                if not turn:
                    turn = next((t for t in reversed(list(win)) if k in win[t]["cards"]), "")
                when = _w.parse_stamp(c.get("at")) or at
                rec = record(win, turn, when)
                card = rec["cards"].setdefault(k, {
                    "card": k, "id": str(c.get("id") or ""),
                    "kind": _cue.NAME_BARE if k.startswith("name:") else "",
                    "why": "", "state": OFFERED})
                card["state"] = DELIVERED
                held[k] = turn
        elif op == "drop":
            for k in row.get("cards") or []:
                turn = held.pop(str(k), None)
                if turn is not None and str(k) in win.get(turn, {}).get("cards", {}):
                    win[turn]["cards"][str(k)]["state"] = DROPPED
        elif op == "clear":
            for k, turn in held.items():
                if k in win.get(turn, {}).get("cards", {}):
                    win[turn]["cards"][k]["state"] = DROPPED
            held.clear()
    return out


def _card_line(lib: dict, c: dict, scope) -> Optional[dict]:
    """A card as the panel shows it: the memory it shows (a name card without an entry:
    the name), what kind it is, why it was picked, and what became of it. None for a card
    whose memory a scoped request may not read: it is left out, id and all."""
    kind = c.get("kind") or ""
    key = c["card"]
    if c.get("id"):
        ref = entry_ref(lib, c["id"], scope)
        if _scope.narrows(scope) and ref["state"] == "missing":
            return None
    else:
        name = key[len("name:"):].rpartition("@")[0] if key.startswith("name:") else ""
        ref = {"id": "", "short": "", "text": name, "state": "", "state_words": ""}
    out = {"card": key, "id": ref["id"], "short": ref["short"], "kind": kind,
           "kind_words": _cue.CARD_KIND_WORDS.get(kind, ""), "why": c.get("why") or "",
           "text": ref["text"], "state": c["state"],
           "state_words": CARD_STATE_WORDS[c["state"]],
           "has_original": bool(ref["id"] and ref["text"] and _has_original(lib, ref["id"]))}
    if ref["state_words"]:
        out["entry_words"] = ref["state_words"]
    return out


def _cards(lib: dict, rec: dict, scope) -> list[dict]:
    """A replayed turn's cards as lines, in the order the turn was handed them."""
    lines =(_card_line(lib, c, scope) for c in rec["cards"].values())
    return [line for line in lines if line is not None]


def _said(cards: list[dict]) -> str:
    """The words of the owner's message a turn's cards matched — never the message itself,
    which Loci does not keep. Due and review cards were not matched on any word."""
    words = [c["why"] for c in cards
             if c.get("why") and c.get("kind") not in (_cue.DUE, _cue.REVIEW)]
    return "、".join(dict.fromkeys(words))


# ── turns ────────────────────────────────────────────────────────────────────

def _turn_lines(usage_rows: Iterable[dict], host: str, turns: set) -> dict[str, list[dict]]:
    """The usage lines written in each of `turns` by `host`, oldest first.

    Q8 (open): this joins on the turn alone, which holds only while the host sends the
    same turn id in `/api/v2/cue`'s `turn` as in its `Loci-Turn` header — and keeps turn
    ids apart across its windows, since a usage line names no window."""
    out: dict[str, list[dict]] = {}
    for row in usage_rows or []:
        h, turn = _usage.turn_of(row)
        if h == host and turn in turns:
            out.setdefault(turn, []).append(row)
    return out


def _road_line(row: dict, lib: dict, scope, panel: bool) -> dict:
    out = {"id": _usage.row_id(row), "road": str(row.get("road") or ""),
           "ids": [str(i) for i in row.get("ids") or []
                   if not _scope.narrows(scope)
                   or entry_ref(lib, i, scope)["state"] != "missing"]}
    if panel and row.get("kind") == _usage.FOUND and "query" in row:
        out["query"] = str(row.get("query") or "")
    return out


def _holds_line(lib: dict, ids: Iterable[str], now: datetime, idx, scope) -> list[dict]:
    """The holds live now on any of `ids` (any version of it), and any of `ids` that is
    itself a live hold."""
    seen: dict[str, dict] = {}
    for bid in dict.fromkeys(ids):
        found = list(_H.holds_on(bid, now, idx))
        got = lib.get(bid)
        if got and _H.hold_is_live(got[0], now):
            found.append(got[0])
        for h in found:
            hid = str(h.get("id") or "")
            if not hid or hid in seen:
                continue
            if not _V.visible_for(h, scope, road=_V.READ).shown:
                continue
            end = _H.hold_end(h)
            until = end.isoformat() if end else (str(h.get("review_after") or "") or None)
            seen[hid] = {"id": hid, "short": short_id(hid),
                         "on": str(h.get("exception_of") or ""), "hold": h.get("hold"),
                         "words": _cue.HOLD_WORDS.get(h.get("hold"), ""), "until": until}
    return list(seen.values())


def turns(ledger, usage_rows: Iterable[dict], buckets: Iterable[dict], *, host: str,
          window: str, now: datetime, offset: int, limit: int, as_of: datetime,
          scope=None, panel: bool = True) -> Optional[dict]:
    """One window of one host, turn by turn, newest first (the panel's turns page):
    {host, window, opened, items: [{turn, at, cards, shown, found, holds}], paging}.
    None when the ledger has never heard of the window."""
    events = ledger.events(host, window)
    state = ledger.window(host, window)
    if state is None and not events:
        return None
    lib = index(buckets)
    replay = card_replay(events).get((host, window), {})
    rows = [(turn, rec) for turn, rec in replay.items() if not past(rec["at"], as_of)]
    rows.sort(key=lambda tr: (tr[1]["at"], tr[1]["n"]), reverse=True)
    paged = page(rows, offset, limit, as_of)
    lines = _turn_lines(usage_rows, host, {t for t, _r in paged["items"]})
    idx = _H.hold_index(buckets)
    items = []
    for turn, rec in paged["items"]:
        cards = _cards(lib, rec, scope)
        mine = lines.get(turn, [])
        shown = [_road_line(r, lib, scope, panel) for r in mine if r.get("kind") == _usage.SHOWN]
        found = [_road_line(r, lib, scope, panel) for r in mine if r.get("kind") == _usage.FOUND]
        ids = [c["id"] for c in cards if c["id"]] + [i for x in shown + found for i in x["ids"]]
        items.append({"turn": turn, "at": stamp(rec["at"]), "cards": cards, "shown": shown,
                      "found": found, "holds": _holds_line(lib, ids, now, idx, scope)})
    opened = state.opened if state is not None else (events[0]["at"] if events else None)
    return {"host": host, "window": window, "opened": stamp(opened),
            **paged, "items": items}


# ── recall's timeline ────────────────────────────────────────────────────────

def timeline_since(now: datetime) -> datetime:
    """The start of the last three natural days: local midnight of the day before
    yesterday."""
    today = _w.to_local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=TIMELINE_DAYS - 1)


def timeline(events: list[dict], usage_rows: Iterable[dict], buckets: Iterable[dict], *,
             now: datetime, offset: int, limit: int, as_of: datetime, scope=None,
             panel: bool = True) -> dict:
    """The last three natural days, newest first: what the model searched for (`search`:
    query, how many it brought back and which) and what cards the owner's words brought
    (`card`: the matched words, and each card with what became of it). A turn whose answer
    held no card is not a line."""
    since = timeline_since(now)
    lib = index(buckets)
    items: list[tuple[datetime, int, dict]] = []
    for n, row in enumerate(usage_rows or []):
        if row.get("kind") != _usage.FOUND or not str(row.get("query") or "").strip():
            continue
        at = _w.parse_stamp(row.get("at"))
        if at is None or at < since or past(at, as_of):
            continue
        ids = [str(i) for i in row.get("ids") or []]
        line = {"id": _usage.row_id(row), "kind": "search", "at": stamp(at),
                "host": str(row.get("host") or ""), "road": str(row.get("road") or ""),
                "n": len(ids), "ids": ids}
        if panel:
            line["query"] = str(row.get("query") or "")
        items.append((at, n, line))
    for (host, window), win in card_replay(events).items():
        for turn, rec in win.items():
            if rec["at"] < since or past(rec["at"], as_of):
                continue
            cards = _cards(lib, rec, scope)
            if not cards:
                continue
            items.append((rec["at"], rec["n"], {
                "id": f"{window}/{turn}", "kind": "card", "at": stamp(rec["at"]),
                "host": host, "window": window, "turn": turn,
                "said": _said(cards), "cards": cards}))
    items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return {"since": since.date().isoformat(),
            **page([line for _at, _n, line in items], offset, limit, as_of)}


# ── how often each memory was handed out ─────────────────────────────────────

def usage_counts(usage_rows: Iterable[dict], buckets: Iterable[dict], *, since: datetime,
                 offset: int, limit: int, as_of: datetime, scope=None) -> dict:
    """Each memory the usage log names since `since` (local midnight of that day): how
    many lines showed it, found it, stood on it. The ones shown and never stood on come
    first, the most shown of them first; then the rest, the same way."""
    counts: dict[str, dict] = {}
    for row in usage_rows or []:
        kind = row.get("kind")
        if kind not in (_usage.SHOWN, _usage.FOUND, _usage.SOURCE):
            continue
        at = _w.parse_stamp(row.get("at"))
        if at is None or at < since or past(at, as_of):
            continue
        for bid in dict.fromkeys(str(i) for i in row.get("ids") or [] if i):
            c = counts.setdefault(bid, {_usage.SHOWN: 0, _usage.FOUND: 0, _usage.SOURCE: 0})
            c[kind] += 1
    lib = index(buckets)
    rows = []
    for bid, c in counts.items():
        ref = entry_ref(lib, bid, scope)
        if _scope.narrows(scope) and ref["state"] == "missing":
            continue
        rows.append({"id": bid, "short": ref["short"], "text": ref["text"],
                     "state_words": ref["state_words"], "shown": c[_usage.SHOWN],
                     "found": c[_usage.FOUND], "source": c[_usage.SOURCE]})
    rows.sort(key=lambda r: (r["source"] > 0, -r["shown"], -r["found"], r["id"]))
    return {"since": _w.to_local(since).date().isoformat(),
            **page(rows, offset, limit, as_of)}
