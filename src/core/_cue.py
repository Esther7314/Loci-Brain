"""
========================================
core/_cue.py — the strong reminder: cards that ride with the owner's message
========================================

`POST /api/v2/cue` (plan 二·三). The host hands over the owner's message the moment it
arrives — `{text, window, turn}` — and gets back at most three cards to paste after that
message (never into the system prompt: cards change every turn, and the prompt cache would
stop matching). Matching is code only, never a model call: the names table and each cue's
phrasings. A card changes nothing — whether the thing happened, whether a hold lifts,
whether a promise closes is the model's to decide after reading it.

------------------------------------------------------------
The cards, in the order they are handed (at most CARD_LIMIT)
------------------------------------------------------------
  due        「提醒」: an open entry whose clock time today has come since this window
             opened (core/profile.due_now). The owner's next message brings it, whatever
             that message says — a window opened after the time read it in breath instead.
             A clock the backfill filled in from a part of the day (「今晚」 -> 19:00) is a
             reference time: the card shows the original words and says the hour is
             approximate and was not agreed.
  memory     「相关记忆」: an entry whose cue (`{condition, phrasings}`) the message matched.
  hold       「条子」: a live hold waiting on an event, whose cue the message matched. Only
             the model lifts it, after reading.
  review     「依据变了」: a memory that entered 依据变了的 since this window opened (its
             breath listed what was there before). Id and status, and the label only when
             the memory may still be read — never the text of one standing on a withdrawn
             or deleted source (core/_invalidation.block).
  name       「相关名字」 with a card: a name the message mentions that has a `card_of` entry.
  name_bare  「相关名字」 without one: where the names table says it comes from.

Not handed: anything the core block already holds (the name page, pinned rules, and any name
that page spells out); the AI's and the owner's own names; anything closed; anything a live
hold covers. Every card goes through the gate on its own road (`cue`; `invalidation` for a
review card), under the request's read scope: a card for something out of scope is simply
not produced, and nothing says one was withheld.

------------------------------------------------------------
How words are matched
------------------------------------------------------------
The message and every spelling are NFKC-normalised and lower-cased.
  Latin / digits   at least 3 characters, and only as a whole word (no letter or digit on
                   either side): "Leon" does not fire inside "Leonardo".
  CJK              a name's own key needs 2 characters, an alias 3 — a 1–2 character
                   nickname is too common a string to stand for a person. A cue phrasing
                   needs 2, the cue's condition (a whole sentence) 3.
  word edges       a CJK name, and a CJK phrasing of 2 characters, fires only where it
                   starts and ends on a word boundary of jieba's dictionary-only cut: 「阿明」
                   does not fire in 「阿明白了」, 「考完」 does in 「还没考完」 and 「终于考完了」.
                   Longer phrasings match as they stand (a phrase of 3+ characters inside a
                   longer word is rare, and a missed reminder costs more).
  overlaps         the longest spelling wins a stretch of text; a shorter one inside it
                   does not fire again. A spelling two names share stands for neither.
Negation and hypotheticals do not stop a card: 「还没考完」 and 「要是考完了的话」 are
exactly when a reminder helps the model not to act on it. The card quotes the clause and
says when it reads as negated, hypothetical or a question, so the model can tell. A thing
that happened without its words being said gets no card: the known miss, covered by dates,
breath and recall.

------------------------------------------------------------
Counting what was delivered (core/_cue_ledger.py)
------------------------------------------------------------
A card is offered, then delivered only when the host confirms it was actually placed in the
model's input (`POST /api/v2/cue/delivered`): by `cards` for exactly the cards placed —
which is how a partial placement is told — and by `turn` only when every card of the turn's
answer was placed. Nothing else counts a card as delivered.

A turn is answered once. The same `turn` asked again (a host retrying) gets that answer's
cards back, in its order, and nothing new — never a fresh pick merged in. Each card is
produced again for the retry, under the retry's own scope and against the entry as it is
then, so one whose entry was since withdrawn, revised (a new version is a new key) or put
out of scope is dropped; the retry's answer becomes the turn's answer (what `turn`
delivers). The first answer holds at most CARD_LIMIT cards, so no retry holds more. A
delivered card is not handed to that window again while
the entry stays at the same version — `fingerprint()`: the body, when, status, bound, cue,
recurrence, direction, a hold's own level and target, the holds ever hung on it (opened or
closed), its open invalidation records, and what the source registry says of its sources.
Rescheduled, a hold lifted, a source revised: a new version, carded again. The host strikes
cards that left the input (`POST /api/v2/cue/dropped`, by card, by turn, or the whole
window), and those can be handed again.

The usage log records every card offered as `shown`, road `cue.<kind>` (what Loci gave;
what was loaded is the host's to say).

Exports: CARD_LIMIT · CARD_KINDS · Card · CueRequestError · fingerprint · cue ·
         handle_cue · handle_delivered · handle_dropped · scope_view
========================================
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from utils import get_ai_name, get_owner_name, is_closed, parse_bool, read_from_ids

from . import _holds as _H
from . import _invalidation as _I
from . import _usage
from . import _when as _w
from . import names as _names
from . import scope as _scope
from . import visibility as _V
from .profile import (_EDITED_BY_USER_TAG, _PROFILE_TAG, due_now, entry_label,
                      is_open_promise, owed_names, short_id, written_days_ago)

logger = logging.getLogger("loci_brain.cue")

CARD_LIMIT = 3
TEXT_MAX = 20000            # characters of one message
ID_MAX = 256                # a window id, a turn id, a card key
LIST_MAX = 256              # cards or turns in one acknowledgement

DUE, MEMORY, HOLD, REVIEW, NAME, NAME_BARE = "due", "memory", "hold", "review", "name", "name_bare"
CARD_KINDS = (DUE, MEMORY, HOLD, REVIEW, NAME, NAME_BARE)
_RANK = {DUE: 0, MEMORY: 1, HOLD: 1, REVIEW: 2, NAME: 3, NAME_BARE: 4}

PANEL_HOST = "panel"         # who a logged-in browser is, in the ledger

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_SPACES = re.compile(r"\s+")
_CLAUSE_STOPS = set(",.!?;:。，！？；：…\n、~")
_NEGATED_BEFORE = re.compile(r"(没有|没|不是|不|别|未|并非|甭)\S{0,2}$")
_HYPOTHETICAL = ("如果", "要是", "假如", "假设", "万一", "的话", "倘若", "假使", "若是")
_DAY_PART = re.compile(
    r"(今天|明天|今|明)?(早上|早晨|清早|上午|中午|下午|傍晚|晚上|夜里|夜晚|今晚|明晚|今早|明早)"
    r"([零〇一二两三四五六七八九十\d]{1,3}(?:点|时)(?:半|一刻|三刻)?)?")
_HOLD_WORD = {"defer": "先别催", "avoid": "别碰"}
_MIN_CJK_KEY, _MIN_CJK_ALIAS, _MIN_CJK_PHRASING, _MIN_CJK_CONDITION = 2, 3, 2, 3
_MIN_LATIN = 3


class CueRequestError(ValueError):
    """A request body the cue routes will not act on (400)."""


# ── words ────────────────────────────────────────────────────────────────────

def _norm(s) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", str(s or "")).lower()).strip()


class _Message:
    """The owner's message, normalised, with a map back to the original characters (the
    card quotes the original) and jieba's dictionary-only word edges, cut on first use."""

    def __init__(self, text: str):
        self.raw = text
        out: list[str] = []
        self.origin: list[int] = []
        for i, ch in enumerate(text):
            for c in unicodedata.normalize("NFKC", ch).lower():
                out.append(c)
                self.origin.append(i)
        self.norm = "".join(out)
        self._edges: Optional[set] = None

    def edges(self) -> Optional[set]:
        """Word boundaries of the dictionary-only cut; None when jieba is unavailable (the
        health check says so loudly), and then edges are not required."""
        if self._edges is None:
            try:
                import jieba
                edges = {0, len(self.norm)}
                for _word, start, end in jieba.tokenize(self.norm, HMM=False):
                    edges.add(start)
                    edges.add(end)
                self._edges = edges
            except Exception as e:  # noqa: BLE001 - a missing cutter must not drop the card road
                logger.warning("cue: jieba unavailable, CJK word edges not checked: %s", e)
                self._edges = set()
        return self._edges or None

    def find(self, spelling: str, *, min_cjk: int, edges: bool) -> Optional[tuple[int, int]]:
        """The first stretch of the message this spelling may fire on, or None."""
        s = _norm(spelling)
        if not s:
            return None
        if _CJK.search(s):
            if len(s) < min_cjk:
                return None
            start = self.norm.find(s)
            while start >= 0:
                end = start + len(s)
                cut = self.edges() if edges else None
                if cut is None or (start in cut and end in cut):
                    return start, end
                start = self.norm.find(s, start + 1)
            return None
        if len(s) < _MIN_LATIN:
            return None
        m = re.search(r"(?<![0-9a-z_])" + re.escape(s) + r"(?![0-9a-z_])", self.norm)
        return (m.start(), m.end()) if m else None

    def clause(self, span: tuple[int, int]) -> tuple[str, str]:
        """(the clause holding the stretch, in the original characters; how it reads:
        "negated" / "hypothetical" / "question" / "")."""
        a, b = span
        lo = a
        while lo > 0 and self.norm[lo - 1] not in _CLAUSE_STOPS:
            lo -= 1
        hi = b
        while hi < len(self.norm) and self.norm[hi] not in _CLAUSE_STOPS:
            hi += 1
        whole = self.norm[lo:hi]
        tail = self.norm[b:hi + 1]
        reads = ""
        if any(w in whole for w in _HYPOTHETICAL):
            reads = "hypothetical"
        elif _NEGATED_BEFORE.search(self.norm[lo:a]):
            reads = "negated"
        elif "?" in tail or "吗" in tail:
            reads = "question"
        text = self.raw[self.origin[lo]:self.origin[hi - 1] + 1] if hi > lo else ""
        return text.strip()[:40], reads


_READS = {"negated": "（原话是否定的，事情不一定发生了）",
          "hypothetical": "（原话是假设，事情不一定发生了）",
          "question": "（原话是在问）"}


# The message type and the normaliser, for the other places that match words the same way:
# the write tools' look-back (core/_reconsolidation.py) and scene question
# (core/_case_recall.py).
Message = _Message
norm = _norm


def table_spellings(extra: tuple = ()) -> list[tuple[str, str, int]]:
    """(name, spelling, least CJK length) for every spelling of the names table that stands
    for one name only — a key needs _MIN_CJK_KEY characters, an alias _MIN_CJK_ALIAS — then
    each name in `extra` that the table does not know, spelled as itself."""
    records = _names.load_names_table()
    flat = _names.load_alias_table()
    out: list[tuple[str, str, int]] = []
    for name, rec in records.items():
        for spelling, min_cjk in [(name, _MIN_CJK_KEY)] + [(a, _MIN_CJK_ALIAS) for a in rec.aliases]:
            if flat.get(str(spelling).strip().lower()) != name:
                continue            # shared by two names: stands for neither
            out.append((name, str(spelling), min_cjk))
    known = {n.lower() for n in records}
    out += [(n, n, _MIN_CJK_KEY) for n in dict.fromkeys(extra)
            if n and n.lower() not in known and not flat.get(n.lower())]
    return out


def pick_names(msg: _Message, spellings: list[tuple[str, str, int]]) -> dict[str, str]:
    """{name: the spelling that fired} for each name the message mentions. The longest
    spelling wins a stretch of text; a shorter one inside it does not fire again."""
    hits: list[tuple[int, int, str, str]] = []
    for name, spelling, min_cjk in spellings:
        span = msg.find(spelling, min_cjk=min_cjk, edges=True)
        if span:
            hits.append((span[0], span[1], name, spelling))
    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    taken: list[tuple[int, int]] = []
    picked: dict[str, str] = {}
    for a, b, name, spelling in hits:
        if any(a < y and x < b for x, y in taken) or name in picked:
            continue
        taken.append((a, b))
        picked[name] = spelling
    return picked


def own_names() -> set[str]:
    """The AI's and the owner's names, normalised, with the table's name for each: a card
    or a look-back on them would fire on nearly every message."""
    own = {_norm(n) for n in (get_ai_name(), get_owner_name()) if n}
    return own | {_norm(_names.canonical(n)) for n in own if n}


def _quoted(msg: _Message, span: tuple[int, int], matched: str) -> str:
    clause, reads = msg.clause(span)
    quote = f"：「{clause}」" if clause and _norm(clause) != _norm(matched) else ""
    return f"← 对上「{matched}」{quote}{_READS.get(reads, '')}"


def _cue_hit(msg: _Message, cue: dict) -> Optional[tuple[str, tuple[int, int]]]:
    """The longest of the cue's phrasings (or its whole condition) the message matches."""
    best = None
    tries = [(p, _MIN_CJK_PHRASING) for p in cue.get("phrasings") or []]
    tries.append((cue.get("condition") or "", _MIN_CJK_CONDITION))
    for phrase, min_cjk in tries:
        p = _norm(phrase)
        if not p:
            continue
        span = msg.find(p, min_cjk=min_cjk, edges=len(p) <= 2)
        if span and (best is None or len(p) > len(best[0])):
            best = (str(phrase).strip(), span)
    return best


# ── versions ─────────────────────────────────────────────────────────────────

def fingerprint(meta: dict, content: str, *, holds=(), registry=None) -> str:
    """An entry's version for the card ledger: 12 hex of a hash over what a card says or
    rests on. The backfill's name and summary are left out (they are rewritten in the
    background and would re-card an unchanged entry); so is anything a read touches."""
    found = _I.source_findings(meta, registry)
    tags = [str(t) for t in meta.get("tags") or []]
    data = {
        "body": str(content or "").strip(),
        "when": str(meta.get("when") or ""),
        "status": str(meta.get("status") or ""),
        "closed": is_closed(meta),
        "bound": [str(x) for x in meta.get("bound") or []],
        "cue": meta.get("cue") if isinstance(meta.get("cue"), dict) else None,
        "recurrence": str(meta.get("recurrence") or ""),
        "direction": str(meta.get("direction_of_fit") or ""),
        "hold": [str(meta.get("hold") or ""), str(meta.get("exception_of") or "")],
        "holds": sorted([str(h), bool(o)] for h, o in holds),
        "invalidation": sorted([str(r.get("kind")), str(r.get("of")), str(r.get("by"))]
                               for r in _I.open_records(meta)),
        "failed": sorted([str(s), str(st)] for s, st in found.failed),
        "revised": sorted([str(s), str(rv)] for s, rv in found.revised),
        "edited": _EDITED_BY_USER_TAG in tags and not _I.edit_confirmed(meta),
        "state": _V.state_of(meta),
    }
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ── cards ────────────────────────────────────────────────────────────────────

@dataclass
class Card:
    kind: str
    key: str            # <id>@<version> | name:<name>@<version>
    id: str             # the entry it shows; "" for a name with no card entry
    text: str           # what the model reads, one line
    why: str            # what matched, or the kind's reason ("due", "review")
    sort: tuple = ()

    def public(self) -> dict:
        out = {"card": self.key, "kind": self.kind, "id": self.id,
               "short": short_id(self.id) if self.id else "", "why": self.why,
               "text": self.text}
        return out


def _standing(meta: dict) -> bool:
    return (not is_closed(meta) and _V.state_of(meta) == _V.LIVE
            and not str(meta.get("superseded_by") or "").strip())


def _age(meta: dict, now: datetime) -> str:
    days = written_days_ago(meta, _w.to_local(now).date())
    if days is None:
        return ""
    return "今天写的" if days <= 0 else f"{days} 天前写的"


def _when_words(meta: dict) -> str:
    moment = _V.clock_moment(meta)
    backfilled = "when" in [str(f) for f in meta.get("backfilled") or []]
    if moment is not None:
        shown = moment.strftime("%m-%d %H:%M")
    else:
        w = str(meta.get("when") or "").strip()
        shown = w[5:10] if re.match(r"^\d{4}-\d{2}-\d{2}$", w) else ""
    if not shown:
        return ""
    return f"时间 {shown}" + ("（是补的约数）" if backfilled else "")


class _Library:
    """One pass over the store for one call: what each kind of card draws on."""

    def __init__(self, buckets: list, registry, scope, now: datetime):
        self.buckets = buckets
        self.registry = registry
        self.scope = scope
        self.now = now
        self.by_id: dict[str, tuple[dict, str]] = {}
        self.core_ids: set[str] = set()
        self.cued: list[str] = []
        self.clocked: list[str] = []
        self.cards: dict[str, list[str]] = {}
        self.subjects: dict[str, list[str]] = {}
        self.hung: dict[str, list[tuple[str, bool]]] = {}
        self.prior: dict[str, str] = {}
        facts: list[str] = []
        for b in buckets:
            meta = b.get("metadata") or {}
            bid = str(meta.get("id") or b.get("id") or "")
            if not bid:
                continue
            content = str(b.get("content") or "")
            self.by_id[bid] = (meta, content)
            if _PROFILE_TAG in [str(t) for t in meta.get("tags") or []]:
                self.core_ids.add(bid)
                # A name page the request may not read does not keep a name card back:
                # whether one is withheld would say what that page holds.
                facts.append(content if scope is None or scope.permits(meta) else "")
            if parse_bool(meta.get("pinned"), default=False):
                self.core_ids.add(bid)
            prev = str(meta.get("supersedes") or "").strip()
            if prev:
                self.prior[bid] = prev
            target = str(meta.get("exception_of") or "").strip()
            if target and meta.get("hold") in _H.HOLD_LEVELS:
                self.hung.setdefault(target, []).append((bid, _standing(meta)))
            cue = meta.get("cue")
            if isinstance(cue, dict) and str(cue.get("condition") or "").strip():
                self.cued.append(bid)
            if meta.get("when") and _V.clock_moment(meta) is not None:
                self.clocked.append(bid)
            card = str(meta.get("card_of") or "").strip()
            if card:
                self.cards.setdefault(card, []).append(bid)
            for s in meta.get("subjects") or []:
                self.subjects.setdefault(str(s).strip(), []).append(bid)
        self.facts = _norm(" ".join(facts))
        self.holds = _H.hold_index(buckets)
        self._fp: dict[str, str] = {}
        self._review: Optional[list] = None

    # versions and the gate

    def lineage(self, bid: str) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []
        seen: set[str] = set()
        while bid and bid not in seen and len(seen) < 64:
            seen.add(bid)
            out.extend(self.hung.get(bid, ()))
            bid = self.prior.get(bid, "")
        return out

    def key(self, bid: str) -> str:
        if bid not in self._fp:
            meta, content = self.by_id[bid]
            self._fp[bid] = fingerprint(meta, content, holds=self.lineage(bid),
                                        registry=self.registry)
        return f"{bid}@{self._fp[bid]}"

    def shown(self, bid: str, road: str = _V.CUE) -> bool:
        meta, _content = self.by_id[bid]
        return bool(_V.visible_for(meta, self.scope, road=road, now=self.now,
                                   holds=self.holds))

    def label(self, bid: str, n: int = 40) -> str:
        meta, content = self.by_id[bid]
        return entry_label(meta, content)[:n]

    # due

    def due_cards(self, opened: datetime) -> list[Card]:
        out = []
        for bid in self.clocked:
            meta, content = self.by_id[bid]
            if bid in self.core_ids or _H.is_hold(meta) or not due_now(meta, self.now):
                continue
            moment = _V.clock_moment(meta)
            created = _w.parse_stamp(meta.get("created"))
            # The window opened after the time came: its breath listed it. Written after
            # the time: a record of it, not something to be reminded of.
            if moment is None or moment <= opened or created is None or created >= moment:
                continue
            if not self.shown(bid):
                continue
            clock = moment.strftime("%H:%M")
            if "when" in [str(f) for f in meta.get("backfilled") or []]:
                phrase = _DAY_PART.search(content or "")
                said = f"原话「{phrase.group(0)}」没说几点，" if phrase else "原话没说几点，"
                when = f"{said}{clock} 是补的参考时间、不是说好的点；大约到时候了"
            else:
                when = f"约的是今天 {clock}，到点了"
            owed = ""
            if is_open_promise(meta):
                names = owed_names(meta.get("bound"))
                owed = f"；答应了的（{names}欠着）" if names else "；答应了的"
            text = (f"【提醒】{self.label(bid)}（{when}{owed}）"
                    f"——做完了 trace(bucket_id=\"{short_id(bid)}\", status=\"resolved\")，"
                    f"卡本身什么都不改 ({short_id(bid)})")
            out.append(Card(DUE, self.key(bid), bid, text, "due", (moment.isoformat(),)))
        out.sort(key=lambda c: c.sort)
        return out

    # cue matches

    def cue_cards(self, msg: _Message) -> list[Card]:
        out = []
        for bid in self.cued:
            meta, content = self.by_id[bid]
            if bid in self.core_ids or is_closed(meta):
                continue
            hold = _H.is_hold(meta)
            if hold and not _H.hold_is_live(meta, self.now):
                continue
            hit = _cue_hit(msg, meta["cue"])
            if hit is None or not self.shown(bid):
                continue
            matched, span = hit
            quote = _quoted(msg, span, matched)
            cond = str(meta["cue"].get("condition") or "").strip()[:24]
            if hold:
                target = str(meta.get("exception_of") or "").strip()
                # The entry it hangs on is named only to a request that may read it: an id
                # is an existence, and out of scope nothing exists.
                on = (f"挂在 {short_id(target)} 上；" if target and (
                    self.scope is None or self.scope.permits_id(target)) else "")
                text = (f"【条子】{self.label(bid)}（{_HOLD_WORD.get(meta.get('hold'), '')}，"
                        f"{on}等的事：{cond}）{quote}"
                        f"——等的事真到了再 trace(bucket_id=\"{short_id(bid)}\", status=\"resolved\") "
                        f"撤条子，没到就不用管 ({short_id(bid)})")
                kind = HOLD
            else:
                parts = []
                if is_open_promise(meta):
                    names = owed_names(meta.get("bound"))
                    parts.append(f"答应了的（{names}欠着）" if names else "答应了的")
                parts.append(f"等的是：{cond}")
                when = _when_words(meta)
                if when:
                    parts.append(when)
                age = _age(meta, self.now)
                if age:
                    parts.append(age)
                parts.extend(self._flags(meta))
                origin = self._origin(meta)
                if origin:
                    parts.append(f"来路：{origin}")
                text = f"【相关记忆】{self.label(bid)}（{'；'.join(parts)}）{quote} ({short_id(bid)})"
                kind = MEMORY
            out.append(Card(kind, self.key(bid), bid, text, matched,
                            (-len(matched), not is_open_promise(meta),
                             str(meta.get("created") or ""))))
        # The longest match first, an open promise before the rest, then the newest.
        out.sort(key=lambda c: c.sort[2], reverse=True)
        out.sort(key=lambda c: c.sort[:2])
        return out

    def _flags(self, meta: dict) -> list[str]:
        flags = []
        found = _I.source_findings(meta, self.registry)
        if found.revised:
            flags.append("⚠️来源出了新版本，待确认，别当成现行安排")
        if _I.open_records(meta, _I.OVERTURN):
            flags.append("⚠️它站着的那条被推翻了，待确认")
        return flags

    def _origin(self, meta: dict) -> str:
        """One layer back: the label of the first entry it stands on that may be shown."""
        for src in read_from_ids(meta):
            if src in self.by_id and self.shown(src):
                return self.label(src, 24)
        return ""

    # review

    def review_items(self) -> list[tuple[str, dict]]:
        """依据变了的 as breath lists it, each with its card key (computed once a call)."""
        if self._review is None:
            items = _I.block(self.buckets, self.registry, scope=self.scope)
            self._review = [(self.key(it["id"]), it) for it in items
                            if it["id"] in self.by_id and it["id"] not in self.core_ids]
        return self._review

    def review_cards(self, baseline: set) -> list[Card]:
        out = []
        for key, it in self.review_items():
            if key in baseline:
                continue
            why: list[str] = []
            if it["edited"]:
                why.append("人在面板上改过")
            for r in it["overturned"]:
                why.append(f"它站着的 {short_id(r['of'])} 被 {short_id(r['by'])} 推翻了")
            for r in it["revised"]:
                why.append(f"来源 {r['source']} 出了新版本")
            for r in it["restored"]:
                why.append(f"来源 {r['source']} 撤回或删除过、现在恢复了，这条从那上面派生、"
                           "等你看过")
            if it["failed"]:
                failed = "、".join(f"{r['source']} {_I.state_word(r['state'])}" for r in it["failed"])
                why.append(f"依据 {failed}，正文不给了")
                act = ("只凭还剩的来源重写（regrow），或者收起来 trace(delete=True)"
                       if it["remaining"] else "一条来源都不剩，只能收起来 trace(delete=True)")
            else:
                act = (f'重写 regrow／收起来 trace(delete=True)／'
                       f'照留 trace(bucket_id="{it["short"]}", invalidation="confirmed")')
            head = it["text"][:40] if it["text"] is not None else "（正文不给）"
            text = f"【依据变了】{head}：{'；'.join(why)}——{act} ({it['short']})"
            out.append(Card(REVIEW, key, it["id"], text, "review"))
        return out

    # names

    def name_cards(self, msg: _Message) -> list[Card]:
        records = _names.load_names_table()
        if not records:
            return []
        own = own_names()
        picked = pick_names(msg, table_spellings())
        out = []
        for name, spelling in picked.items():
            rec = records[name]
            spellings = [name, *rec.aliases]
            if _norm(name) in own or any(len(_norm(s)) >= 2 and _norm(s) in self.facts
                                         for s in spellings):
                continue
            related = [b for b in self.subjects.get(name, ())
                       if b in self.by_id and _V.on_timeline(self.by_id[b][0], self.scope)]
            count = f"（相关 {len(related)} 条）" if related else ""
            card = self._card_entry(name)
            if card is not None:
                text = f"【相关名字】{name}：{self.label(card)}{count} ({short_id(card)})"
                out.append(Card(NAME, self.key(card), card, text, spelling, (0, name)))
                continue
            where = self._where(rec)
            if self.scope is not None and not related:
                continue            # nothing this request may read says anything about it
            if not where and not related:
                continue
            fp = hashlib.sha256(json.dumps(
                [name, list(rec.aliases), rec.instance_of, list(rec.present_in),
                 list(rec.member_of)], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
            text = f"【相关名字】{name}：{where or '名字表里有，还没有卡'}{count}"
            out.append(Card(NAME_BARE, f"name:{name}@{fp}", "", text, spelling, (1, name)))
        out.sort(key=lambda c: c.sort)
        return out

    def _card_entry(self, name: str) -> Optional[str]:
        live = [b for b in self.cards.get(name, ())
                if b in self.by_id and b not in self.core_ids
                and not is_closed(self.by_id[b][0]) and self.shown(b)]
        live.sort(key=lambda b: str(self.by_id[b][0].get("created") or ""), reverse=True)
        return live[0] if live else None

    @staticmethod
    def _where(rec) -> str:
        bits = []
        if rec.present_in:
            bits.append("只在" + "、".join(f"《{p}》" for p in rec.present_in) + "里出现过")
        if rec.member_of:
            bits.append("是" + "、".join(rec.member_of) + "的成员")
        if rec.instance_of and rec.instance_of != "人" and not bits:
            bits.append(f"是一个{rec.instance_of}")
        return "；".join(bits)


# ── the call ─────────────────────────────────────────────────────────────────

async def cue(store, *, text: str, window: str, turn: str, host: str, scope=None,
              now: Optional[datetime] = None, limit: int = CARD_LIMIT) -> dict:
    """The cards for one message: {window, turn, cards: [{card, kind, id, short, why,
    text}], text}. Records the window's opening, the offer and the usage log."""
    now = now or _w.now()
    buckets = await store.list_all(include_archive=False)
    lib = _Library(buckets, getattr(store, "sources", None), scope, now)
    ledger = store.cues
    win, _new = ledger.open_window(host, window,
                                   lambda: [k for k, _it in lib.review_items()])
    msg = _Message(text)
    candidates = (lib.due_cards(win.opened) + lib.cue_cards(msg)
                  + lib.review_cards(win.baseline) + lib.name_cards(msg))
    candidates.sort(key=lambda c: _RANK[c.kind])     # stable: each list keeps its order
    prior = win.offers.get(turn)
    if prior is not None:
        # A retry of this turn: the cards it was answered with, in that order, each
        # produced again just now — under this request's scope, against the entry as it
        # is now. A card whose entry was withdrawn, revised (a new version is a new key) or
        # is out of scope is not produced again, and is dropped. Nothing new is added.
        fresh = {c.key: c for c in candidates}
        chosen = [fresh[c["card"]] for c in prior if c["card"] in fresh]
        cards = [c.public() for c in chosen]
        ledger.offer(host, window, turn, cards, retry=True)
        return {"window": window, "turn": turn, "cards": cards,
                "text": "\n".join(c.text for c in chosen)}
    chosen = []
    used: set[str] = set()
    for c in candidates:
        if len(chosen) >= limit:
            break
        if c.key in win.delivered or (c.id and c.id in used):
            continue
        chosen.append(c)
        if c.id:
            used.add(c.id)
    cards = [c.public() for c in chosen]
    ledger.offer(host, window, turn, cards)
    usage = getattr(store, "usage", None)
    if usage is not None:
        for kind in CARD_KINDS:
            ids = [c.id for c in chosen if c.kind == kind and c.id]
            if ids:
                usage.record(_usage.SHOWN, ids, f"cue.{kind}")
    return {"window": window, "turn": turn, "cards": cards,
            "text": "\n".join(c.text for c in chosen)}


# ── the routes' bodies ───────────────────────────────────────────────────────

def _string(body: dict, key: str, *, required: bool = True, limit: int = ID_MAX,
            empty_ok: bool = False) -> Optional[str]:
    if key not in body or body[key] is None:
        if required:
            raise CueRequestError(f"{key} is required")
        return None
    v = body[key]
    if not isinstance(v, str):
        raise CueRequestError(f"{key} must be a string")
    if limit == ID_MAX:
        v = v.strip()
        if any(ord(c) < 32 for c in v):
            raise CueRequestError(f"{key} must not hold control characters")
    if not v and not empty_ok:
        raise CueRequestError(f"{key} must not be empty")
    if len(v) > limit:
        raise CueRequestError(f"{key} is longer than {limit} characters")
    return v


def _strings(body: dict, key: str) -> Optional[list[str]]:
    if key not in body or body[key] is None:
        return None
    v = body[key]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise CueRequestError(f"{key} must be a list of non-empty strings")
    if len(v) > LIST_MAX:
        raise CueRequestError(f"{key} holds more than {LIST_MAX} items")
    if any(len(x) > ID_MAX for x in v):
        raise CueRequestError(f"an item of {key} is longer than {ID_MAX} characters")
    return [x.strip() for x in v]


def _host_of(req) -> Optional[str]:
    """Whose ledger: the calling host, or the panel for a logged-in browser. None when the
    request names no host it could be keyed by."""
    if req is None:
        return PANEL_HOST
    if req.refusal == _scope.NO_HOST:
        return None
    return req.host.name if req.host is not None else PANEL_HOST


def _refused(req) -> Optional[tuple[int, dict]]:
    if req is None or not req.refused:
        return None
    line = req.first_line()
    return (400 if req.refusal == _scope.MALFORMED else 403), {"error": line, "scope": line}


async def scope_view(store, req):
    """The request's read scope over this library, or None when nothing is filtered."""
    if req is None or req.whole_library:
        return None
    metas: dict = {}
    for b in await store.list_all(include_archive=True):
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if bid:
            metas[bid] = meta
    return _scope.ScopeView(req, metas, getattr(store, "sources", None))


async def handle_cue(store, body, req, *, now: Optional[datetime] = None) -> tuple[int, dict]:
    """`POST /api/v2/cue`: {text, window, turn} -> (status, reply). A refused request is
    refused (the same as every read); otherwise 200 with the cards and the scope line."""
    refused = _refused(req)
    if refused is not None:
        return refused
    host = _host_of(req)
    try:
        if not isinstance(body, dict):
            raise CueRequestError("the body must be a JSON object")
        text = _string(body, "text", limit=TEXT_MAX, empty_ok=True)
        window = _string(body, "window")
        turn = _string(body, "turn")
    except CueRequestError as e:
        return 400, {"error": str(e)}
    scope = await scope_view(store, req)
    out = await cue(store, text=text, window=window, turn=turn, host=host, scope=scope, now=now)
    out["scope"] = req.first_line() if req is not None else ""
    return 200, out


async def handle_delivered(store, body, req) -> tuple[int, dict]:
    """`POST /api/v2/cue/delivered`: {window, turn} and/or {window, cards: [keys]} — the
    cards actually placed in the model's input: `turn` for every card of that turn's
    answer (only when all of them were placed), `cards` for exactly those (a partial
    placement). -> {window, delivered, unknown}. Reads no memory, so a Loci-Scope is not
    needed; the host's credential keys the ledger."""
    host = _host_of(req)
    if host is None:
        return 403, {"error": req.first_line()}
    try:
        if not isinstance(body, dict):
            raise CueRequestError("the body must be a JSON object")
        window = _string(body, "window")
        turn = _string(body, "turn", required=False)
        cards = _strings(body, "cards")
        if turn is None and not cards:
            raise CueRequestError("say which cards: turn, cards, or both")
    except CueRequestError as e:
        return 400, {"error": str(e)}
    done, unknown = store.cues.deliver(host, window, turn=turn, cards=cards)
    return 200, {"window": window, "delivered": done, "unknown": unknown}


async def handle_dropped(store, body, req) -> tuple[int, dict]:
    """`POST /api/v2/cue/dropped`: {window, cards: [keys]} / {window, turns: [ids]} /
    {window, all: true} — cards no longer in the model's input (the window was compacted,
    or cleared). -> {window, dropped, cleared}. Reads no memory; the credential keys it."""
    host = _host_of(req)
    if host is None:
        return 403, {"error": req.first_line()}
    try:
        if not isinstance(body, dict):
            raise CueRequestError("the body must be a JSON object")
        window = _string(body, "window")
        cards = _strings(body, "cards")
        turns = _strings(body, "turns")
        everything = body.get("all", False)
        if not isinstance(everything, bool):
            raise CueRequestError("all must be true or false")
        if not everything and not cards and not turns:
            raise CueRequestError("say which cards: cards, turns, or all")
    except CueRequestError as e:
        return 400, {"error": str(e)}
    gone = store.cues.drop(host, window, cards=cards, turns=turns, everything=everything)
    return 200, {"window": window, "dropped": gone, "cleared": everything}
