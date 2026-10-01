"""
========================================
core/_reconsolidation.py — 回望: a new entry runs into an old view
========================================

Plan part three, one. When grow or regrow writes something that touches a view already in
the library, the write's own return says so — 「撞上了旧看法：『……』，要看一眼吗」 — and the
model decides whether to keep the view, add a line, change it (regrow) or leave it for now.
Nothing is changed here, nothing is queued and nothing runs later: the return is the only
place this is said, computed before it is handed back.

------------------------------------------------------------
Two ways to run into an old view
------------------------------------------------------------
  name     the new body names something that has a card (`card_of`, a MIND entry): the
           card is the old view. The new entry's subjects are not backfilled yet when the
           return is written, so the body itself is matched against the names table and
           the card names, by the strong reminder's rules (core/_cue.pick_names): a name
           shared by two entries fires for neither, Latin names whole-word, CJK names on
           word edges. The AI's and the owner's own names are not matched.
  meaning  the new body's vector is at least SIMILARITY_LINE from an old MIND entry's
           (events and thoughts alike are compared, only against MIND). The vector call
           is bounded by MEANING_BUDGET_SECONDS; no vectors, a failed call or a call that
           runs out of time gives no meaning hits and says nothing — the write itself never
           waits on it beyond that and never fails because of it.

------------------------------------------------------------
What is not mentioned
------------------------------------------------------------
  · more than LIMIT views: names first (exact, cheap), then meaning by similarity
  · a view in the new entry's own lineage: what it stands on (its `from`, layer by layer),
    the versions it replaces, and for a new version what grew out of the old one
  · a view the gate keeps off (core/visibility, road `reconsolidation`, under the caller's
    read scope): an old version, a deliberate dont_surface, a covered view, one a hold is
    hung on, one out of scope
  · a pinned rule or the name page: breath puts those in front of the model already

Exports: LIMIT · SIMILARITY_LINE · MEANING_BUDGET_SECONDS · NAME · MEANING · ROAD · Hit ·
         lineage() · look_back() · render() · record_shown()
========================================
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from utils import WAS_REVISION_OF, parse_bool, prov_targets, read_from_ids, read_prov

from . import _cue
from . import _holds as _H
from . import _when as _w
from . import visibility as _V
from ._rooms import is_mind_room
from .profile import _PROFILE_TAG, entry_label, short_id

logger = logging.getLogger("loci_brain.reconsolidation")

LIMIT = 2
# The same line as the panel's similarity page elbow and the old "possibly the same
# thing" hint: a pairwise scan of the library puts the elbow at 0.80.
SIMILARITY_LINE = 0.80
# How long the write's return may wait for the vector of the new body. Ollama answers a
# loaded model in well under this; a cold model or a dead backend costs the meaning hits of
# this one write, not the write.
MEANING_BUDGET_SECONDS = 2.0
# How many neighbours the vector search returns before lineage and the gate thin them.
_SEARCH_TOP_K = 8
# How far a lineage walk goes; a prov loop or a very long chain ends here.
_LINEAGE_MAX = 512

NAME, MEANING = "name", "meaning"
ROAD = "write.reconsolidation"      # the usage log's road for what a return showed


@dataclass
class Hit:
    id: str
    how: str            # NAME | MEANING
    why: str            # the spelling that fired, or the similarity as text
    score: float = 0.0
    label: str = ""     # what the line shows of the view (profile.entry_label)


def _meta(b) -> dict:
    return (b or {}).get("metadata") or {}


def _bid(b) -> str:
    return str(_meta(b).get("id") or (b or {}).get("id") or "")


def lineage(start: Iterable[str], buckets: list, *, down_from: Iterable[str] = ()) -> set[str]:
    """The ids in an entry's own lineage: `start` and everything it stands on, layer by layer
    (its library sources and the versions it replaces), plus everything that grew out of
    `down_from` (a new version passes the version it replaces)."""
    by_id = {_bid(b): _meta(b) for b in buckets if _bid(b)}
    up: dict[str, list[str]] = {}
    down: dict[str, list[str]] = {}
    for bid, m in by_id.items():
        parents = list(read_from_ids(m))
        parents += prov_targets(read_prov(m), rels=(WAS_REVISION_OF,))
        prev = str(m.get("supersedes") or "").strip()
        if prev:
            parents.append(prev)
        up[bid] = parents
        for p in read_from_ids(m):
            down.setdefault(p, []).append(bid)
    seen: set[str] = set()

    def walk(frontier: list[str], edges: dict[str, list[str]]) -> None:
        while frontier and len(seen) < _LINEAGE_MAX:
            nxt: list[str] = []
            for bid in frontier:
                for other in edges.get(bid, ()):
                    if other and other not in seen:
                        seen.add(other)
                        nxt.append(other)
            frontier = nxt

    first = [str(s) for s in start if s]
    seen.update(first)
    walk(first, up)
    roots = [str(s) for s in down_from if s]
    seen.update(roots)
    walk(roots, down)
    return seen


class _Views:
    """The old views one write may run into, from the library as it stood before the write."""

    def __init__(self, buckets: list, scope, now: datetime, skip: set[str]):
        self.by_id: dict[str, tuple[dict, str]] = {}
        self.cards: dict[str, list[str]] = {}
        holds = _H.hold_index(buckets)
        for b in buckets:
            bid = _bid(b)
            m = _meta(b)
            if not bid or bid in skip or not is_mind_room(m.get("room")):
                continue
            if parse_bool(m.get("pinned"), default=False) \
                    or _PROFILE_TAG in [str(t) for t in m.get("tags") or []]:
                continue
            if not _V.visible_for(m, scope, road=_V.RECONSOLIDATION, now=now, holds=holds):
                continue
            self.by_id[bid] = (m, str((b or {}).get("content") or ""))
            card = str(m.get("card_of") or "").strip()
            if card:
                self.cards.setdefault(card, []).append(bid)

    def card_of_name(self, name: str) -> Optional[str]:
        """The newest visible card filed under `name` (a card filed under a spelling that is
        now an alias counts for the name it stands for)."""
        from tools import _subjects as _S

        want = name.lower()
        found = [bid for card, ids in self.cards.items()
                 if (_S.name_key(card) or card).lower() == want for bid in ids]
        found.sort(key=lambda b: str(self.by_id[b][0].get("created") or ""), reverse=True)
        return found[0] if found else None


def _name_hits(views: _Views, text: str) -> list[Hit]:
    if not views.cards:
        return []
    from tools import _subjects as _S

    own = _cue.own_names()
    card_names = [(_S.name_key(c) or c) for c in views.cards]
    picked = _cue.pick_names(_cue.Message(text), _cue.table_spellings(tuple(card_names)))
    hits: list[Hit] = []
    for name, spelling in picked.items():
        if _cue.norm(name) in own:
            continue
        card = views.card_of_name(name)
        if card:
            hits.append(Hit(card, NAME, spelling))
    return hits


async def _meaning_hits(store, views: _Views, text: str) -> list[Hit]:
    engine = getattr(store, "embedding_engine", None)
    if engine is None or not getattr(engine, "enabled", False) or not views.by_id:
        return []
    try:
        pairs = await asyncio.wait_for(
            engine.search_similar_strict(text, top_k=_SEARCH_TOP_K, among=list(views.by_id)),
            timeout=MEANING_BUDGET_SECONDS)
    except Exception as e:  # noqa: BLE001 - no vectors means no meaning hits, never a failed write
        logger.info("look-back: meaning hits skipped (%s: %s)", type(e).__name__, e)
        return []
    return [Hit(str(bid), MEANING, f"{float(s):.2f}", float(s))
            for bid, s in pairs if float(s) >= SIMILARITY_LINE and str(bid) in views.by_id]


async def look_back(store, buckets: list, writes: list[tuple[str, str]], *, scope=None,
                    now: Optional[datetime] = None, skip: Iterable[str] = ()) -> list[Hit]:
    """The old views these writes run into, at most LIMIT. `buckets` is the library as it
    stood before the write (listing it after would re-read every file: the write cleared the
    store's cache). `writes` are (new id, body); `skip` is the writes' lineage."""
    if not writes:
        return []
    now = now or _w.now()
    views = _Views(buckets, scope, now, set(skip) | {bid for bid, _ in writes})
    if not views.by_id:
        return []
    names: list[Hit] = []
    for _new_id, text in writes:
        names += _name_hits(views, text)
    meanings = await asyncio.gather(*(_meaning_hits(store, views, text)
                                      for _new_id, text in writes))
    ranked = names + sorted((h for hs in meanings for h in hs), key=lambda h: -h.score)
    out: list[Hit] = []
    for h in ranked:
        if h.id not in {o.id for o in out}:
            out.append(h)
        if len(out) >= LIMIT:
            break
    for h in out:
        h.label = entry_label(*views.by_id[h.id])
    return out


def render(hits: list[Hit]) -> list[str]:
    """The lines a write's return carries for these hits."""
    lines = []
    for h in hits:
        why = (f"提到了「{h.why}」，这是它的名字卡" if h.how == NAME else "意思很近")
        lines.append(f"🔁 撞上了旧看法：『{h.label}』（{short_id(h.id)}，{why}），"
                     f"要看一眼吗？recall(query=\"{short_id(h.id)}\")")
    if lines:
        lines.append("看完：没变就不用管；要补一句 regrow(bucket_id=那条, mode=\"supplement\")，"
                     "说错了 regrow(bucket_id=那条, mode=\"overturn\")；先放着也行。")
    return lines


def record_shown(store, hits: list[Hit]) -> None:
    """The usage log's `shown` line for the views a return named."""
    usage = getattr(store, "usage", None)
    if usage is None or not hits:
        return
    try:
        from . import _usage
        usage.record(_usage.SHOWN, [h.id for h in hits], ROAD)
    except Exception as e:  # noqa: BLE001 - the log is a record, not part of the write
        logger.warning("look-back: usage log not written: %s", e)
