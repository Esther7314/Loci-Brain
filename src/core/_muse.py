# -*- coding: utf-8 -*-
"""
========================================
core/_muse.py — the threshold engine behind muse
========================================

------------------------------------------------------------
🔴 The governing rule — this whole version was rewritten around it
------------------------------------------------------------
> # The system may not point at things on a guess. Every pointer must carry a trace we
> # left ourselves.

**Traces, ordered by hardness**:
  `tags` are words we wrote down at the moment of storing (guaranteed to appear
         literally in the body)
  `v/a`  are coordinates I assigned by hand
  `from` is a chain I linked by hand
  — **vectors are fit for the open audition only** (a fallback, always ranked last,
    always labelled as such).

📌 Where the previous version went wrong: gist proposals were built as **semantic
   clustering**, so a theme that recurs every week got strung into a month-long band
   that no "cover a stretch of days" gesture could match. The clustering itself was
   fine; **the error was passing a through-line off as a stretch of days.** Pulled
   apart, those clusters were **through-lines** — and a through-line already has a
   viewer, `recall(query=)`: search for the thing and the whole run of memories about
   it comes up. -> **Through-lines were cut; muse does not do that part.**

------------------------------------------------------------
🔴 The constitution — first, not as a footnote
------------------------------------------------------------
> # The system only retrieves and arranges. The writing is always mine.

There is **no LLM call path in this file and there may never be one**: it does not write
summaries, does not suggest phrasings, does not offer sample sentences. It does exactly
three things: **find it · put it in front of me · then shut up.**
**It may not compute new vectors either.** It only reads what is already in
`embeddings.db` (`mode=ro`); an entry without a vector simply sits out the semantic layer
and is honestly counted as "scattered". Quietly generating one more vector would be
stealing the backfill job out of deepseek's and ollama's hands.

------------------------------------------------------------
Two sides, two sets of gestures (separate pools)
------------------------------------------------------------
**The event side = gist proposals = three gestures, every one carrying evidence**
(`kind="gist"`)

| gesture | what the trace is | what it points at |
|---|---|---|
| **word burst** `word_burst()` | `tags` (words we wrote down at storage time) | a stretch: word W appears 12 times between 08-09 and 08-12 and only twice outside that window |
| **composition drift** `composition_drift()` | the centroid of existing vectors (**a ruler only, never a reason**) | a **boundary**: memories look different before and after 07-04 |
| **blank ledger** `blank_ledger()` | **the range of a period** (a circle I drew by hand) | a stretch of days no period covers |

🔴 **The test for "does it have a name yet" is range coverage**, not `covered_by`: an
   event **whose date falls inside the range of any living period** has a name.
   A period never writes `covered_by` (it is a pure naming layer storing only
   a name and a range), so asking a field cannot answer the question — it is
   **computed on the spot** (`era_spans()` + `mark_named()`). Two things come free:
   a backfilled entry landing inside an old range **acquires its name automatically**
   (no going back to patch a roster), and moving a period's boundary (regrow with a new
   `when`) **changes the next muse immediately.**

🔴 **The blank ledger shuts up entirely when the store holds no through-line at all.**
   The failure it guards against is the engine announcing that nothing all year has a
   name — **that must not happen.** Without a map, "where is nothing covered" is a fake
   question. The first version of the map is written by hand (narrate first, point
   later), and the word-burst gesture can help pre-cut the segments.

**The mind side = musing = three evidence tiers, ordered by how hard the trace is**
(`kind="muse"`)

  ① **v/a neighbourhood shelving** `make_shelves()` — coordinates I assigned by hand.
     Radius `va_radius`; one shelf = one ball of radius r (**not single linkage**:
     single linkage follows density and strings the entire coordinate plane into one
     piece, which is no longer "this one square").
     🔴 **Anything exactly equal to `(0.5, 0.3)` is kept off the shelves** — that is an
     old default value, not a feeling. The 115 legacy mind entries are waiting to have
     their coordinates reassigned by hand, and **this gate stays up until that is done**
     (simply avoid that exact point when reassigning).
  ② **from-chains within a shelf** `cluster_shelf()` — chains I linked by hand, **the
     hardest evidence there is**, and labelled separately ("three of these grew out of
     the same night").
  ③ **semantics within a shelf** — the open-audition fallback; anything pulled in this
     way is labelled separately and reported with its lowest similarity.

🔴 **Time is not evidence here**: an insight does not answer to the calendar. So the mind
   side has no "it stopped" gate, and the entry lines in step two give **v/a coordinates
   rather than dates**.
🔪 **Concept-word tags: shelved, not built** (six were tried by hand and judged wrong —
   the v/a experiment beat it outright).
🔪 `seed` (the thirteen emotional roots) was cut; roots take no part.

------------------------------------------------------------
The two gates that were kept (skeleton borrowed from consolidation-draft; the pen was not)
------------------------------------------------------------
| gate | rule | what happens without it |
|---|---|---|
| **cooldown** | `created` less than `cooldown_days` ago -> stays out of the pool | something that just happened is instantly pronounced "this is simply who you are" |
| **rejection count** | **the same group**, once I have said "these are not the same thing", is never raised again | "the judgement is left to me" degrades into "being pestered by the same thing once a week" |

"It stopped" (`stopped_days`) **is kept on the event side only**: while a stretch is
still happening, I cannot say what it is.

------------------------------------------------------------
🔴 The factory values are placeholders, **to be settled against real data**
------------------------------------------------------------
Run `scripts/muse_dryrun.py` first: **the word-burst candidate table, the v/a radius
sweep, and the drift window table.** Read all three side by side before writing anything
into the `muse:` section of `config.yaml`. The copy in code is only the fallback.

------------------------------------------------------------
Exports
------------------------------------------------------------
MUSE_DEFAULTS · POOL_SPECS · muse_config()
Item · Shelf · Cluster · Finger · item_of() · in_pool() · pool_of() · read_vectors()
make_shelves() · cluster_shelf() · daydream()                            <- the mind side
word_burst() · composition_drift() · blank_ledger() · era_count()        <- the event side
era_spans() · is_named() · mark_named()      <- computing "does it have a name" on the spot
rejection_key() · load_rejected() · is_rejected() · record_rejection()
load_records() (async) · propose_mind() · propose_gist()
(The dream path only borrows `pool_of(recs, "dream", ...)` to pick ingredients; the
 weaving lives in `tools/_dream.py` — no LLM call may ever appear in this file, and the
 dream is the one exception, which is why it lives elsewhere.)
========================================
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import _when as _w
from . import runtime as rt
from ._bigevent import BIGEVENT_TAG
from ._fold import GIST_TAG, is_covered
from ._rooms import is_event_room, is_mind_room
from . import visibility as _V    # the one gate: what may be put in front of the model

# ============================================================
# Thresholds — the factory values are placeholders; the real ones come from the three
# muse_dryrun tables
# ============================================================
MUSE_DEFAULTS: dict = {
    # ---- shared by both sides ----
    "min_cluster": 3,        # how many entries a cluster needs before covering it is worth it
    "cooldown_days": 7,      # cooldown: created less than N days ago -> stays out of the pool
    "cooldown_clock": "created",   # "created" (default) / "when": which clock the cooldown reads
    "stopped_days": 4,       # stopped: **event side only**; the last entry of a stretch is more than N days old
    "reject_limit": 1,       # how many rejections of the same group before it is never raised again
    "max_clusters": 8,       # mind side: most clusters laid out at once (laying out too many is the same as laying out none)
    "max_fingers": 6,        # event side: most entries laid out **per gesture**
    "emotion_line": 0.15,    # dreams: |v-0.5| or |a-0.5| must exceed this to count as "carrying emotion"
    # ---- mind side: the three evidence tiers ----
    "va_radius": 0.10,       # ① v/a neighbourhood shelving: one shelf = a ball of radius r (Euclidean)
    "va_default_v": 0.5,     # 🔴 the old default coordinate; anything exactly equal to it stays off the shelves
    "va_default_a": 0.3,
    "sim_line": 0.76,        # ③ the open-audition line for semantics within a shelf (0.70 strings our store into one piece)
    # ---- event side: the three gestures ----
    "burst_window_days": 7,      # word burst: window length
    "burst_min_hits": 5,         # word burst: minimum occurrences inside the window
    "burst_outside_ratio": 0.5,  # word burst: ceiling on outside / inside occurrences (smaller = burstier)
    "drift_window_days": 7,      # composition drift: length of the adjacent time windows
    "drift_line": 0.25,          # composition drift: centroid cosine distance above this counts as a big step
    "drift_min_items": 3,        # composition drift: minimum entries in a window before a centroid means anything
    "blank_min_items": 20,       # blank ledger: how many uncovered entries a stretch needs before it is worth noting
    "blank_gap_days": 3,         # blank ledger: a gap this long breaks the stretch in two
    # ---- The tipping point for "is it time to muse" (read by `/api/muse/pending`;
    #      **whether the user is idle, and how to nudge, belongs to the host**) ----
    # 🔴 Loci only exposes the query (count + age). What counts as idle, whether to nudge,
    #    and whether to stay quiet at night are the gateway's business.
    "poke_min_clusters": 2,      # how many clusters/gestures must pile up before a nudge is worth it
    "poke_min_age_days": 3,      # how long the oldest one must have hung before a nudge is worth it
}

# Three pool configurations — **one engine, three ways of feeding it**
# 🔴 The pools are separate: dreams eat events, musing eats minds. They are not the same
#    material.
POOL_SPECS: dict[str, dict] = {
    # Dreams: only the ingredient end differs; the weaving is untouched. **No cooldown** —
    # day residue is the core ingredient of a dream, and holding entries for 7 days would
    # starve the most recent tier entirely. ⏳ If a cooldown is ever wanted here, flip this
    # to True.
    "dream": {"支": "EVENT", "情绪": True, "没消化": True,
              "冷却期": False, "没被盖过": True},
    # Musing: eats scattered minds. **Not covered** is a hard gate (anything covered stops
    # surfacing on its own).
    "muse":  {"支": "MIND", "情绪": False, "没消化": False,
              "冷却期": True, "没被盖过": True},
    # gist proposals: eat events, and **covered ones join the pool too** — how many times a
    # word burst is a property of the word itself, and being covered does not change it.
    # Whether something "has a name" is each gesture's own business:
    # 🔴 the blank ledger looks only at the unnamed; the word burst requires unnamed entries
    #    still in the stretch; drift is only a ruler and has nothing to do with names.
    # ⚠️ "Has a name" now means **range coverage** (a period's range, computed on the spot),
    #    not the `covered_by` field. The `没被盖过: False` in this row is kept because it
    #    blocks a real cover (a mind snapshot, or an event corrected by a new version);
    #    periods write none of those any more, so it is inert here, and harmless.
    "gist":  {"支": "EVENT", "情绪": False, "没消化": False,
              "冷却期": True, "没被盖过": False},
}


def muse_config(cfg: dict | None = None) -> dict:
    """Factory values plus the `muse:` section of config.yaml. Config is the single source
    of truth; the copy in code is only the fallback."""
    out = dict(MUSE_DEFAULTS)
    src = (cfg or {}).get("muse") if isinstance(cfg, dict) else None
    if isinstance(src, dict):
        for k, v in src.items():
            if k in out and v is not None:
                out[k] = type(out[k])(v) if not isinstance(out[k], str) else str(v)
    return out


# ============================================================
# What one memory looks like to the engine
# ============================================================
@dataclass
class Item:
    id: str
    room: str
    ts: datetime | None          # calendar coordinate: when || created (same definition as recall's timeline)
    created: datetime | None     # the moment it was written down
    v: float
    a: float
    tags: list[str]
    text: str
    from_ids: list[str] = field(default_factory=list)   # a chain I linked by hand
    covered: bool = False        # explicitly **named** in some gist's cover (the snapshot half: a mind merge, or an event corrected by a new version)
    # 🔴 **Does it have a name yet**: its date falls inside the range of a living period
    #    (computed on the spot by `mark_named()`).
    #    Deliberately a separate field from `covered`, because they are two different
    #    things: one means "it has been suppressed", the other means "it has been named".
    named: bool = False


@dataclass
class Shelf:
    """One square of the v/a neighbourhood. **The centre of a shelf is a coordinate, not a
    label** — it is the position I put there myself."""
    v: float
    a: float
    items: list[Item]

    def __len__(self) -> int:
        return len(self.items)


@dataclass
class Cluster:
    """One cluster laid out by musing. All three kinds of evidence are kept separately, and
    not one of them may be omitted when it is laid out."""
    ids: list[str]
    items: list[Item]
    shelf_v: float
    shelf_a: float
    from_core: list[str] = field(default_factory=list)   # pulled together by from-chains
    shared_from: list[str] = field(default_factory=list)       # the source(s) they have in common
    semantic_add: list[str] = field(default_factory=list)     # brought in by the open audition
    min_sim: float = 0.0

    def __len__(self) -> int:
        return len(self.ids)


@dataclass
class Finger:
    """One gesture laid out by the event side. Every word on its `evidence` line has to be
    a trace we left ourselves."""
    name: str
    ids: list[str]
    items: list[Item]
    start: datetime | None = None
    end: datetime | None = None
    boundary: datetime | None = None      # composition drift only: it offers a boundary and never draws the stretch
    evidence: str = ""
    next_step: str = ""
    score: float = 0.0                   # for ordering (hits / drift / count); never displayed

    def __len__(self) -> int:
        return len(self.ids)


def _f(x, d: float) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# Machine-voiced tags: things like `相似认知:e9854d`, `疑似同件:77643f`, `aspect:patterns`.
# Caught once in the wild, with `aspect:patterns` leaking onto a cluster's "face":
# **the face is only fit for scene words in human language.**
# A tag the machine assigned to itself **is not a trace we left**, and using it as evidence
# is the system citing itself.
_MACHINE_TAG_RE = re.compile(r"^[^:：]{1,12}[:：]")


def is_scene_word(tag: str) -> bool:
    t = str(tag or "").strip()
    return bool(t) and not t.startswith("__") and not _MACHINE_TAG_RE.match(t)


def item_of(meta: dict, text: str) -> Item | None:
    """One bucket -> an Item. Without an id it is not a memory anything can be proposed
    about, so None."""
    bid = str(meta.get("id") or "").strip()
    if not bid:
        return None
    try:
        from utils import read_from_ids
        froms = read_from_ids(meta)
    except Exception:                      # noqa: BLE001 - the engine must not die just because a chain would not read
        froms = []
    return Item(
        id=bid,
        room=str(meta.get("room") or ""),
        ts=(_w.parse_stamp(meta.get("when")) or _w.parse_stamp(meta.get("created"))),
        created=_w.parse_stamp(meta.get("created")),
        v=_f(meta.get("valence"), 0.5),
        a=_f(meta.get("arousal"), 0.5),
        tags=[str(t) for t in (meta.get("tags") or [])],
        text=str(text or ""),
        from_ids=froms,
        covered=is_covered(meta),
    )


def _is_utility_record(meta: dict) -> bool:
    """Periods and the note at the door are machinery, not memories anything can be
    proposed about.

    A gist is not machinery: it is a sentence I wrote over what it folds, and two gists
    saying the same thing are exactly what muse should put side by side (the merge is a
    new fold covering both). It stays out only while a higher layer covers it — then the
    one on top speaks for it, the same rule that keeps any covered entry from surfacing
    on its own (`_fold.is_covered`)."""
    tags = [str(t) for t in (meta.get("tags") or [])]
    if BIGEVENT_TAG in tags or "__档案事实__" in tags:
        return True
    return GIST_TAG in tags and is_covered(meta)


def in_pool(meta: dict, item: Item, kind: str, cfg: dict, now: datetime,
            digested_ids: set[str] | None = None) -> bool:
    """Does this belong in the `kind` pool?"""
    spec = POOL_SPECS[kind]

    # --- excluded from every pool ---
    if _is_utility_record(meta):
        return False
    # Whether the entry may surface at all is the gate's `muse` road (an archived or
    # deleted one never does). Covers are this pool's own spec below.
    if not _V.visible_for(meta, road=_V.MUSE):
        return False
    if meta.get("pinned") or meta.get("protected"):
        return False                            # a rule is never something to be summarised
    if not item.text.strip():
        return False
    if spec["没被盖过"] and item.covered:        # `_fold.is_covered` is the source of this rule
        return False

    # --- the pools part company ---
    if spec["支"] == "MIND" and not is_mind_room(item.room):
        return False
    if spec["支"] == "EVENT" and not is_event_room(item.room):
        return False

    # --- cooldown: something just written down does not get summarised ---
    if spec["冷却期"]:
        clock = item.created if str(cfg["cooldown_clock"]) == "created" else item.ts
        if clock is None:
            return False
        if (now - clock) < timedelta(days=float(cfg["cooldown_days"])):
            return False

    # --- dreams: carrying emotion, and not yet digested ---
    if spec["情绪"]:
        line = float(cfg["emotion_line"])
        if abs(item.v - 0.5) <= line and abs(item.a - 0.5) <= line:
            return False
    if spec["没消化"] and digested_ids is not None and item.id in digested_ids:
        return False

    return True


def pool_of(recs: list[tuple[dict, str]], kind: str, cfg: dict, now: datetime,
            digested: set[str] | None = None) -> list[Item]:
    out: list[Item] = []
    for meta, text in recs:
        it = item_of(meta, text)
        if it is None:
            continue
        if in_pool(meta, it, kind, cfg, now, digested):
            out.append(it)
    return out


# ============================================================
# Vectors: embeddings.db is read only; not one new vector is ever computed
# ============================================================
def _normalize(vec) -> list[float] | None:
    if not vec:
        return None
    n = math.sqrt(sum(x * x for x in vec))
    if not n:
        return None
    return [x / n for x in vec]


def read_vectors(db_path: str, ids: list[str], retries: int = 3) -> dict[str, list[float]]:
    """Fetch existing embeddings by id. 🔴 A `mode=ro` read-only connection — this file
    never writes to the vector store.

    ⚠️ A read-only connection meeting a **hot journal** (a vector write caught mid-flight)
    reports `attempt to write a readonly database`: SQLite wants to roll the journal back,
    and rolling back means writing. That is not "this entry has no vector", it is "it
    cannot be read at this instant", and the two **must never be fused into one**. Fused,
    muse would silently report the whole pool as "scattered" and nothing would show that
    anything had happened.
    So: back off and retry a few times, and if it still fails, **say so out loud** (in the
    log) and return whatever part is in hand.
    """
    out: dict[str, list[float]] = {}
    if not db_path or not os.path.exists(db_path) or not ids:
        return out
    want = list(dict.fromkeys(ids))
    for attempt in range(max(1, retries)):
        out = {}
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            for i in range(0, len(want), 400):
                chunk = want[i:i + 400]
                q = ("SELECT bucket_id, embedding FROM embeddings WHERE bucket_id IN ("
                     + ",".join("?" * len(chunk)) + ")")
                for bid, raw in conn.execute(q, chunk):
                    try:
                        vec = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(vec, list) and vec:
                        out[str(bid)] = vec
            return out
        except sqlite3.Error as e:
            if attempt == max(1, retries) - 1:
                try:
                    rt.logger.warning(
                        f"[muse] 读向量库失败（{e}）——这一趟的语义那一层等于没有，"
                        f"团会比平时少。**不是没有向量，是读不着。**")
                except Exception:      # noqa: BLE001 - the dry-run script has no runtime; do not crash a second time just to report an error
                    pass
                return out
            import time as _t
            _t.sleep(0.2 * (attempt + 1))
        finally:
            if conn is not None:
                conn.close()
    return out


def _cos(u: list[float], v: list[float]) -> float:
    """Cosine of two vectors that have **already been normalised**."""
    return sum(x * y for x, y in zip(u, v))


# ============================================================
# Mind side ① — v/a neighbourhood shelving (coordinates I assigned by hand)
# ============================================================
def make_shelves(items: list[Item], cfg: dict) -> tuple[list[Shelf], int]:
    """Shelve the v/a plane by radius r. Returns (the shelves, how many were held back for
    sitting on the old default coordinate).

    **One shelf = one ball of radius r**, not single linkage. Single linkage follows
    density and strings the whole coordinate plane into one piece (a radius of 0.10 is
    enough to connect 250 entries), and that is no longer "this one square", it is "every
    square".
    Greedy: each round picks the entry with **the most neighbours** as the shelf centre,
    takes everything within its radius, removes them, and goes again.
    Ties break on (v, a, id), so **two calls produce the same shelves** — there is no LLM
    here, and no randomness is allowed either.

    🔴 Anything exactly equal to `(va_default_v, va_default_a)` stays off the shelves
    entirely: that is an old default value, not a feeling.
    """
    r = float(cfg["va_radius"])
    dv, da = float(cfg["va_default_v"]), float(cfg["va_default_a"])

    live: list[Item] = []
    default_coords = 0
    for it in items:
        if abs(it.v - dv) < 1e-9 and abs(it.a - da) < 1e-9:
            default_coords += 1
            continue
        live.append(it)
    live.sort(key=lambda x: (x.v, x.a, x.id))

    n = len(live)
    neighbors: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        vi, ai = live[i].v, live[i].a
        for j in range(i + 1, n):
            if abs(live[j].v - vi) > r:          # already sorted by v: once it is past r, everything after is too
                break
            if (live[j].v - vi) ** 2 + (live[j].a - ai) ** 2 <= r * r:
                neighbors[i].add(j)
                neighbors[j].add(i)

    left = set(range(n))
    out: list[Shelf] = []
    while left:
        # sorted(left) rather than max(left): a set's iteration order must not decide the
        # shelf centre. `live` is already sorted by (v, a, id), so ascending index means a
        # fixed order, and max takes the first maximum -> **two calls give the same shelves**.
        center = max(sorted(left), key=lambda i: len(neighbors[i] & left))
        members = [live[i] for i in sorted((neighbors[center] & left) | {center})]
        left -= ({center} | neighbors[center])
        vs = [m.v for m in members]
        as_ = [m.a for m in members]
        out.append(Shelf(v=sum(vs) / len(vs), a=sum(as_) / len(as_), items=members))
    out.sort(key=lambda s: (-len(s.items), s.v, s.a))
    return out, default_coords


# ============================================================
# Mind side ②③ — from-chains within a shelf (hardest) -> semantics within a shelf (the
# open-audition fallback)
# ============================================================
def _from_edges(sh: Shelf) -> dict[str, set[str]]:
    """The from-relation between two entries on a shelf: they share a source, or one of
    them IS the other's source."""
    edges: dict[str, set[str]] = {it.id: set() for it in sh.items}
    for i, a in enumerate(sh.items):
        for b in sh.items[i + 1:]:
            shared = set(a.from_ids) & set(b.from_ids)
            chain = (b.id in a.from_ids) or (a.id in b.from_ids)
            if shared or chain:
                edges[a.id].add(b.id)
                edges[b.id].add(a.id)
    return edges


def cluster_shelf(sh: Shelf, vectors: dict[str, list[float]], cfg: dict) -> list[Cluster]:
    """One shelf -> several clusters. **from-chains first, semantics second**, with the two
    recorded separately in the evidence."""
    line = float(cfg["sim_line"])
    min_items = int(cfg["min_cluster"])
    by_id = {it.id: it for it in sh.items}
    normed = {bid: _normalize(vectors.get(bid)) for bid in by_id}

    # --- ② from-chains: union-find ---
    edges = _from_edges(sh)
    parent = {bid: bid for bid in by_id}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, bs in edges.items():
        for b in bs:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for bid in by_id:
        groups.setdefault(find(bid), []).append(bid)
    cores = [sorted(v) for v in groups.values() if len(v) >= 2]
    cores.sort(key=lambda g: (-len(g), g[0]))
    singles = sorted(bid for v in groups.values() if len(v) < 2 for bid in v)

    clusters: list[Cluster] = []

    def _make_cluster(ids_core, ids_add, lowest_sim):
        members = [by_id[b] for b in ids_core + ids_add]
        shared = set(by_id[ids_core[0]].from_ids) if ids_core else set()
        for b in ids_core[1:]:
            shared &= set(by_id[b].from_ids)
        return Cluster(ids=[m.id for m in members], items=members, shelf_v=sh.v, shelf_a=sh.a,
                       from_core=list(ids_core), shared_from=sorted(shared), semantic_add=list(ids_add),
                       min_sim=lowest_sim)

    # --- ③ semantics within the shelf: hang each singleton onto the from-cluster it is
    #     closest to (the open-audition fallback, labelled separately) ---
    used: set[str] = set()
    assigned: dict[int, list[str]] = {k: [] for k in range(len(cores))}
    lowest_sim: dict[int, float] = {k: 0.0 for k in range(len(cores))}
    for bid in singles:
        nv = normed.get(bid)
        if nv is None:
            continue
        # Hang it on **the single closest** from-cluster. `>` rather than `>=`: on a tie
        # the earlier one wins, otherwise the enumeration order of the cores would decide
        # membership — and then order would be doing the talking instead of evidence.
        best, score = -1, -1.0
        for k, g in enumerate(cores):
            for m in g:
                mv = normed.get(m)
                if mv is None:
                    continue
                s = _cos(nv, mv)
                if s >= line and s > score:
                    best, score = k, s
        if best >= 0:
            assigned[best].append(bid)
            lowest_sim[best] = score if not lowest_sim[best] else min(lowest_sim[best], score)
            used.add(bid)

    for k, g in enumerate(cores):
        clusters.append(_make_cluster(g, sorted(assigned[k]), lowest_sim[k]))

    # --- A shelf with not a single from-edge: the whole shelf goes through semantic single
    #     linkage (pure open audition; the evidence line says so plainly) ---
    left = [b for b in singles if b not in used and normed.get(b) is not None]
    if len(left) >= min_items:
        parent2 = {b: b for b in left}

        def find2(x):
            while parent2[x] != x:
                parent2[x] = parent2[parent2[x]]
                x = parent2[x]
            return x

        low = {b: 1.0 for b in left}
        for i, a in enumerate(left):
            for b in left[i + 1:]:
                s = _cos(normed[a], normed[b])
                if s >= line:
                    ra, rb = find2(a), find2(b)
                    if ra != rb:
                        parent2[ra] = rb
                    low[a] = min(low[a], s)
                    low[b] = min(low[b], s)
        groups2: dict[str, list[str]] = {}
        for b in left:
            groups2.setdefault(find2(b), []).append(b)
        for g in groups2.values():
            if len(g) < min_items:
                continue
            g = sorted(g)
            members = [by_id[b] for b in g]
            clusters.append(Cluster(ids=g, items=members, shelf_v=sh.v, shelf_a=sh.a,
                                    from_core=[], shared_from=[], semantic_add=g,
                                    min_sim=min(low[b] for b in g)))

    out = [t for t in clusters if len(t) >= min_items]
    for t in out:
        t.items.sort(key=lambda m: (m.id))
        t.ids = [m.id for m in t.items]
    return out


def daydream(items: list[Item], vectors: dict[str, list[float]], cfg: dict
             ) -> tuple[list[Cluster], int, int]:
    """A full pass of the mind side: shelve, then cluster within each shelf. Returns
    (clusters, how many are scattered, how many sit on the old default coordinate).

    Ordering: **anything with from-evidence comes first** (the harder trace), then larger
    before smaller. "These grew out of the same night" weighs far more than "these have
    similar vectors", and the order things are laid out in should say so.
    """
    shelves, default_coords = make_shelves(items, cfg)
    clusters: list[Cluster] = []
    clustered: set[str] = set()
    for sh in shelves:
        for t in cluster_shelf(sh, vectors, cfg):
            clusters.append(t)
            clustered.update(t.ids)
    scattered = len(items) - default_coords - len(clustered)
    clusters.sort(key=lambda t: (0 if t.from_core else 1, -len(t), t.shelf_v, t.shelf_a, t.ids[0]))
    return clusters, scattered, default_coords


# ============================================================
# Event side — three gestures, every one carrying evidence
# ============================================================
def _short_date(dt: datetime | None) -> str:
    return dt.strftime("%m-%d") if dt else "?"


def _full_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


def word_burst(items: list[Item], cfg: dict, now: datetime) -> list[Finger]:
    """**One scene word appearing densely across a run of days and sparsely outside it**
    -> propose that stretch.

    The trace is `tags`: **words we wrote down at the moment of storing** (which guarantees
    they appear literally in the body). Machine-assigned `xx:yy` tags do not count — using
    one as evidence is the system citing itself.

    The observation behind it: a word that appears on 08-09 and is gone by 08-12 has
    **the shape of a period**.
    """
    window = timedelta(days=float(cfg["burst_window_days"]))
    line = int(cfg["burst_min_hits"])
    ratio = float(cfg["burst_outside_ratio"])
    stopped = timedelta(days=float(cfg["stopped_days"]))

    by_word: dict[str, list[Item]] = {}
    for it in items:
        if it.ts is None:
            continue
        for t in set(it.tags):
            if is_scene_word(t):
                by_word.setdefault(t, []).append(it)

    dated = sorted([i for i in items if i.ts], key=lambda x: x.ts)
    out: list[Finger] = []
    for tag, occ in by_word.items():
        occ.sort(key=lambda x: x.ts)
        total = len(occ)
        if total < line:
            continue
        best = (0, 0, 0)          # (hits inside the window, start index, end index)
        j = 0
        for i in range(total):
            while j + 1 < total and occ[j + 1].ts - occ[i].ts <= window:
                j += 1
            if j < i:
                j = i
            n = j - i + 1
            if n > best[0]:
                best = (n, i, j)
        inside, i0, j0 = best
        if inside < line:
            continue
        outside = total - inside
        if outside > ratio * inside:
            continue                     # not sparse outside = this word was always there; not a burst
        start, end = occ[i0].ts, occ[j0].ts
        if (now - end) <= stopped:
            continue                     # still happening: I cannot say what it is yet
        in_span = [it for it in dated if start <= it.ts <= end]
        # "Unnamed" = the date falls inside no living period's range (set on the spot by `mark_named()`)
        unnamed = [it for it in in_span if not it.named]
        if not unnamed:
            continue                     # nothing unnamed is left in this stretch, so there is nothing to point at
        out.append(Finger(
            name="词爆发",
            ids=[it.id for it in unnamed], items=unnamed, start=start, end=end,
            evidence=(f"「{tag}」{_short_date(start)}~{_short_date(end)} 出现 {inside} 次"
                      f"（全库共 {total} 次，窗外 {outside} 次）· 段上 {len(unnamed)} 条没名字"),
            next_step=f'fold(when="{_full_date(start)}..{_full_date(end)}", text=我写的那句)',
            score=float(inside)))
    out.sort(key=lambda x: (-x.score, x.start or now, x.evidence))
    return out


def _window_index(dt: datetime, W: int) -> int:
    """The window number on a fixed calendar grid — it does not shift with the data, so a
    report and a live run always land on the same square."""
    return ((date(dt.year, dt.month, dt.day) - date(1970, 1, 1)).days) // W


def _window_start(k: int, W: int) -> datetime:
    """The start of a window. **This must go through `_when.parse_date`**: midnight of that
    local day, timezone-aware.
    A bare `datetime(...)` is naive and blows up the moment it is subtracted from `now()`.
    """
    d = date(1970, 1, 1) + timedelta(days=k * W)
    return _w.parse_date(d.isoformat())


def composition_drift(items: list[Item], vectors: dict[str, list[float]], cfg: dict,
                      now: datetime) -> list[Finger]:
    """**The vector centroid takes a big step between adjacent time windows** -> propose a
    **boundary** (a boundary only; it never draws the stretch).

    The observation it captures: things are different before and after some date. The
    vectors here are only a **ruler** — they state the fact that something changed and do
    not presume to say what it changed into. That sentence I write myself.
    ⚠️ **Never compare across an empty window**: with not one memory in between, "adjacent"
    does not mean anything.
    """
    W = int(cfg["drift_window_days"])
    line = float(cfg["drift_line"])
    min_items = int(cfg["drift_min_items"])
    stopped = timedelta(days=float(cfg["stopped_days"]))

    cells: dict[int, list[Item]] = {}
    for it in items:
        if it.ts is None or _normalize(vectors.get(it.id)) is None:
            continue
        cells.setdefault(_window_index(it.ts, W), []).append(it)

    def centroid(k: int) -> list[float] | None:
        vs = [_normalize(vectors[i.id]) for i in cells[k]]
        vs = [v for v in vs if v]
        if not vs:
            return None
        dim = len(vs[0])
        s = [0.0] * dim
        for v in vs:
            for d in range(dim):
                s[d] += v[d]
        return _normalize(s)

    out: list[Finger] = []
    for k in sorted(cells):
        if k + 1 not in cells:
            continue                       # never compare across an empty window
        if len(cells[k]) < min_items or len(cells[k + 1]) < min_items:
            continue
        c0, c1 = centroid(k), centroid(k + 1)
        if not c0 or not c1:
            continue
        drift = 1.0 - _cos(c0, c1)
        if drift <= line:
            continue
        boundary = _window_start(k + 1, W)
        if (now - boundary) <= stopped:
            continue
        before, after = sorted(cells[k], key=lambda x: x.ts), sorted(cells[k + 1], key=lambda x: x.ts)
        out.append(Finger(
            name="成分漂移",
            ids=[i.id for i in before + after], items=before + after, boundary=boundary,
            start=before[0].ts, end=after[-1].ts,
            evidence=(f"{_short_date(boundary)} 前后记忆的样子变了（漂移 {drift:.2f}，线 {line}）· "
                      f"前 {W} 天 {len(before)} 条 / 后 {W} 天 {len(after)} 条"),
            next_step=('边界摆在这儿，段自己划：fold(when="起..止", text=我写的那句)'),
            score=drift))
    out.sort(key=lambda x: (-x.score, x.boundary or now))
    return out


def era_spans(recs: list[tuple[dict, str]]) -> list[tuple[datetime, datetime | None]]:
    """The ranges `[start, end)` of every **period that still counts** in the store
    (`end=None` means still ongoing).

    The test is **literally the same function** `_bigevent.covering()` uses (`_usable`):
    not superseded, not covered by a higher layer, not closed, not archived. The two
    disagreeing is by definition a bug — whichever periods are laid over that stretch on
    screen are exactly the periods "does this stretch have a name" must be computed from.
    """
    from ._bigevent import _usable as _span_usable, is_big, parse_span

    out: list[tuple[datetime, datetime | None]] = []
    for meta, _t in recs:
        if not (is_big(meta) and _span_usable(meta)):
            continue
        start, end = parse_span(meta)
        if start is not None:
            out.append((start, end))
    return out


def is_named(it: Item, spans: list[tuple[datetime, datetime | None]]) -> bool:
    """**Does this one have a name yet** — yes if its date falls inside the range of any
    living period (computed on the spot).

    `it.covered` counts too: that is a real cover (an event corrected by a new version).
    It has already stopped surfacing, and pointing at it to say "nothing here has a name"
    would be pointing at a memory that has already been dealt with.
    """
    if it.covered:
        return True
    if it.ts is None:
        return False
    for start, end in spans:
        if it.ts >= start and (end is None or it.ts < end):
            return True
    return False


def mark_named(items: list[Item], spans: list[tuple[datetime, datetime | None]]) -> int:
    """Set `named` on every entry in the pool and return how many are **still unnamed**.
    All three gestures share this one definition."""
    unnamed_count = 0
    for it in items:
        it.named = is_named(it, spans)
        if not it.named:
            unnamed_count += 1
    return unnamed_count


def era_count(recs: list[tuple[dict, str]]) -> int:
    """How many periods in the store still count (the blank ledger's map gate reads this)."""
    return len(era_spans(recs))


def blank_ledger(items: list[Item], era_n: int, cfg: dict, now: datetime) -> list[Finger]:
    """**A run of consecutive days with not one entry covered by a period** -> a tap on the
    shoulder.

    🔴 **When the store holds no through-line at all, this gesture shuts up entirely.**
       The failure it guards against is the engine announcing that nothing all year has a
       name — **that must not happen.** Without a map, "where is nothing covered" is a fake
       question: that is not a blank, that is a drawing nobody has started.
       (The first version of the map is written by hand — **narrate first, point later.**)
    ⚠️ "Unnamed" means **outside every period's range** (set on the spot onto `named` by
       `mark_named()`), not a question about `covered_by` — periods keep no books. A backfilled entry landing inside an old range acquires its name
       automatically and is never counted as a blank a second time.
    """
    if int(era_n) < 1:
        return []
    min_items = int(cfg["blank_min_items"])
    gap = timedelta(days=float(cfg["blank_gap_days"]))
    stopped = timedelta(days=float(cfg["stopped_days"]))

    unnamed = sorted([i for i in items if not i.named and i.ts], key=lambda x: x.ts)
    out: list[Finger] = []
    segment: list[Item] = []

    def close_segment(segment):
        if len(segment) < min_items:
            return
        start, end = segment[0].ts, segment[-1].ts
        if (now - end) <= stopped:
            return
        out.append(Finger(
            name="空白记账",
            ids=[i.id for i in segment], items=list(segment), start=start, end=end,
            evidence=f"{_short_date(start)}~{_short_date(end)} 有 {len(segment)} 条没落在任何一条时期的范围里",
            next_step=f'fold(when="{_full_date(start)}..{_full_date(end)}", text=我写的那句)',
            score=float(len(segment))))

    for it in unnamed:
        if segment and (it.ts - segment[-1].ts) > gap:
            close_segment(segment)
            segment = []
        segment.append(it)
    close_segment(segment)
    out.sort(key=lambda x: (-x.score, x.start or now))
    return out


# ============================================================
# The rejection count — a sidecar file, **never the frontmatter**
# ============================================================
# 🔴 A rejection is about **the group**, and belongs to no single entry. Writing it into
#    the frontmatter would tear a fact about a combination apart and stuff the pieces into
#    its members, and every member would then have to be wiped by hand the next time the
#    group changed.
REJECT_FILE = "muse_rejected.json"


def state_dir(buckets_dir: str) -> str:
    return os.path.join(str(buckets_dir or "."), "_state")


def rejection_key(ids) -> str:
    """The key is the sorted set of ids. **Sorting** is the point: the same group in a
    different order is still the same group."""
    return ",".join(sorted({str(i).strip() for i in (ids or []) if str(i).strip()}))


def load_rejected(buckets_dir: str) -> dict:
    p = os.path.join(state_dir(buckets_dir), REJECT_FILE)
    if not os.path.exists(p):
        return {"version": 1, "rejected": {}}
    try:
        data = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "rejected": {}}
    if not isinstance(data, dict) or not isinstance(data.get("rejected"), dict):
        return {"version": 1, "rejected": {}}
    return data


def is_rejected(data: dict, ids, limit: int) -> bool:
    ent = (data.get("rejected") or {}).get(rejection_key(ids))
    if not isinstance(ent, dict):
        return False
    return int(ent.get("count") or 0) >= int(limit)


def record_rejection(buckets_dir: str, ids) -> tuple[str, int]:
    """Record one "these are not the same thing". Returns (key, running count).

    **A changed group is a different key** (one entry more or fewer counts), so a
    backfilled entry makes the group eligible to be raised again — which is intended: at
    that point it genuinely is a new group.
    """
    key = rejection_key(ids)
    data = load_rejected(buckets_dir)
    ent = data["rejected"].get(key) or {}
    now = _w.now().isoformat(timespec="seconds")
    cnt = int(ent.get("count") or 0) + 1
    data["rejected"][key] = {
        "ids": sorted({str(i).strip() for i in ids if str(i).strip()}),
        "count": cnt,
        "first": ent.get("first") or now,
        "last": now,
    }
    d = state_dir(buckets_dir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, REJECT_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return key, cnt


# ============================================================
# Server side: load the material -> emit clusters / gestures
# ============================================================
async def load_records() -> tuple[list[tuple[dict, str]], set[str]]:
    """One pass over the whole store (archive excluded), plus the set of ids that some
    insight's `from` points at.

    `mind_from_ids()` IS night_fall's old "not digested" test — **reused as it stands,
    never reimplemented.**
    """
    recs: list[tuple[dict, str]] = []
    for b in await rt.bucket_mgr.list_all(include_archive=False):
        meta = b.get("metadata", {}) or {}
        recs.append((meta, str(b.get("content") or "")))
    try:
        digested = await rt.bucket_mgr.mind_from_ids()
    except Exception:
        digested = set()
    return recs, digested


def _db_path() -> str:
    p = getattr(rt.embedding_engine, "db_path", "") or ""
    if p:
        return p
    return os.path.join(str((rt.config or {}).get("buckets_dir") or ""), "embeddings.db")


def _drop_rejected(candidates: list, cfg: dict) -> tuple[list, int]:
    rejected = load_rejected(str((rt.config or {}).get("buckets_dir") or ""))
    limit = int(cfg["reject_limit"])
    keep, rejected_n = [], 0
    for x in candidates:
        if is_rejected(rejected, x.ids, limit):
            rejected_n += 1
            continue
        keep.append(x)
    return keep, rejected_n


async def propose_mind(cfg: dict | None = None, loaded=None
                       ) -> tuple[list[Cluster], int, int, dict]:
    """A full pass of the mind side. Returns (clusters, how many scattered, how many on the
    old default coordinate, stats). **Nothing is written.**"""
    c = muse_config(cfg if cfg is not None else rt.config)
    now = _w.now()
    recs, digested = loaded if loaded is not None else await load_records()
    items = pool_of(recs, "muse", c, now, digested)
    vectors = read_vectors(_db_path(), [i.id for i in items])
    clusters, scattered, default_coords = daydream(items, vectors, c)
    # A rejected group is not counted as "scattered" — it is not scattered, it is a group
    # **I already said was not one thing**; do not raise it again.
    clusters, rejected_n = _drop_rejected(clusters, c)
    stats = {"池子": len(items), "有向量": len(vectors), "团": len(clusters),
             "散着": scattered, "老默认坐标": default_coords, "被拒过的组": rejected_n}
    return clusters, scattered, default_coords, stats


async def propose_gist(cfg: dict | None = None, loaded=None
                       ) -> tuple[dict[str, list[Finger]], dict]:
    """A full pass of the event side: each of the three gestures runs once. Returns
    ({gesture name: [gestures]}, stats)."""
    c = muse_config(cfg if cfg is not None else rt.config)
    now = _w.now()
    recs, digested = loaded if loaded is not None else await load_records()
    items = pool_of(recs, "gist", c, now, digested)
    vectors = read_vectors(_db_path(), [i.id for i in items])
    # 🔴 "Does it have a name" is **computed on the spot**: a period persists only a name
    #    and a range, so no field can answer the question.
    spans = era_spans(recs)
    era_n = len(spans)
    unnamed = mark_named(items, spans)

    out: dict[str, list[Finger]] = {
        "词爆发": word_burst(items, c, now),
        "成分漂移": composition_drift(items, vectors, c, now),
        "空白记账": blank_ledger(items, era_n, c, now),
    }
    rejected_n = 0
    for k in out:
        out[k], n = _drop_rejected(out[k], c)
        rejected_n += n
    stats = {"池子": len(items), "有向量": len(vectors), "主线": era_n,
             "没名字": unnamed, "被拒过的组": rejected_n}
    return out, stats


# ============================================================
# The view cache: one scan of the whole store, shared by both steps
# ============================================================
# **Why it exists**: `muse()` lays out the clusters, and then `muse(cluster=3)` looks at
# one of them — the second step needs **the same** result as the first (the [N] numbering
# has to mean the same thing), yet without a cache it would rescan the entire store
# (`load_records` + vectors + all three gestures). One call is perceptibly slow to the
# naked eye; two steps is twice that.
#
# 🔴 **Better to invalidate too eagerly than to ever serve stale data.**
#    The key is `bucket_manager._active_cache_generation`, which is incremented on **every
#    managed write** (create/update/archive/delete, via `_invalidate_bm25`) and on **every
#    touch** (`_cache_bump`), and again whenever polling notices an external edit
#    (Obsidian, a hand-edit through git). So grow / fold / regrow / trace all invalidate on
#    the spot, and not one of them needs a hook of its own.
#    ⚠️ Which cuts the other way too: **it would rather invalidate too often** (a single id
#    lookup in recall touches the entry) — and that is exactly the direction to err in.
#    The cache only guarantees "this screen and the next screen are the same screen"; it
#    guarantees nothing across a conversation.
# ⚠️ `not_same` (the rejection count) writes json under `_state/` and **does not touch a
#    bucket**, so the key does not change -> `record_rejection()` must be followed by a
#    **manual clear** (`clear_view_cache()`).
_view_cache: dict = {"钥匙": None, "值": None}


def view_cache_key() -> tuple | None:
    """Whether this screen has to be recomputed. No generation available -> None, meaning
    **do not dare to cache**."""
    gen = getattr(rt.bucket_mgr, "_active_cache_generation", None)
    if gen is None:
        return None
    # config goes into the key too: change a threshold in the muse section from the panel
    # and the very next screen should be computed against the new line
    c = muse_config(rt.config)
    return (int(gen), tuple(sorted((k, str(v)) for k, v in c.items())))


def clear_view_cache() -> None:
    _view_cache["钥匙"] = None
    _view_cache["值"] = None


async def both_sides(force: bool = False, scope=None) -> tuple[list, int, int, dict, dict]:
    """A full pass of both the mind and event sides, **with the view cache**. Returns
    (clusters, scattered, default-coordinate count, gestures, stats).

    Two callers share this one: `tools/muse/__init__.py` (the two-step tool surface) and
    `web/loci_dream.py::build_muse_pending` ("is it time to muse", which needs only the count and
    the age).
    **They must never compute their own.** A page saying "3 clusters have piled up" while
    muse() sees 4 is two different brains.

    Under a read scope (`scope`, a core.scope.ScopeView) both sides work only on what the
    gate's `muse` road lets the request see — the clusters, the gestures and every count
    among them — and nothing is cached: the cache holds the whole library's view.
    """
    key = view_cache_key() if scope is None else None
    if not force and key is not None and _view_cache["钥匙"] == key:
        return _view_cache["值"]
    loaded = await load_records()
    if scope is not None:
        recs, digested = loaded
        loaded = ([(m, t) for m, t in recs if _V.visible_for(m, scope, road=_V.MUSE)],
                  digested)
    clusters, scattered, default_coords, s1 = await propose_mind(loaded=loaded)
    fingers, s2 = await propose_gist(loaded=loaded)
    out = (clusters, scattered, default_coords, fingers, {"mind": s1, "event": s2})
    if key is not None:
        _view_cache["钥匙"] = key
        _view_cache["值"] = out
    return out


# 🔴 **A dream's ingredient selection goes through here and nowhere else**:
#    `POOL_SPECS["dream"]` plus `pool_of(recs, "dream", ...)` is the only entry point, and
#    it hands the dream engine (`core/_dream.py`) **the Items themselves** (body + v/a +
#    date), not a string of ids. A second helper would mean two copies of "which pool a
#    dream eats".
