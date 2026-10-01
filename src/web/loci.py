"""
========================================
web/loci.py — the data layer behind the standalone Loci dashboard
========================================

Four pages: the main pool, the starfield, the similarity check, and the profile. This file
produces data; all rendering lives in frontend/loci.html.

    GET  /loci                        -> the page itself
    GET  /api/loci/recall             -> recall's second skin (card + list)
    GET  /api/loci/graph              -> starfield: nodes + real edges + weak edges + constellations
    GET  /api/loci/similar            -> suspected-duplicate pairs + score distribution (adjustable threshold)
    GET  /api/loci/profile            -> the note by the door
    GET  /api/loci/bucket/{id}        -> one bucket, verbatim, plus its metadata
    GET  /api/dream/current           -> the current dream (current layer + level; 204 when there is none, and it writes a recall state)
    GET  /api/muse/pending            -> is it time to muse? (cluster count + age + worth_poking)
    GET  /api/loci/pulse              -> health check: how many entries, how much space, are the engines alive
    GET  /api/loci/poke               -> dream (delivery) + muse cluster count (nudge) + structured recall scores, all in one read-only call
    GET  /api/loci/health             -> this project's own health check (not the upstream diagnostics endpoint)
    GET  /api/loci/setup              -> the settings page's top block: five status rows, each saying what breaks if it is left unset
    GET  /api/loci/rooms              -> the four rooms and what is in them
    GET  /api/loci/subjects           -> the "who is in here" screen
    GET  /api/loci/recollect          -> pull a faded or sunk memory back up
    GET  /api/loci/auth/state         -> where the password currently lives, and whether one needs setting
    GET  /api/logs                    -> the tail of server.log
    GET  /loci/vendor/{path:path}     -> three.js, served locally, which the starfield page needs
    GET  /api/v2/slices               -> the host's own read of the pending slices (hook key)
    GET  /api/v2/breath               -> breath's waking screen for a host's window-opening hook:
                                         the same text the tool returns, or `?format=json` for
                                         its structured form (hook key). Like the tool, it
                                         stamps a question it hands out as asked
    GET  /api/v2/changes              -> the ledger from seq N on (`?since=N&limit=`): ids, kinds,
                                         source identities and hashes, never text; without
                                         "was merely touched"; only what the host's credential
                                         reaches (hook key; core/_ledger.py)

🔴 THE WRITE SURFACE — nine POST routes, and every one of them writes something.

    POST /api/loci/similar/action     -> a human verdict on a suspected duplicate: keep
                                         both, or sink one (trace delete=True — a soft
                                         delete, always recoverable by direct id lookup)
    POST /api/loci/want/resolve       -> close something that was wanted (trace status)
    POST /api/loci/want/asked         -> record that it was asked about (trace)
    POST /api/loci/event/correct      -> regrow: writes a NEW VERSION of a memory
    POST /api/loci/subjects/action    -> edits the alias table in the data volume
    POST /api/loci/auth/set-password  -> sets the password guarding remote MCP access
    POST /api/loci/dream/wake         -> the demotion signal: drop a live "whole" dream
                                         layer down to the fragment layer (idempotent)
    POST /api/v2/slices               -> the host hands over a day's raw lines; a side model
                                         slices them and the slices are stored as pending
                                         (hook key; the raw text is not kept)
    POST /api/v2/source/change        -> a host's change to one piece of its material: the
                                         source registry, the block on what stood on it, and
                                         for withdrawn / deleted the clearing of every place
                                         its text reached (host credential; core/_source_change.py)

⚠️ This header used to say the file was "read-only, with a single write endpoint", and
   listed two of the seven. That was true when it was written and then five routes were
   added underneath it. Anyone sizing up what this surface can do — which is exactly what
   someone deciding whether to expose the port would be doing — would have been wrong by
   a factor of seven, and wrong specifically about the password and the alias table.

   📌 The general version, since it has now happened three times in this repository: a
      comment that enumerates things rots the moment something is added, and it rots
      silently, because nothing checks a list written in prose.

Rules: do not use Optional[simple type] for parameters; run all three smoke suites after
changing anything here.

Public surface: register(mcp).
========================================
"""

import json
import os
import re
import sqlite3
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from core import _when as _w      # "today" in the user's local timezone — never call datetime.now() directly
from utils import read_from_ids

logger = sh.logger

_PROFILE_TAG = "__档案事实__"
_BIGEVENT_TAG = "__大event__"
# The default is 88. Below 85 the thirteen emotional-root seeds start mixing in, and those
# are supposed to resemble each other.
_SIM_DEFAULT = 88.0
_SIM_FLOOR = 60.0      # pairs scoring below this are not even computed, to keep hundreds of thousands of them out of memory
# Memory gate: at most this many pairs are kept. At 628 entries that is currently ~29,000
# pairs; the same ~15% ratio at ten thousand entries would be 7.5 million tuples, roughly
# 0.7 GB.
_PAIRS_CAP = 200_000
# `_REMIND_DAYS` (30 days) moved, along with the note by the door, into its contract source
# (`tools/breath/awaken.py`) — **do not put a second 30 here**. Two 30s are two rulesets, and
# that is exactly how one gets changed and the other forgotten. Same for `_is_closed`: the
# reminder rule lives over there.


# ============================================================
# The gate on the two write endpoints
# ============================================================

def _origin_reject(request: Request) -> str:
    """Same-origin check. An empty string means allow; anything else is the reason to refuse.

    Why a cookie alone is not enough: `SameSite=Lax` only blocks **cross-site** requests,
    not **same-site cross-origin** ones. A page on another port of the same host counts as
    the same site, so if it posts valid JSON as `text/plain` the browser attaches the cookie
    anyway. These two write endpoints therefore have to check Origin themselves.

    A missing Origin header is always refused: a browser always sends one on a POST, so its
    absence means the request did not come from a browser (curl, a script). These endpoints
    exist only for buttons on the page, so refusing is correct.
    """
    origin = request.headers.get("origin") or ""
    host = request.headers.get("host") or ""
    if not origin:
        return "缺 Origin 头（这个写口只接受页面上的按钮）"
    if not host:
        return "缺 Host 头"
    from urllib.parse import urlsplit
    try:
        o = urlsplit(origin)
    except ValueError:
        return f"Origin 解析不了：{origin}"
    if o.scheme not in ("http", "https"):
        return f"Origin 的协议不对：{origin}"
    # netloc includes the port, so "another port on the same machine" is caught here too —
    # which is exactly what needs catching
    if not o.netloc or o.netloc != host:
        return f"Origin 和 Host 对不上：{o.netloc} ≠ {host}"
    return ""


async def _write_body(request: Request) -> dict:
    """Body reading for write endpoints: verify the origin and the Content-Type first, then
    parse.

    Raising `PermissionError` means "answer 403"; raising `ValueError` means "answer 400".
    (This used to call `sh._read_json_object` directly, and malformed JSON bubbled all the
    way out as a 500.)
    """
    why = _origin_reject(request)
    if why:
        raise PermissionError(why)
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        raise ValueError(f"Content-Type 必须是 application/json（收到 {ct or '空'}）")
    try:
        body = await request.json()
    except Exception:
        raise ValueError("body 不是合法 JSON")
    if not isinstance(body, dict):
        raise ValueError("body 必须是一个 JSON 对象")
    return body


# ============================================================
# Similarity: pairwise cosine across the whole store. Nothing is recomputed while the vector
# store is unchanged, so the slider stays responsive.
# ============================================================

_sim_lock = threading.Lock()
_sim_cache: dict = {"key": None, "pairs": [], "hist": [], "n": 0, "total_pairs": 0}


def _emb_db_path() -> str:
    return os.path.join(sh.config["buckets_dir"], "embeddings.db")


_rev_cache: dict = {"at": 0.0, "val": (0, 0.0)}
_REV_TTL = 2.0          # seconds. While the slider is being dragged, do not walk nine hundred files on every tick.


def _buckets_rev() -> tuple:
    """The bucket directory's "version": file count plus the most recent modification time.

    Why the cache key cannot be `embeddings.db` alone: changing only name / room / tags /
    importance / domain **does not touch the vector store**. Tag an entry `__seed__`, for
    instance — it should vanish from this page — and it stays in the cached pairs, with a
    stale name and summary on its card. But every one of those changes does rewrite that
    bucket's .md.

    WARNING: **this must recurse.** There is another level below `dynamic/`, and a top-level
    listdir sees only 117 files where there are really more than 900 — which means missing
    nine tenths of all changes, so the fix would be no fix at all.
    (The first version did exactly that. It was the file count printed by the smoke test
    that caught it.)
    """
    import time
    now_s = time.monotonic()
    if now_s - _rev_cache["at"] < _REV_TTL:
        return _rev_cache["val"]

    root = str(sh.config.get("buckets_dir") or "")
    newest, count = 0.0, 0
    for sub in ("dynamic", "permanent", "feel", "letters", "archive"):
        for dirpath, _dirs, names in os.walk(os.path.join(root, sub)):
            for name in names:
                if not name.endswith(".md"):
                    continue
                count += 1
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    pass
    val = (count, round(newest, 3))
    _rev_cache.update({"at": now_s, "val": val})
    return val


def _load_vectors() -> tuple[list, object]:
    """Read every vector from embeddings.db; returns (ids, the normalized matrix)."""
    import numpy as np
    ids, vecs = [], []
    con = sqlite3.connect(f"file:{_emb_db_path()}?mode=ro", uri=True)
    try:
        for bid, emb in con.execute("select bucket_id, embedding from embeddings"):
            try:
                v = np.asarray(json.loads(emb), dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if v.ndim != 1 or v.shape[0] < 8:
                continue
            ids.append(str(bid))
            vecs.append(v)
    finally:
        con.close()
    if not vecs:
        return [], None
    dim = Counter(v.shape[0] for v in vecs).most_common(1)[0][0]
    keep = [i for i, v in enumerate(vecs) if v.shape[0] == dim]
    ids = [ids[i] for i in keep]
    M = np.vstack([vecs[i] for i in keep])
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    return ids, M


def _sim_visible(meta: dict) -> bool:
    """Which buckets appear on this page.

    Several categories are excluded. All of them are things that are *supposed* to look
    alike, so flagging them as duplicates is pure false positive:
      - emotional seeds (the thirteen roots score 87~89 against each other; they are a
        coordinate system, not memories)
      - the archive (already sunk once; do not ask again)
      - superseded realizations (regrow's version chain: old and new should resemble each
        other)
      - **anything folded under a gist**: it is supposed to resemble the gist that folded
        it, and it no longer surfaces independently, so raising it again here achieves
        nothing
    """
    if str(meta.get("type") or "") in ("archived", "letter"):
        return False
    if (meta.get("domain") or [""])[0] == "seed":
        return False
    if "__seed__" in [str(t) for t in (meta.get("tags") or [])]:
        return False
    from core import _fold as _F
    if (_F.is_covered(meta)
            or meta.get("tombstone") or meta.get("deleted_at")):
        return False
    return True


async def _compute_pairs() -> dict:
    """Compute pairwise cosine across the whole store once, and cache it until
    embeddings.db's mtime changes."""
    import numpy as np
    try:
        key = (os.path.getmtime(_emb_db_path()), os.path.getsize(_emb_db_path()),
               _buckets_rev())      # a metadata change must invalidate this too
    except OSError:
        key = None

    with _sim_lock:
        if key is not None and _sim_cache["key"] == key:
            return _sim_cache

    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    info: dict[str, dict] = {}
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if not bid or not _sim_visible(meta):
            continue
        info[bid] = {
            "id": bid,
            "short": bid[:6] if re.fullmatch(r"[0-9a-f]{12}", bid) else bid,
            "name": str(meta.get("name") or ""),
            "summary": str(meta.get("summary") or ""),
            "room": str(meta.get("room") or ""),
            "created": str(meta.get("created") or "")[:10],
            "body": str(b.get("content") or ""),
            "tagged": [str(t).split(":", 1)[1] for t in (meta.get("tags") or [])
                       if str(t).startswith("疑似同件:")],
        }

    ids, M = _load_vectors()
    if M is None:
        result = {"key": key, "pairs": [], "hist": [], "n": 0, "total_pairs": 0,
                  "info": info, "no_vectors": True}
        with _sim_lock:
            _sim_cache.update(result)
        return result

    keep = [i for i, b in enumerate(ids) if b in info]
    ids = [ids[i] for i in keep]
    M = M[keep]

    n = len(ids)
    hist = [0] * 20  # one bucket per 5 points, over 0~100
    pairs = []
    capped = False
    if n >= 2:
        # Compute in blocks rather than allocating one n x n matrix. At 671 entries it makes
        # no difference; at ten thousand it very much does.
        step = 256
        for s in range(0, n, step):
            block = M[s:s + step] @ M.T                      # (b, n)
            for r in range(block.shape[0]):
                i = s + r
                row = block[r]
                row[:i + 1] = -1.0                           # keep the upper triangle only; do not count each pair twice
                counts = np.bincount(
                    np.clip((np.maximum(row[i + 1:], 0) * 20).astype(int), 0, 19),
                    minlength=20)
                hist = [h + int(c) for h, c in zip(hist, counts)]
                for j in np.where(row >= _SIM_FLOOR / 100.0)[0]:
                    pairs.append((float(row[j]) * 100.0, ids[i], ids[int(j)]))
            # The cap. At 628 entries this keeps ~29,000 pairs, which is nothing; but the
            # same ratio at ten thousand entries is 7.5 million tuples, roughly 0.7 GB, and
            # the process would not survive it.
            # Only the most similar pairs are kept — duplicate review reads from the top
            # score downwards anyway, and nobody will ever scroll through the millions of
            # pairs sitting around 60.
            # WARNING: the cost is that once the cap trips, dragging the threshold very low
            # shows fewer pairs than really exist. So a `capped` flag is returned below;
            # the number on the page must not lie.
            if len(pairs) > _PAIRS_CAP * 2:
                pairs.sort(key=lambda p: -p[0])
                del pairs[_PAIRS_CAP:]
                capped = True
    pairs.sort(key=lambda p: -p[0])
    if len(pairs) > _PAIRS_CAP:
        del pairs[_PAIRS_CAP:]
        capped = True
    result = {"key": key, "pairs": pairs, "hist": hist, "n": n, "capped": capped,
              "total_pairs": n * (n - 1) // 2, "info": info, "no_vectors": False}
    with _sim_lock:
        _sim_cache.update(result)
    return result


# ============================================================
# Starfield: nodes / edges / constellations
# ============================================================

_WIKI_RE = re.compile(r"\[\[([^\[\]|]{1,40})\]\]")
_SEED_NAMES = frozenset({
    "joy", "anger", "sorrow", "fear", "love", "aversion", "desire",
    "lust", "sound", "scent", "taste", "touch", "dharma", "greed",
})


def _split_ids(raw) -> list[str]:
    """An id field persisted as a comma-separated string (`supersedes`); a list is
    accepted too."""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").split(",")
    return [x.strip() for x in items if x.strip()]


def _bigevent_members(content: str, entries: list,
                      since: str = "", until: str = "") -> tuple[list[str], str]:
    """The member stars of a big event -> (list of member ids, a plain-language description
    of this query).

    **A constellation is the patch of sky one big event lights up.** The timeline and the
    constellation are the same thing, and the name for it is "constellation".
    The start and end come from `when` first, where the caller passes them in; the
    `range: ...` line in the body is only a fallback for older buckets. The filter
    conditions are still read out of the body:

        filter: tag=<tag name>       -> tag
        filter: room=EVENT/SELF      -> room (prefix match)

    Whether to make these structured fields instead has been asked. **They are deliberately
    left as text**: that line is part of the body, so when the model recalls this big event
    it **can read it**. Hidden in metadata, it would be invisible to the model.
    What is actually fragile is not free text, it is "getting it wrong without saying so" —
    so a parse failure returns an empty query_text and the page says so out loud, rather
    than quietly drawing an empty constellation.
    """
    m_from = re.search(r"范围[:：]\s*(\d{4}-\d{2}-\d{2})(?:\s*\.\.\s*(\d{4}-\d{2}-\d{2}))?", content)
    m_tag = re.search(r"过滤[:：][^\n]*?\btag\s*=\s*([^\s,，;；]+)", content)
    m_room = re.search(r"过滤[:：][^\n]*?\broom\s*=\s*([A-Za-z/]+)", content)
    if not since and m_from:
        since = m_from.group(1)
        until = m_from.group(2) or ""
    if not (since or m_tag or m_room):
        return [], ""
    want_tag = m_tag.group(1) if m_tag else ""
    want_room = (m_room.group(1).rstrip("/") if m_room else "")

    out = []
    for e in entries:
        if since and e["date"] < since:
            continue
        if until and e["date"] > until:
            continue
        if want_tag and want_tag not in e["_tags_all"]:
            continue
        if want_room:
            r = e.get("room") or ""
            if not (r == want_room or r.startswith(want_room + "/")):
                continue
        out.append(e["id"])

    bits = []
    if since:
        bits.append(f"{since} 起" if not until else f"{since} ~ {until}")
    if want_room:
        bits.append(f"room={want_room}")
    if want_tag:
        bits.append(f"tag={want_tag}")
    return out, " · ".join(bits)


# ============================================================
# The builders are **deliberately outside the route closures**, so each can be run on its
# own without faking a login session
# (`python -c "asyncio.run(loci.build_graph())"`). The routes handle only auth and the JSON
# envelope.
# ============================================================

async def build_rooms() -> dict:
    """The directory both doors open onto: how many entries in each of the four rooms, plus
    the ten most frequent tags.

    The rooms went from ten to four, and the doors from I/YOU to EVENT/MIND.
    The I/YOU dimension was not lost, it moved to subjects — "who is this about" is no
    longer carried by the room.
    """
    from tools.recall.core import _room_cn, _visible
    from core._rooms import ALL_ROOMS, normalize_room
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    counts: Counter = Counter()
    tags: Counter = Counter()
    homeless = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not _visible(meta):
            continue
        # Normalize before counting: older stores still carry the ten-room names, and
        # without normalization all four doors read 0 while `homeless` swells to the entire
        # store — and that 0 looks quite a lot like "there is simply no data".
        r = normalize_room(meta.get("room"))
        if r:
            counts[r] += 1
        else:
            homeless += 1
        for t in (meta.get("tags") or []):
            t = str(t)
            if not t.startswith(("__", "aspect:", "疑似同件:")):
                tags[t] += 1
    doors: dict[str, list] = {"EVENT": [], "MIND": []}
    for r in ALL_ROOMS:
        doors["MIND" if r.startswith("MIND/") else "EVENT"].append(
            {"room": r, "cn": _room_cn(r), "n": counts.get(r, 0)})
    return {
        "doors": doors,
        "homeless": homeless,
        "total": sum(counts.values()) + homeless,
        "top_tags": [{"tag": t, "n": n} for t, n in tags.most_common(10)],
    }


async def build_subjects() -> dict:
    """"Who is in here": every subject that has appeared in the store, how many entries each
    has, and when each last appeared. **Read-only; nothing is written to disk.**

    Why this screen exists: `aliases.yaml` is maintained by hand, and maintaining anything by
    hand presupposes knowing there is something to change — **and that step was missing
    entirely**. Nobody tells you when a new person appears; nobody tells you when one person
    splits into two spellings; nobody tells you when extraction noise creeps in. A full-store
    scan turned up one "subject" that was the model mistaking a fragment of ordinary prose
    for a name.

    Same principle as muse and fold: **the system only lays things out; the merge itself is
    a human click.**
    So this endpoint counts, and writes not one character into aliases.yaml.

    It measures the same way recall and rooms do: `_visible` filters first (superseded
    realizations, anything folded under a gist, and seeds do not count), and time comes from
    `_node_ts` (when first, created as fallback). The number on the panel has to be the same
    number the model sees on waking.
    """
    from tools.recall.core import _visible
    from tools import _subjects as subj
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    table = subj.load_alias_table()           # {alias in lowercase: canonical name}
    blocked = subj.load_not_person()          # entries marked "this is not a person" (lowercase)
    counts: Counter = Counter()
    variants: dict[str, set] = {}             # canonical name -> the spellings actually found on disk
    last: dict[str, datetime] = {}
    last_bucket: dict[str, str] = {}
    blocked_hits: Counter = Counter()
    total = 0
    with_subj = 0
    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        if not _visible(meta):
            continue
        total += 1
        raws = [str(x).strip() for x in (meta.get("subjects") or []) if str(x).strip()]
        if raws:
            with_subj += 1
        ts = _node_ts(meta)
        bid = str(meta.get("id") or "")
        seen = set()
        for raw in raws:
            low = raw.lower()
            if low in blocked:
                blocked_hits[raw] += 1
                continue
            # This deliberately does not call subj.canonical(): that returns an empty string
            # for pronouns too, which would quietly swallow the fact that a pronoun leaked
            # into subjects. That leak is exactly what this screen is for.
            canon = table.get(low, raw)
            if canon in seen:                 # two spellings of one person in the same entry count once
                continue
            seen.add(canon)
            counts[canon] += 1
            if canon != raw:
                variants.setdefault(canon, set()).add(raw)
            if ts is not None and (canon not in last or ts > last[canon]):
                last[canon] = ts
                last_bucket[canon] = bid
    names_out = []
    for nm, c in counts.most_common():
        ts = last.get(nm)
        rec = subj.record_of(nm)
        names_out.append({
            "name": nm,
            "n": c,
            "last": ts.strftime("%Y-%m-%d") if ts else "",
            "last_bucket": last_bucket.get(nm, ""),
            # empty = not in the alias table yet (a newly appeared name)
            "canonical": table.get(nm.lower(), ""),
            # older spellings still on disk; a merge applies going forward and never
            # rewrites history
            "variants": sorted(variants.get(nm, ())),
            # a pronoun should never be a subject (the gate is on the write side), so one
            # showing up here means that gate leaked — flag it
            "pronoun": subj.is_pronoun(nm),
            # what the table says the name is (instance_of) and where it appears; empty
            # when it says nothing (a table of aliases alone, or a name not in it)
            "kind": rec.instance_of if rec else "",
            "present_in": list(rec.present_in) if rec else [],
            "member_of": list(rec.member_of) if rec else [],
        })
    return {
        "total": total,                       # visible entries
        "with_subjects": with_subj,           # of those, the ones a person was extracted from
        "distinct": len(counts),              # how many distinct names, after alias merging
        "names": names_out,                   # descending by count
        "alias_table_size": len(table),
        # Entries marked "this is not a person": kept out of the table above, but they must
        # not vanish into thin air — nobody can undo something that disappeared quietly.
        "blocked": [{"name": k, "n": v} for k, v in blocked_hits.most_common()],
    }


def _node_ts(meta: dict) -> datetime | None:
    """Where a star sits in the sky: `when` first, `created` as fallback — the same rule
    recall uses.

    It goes through `tools/_when`, the same ruler recall uses, and returns timezone-aware
    local time.
    (This used to carry the same `[:19]` slice found elsewhere, which cuts off the Z or the
    +08:00 offset.)
    """
    for k in ("when", "created"):
        ts = _w.parse_stamp(meta.get(k))
        if ts is not None:
            return ts
    return None


async def build_graph() -> dict:
    """Starfield: nodes + real edges + weak edges + constellations + meteors.

    WARNING: **only the current version is drawn.** Superseded versions — folded under a
    gist, or replaced by regrow — do not go into the sky. Search, direct id lookup and the
    version chain are all unaffected; this one screen simply does not draw them. The test is
    `_F.is_covered()`, the same contract source awaken and recall use. Do not write a second
    one: when the rooms were renamed, the same test had been written out in two places, one
    got fixed and the other did not, and that lesson still stands (see the header of
    tools/breath/awaken.py).
    """
    from tools.recall.core import _room_cn, _visible, _label_of, _short_id
    from core._rooms import is_mind_room, normalize_room
    from core import _fold as _F
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    now = _w.now()          # local timezone
    fresh_line = now - timedelta(hours=24)

    nodes: list[dict] = []
    by_id: dict[str, dict] = {}
    by_name: dict[str, str] = {}
    raw: dict[str, dict] = {}
    big_events: list[tuple] = []

    for b in all_buckets:
        meta = b.get("metadata", {}) or {}
        bid = str(meta.get("id") or b.get("id") or "")
        content = str(b.get("content") or "")
        tags_all = [str(t) for t in (meta.get("tags") or [])]
        if _BIGEVENT_TAG in tags_all:
            # A superseded version does not hang in the sky: constellations draw only the
            # current version. The evolution is in the version chain, still visible when a
            # single bucket is opened. The test goes through _F.is_covered() — the same gate
            # ordinary nodes use below, so there are not two copies of it.
            if not _F.is_covered(meta):
                big_events.append((bid, meta, content))
            continue
        if not bid or not _visible(meta):
            continue
        # Superseded versions (folded under a gist, or replaced by regrow) do not go into
        # the sky. This affects the starfield only; search, direct id lookup and the version
        # chain all still reach the superseded entry.
        if _F.is_covered(meta):
            continue
        ts = _node_ts(meta)
        if ts is None:
            continue

        def _f(key, default, _meta=meta):
            try:
                return float(_meta.get(key, default))
            except (TypeError, ValueError):
                return default

        room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
        created_dt = _node_ts({"created": meta.get("created")}) or ts
        node = {
            "id": bid,
            "short": _short_id(bid),
            "label": _label_of({"meta": meta, "content": content}),
            "room": room,
            "room_cn": _room_cn(meta.get("room")),
            "v": _f("valence", 0.5),
            "a": _f("arousal", 0.3),
            "date": ts.strftime("%Y-%m-%d"),
            "ts": ts.isoformat(timespec="seconds"),
            "pinned": bool(meta.get("pinned")),
            # Anything stored tonight is the brightest star in the sky, and still faintly
            # twinkling.
            "fresh": created_dt >= fresh_line,
            "kind": "mind" if is_mind_room(meta.get("room")) else "event",
            "seeds": sorted({s for s in _WIKI_RE.findall(content) if s in _SEED_NAMES}),
        }
        nodes.append(node)
        by_id[bid] = node
        nm = str(meta.get("name") or "").strip()
        if nm and nm not in by_name:
            by_name[nm] = bid
        raw[bid] = {"meta": meta, "content": content, "tags_all": tags_all}

    # ---- Real edges: from / supersedes / [[wikilinks]] in the body ----
    edges: list[dict] = []
    seen_edge: set = set()

    def _add(a: str, b: str, kind: str) -> None:
        if a == b or a not in by_id or b not in by_id:
            return
        k = (a, b, kind)
        if k in seen_edge:
            return
        seen_edge.add(k)
        edges.append({"from": a, "to": b, "kind": kind})

    unresolved: Counter = Counter()
    for bid, r in raw.items():
        meta, content = r["meta"], r["content"]
        # The edge kind carries the tools' word, `from`: the memories this one stands
        # on. Its own previous version is drawn once, by the supersedes edge below, and
        # a quoted line points outside the library, where there is no star.
        for src in read_from_ids(meta):
            _add(src, bid, "from")
        for old in _split_ids(meta.get("supersedes")):
            _add(bid, old, "supersedes")
        for target in _WIKI_RE.findall(content):
            t = target.strip()
            if t in _SEED_NAMES:
                continue          # [[love]] is an emotional root, not a pointer to another memory
            if re.fullmatch(r"[0-9a-f]{12}", t):
                _add(bid, t, "wikilink")
            elif t in by_name:
                _add(bid, by_name[t], "wikilink")
            else:
                unresolved[t] += 1

    # ---- Weak edges: real edges bright, weak edges dim, and toggleable ----
    weak: list[dict] = []
    seen_weak: set = set()

    def _add_weak(a: str, b: str, kind: str) -> None:
        if a == b:
            return
        k = (a, b) if a < b else (b, a)
        if k in seen_weak:
            return
        seen_weak.add(k)
        weak.append({"from": k[0], "to": k[1], "kind": kind})

    by_day: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_day[n["date"]].append(n)
    for _day, group in by_day.items():
        group.sort(key=lambda n: n["ts"])
        # Chain them, do not mesh them: 30 entries on one day meshed together is 435 lines,
        # which is not a relationship, it is a smear.
        for a, b in zip(group, group[1:]):
            _add_weak(a["id"], b["id"], "same_day")

    by_tag: dict[str, list] = defaultdict(list)
    for bid, r in raw.items():
        for t in r["tags_all"]:
            if not t.startswith(("__", "aspect:", "疑似同件:")):
                by_tag[t].append(bid)
    for _t, members in by_tag.items():
        if not (2 <= len(members) <= 12):
            continue    # an over-large tag (50 entries under one word) draws as a blob and carries no information
        # Chain rather than mesh here too: 12 entries meshed is 66 lines, chained is 11 —
        # and a chain in time order shows how this tag's thread ran, which is far more
        # interesting than a blob.
        members.sort(key=lambda b: by_id[b]["ts"] if b in by_id else "")
        for a, b in zip(members, members[1:]):
            _add_weak(a, b, "same_tag")

    # ---- Constellations, built from big events: the timeline and the constellation are the
    # ---- same thing ----
    entries_for_big = [{"id": n["id"], "date": n["date"], "room": n["room"],
                        "_tags_all": raw[n["id"]]["tags_all"]} for n in nodes]
    _span_re = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})?$")
    constellations = []
    for bid, meta, content in big_events:
        first = (content.strip().splitlines() or [""])[0]
        # Start and end come from `when` first; older buckets fall back to the range line in
        # the body.
        m = _span_re.match(str(meta.get("when") or "").strip())
        since = m.group(1) if m else ""
        until = (m.group(2) or "") if m else ""
        members, query_text = _bigevent_members(content, entries_for_big,
                                                since=since, until=until)
        # The title has to be short; do not put the summary up there. Even the fallback for
        # an unnamed entry takes only the head of the body and cuts at the first punctuation
        # mark — a name is a name, and the sentence is for after you click.
        name = re.sub(r"^[\d\- :]+", "", str(meta.get("name") or "")).strip()
        if not name:
            name = re.split(r"[：:，,。；;——]", first, 1)[0][:12]
        constellations.append({
            "id": bid,
            "short": _short_id(bid),
            "name": name,
            "line": first,
            "members": members,
            # empty string = the range is missing or malformed. The front-end raises on
            # this rather than quietly drawing an empty constellation.
            "query_text": query_text,
            "start": since,
            "ongoing": bool(since) and not until,
            "resolved": str(meta.get("status") or "") == "resolved",
        })
    # Stack them one on another, newest on top — a stack, not a side-by-side timeline.
    constellations.sort(key=lambda c: c.get("start") or "", reverse=True)

    # ---- Meteors: how many dreams are left on disk. They expire on their own, so this
    # ---- number changes over the course of a day. ----
    # What is counted moved from night_fall's `.md` files to the new engine's dream files;
    # the directory is the same one, kept as it was.
    try:
        from core import _dream as _D
        meteors = len(_D.load_dreams())
    except Exception:                       # noqa: BLE001 - the starfield must not crash because dreams could not be counted
        meteors = 0

    return {
        "nodes": nodes,
        "edges": edges,
        "weak_edges": weak,
        "constellations": constellations,
        "meteors": meteors,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "weak": len(weak),
            "by_kind": dict(Counter(e["kind"] for e in edges)),
            # Wikilinks that resolve to nothing (a person or project with no bucket of that
            # name). Report them honestly rather than pretending they connected.
            "unresolved_links": unresolved.most_common(8),
        },
    }


def _collect_events(all_buckets: list) -> list[dict]:
    """The pool behind the "something comes back to you" section of the waking screen.
    **The rule is not here** — it is in the contract source.

    This function used to be a **parallel implementation** of the pool logic in
    `tools/breath/awaken.py`. (Copying the same line into two files is not sharing a
    source.) It now does exactly one thing: call `tools.breath.awaken.event_pool()` and
    reshape the dict into what the front-end wants.
    WARNING: the direction is fixed at **web -> tools**, never the reverse. The MCP surface
       must not depend on the panel.
    WARNING: the cost of parallel implementations has been paid here before. When the rooms
       were renamed, `.find("/EVENT/") > 0` was written out in both places and **both went
       silently empty together**. A second instance turned up later: the "folded entries do
       not enter the pool" gate was added only on the awaken side, so they kept surfacing on
       the page.
    """
    from tools.recall.core import _room_cn, _label_of, _short_id
    from core._rooms import normalize_room
    from core.profile import event_pool
    pool: list[dict] = []
    for e in event_pool(all_buckets):
        meta, content, bid = e["meta"], e["content"], e["id"]
        room = normalize_room(meta.get("room"))
        pool.append({
            "id": bid, "short": _short_id(bid), "room": room,
            "room_cn": _room_cn(room),
            "label": _label_of({"meta": meta, "content": content}),
            "created": str(meta.get("created") or "")[:10],
        })
    return pool


def _pick_recollect(pool: list[dict], n: int = 2) -> list[dict]:
    """One or two at random. **The absence of a reason is precisely what makes this feel
    like a mind rather than a database** — no sorting, no weighting.

    The cap is 2 because the waking screen's contract is one to two entries (`awaken.py`
    hardcodes `min(2, ...)`).
    It is clamped here rather than in the route, so that adding another route, or calling
    this function directly some day, cannot get around the contract. (The route used to
    allow up to n=5.)
    """
    import random
    if not pool:
        return []
    return random.sample(pool, max(1, min(n, 2, len(pool))))


async def build_recollect(n: int = 2) -> dict:
    """The "give me another" button hits this endpoint on its own, rather than re-fetching
    the whole profile page."""
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    pool = _collect_events(all_buckets)
    return {"recollect": _pick_recollect(pool, n), "pool": len(pool)}


async def build_profile() -> dict:
    """The note by the door: name, principles with their provenance, reminders, and what is
    weighing on the mind. **Every rule lives in the contract source.**

    This used to be a **parallel implementation** of `tools/breath/awaken.py`. Its own
    comment said "change one and you must change the other" — and the unchanged side was
    duly caught: the falsy `or 0.5` fallback on `weight` had been fixed in awaken but was
    still here, so a want whose weight had been cleared by a dream **kept pressing down on
    the page anyway**.
    There is now one rule and one place: `tools.breath.awaken.door_note()`. This function
    only turns the dict into JSON.

    WARNING: the "things that keep coming up about a person" section was removed. It ranked
    by activation_count, and **what gets mentioned most is not what is most true**. Reading
    a dossier of "what this person is like" on waking and then treating them according to
    the dossier turns a person into a character sheet. The rule is now about timing instead:
    **only what there is no time to look up before speaking belongs by the door.**
    """
    from tools.recall.core import _room_cn, _label_of, _short_id
    from core._rooms import normalize_room
    from core.profile import door_note, edited_by_user
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    now = _w.now()          # local timezone
    door = door_note(all_buckets, now)
    heavy_q_id = door["heavy_question_id"]      # ask only about the longest-standing one

    def _label(x) -> str:
        return _label_of({"meta": x["meta"], "content": x["content"]})

    facts = [{"id": f["id"], "short": _short_id(f["id"]),
              "created": f["created"], "content": f["content"].strip()}
             for f in door["facts"]]
    big = [{"id": g["id"], "short": _short_id(g["id"]),
            "line": (g["content"].strip().splitlines() or [""])[0]}
           for g in door["big"]]
    reminders = [{"id": r["id"], "short": _short_id(r["id"]), "days": r["days"],
                  "when": r["when"], "status": r["status"],
                  "label": _label(r), "loud": r["loud"]}
                 for r in door["reminders"]]
    # **No truncation here.** The waking screen shows only two, because one screen is
    # finite; this is a page someone is browsing deliberately, so however many are pending
    # is however many they should see. The ordering — heaviest first, and among equals the
    # longest-standing first — lives in the contract source.
    # clock/clock_note are the category decided by the three kinds of clock, plus a note for
    # legacy data (a read-side judgement; see core/profile._want_clock). is_question marks
    # the "ask only about the longest-standing one" entry. last_asked/closed_by pass meta
    # straight through, for the front-end to build the "never asked about this" phrase and
    # to decide whether to show the close button, which only appears for an open telic entry.
    heavy = [{"id": h["id"], "short": _short_id(h["id"]), "held": h["held"],
              "weight": h["weight"], "label": _label(h), "loud": h["loud"],
              "clock": h["clock"], "clock_note": h["clock_note"],
              "last_asked": h["last_asked"],
              "closed_by": str(h["meta"].get("closed_by") or ""),
              "is_question": h["id"] == heavy_q_id}
             for h in door["heavy"]]
    # The notification pool: entries the user edited that have not yet been read or folded.
    edited = [{"id": e["id"], "short": _short_id(e["id"]),
               "label": _label(e), "content": e["content"].strip(),
               "corrects": (read_from_ids(e["meta"]) or [""])[0]}
              for e in edited_by_user(all_buckets)]
    rules = []
    for r in door["rules"]:
        room = normalize_room(r["meta"].get("room"))
        rules.append({"id": r["id"], "short": _short_id(r["id"]), "room": room,
                      "room_cn": _room_cn(room), "label": _label(r),
                      "content": r["content"].strip()})

    # The mid-range span (the last three days). This cell was found missing once. The waking
    # screen has six parts — profile, reminders, **mid-range**, long-range, something coming
    # back, and dreams — and the profile page has to lay them out the same way to line up.
    # It uses the same call awaken.py makes (recall over 3d, collapsed into one card) rather
    # than computing its own.
    try:
        from tools.recall.core import recall_core
        mid = await recall_core(when="3d", room="", tag="", query="", max_cells=1)
        if "没有东西" in mid:
            mid = ""
    except Exception as e:
        logger.warning(f"[loci] profile 取中期失败: {e}")
        mid = ""

    # "Something comes back to you", the fifth of the six parts — reusing the same list_all
    # result rather than hitting the store a second time.
    _ev_pool = _collect_events(all_buckets)

    return {
        "mid": mid,
        "recollect": _pick_recollect(_ev_pool, 2),
        "recollect_pool": len(_ev_pool),
        "facts": facts,
        "facts_warning": (f"有 {len(facts)} 个 {_PROFILE_TAG} 桶——只该有一个，去合并"
                          if len(facts) > 1 else
                          f"名字页 {door['facts_covered'][0]['id']} 被换掉了，"
                          f"新版没带 {_PROFILE_TAG}——门口那格是空的"
                          if not facts and door["facts_covered"] else ""),
        "rules": rules,
        # freq_i / freq_you were removed. A front-end still reading those keys gets
        # undefined rather than an empty array, so the section simply disappears. That is
        # intended: half-rendering an empty section makes it much harder to notice it should
        # no longer be there than having it vanish outright.
        "reminders": reminders,
        "heavy": heavy,
        "edited": edited,     # the notification pool: user-edited entries not yet handled
        "big_events": big,
    }


async def build_muse_pending() -> dict:
    """"Is it time to muse?" — **counts and ages only, never content.**

    ------------------------------------------------------------
    What this is
    ------------------------------------------------------------
    Musing is **something that happens once things are quiet**; nobody muses while busy. So
    it neither waits for the model to remember to call it, nor goes into breath (only what
    there is no time to look up before speaking belongs by the door, and "there are N
    clusters left to look at" is not something that must be known before speaking).
    Instead, the system checks quietly in the background each day and only nudges once both
    conditions hold: (1) a long stretch with no activity, meaning things are quiet, and
    (2) there is actually something there.

    **The boundary**: Loci exposes this one query endpoint and nothing more. **What counts
       as quiet, how to nudge, and whether to stay silent at night all belong to the host.**
       Software released to other people cannot assume anything about the host's energy
       budget — Loci provides the capability, and the host decides what it costs.

    **Counts and ages only, not one character of content** — the same rule as the automatic
       memory hint, which reports how many and never what. Give a summary and the system has
       done the remembering, and the model will carry on from that summary. Give a count and
       it is a tap on the shoulder; the model goes and looks for itself.

    Shape (exactly four keys):
        {"mind_clusters": clusters accumulated on the realization side,
         "gist_fingers": fingers on the event side,
         "oldest_days": how many days the oldest one has been pending,
         "worth_poking": whether a nudge is warranted}
    `worth_poking` = (clusters or fingers reached `poke_min_clusters`) **and** (the oldest
    has been pending for `poke_min_age_days`).
    Both thresholds live in the `muse:` section of `config.yaml` and are read live —
    **nothing is hardcoded ahead of time.**
    """
    from core import _muse as M
    from core import _when as W

    cfg = M.muse_config(sh.config)
    # This takes **the same pass** the tool surface takes, view cache included. It used to
    #    run its own `load_records + propose_mind + propose_gist` over the whole store — a
    #    third parallel implementation, so the page could say "3 clusters" while muse() saw
    #    4, which is two different minds. The cache key is the buckets' write generation;
    #    better to invalidate too eagerly than to disagree.
    clusters, _scattered, _default_coords, fingers, _stats = await M.both_sides()

    now = W.now()
    ages: list[int] = []
    for t in clusters:
        ages += [(now - it.created).days for it in t.items if it.created]
    finger_count = 0
    for lst in fingers.values():
        finger_count += len(lst)
        for x in lst:
            # A finger's age is measured from the **end** of its span: how long it has sat
            # finished without being given a name.
            edge = x.end or x.boundary or x.start
            if edge is not None:
                ages.append((now - edge).days)

    oldest = max(ages) if ages else 0
    cluster_count = len(clusters)
    min_clusters = int(cfg["poke_min_clusters"])
    min_age_days = int(cfg["poke_min_age_days"])
    return {
        "mind_clusters": cluster_count,
        "gist_fingers": finger_count,
        "oldest_days": int(oldest),
        "worth_poking": bool((cluster_count >= min_clusters or finger_count >= min_clusters)
                             and oldest >= min_age_days),
    }


async def build_poke(query: str = "", when: str = "", room: str = "",
                      tag: str = "", floor=None) -> dict:
    """The nudge endpoint: Loci's single read-only poke — dreams (delivery) plus muse cluster
    counts (the nudge) plus structured recall scores, all answered in one call. **It reports
    state; it never writes and never decides** (the constitution: the system retrieves and
    lays things out, and the writing is always the model's own). The gateway asks this once
    per window, and invents no judgements of its own.

    The boundary of each of the three:

    `dreams`: the dreams still alive and waiting to be delivered, at whichever layer they
    are on. It **deliberately does not go through `core._dream.current_dream()`** — that
    endpoint is for a person actually fetching a dream, and each call counts as an act of
    recollection: it pushes the expiry point out and writes to disk (recollection can delay
    a dream's fading, but not prevent it). This endpoint exists for the host to ask "is
    there anything there", and quietly performing a recollection on someone's behalf every
    time it is asked would be stealing something. **This reads from disk and performs only
    the pure computation in `layer_of()`; it calls nothing that writes state.** The dream
    lifecycle — fragment for 30 minutes, then a single sentence for an hour, then the file
    is deleted and a trace remains — proceeds exactly as it would. Deletion and trace-leaving
    belong to other hooks (breath's maintain(), and the older `/api/dream/current`); this
    endpoint never does their work and never extends a dream's life.

    The layer may be the whole dream: during a long stretch with no messages — a real night —
    the whole version survives on disk, and this endpoint hands back the full text as-is
    (`rec["完整"]`, uncut and unmodified, following the same discipline as the fragment
    layer: a dream is a delivery, so give the whole thing). The whole layer does not decay
    with time. Only when the dream has been handed over and the user sends their next message,
    and the bridge hits `POST /api/loci/dream/wake` (`core._dream.degrade_on_wake()`), does it
    drop to the fragment layer. This endpoint remains **read-only**: it does not call
    `degrade_on_wake()`. Demotion is always something the bridge asks for explicitly, and is
    never done on its behalf here.
    A dream with an ingredient that may no longer be seen (archived, deleted, put out of
    mind, hung with an avoid hold since) is left out of `dreams` this round —
    `core._dream.withheld_ingredients()`. Left out is a shape the bridge already reads:
    no dream is the ordinary daytime answer.

    `muse_pending`: the cluster count, reusing `build_muse_pending()` directly (one cached
    pass, the same numbers muse()'s first step sees, with no rescan of the store).
    **The threshold reuses the existing `worth_poking`** (`poke_min_clusters` and
    `poke_min_age_days` in config.yaml's `muse:` section). Below the threshold it reports 0,
    so that a non-zero value on the gateway side genuinely means "now is the time to nudge"
    and the gateway need not invent a second set of thresholds. This matches the boundary
    above: Loci exposes only the "is it time to muse?" query, and the host decides how to
    nudge.

    `recall_scores`: present only when `query` is given, reusing `recall_data()` directly —
    **no new ranking logic**, and the same retrieval path the panel's `/api/loci/recall`
    takes. The related-memory hint currently reads `_render_search`'s rendered layout with a
    regex, which is fragile: change the layout and it silently stops working. This endpoint
    offers it a structured path instead, though **actually switching it over is a separate
    piece of work**; this only opens the door.
    """
    from core import _dream as _D

    dreams: list[dict] = []
    try:
        c = _D._c()
        now = _D._w.now()
        for rec in await _D.handable_dreams(_D.load_dreams()):
            layer = _D.layer_of(rec, now, c)
            if layer == "没了":
                continue          # something past its time does not play dead — but this gate is pure computation and deletes no files
            # The whole layer hands back the full text, uncut, following the same discipline
            #    as the fragment layer: a dream is a delivery, so give the whole thing. Only
            #    after demotion, when degrade_on_wake() has stripped the whole-text field,
            #    does it fall back to the fragment or single-sentence layer.
            if layer == "完整":
                content = rec.get("完整") or ""
            elif layer == "碎片":
                content = rec["碎片"]
            else:
                content = _D.first_sentence(rec["碎片"])
            dreams.append({
                "id": rec.get("id"), "层": layer, "内容": content,
                "v": rec.get("v"), "a": rec.get("a"),
                "nightmare": bool(rec.get("nightmare")),
                "织于": rec.get("织于"),
            })
    except Exception as e:                      # noqa: BLE001 - an unreadable dream must not blow up the whole poke endpoint
        logger.warning(f"[loci] poke 取梦失败: {e}")

    muse = await build_muse_pending()
    muse_pending = int(muse["mind_clusters"]) if muse.get("worth_poking") else 0

    scores: list[dict] = []
    q = str(query or "").strip()
    if q:
        from tools.recall.core import recall_data
        data = await recall_data(when=when, room=room, tag=tag, query=q, floor=floor)
        if data.get("ok"):
            scores = [{"id": e["id"], "score": e.get("score"),
                      "is_mind": e.get("kind") == "mind"}
                      for e in data.get("entries", []) if e.get("score") is not None]

    return {"dreams": dreams, "muse_pending": muse_pending, "recall_scores": scores}


async def build_setup() -> dict:
    """The screen at the top of the settings page: **make the silent things visible.**

    The rule comes from a single day on which every failure encountered was silent:
        no tagging key       -> memories save, but with no tags and no summary   no error
        no embedding         -> search loses one of its two legs                 no error
        no names configured  -> both names bleed into tags and drown search      no error
        no alias table       -> two spellings of one person stay two people      no error
        TZ set in container  -> new memories become "the future" and vanish      no error
    All five were hit, and every one was found either by a smoke test or by a human noticing.
    So the most important thing this screen does is not collect settings, it is **tell the
    reader what happens if they do not set them.**

    Every row carries three things: what it is, what state it is in now, and **what goes
       wrong if it is not right**. The third column is the point: a newcomer does not know
       what "AI_NAME unset" means. Write "not configured" and they skip it; write "both of
       your names will bleed into the tags" and they understand.
    This screen has two readers: a person, and **that person's AI** — they screenshot these
       rows into their own assistant, which then knows immediately what to change. So it has
       to be written in plain language, not as KEY_NAME unset.
    """
    import os as _os
    from tools import _subjects as subj

    rows: list[dict] = []

    def row(key, label, ok, now, why, fix="", note=False):
        """note=True means **a fact that has to be stated clearly**, not "you configured
        this wrong".

        Why they are separated: without the distinction, "the panel has no lock" sits there
        forever reporting "1 item needs attention" — even where leaving it unlocked was a
        deliberate decision. A decision that raises an alarm every day is not an alarm any
        more, it is noise, and then the real alarms get ignored along with it.
        """
        rows.append({"key": key, "label": label, "ok": bool(ok), "note": bool(note),
                     "now": now, "why": why, "fix": fix})

    cfg = sh.config or {}
    dehy = cfg.get("dehydration", {}) or {}
    emb = cfg.get("embedding", {}) or {}

    # 1. The tagging model
    d_key = str(dehy.get("api_key") or "")
    d_model = str(dehy.get("model") or "")
    d_base = str(dehy.get("base_url") or "")
    row("dehydration", "打标模型", bool(d_key and d_model),
        (d_model + "（" + (d_base or "默认地址") + "）") if d_key and d_model else "没配",
        "存得进去，但没有标签、没有摘要、也抽不出人名 —— 而且一声不响。"
        "搜索靠标签和摘要，所以等于存了一堆搜不到的东西。")

    # 2. Vectors — search's second leg
    e_on = str(emb.get("enabled", "")).strip().lower() in ("1", "true", "yes", "on")
    e_key = str(emb.get("api_key") or "")
    e_model = str(emb.get("model") or "")
    e_base = str(emb.get("base_url") or "")
    e_ok = e_on and bool(e_model) and (bool(e_key) or "localhost" in e_base or "ollama" in e_base)
    # 🔴 Being configured and being usable are two different things, and this row used to
    #    check only the first. On a fresh install the bundled compose file starts an Ollama
    #    container with **no model pulled**: everything comes up, every service reports
    #    healthy, THIS ROW SHOWS A GREEN TICK, and the first real write is what fails.
    #    So when the configuration looks complete, ask the backend for one actual vector.
    #    Configuration is the one moment someone is prepared to hear about configuration.
    e_detail = ""
    if e_ok:
        # Read it off `sh` every time rather than binding it once: hot reload replaces the
        # instance by assigning to `sh.embedding_engine`, and a captured reference would
        # keep probing the engine that is no longer in use.
        engine = getattr(sh, "embedding_engine", None)
        probe = getattr(engine, "probe", None)
        if callable(probe):
            try:
                works, why = await probe(timeout_seconds=3.0)
            except Exception as exc:                  # noqa: BLE001 - a screen must not die
                works, why = False, str(exc)
            if not works:
                e_ok = False
                e_detail = f"{e_model} 配好了，但用不了：{why}"
    row("embedding", "向量", e_ok,
        e_detail or ((e_model + "（" + (e_base or "默认地址") + "）") if e_ok else
                     ("开着但没配全" if e_on else "关着")),
        "搜索少一条腿：只剩字面匹配。换个说法搜同一件事就搜不到了 —— "
        "而它不会告诉你「这次没用上向量」。")

    # 3. Your name and the AI's name
    ai_name = _os.environ.get("AI_NAME", "").strip()
    owner = _os.environ.get("LOCI_OWNER_NAME", "").strip()
    row("names", "你和他的名字", bool(ai_name and owner),
        ((ai_name or "没配") + " / " + (owner or "没配")),
        "这两个名字要挡在标签外面。没配的话，你俩的名字会被当成普通词抽进标签 —— "
        "而几乎每条记忆都有你们，于是这两个词淹掉整个搜索。",
        "改的是容器的环境变量 AI_NAME / LOCI_OWNER_NAME")

    # 4. The alias table
    apath = subj._alias_path()
    a_exists = _os.path.isfile(apath)
    table = subj.load_alias_table()
    row("aliases", "别名表", a_exists,
        (apath + "（" + str(len(table)) + " 条写法）") if a_exists else ("还没有：" + apath),
        "同一个人的几种写法会被当成几个人。「老张」和「张三」各算一个，"
        "按人找记忆就永远只找到一半。",
        "面板「整理 → 人名表」上点一下就是往这张表里写")

    # 5. Which timezone "today" is cut in. (The container's own TZ no longer matters:
    #    stamps are written as UTC by name, see utils.now_iso.)
    tz = _w.tz_status()
    row("tz", "时区", not tz["problem"],
        ("LOCI_TZ=" + tz["name"]) if not tz["problem"] else (tz["problem"] + " ⚠️"),
        "「今天」「昨天」「这周」都按这个时区切。设错或者没设、又不在 +8 的话，"
        "每天有几个小时的记忆会算到隔壁那天去，「今天存的东西今天翻不到」。",
        "在启动环境里设 LOCI_TZ，例如 Asia/Shanghai、America/Los_Angeles")

    # 6. The panel lock — after the strip-down /api/* stopped authenticating, so that gate
    #    never appeared again
    locked = False
    try:
        from . import panel_auth as _pa
        locked = bool(_pa.gate_needed())
    except Exception:                                # noqa: BLE001
        locked = False
    has_pw = False
    try:
        has_pw = sh._load_password_hash() is not None
    except Exception:                                # noqa: BLE001
        has_pw = False
    sw_on = str(cfg.get("panel_auth", True)).strip().lower() not in (
        "0", "false", "no", "off", "none", "")
    if locked:
        row("panel_lock", "面板的锁", True, "锁着（要输密码）",
            "", note=True)
    elif sw_on and not has_pw:
        row("panel_lock", "面板的锁", False, "开关开着，但还没设密码 —— 所以现在没锁",
            "锁一个还没有钥匙的门只会把你自己关在外面，所以没密码的时候这道门不生效。"
            "去上面「账号」设一把密码，锁立刻就生效了。")
    else:
        # The switch was **explicitly turned off** -> that is a decision someone made, not a
        # problem. Still say what it means (anyone can read all of your memories), but do
        # not count it in "N items need attention" — a decision that raises an alarm every
        # day is not an alarm any more, it is noise, and the real alarms get ignored with it.
        row("panel_lock", "面板的锁", False, "没有锁（你关掉的）",
            "任何能访问到这个地址的人都能看你全部记忆 —— 这台机器监听 0.0.0.0，"
            "同一个网里的设备都算。要锁上：上面「账号」里那个开关。",
            note=True)

    # 🔴 The gap between two pieces of advice that are each correct on their own.
    #    Locking the panel is the recommended setup. Once it is locked, the four hook
    #    routes the bridge uses stop being exempt and need a key — and if that key was
    #    never set, the bridge starts getting 401s.
    #
    #    Nobody does anything wrong to reach that state: they follow the instruction to
    #    set a password, and something they were not told about breaks. The 401 does say
    #    what to configure, but only to whoever reads the bridge's log, and the symptom
    #    people actually notice is that dreams and nudges quietly stop arriving.
    #    So it is said HERE, on the screen that exists to answer "is this set up right".
    if locked:
        try:
            from web.panel_auth import hook_token
            key = hook_token()
        except Exception:                            # noqa: BLE001
            key = ""
        row("hook_token", "桥的钥匙", bool(key),
            "配好了" if key else "面板锁着，但没配钥匙 —— 用桥的话它会被挡在门外",
            "" if key else
            "面板一上锁，桥走的那四条口就不再免检了。没有钥匙的话，"
            "梦和「该发呆了」会安静地不再送达 —— 桥那边收到的是 401，"
            "而你这边只会觉得它们不来了。"
            "设一个环境变量 LOCI_HOOK_TOKEN（随便一串够长的字），桥那边设同一个。")

    # ---- Read-only facts: not "is this configured correctly", but "where things are" ----
    ver = ""
    try:
        vp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        with open(vp, "r", encoding="utf-8") as f:
            ver = f.read().strip()
    except OSError:
        ver = ""
    # The count goes through _visible — the same ruler recall, rooms and the subjects screen
    # use. Without that filter this reads 990 while the subjects screen reads 941: one thing
    # with two numbers, and anyone who sees both will simply assume one of them is wrong.
    try:
        from tools.recall.core import _visible as _vis
        allb = await sh.bucket_mgr.list_all(include_archive=False)
        n_buckets = sum(1 for b in allb if _vis(b.get("metadata", {}) or {}))
    except Exception:                                # noqa: BLE001
        n_buckets = -1
    return {
        "rows": rows,
        # Note rows do not count as "needs attention", or a decision already made would
        # raise an alarm every day.
        "bad": sum(1 for r in rows if not r["ok"] and not r["note"]),
        "facts": {
            "buckets_dir": str(cfg.get("buckets_dir") or os.environ.get("LOCI_BUCKETS_DIR") or ""),
            "log_file": os.environ.get("LOCI_LOG_FILE", ""),
            "alias_table": apath,
            "version": ver,
            "buckets": n_buckets,
            "in_docker": bool(sh.in_docker()) if hasattr(sh, "in_docker") else None,
            "tz_display": os.environ.get("LOCI_TZ", "").strip() or "Asia/Shanghai",
        },
    }


async def build_health() -> dict:
    """**Our own health check.**

    The upstream `/api/system/diagnostics` checks whether this open-source project is
    released correctly — ADR documents, the public tool manifest, vNext preflight, hosting
    environment variables. Not one of those is relevant to a person whose memories live in
    here.

    What this checks is **whether the memory itself is doing well**: is everything still
    there, can it be found, are the two external dependencies reachable, and could anything
    lost be recovered.
    """
    import shutil
    checks: list[dict] = []

    def add(label, status, message, action=""):
        checks.append({"label": label, "status": status,
                       "message": message, "action": action})

    # WARNING: this whole function used to run as one straight line. A failed bucket read, a
    # malformed metadata shape, a config section that was not a dict — an exception anywhere
    # meant **the entire health check returned 500**. Meanwhile the disk and dream sections
    # were `except: pass`, so two checks quietly disappeared and the summary still read
    # healthy. **A health check must not be all-or-nothing about itself.**
    # Each check now runs independently; one that blows up records a red row in place and the
    # rest carry on.
    def guard(label, fn, action=""):
        try:
            fn()
        except Exception as e:
            add(label, "error", f"这一项自己出错了：{type(e).__name__}: {e}", action)

    def need_buckets(label, fn, action=""):
        if not buckets_ok:
            add(label, "error", "读不到记忆库，这一项没法查", "先解决上面「记忆库读取」那条")
            return
        guard(label, fn, action)

    # ---- The base ingredient: read the buckets. A failure here must not take down the whole
    # ---- check; the independent items (config, disk) still run. ----
    metas: list[dict] = []
    n_with_archive = -1
    buckets_ok = True
    try:
        all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
        metas = [(b.get("metadata", {}) or {}) for b in all_buckets]
        # The wording used to say "N entries including the archive", but N was the count
        # above, which **excludes** the archive — the label did not match the number. The
        # archive has to be counted separately.
        n_with_archive = len(await sh.bucket_mgr.list_all(include_archive=True))
    except Exception as e:
        buckets_ok = False
        add("记忆库读取", "error", f"读不出记忆桶：{type(e).__name__}: {e}",
            "看容器日志 + buckets 目录挂载对不对")

    now = _w.now()          # local timezone

    # ---- Is everything still there ----
    from tools.recall.core import _visible
    visible: list[dict] = []
    bad_meta = 0
    for m in metas:
        # try per entry: one bad metadata record (a domain that is an integer, say) must not
        # silence the whole health check
        try:
            if _visible(m):
                visible.append(m)
        except Exception:
            bad_meta += 1
    if bad_meta:
        add("元数据形状", "error", f"{bad_meta} 条记忆的元数据读不动（字段类型不对）",
            "在「日志」里搜这几条的 id，多半是早期写入留下的")

    def sec_total():
        homeless = [m for m in visible if not str(m.get("room") or "")]
        # "Alive" goes through _visible, the same ruler used by recall, the subjects screen
        # and the small print on the settings page — one thing with two numbers means
        # whoever sees both will assume one of them is wrong.
        add("记忆总量", "ok",
            f"{len(visible)} 条活着的"
            + (f"（盘上一共 {n_with_archive} 条，含归档和旧版）"
               if n_with_archive >= 0 else ""))
        if homeless:
            add("没房间的记忆", "warn",
                f"{len(homeless)} 条没有 room，recall 的房间门筛不到它们",
                "跑 scripts/backfill_rooms.py --buckets <库目录> 先看，再加 --apply 补房间")
        else:
            add("房间", "ok", "每条都有房间")
    need_buckets("记忆总量", sec_total)

    # ---- Can it be found? (vector coverage) ----
    have_vec = 0
    try:
        con = sqlite3.connect(f"file:{_emb_db_path()}?mode=ro", uri=True)
        try:
            ids = {r[0] for r in con.execute("select bucket_id from embeddings")}
        finally:
            con.close()
        live_ids = {str(m.get("id") or "") for m in visible}
        have_vec = len(live_ids & ids)
        miss = len(live_ids) - have_vec
        if miss > max(3, len(live_ids) * 0.02):
            add("语义搜索覆盖", "warn",
                f"{miss} 条没有向量，query 门搜不到它们（只能靠关键词撞）",
                "看「日志」里 embedding 回填有没有报错；ollama 断了会积压")
        else:
            add("语义搜索覆盖", "ok", f"{have_vec}/{len(live_ids)} 条有向量")
    except Exception as e:
        add("语义搜索覆盖", "error", f"读不到向量库：{e}", "检查 embeddings.db")

    # ---- The two external dependencies (the only two places the system reaches the network) ----
    # Any section of cfg may fail to be a dict — a hand-edited config.yaml can do that — so
    # each gets its own guard.
    cfg = sh.config if isinstance(sh.config, dict) else {}

    def sec_deepseek():
        dehy = cfg.get("dehydration") or {}
        if not isinstance(dehy, dict):
            raise TypeError("config.yaml 里的 dehydration 不是一个配置块")
        if str(dehy.get("api_key") or "").strip() or os.environ.get("LOCI_API_KEY", ""):
            add("摘要/标签", "ok", f"配着 {dehy.get('model') or '?'}")
        else:
            add("摘要/标签", "warn",
                "没配 key —— 存进去的东西不会自动生成摘要和标签",
                "在 config.yaml 里配 dehydration.api_key")
    guard("摘要/标签", sec_deepseek, "检查 config.yaml 的 dehydration 段")

    def sec_embedding():
        emb = cfg.get("embedding") or {}
        if not isinstance(emb, dict):
            raise TypeError("config.yaml 里的 embedding 不是一个配置块")
        if _parse_ok(emb.get("enabled")):
            add("向量", "ok", f"开着，模型 {emb.get('model') or '?'}")
        else:
            add("向量", "warn",
                "关着 —— query 门只能靠关键词，搜不到「意思相近」的",
                "在 config.yaml 里开 embedding.enabled")
    guard("向量", sec_embedding, "检查 config.yaml 的 embedding 段")

    def sec_literal():
        from core.bm25_index import dependency_status
        deps = dependency_status()
        missing = [name for name, ok in deps.items() if not ok]
        if not missing:
            add("字面搜索", "ok", "rank_bm25 和 jieba 都在")
        else:
            add("字面搜索", "error",
                f"缺 {' / '.join(missing)} —— 字面搜索退成了整句子串匹配，"
                "换个说法、中文拆词都搜不到",
                "pip install " + " ".join(
                    "rank-bm25" if n == "rank_bm25" else n for n in missing)
                + "，装完重启")
    guard("字面搜索", sec_literal)

    def sec_tz():
        st = _w.tz_status()
        if st["problem"]:
            add("时区", "error",
                f"{st['problem']} —— 「今天」「昨天」「这周」按这个时区切",
                "在启动环境里设 LOCI_TZ，例如 Asia/Shanghai，然后重启")
        else:
            add("时区", "ok", f"LOCI_TZ={st['name']}")
    guard("时区", sec_tz)

    # ---- Could anything lost be recovered ----
    bd = str(cfg.get("buckets_dir") or "")

    def sec_persist():
        pers = sh.data_dir_persistence(bd)
        if pers.get("persistent"):
            add("数据持久性", "ok", pers.get("note") or "记忆目录在持久位置")
        else:
            add("数据持久性", "error", "记忆目录没挂到持久卷 —— 容器重建会丢！",
                "在 docker-compose 里挂到命名卷或宿主机目录")
    guard("数据持久性", sec_persist)

    def sec_disk():
        # This used to be `except: pass` — the disk check going quiet at exactly the moment
        # it most needed to speak.
        free_gb = shutil.disk_usage(bd).free / (1024**3)
        add("磁盘", "ok" if free_gb > 2 else "warn", f"还剩 {free_gb:.1f} GB",
            "" if free_gb > 2 else "腾点地方，写不进去就存不了记忆")
    guard("磁盘", sec_disk, f"确认 buckets_dir 存在：{bd or '(没配)'}")

    def sec_schema():
        from core import schema as _schema
        st = _schema.status(bd)
        if st["error"]:
            add("库的版本", "error", f"读不出库的版本：{st['error']}",
                "看 buckets/_state/schema.json")
        elif st["behind"]:
            add("库的版本", "error",
                f"库是第 {st['version']} 版，代码要第 {st['current']} 版",
                "停掉服务，先跑 python scripts/migrate.py 看要改什么，"
                "再加 --apply（会先备份整个库）")
        else:
            add("库的版本", "ok", f"第 {st['version']} 版")
    guard("库的版本", sec_schema)

    # ---- Is it still growing lately ----
    def sec_fresh():
        # An empty store is not "writes are broken": a fresh install should see "nothing yet"
        # rather than a screen of yellow. Memories present but none in the last seven days —
        # *that* might mean writes are broken, and only then is a warning warranted.
        fresh = 0
        for m in visible:
            # This used to be fromisoformat(str(created)[:19]) — exactly the slice that is
            # banned elsewhere. It cuts off the timezone suffix, producing a naive datetime,
            # while `now = _w.now()` above is timezone-aware; subtracting the two raises
            # TypeError, which the surrounding except then swallowed -> fresh was always 0 ->
            # the panel reported "nothing stored at all" every single day. This was the last
            # surviving [:19] in the codebase, and it was spotted in a screenshot. Silently
            # wrong, and frightening in exactly the wrong direction.
            ts = _w.parse_stamp(m.get("created"))
            if ts is not None and (now - ts).days < 7:
                fresh += 1
        if fresh:
            add("最近七天", "ok", f"存了 {fresh} 条")
        elif not visible:
            add("最近七天", "note", "还没存过东西 —— 存第一条之后这儿就有数了")
        else:
            add("最近七天", "warn",
                "一条都没存 —— 要么最近没聊，要么写入坏了",
                "去「日志」看看 grow 有没有报错")
    need_buckets("最近七天", sec_fresh)

    # ---- Are the things that should be there still there ----
    def _tags_of(m) -> list[str]:
        raw = m.get("tags")
        return [str(t) for t in raw] if isinstance(raw, (list, tuple)) else []

    def sec_profile():
        # The same gate the door uses (core.profile.door_note): a covered page keeps its
        # tag but is no longer the page.
        from core import _fold as _F
        tagged = [m for m in metas if _PROFILE_TAG in _tags_of(m)]
        profile = [m for m in tagged if not _F.is_covered(m)]
        if len(profile) == 1:
            add("门口那张纸", "ok", "名字页在，且只有一张")
        elif not profile and tagged:
            gone = tagged[0]
            add("门口那张纸", "error",
                f"名字页 {gone.get('id')} 被 {'、'.join(_F.covers_of(gone))} 换掉了，"
                "新版没带 tag —— 睁眼时档案那格是空的",
                f"给新版补上 tag {_PROFILE_TAG}（trace 的 tags 是整份替换，原来的一起写上）")
        elif not profile:
            add("门口那张纸", "note" if not visible else "warn",
                "还没有名字页 —— 睁眼时档案那格是空的"
                if not visible else "没有名字页 —— 睁眼时档案那格是空的",
                f"存一条带 tag {_PROFILE_TAG} 的记忆")
        else:
            add("门口那张纸", "error", f"有 {len(profile)} 张名字页，只该有一张", "合并掉多的")
    need_buckets("门口那张纸", sec_profile)

    def sec_pinned():
        pinned = [m for m in visible if m.get("pinned")]
        # Nothing pinned means "nothing pinned yet", not "broken": principles grow one at a
        # time.
        add("钉着的准则", "ok" if pinned else "note",
            f"{len(pinned)} 条" if pinned else "一条都没钉 —— 睁眼时准则那格是空的")
    need_buckets("钉着的准则", sec_pinned)

    def sec_big():
        # Renamed and downgraded. Two things changed:
        #   - the "big event" entry point was withdrawn; it is called a **period** now, and
        #     goes through fold(when=...)
        #   - the "long range" cell was dropped from both the waking screen and the panel
        #     (the back end had stopped supplying it long before)
        # And a period is **optional by design** — use one if you have one. Having no periods
        # is not a fault, and reporting it as a warning tells a fresh install "you are
        # missing something".
        big = [m for m in metas if _BIGEVENT_TAG in _tags_of(m)]
        add("时期", "ok" if big else "note",
            f"{len(big)} 个" if big else "还没给哪段日子起过名（不强制，有就用）")
    need_buckets("时期", sec_big)

    # ---- Chains that point at nothing ----
    async def sec_orphan():
        """A `from` that no longer resolves — **there are two kinds, and they are not the
        same thing.**

        This check used to merge both into one sentence: "the source of N memories points at
        a bucket that does not exist, most likely because that source was hard-deleted."
        Asked whether the health check was actually correct, a look at the data showed that
        of 26 such entries, **24 had their source sitting safely in the archive** —
        `trace(delete=True)` is a soft delete and a direct id lookup always recovers it —
        and only 2 were genuinely missing.
        So the number was right and the sentence was wrong, in the worst possible direction:
        describing something perfectly normal as "hard-deleted" sends someone hunting for an
        incident that never happened.
        """
        live_ids = {str(m.get("id") or "") for m in metas}
        try:
            allb = await sh.bucket_mgr.list_all(include_archive=True)
            all_ids = {str((b.get("metadata") or {}).get("id") or "") for b in allb}
        except Exception:                            # noqa: BLE001
            all_ids = live_ids                       # if the archive cannot be read, fall back to the old measure
        sunk = gone = 0
        for m in metas:
            for src in read_from_ids(m):
                if src in live_ids:
                    continue
                if src in all_ids:
                    sunk += 1
                else:
                    gone += 1
        if gone:
            add("断掉的 from 链", "warn",
                f"{gone} 条记忆的来源哪儿都找不到了",
                "这才是真断了：多半那条源被物理删过。星空里它们少一根线")
        elif not sunk:
            add("from 链", "ok", "每条 from 都指得到")
        if sunk:
            add("来源沉进归档区", "note",
                f"{sunk} 条记忆的来源已经归档 —— 没断，拿 id 直查捞得回",
                "" if gone else "")
    if not buckets_ok:
        add("from 链", "error", "读不到记忆库，这一项没法查", "先解决上面「记忆库读取」那条")
    else:
        try:
            await sec_orphan()
        except Exception as e:                       # noqa: BLE001
            add("from 链", "error", f"这一项自己出错了：{type(e).__name__}: {e}", "")

    # ---- Dreams on disk ----
    # Since night_fall was retired, this counts the new engine's dream files. **Empty is
    # normal**: dreams are time-driven and disappear if left alone, and a backlog that never
    # reaches the threshold simply means a night without dreams.
    def sec_dreams():
        # Same story: this used to be `except OSError: pass`, so a missing directory made
        # the whole check vanish.
        from core import _dream as _D
        n = len(_D.load_dreams())
        add("盘上的梦", "ok",
            f"{n} 个还在（时间到了自己会没）" if n else "空的（攒不到线就一夜无梦，正常）")
    guard("盘上的梦", sec_dreams, "确认 buckets/night_fall/dreams 目录在")

    # This used to hardcode three states (ok/warn/error), while the health check **has a
    #    fourth, `note`** — "you have not started yet", "this one is optional": neutral
    #    statements, not problems.
    #    The consequence: the page said "14 items" while the three summary numbers added up
    #    to 13. **The health check was under-reporting itself**, and the health check is
    #    precisely the thing whose job is to tell the truth.
    #    (The smoke test that asserts the summary matches the item count catches this.)
    # The rule: **count whatever states actually appear**, never a hardcoded list — a
    #    hardcoded list would miss the same way again when a fifth state is added.
    summary = {"ok": 0, "warn": 0, "error": 0}
    for c in checks:
        st = str(c.get("status") or "").lower() or "unknown"
        summary[st] = summary.get(st, 0) + 1
    return {"ok": summary["error"] == 0, "summary": summary, "checks": checks}


def _parse_ok(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# Routes
# ============================================================

def _request_of(request: Request):
    """The hook request as the guard resolved it (web/__init__ `_Gated`, core/scope.py);
    None outside the guard (a route called directly)."""
    return getattr(request.state, "loci_request", None)


def _scope_refusal(request: Request):
    """A refused request (no scope from a restricted host, a scope past its host's
    ceiling, one that cannot be read): the response saying so, else None. Nothing is read."""
    from starlette.responses import JSONResponse
    from core import scope as _scope
    req = _request_of(request)
    if req is None or not req.refused:
        return None
    line = req.first_line()
    return JSONResponse({"error": line, "scope": line},
                        status_code=400 if req.refusal == _scope.MALFORMED else 403)


def _scope_withholds(request: Request, what: str):
    """A road this version cannot filter by scope gives nothing to a scoped request, and
    says so: the response, else None (a refused request gets its refusal)."""
    from starlette.responses import JSONResponse
    from core import scope as _scope
    refused = _scope_refusal(request)
    if refused is not None:
        return refused
    req = _request_of(request)
    if req is None or req.whole_library:
        return None
    return JSONResponse({"scope": _scope.unsupported_line(what)})


def _slices_config() -> tuple[int, float]:
    """`slices:` in config -> (max lines per batch, guess threshold)."""
    from core import _slicer as _sl
    cfg = (sh.config or {}).get("slices") or {}
    try:
        max_lines = int(cfg.get("max_lines_per_batch") or _sl.DEFAULT_MAX_LINES_PER_BATCH)
    except (TypeError, ValueError):
        max_lines = _sl.DEFAULT_MAX_LINES_PER_BATCH
    try:
        threshold = float(cfg.get("guess_threshold", _sl.DEFAULT_GUESS_THRESHOLD))
    except (TypeError, ValueError):
        threshold = _sl.DEFAULT_GUESS_THRESHOLD
    return max(1, max_lines), threshold


def register(mcp) -> None:

    @mcp.custom_route("/loci", methods=["GET"])
    async def loci_page(request: Request) -> Response:
        from starlette.responses import HTMLResponse
        path = os.path.join(sh.repo_root, "frontend", "loci.html")
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            return HTMLResponse("<h1>loci.html not found</h1>", status_code=404)
        # The page hardcodes nobody's name: `{{AI_NAME}}` is filled in at serve time.
        # The shipped copy has to be blank, so that whoever clones it sees their own AI's
        # name rather than someone else's. The name comes from utils.get_ai_name() — the
        # AI_NAME environment variable, falling back to "AI".
        from utils import get_ai_name
        html = html.replace("{{AI_NAME}}", get_ai_name())
        return HTMLResponse(
            html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @mcp.custom_route("/loci/vendor/{path:path}", methods=["GET"])
    async def loci_vendor(request: Request) -> Response:
        """Locally served three.js, which the starfield page needs.

        The memory-starmap this was based on pulls three from a CDN at page load, so with no
        network it is just a black screen. The whole memory system runs on the user's own
        machine, and the starfield should not be the one place that breaks when the network
        does — so the library was vendored locally.

        Security: only .js is served, and no string from the request is ever concatenated
        into a path directly. After realpath it must still be inside the vendor directory,
        or this becomes the ?path=../../../etc/passwd kind of traversal.
        """
        from starlette.responses import Response as _Resp, JSONResponse
        rel = str(request.path_params.get("path") or "")
        if not rel.endswith(".js"):
            return JSONResponse({"error": "not found"}, status_code=404)
        root = os.path.realpath(os.path.join(sh.repo_root, "frontend", "vendor"))
        target = os.path.realpath(os.path.join(root, rel))
        if target != root and not target.startswith(root + os.sep):
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            with open(target, "rb") as f:
                return _Resp(f.read(), media_type="text/javascript",
                             headers={"Cache-Control": "public, max-age=604800"})
        except OSError:
            return JSONResponse({"error": "not found"}, status_code=404)

    # ---------------------------------------------------------
    # The main pool: recall's two skins — the card above, for the model, and the list below,
    # for a person.
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/recall", methods=["GET"])
    async def api_loci_recall(request: Request) -> Response:
        from starlette.responses import JSONResponse
        q = request.query_params
        # `by` was removed; `view="scene"` took its place.
        # The filters on this page match the tool surface's parameter list **word for word**:
        # never leave the panel with a knob the tool does not have.
        gates = {k: (q.get(k) or "").strip()
                 for k in ("when", "room", "tag", "query", "view")}
        try:
            slices = int(q.get("slices") or 0)
        except (TypeError, ValueError):
            slices = 0
        # The relevance floor is dragged on the page, following the similarity page's
        # existing pattern: the person draws the line, and this side only lays out the
        # distribution. Unset, it falls back to RELEVANCE_FLOOR (currently 35, the same
        # number as the slider's default).
        floor = None
        try:
            if (q.get("floor") or "").strip():
                floor = max(0.0, min(100.0, float(q.get("floor"))))
        except (TypeError, ValueError):
            floor = None
        try:
            # This used to call recall_data() and recall_core() separately, and each of them
            #    runs its own _collect — **the same search computed twice**.
            #    Measured with a query: 3 seconds on the tool surface, 8.6 seconds here, and
            #    the difference was that second pass.
            #    recall_text_and_data() gathers once, and both skins share it.
            from tools.recall.core import recall_text_and_data
            data = await recall_text_and_data(**gates, floor=floor, max_cells=slices)
            if not data.get("ok"):
                return JSONResponse(data, status_code=400)
            return JSONResponse(data)
        except Exception as e:
            logger.warning(f"[loci] recall 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # The room directory plus frequent tags — the first thing seen through either door.
    @mcp.custom_route("/api/loci/rooms", methods=["GET"])
    async def api_loci_rooms(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_rooms())
        except Exception as e:
            logger.warning(f"[loci] rooms 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # Starfield
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/graph", methods=["GET"])
    async def api_loci_graph(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_graph())
        except Exception as e:
            logger.warning(f"[loci] graph 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # Similarity check
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/similar", methods=["GET"])
    async def api_loci_similar(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            th = float(request.query_params.get("threshold") or _SIM_DEFAULT)
        except (TypeError, ValueError):
            th = _SIM_DEFAULT
        th = max(_SIM_FLOOR, min(99.9, th))
        try:
            limit = int(request.query_params.get("limit") or 120)
        except (TypeError, ValueError):
            limit = 120
        try:
            data = await _compute_pairs()
        except Exception as e:
            logger.warning(f"[loci] similar 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        info = data.get("info") or {}

        def _side(bid: str) -> dict:
            it = info.get(bid) or {}
            body = it.get("body") or ""
            return {
                "id": bid,
                "short": it.get("short") or bid[:6],
                "label": it.get("summary") or it.get("name") or body[:40],
                "room": it.get("room") or "",
                "created": it.get("created") or "",
                "preview": re.sub(r"\s+", " ", body)[:400],
                "len": len(body),
            }

        out = []
        for score, a, b in data.get("pairs", []):
            if score < th:
                break            # pairs is already sorted by descending score, so stop once below the line
            if a not in info or b not in info:
                continue
            out.append({"score": round(score, 1), "a": _side(a), "b": _side(b),
                        # whether the automatic tagger (threshold 80, and not the same ruler
                        # as this page) has already flagged this pair
                        "tagged": b in (info[a].get("tagged") or [])
                                  or a in (info[b].get("tagged") or [])})
            if len(out) >= limit:
                break

        counted = sum(1 for score, a, b in data.get("pairs", []) if score >= th)
        return JSONResponse({
            "threshold": th,
            "default": _SIM_DEFAULT,
            "floor": _SIM_FLOOR,
            "pairs": out,
            "matched": counted,
            "truncated": counted > len(out),
            "n": data.get("n", 0),
            "total_pairs": data.get("total_pairs", 0),
            # histogram: 20 buckets, 5 points each
            "hist": data.get("hist", []),
            "no_vectors": bool(data.get("no_vectors")),
            # True = the memory cap was hit, so the low-score range is incomplete and
            # `matched` reads lower than reality
            "capped": bool(data.get("capped")),
        })

    @mcp.custom_route("/api/loci/similar/action", methods=["POST"])
    async def api_loci_similar_action(request: Request) -> Response:
        """The human verdict. **The only write endpoint here.**

        keep = do nothing (both stay; WARNING: it merely stops showing in this page session,
               nothing is written to disk, and a refresh brings it back — stated honestly
               here because it was not obvious)
        sink = sink one: go through trace(delete=True), a soft delete into the archive that a
               direct id lookup always recovers.

        WARNING: **the authorization check added here was the most serious issue found in
        review.** This used to accept a single `id` and call `trace(delete=True)` on it. But
        trace's delete branch runs **before** its protected check — meaning that once logged
        in, anyone could construct `{"action":"sink","id":<any bucket id>}` and sink the
        profile fact, a big event, or a pinned core bucket, even one that never appeared on
        the similarity page at all.
        Both ends of the pair must now be supplied, and the server verifies for itself that
        the pair really exists.
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        action = str(body.get("action") or "").strip().lower()
        if action == "keep":
            return JSONResponse({"ok": True, "action": "keep", "persisted": False})
        if action != "sink":
            return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

        bucket_id = str(body.get("id") or "").strip()
        a = str(body.get("a") or "").strip()
        b = str(body.get("b") or "").strip()
        if not bucket_id or not a or not b:
            return JSONResponse(
                {"error": "sink 必须同时给 a、b（这一对的两端）和 id（要沉的那个）"},
                status_code=400)
        if bucket_id not in (a, b):
            return JSONResponse({"error": "id 必须是 a 或 b 其中之一"}, status_code=400)
        if a == b:
            return JSONResponse({"error": "a 和 b 不能是同一个"}, status_code=400)

        try:
            data = await _compute_pairs()
            info = data.get("info", {})
            # 1. Both ends must be buckets that really exist and are visible on this page.
            if a not in info or b not in info:
                return JSONResponse(
                    {"error": "这一对里有一端不在相似度页上（可能已归档、已换版或是情绪种子）"},
                    status_code=409)
            # 2. This pair must actually have been computed (order does not matter).
            hit = any((x == a and y == b) or (x == b and y == a)
                      for _s, x, y in data.get("pairs", []))
            if not hit:
                return JSONResponse(
                    {"error": "这一对不在当前的相似结果里，不能从这儿沉"},
                    status_code=409)
            # 3. Nothing that should never have been up for duplicate review may be sunk
            #    through this endpoint.
            target = await sh.bucket_mgr.get(bucket_id)
            if not target:
                return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)
            tmeta = target.get("metadata", {}) or {}
            ttags = [str(t) for t in (tmeta.get("tags") or [])]
            if tmeta.get("pinned") or tmeta.get("protected"):
                return JSONResponse(
                    {"error": "这是 pinned/protected 的核心桶，不从判重这儿沉"},
                    status_code=409)
            if _PROFILE_TAG in ttags or _BIGEVENT_TAG in ttags:
                return JSONResponse(
                    {"error": "档案事实 / 大 event 不从判重这儿沉"}, status_code=409)

            from tools.trace.core import trace_core
            msg = str(await trace_core(bucket_id=bucket_id, delete=True))
            # trace's delete branch returns one sentence and no structured result — a
            # "not found" reply means the delete did not happen
            ok = not msg.startswith("未找到")
            if not ok:
                return JSONResponse({"ok": False, "action": "sink", "id": bucket_id,
                                     "msg": msg}, status_code=404)
            with _sim_lock:
                _sim_cache["key"] = None      # one entry sank, so recompute next time
            return JSONResponse({"ok": True, "action": "sink", "id": bucket_id,
                                 "msg": msg})
        except Exception as e:
            logger.warning(f"[loci] 裁决失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # Three write endpoints the user drives by hand:
    # 1. The close button — the user is the one who knows whether something is finished, and
    #    should not have to wait to be asked.
    # 2. The "asked about this" ping — the stamp is only applied at the moment the question
    #    is actually shown to them.
    # 3. Event correction — only an event can be corrected this way; mind has no such route
    #    by design. The original text is left untouched, and a correction is stored as a new
    #    entry pointing back through `from`. Nothing is ever really deleted.
    # All three go through `_write_body` (the same-origin check) plus a direct in-process
    # call to `trace_core` / `bucket_mgr`, following the similar/action precedent rather than
    # going out through the MCP layer.
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/want/resolve", methods=["POST"])
    async def api_loci_want_resolve(request: Request) -> Response:
        """The close button: one click sets status to resolved or abandoned and records that
        the user closed it.

        Open only to something wanted that is still open (telic, not closed) — closing is not
        an action that applies to anything else. A repeat click on something already closed is caught
        by trace's "no fields need changing" path, so it neither errors nor overwrites
        closed_by a second time.
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        bucket_id = str(body.get("id") or "").strip()
        new_status = str(body.get("status") or "").strip().lower()
        if not bucket_id:
            return JSONResponse({"error": "缺 id"}, status_code=400)
        if new_status not in ("resolved", "abandoned"):
            return JSONResponse(
                {"error": f'status 只能是 "resolved"（放下了）或 "abandoned"（不做了），收到：{new_status}'},
                status_code=400)

        target = await sh.bucket_mgr.get(bucket_id)
        if not target:
            return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)
        tmeta = target.get("metadata", {}) or {}
        from utils import is_closed, is_telic
        if not is_telic(tmeta) or is_closed(tmeta):
            return JSONResponse(
                {"error": "这条不是还开着的想要（telic 且没关），没有结案这个动作"},
                status_code=409)

        try:
            from tools.trace.core import trace_core
            # `closed_by` records that a PERSON closed this, rather than that I noticed it
            # myself — see the note in tools/trace/core.py. Nothing compares this value; it
            # is free text that exists to be read. It used to name one specific person,
            # which meant every install would write that name into its own data.
            msg = str(await trace_core(bucket_id=bucket_id, status=new_status, closed_by="user"))
            return JSONResponse({"ok": True, "id": bucket_id, "status": new_status, "msg": msg})
        except Exception as e:
            logger.warning(f"[loci] 结案失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/want/asked", methods=["POST"])
    async def api_loci_want_asked(request: Request) -> Response:
        """The "last asked" stamp, applied once at the moment the panel **actually renders**
        the question line where the user can see it.

        This does not decide whether the entry is the longest-standing one — that is already
        computed as `heavy_question_id` in `/api/loci/profile`. This endpoint only applies
        the stamp, and trusts whoever calls it.
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        bucket_id = str(body.get("id") or "").strip()
        if not bucket_id:
            return JSONResponse({"error": "缺 id"}, status_code=400)
        target = await sh.bucket_mgr.get(bucket_id)
        if not target:
            return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)

        try:
            from tools.trace.core import trace_core
            await trace_core(bucket_id=bucket_id, mark_asked=True)
            fresh = await sh.bucket_mgr.get(bucket_id)
            last_asked = str((fresh or {}).get("metadata", {}).get("last_asked") or "")
            return JSONResponse({"ok": True, "id": bucket_id, "last_asked": last_asked})
        except Exception as e:
            logger.warning(f"[loci] 记「问过了」失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/event/correct", methods=["POST"])
    async def api_loci_event_correct(request: Request) -> Response:
        """The user corrects an event: **the original text is left untouched**, and the
        correction is stored as a separate entry whose `from` points back at it.

        A user's edit does not become truth directly. What lands is **a new event**, carrying
        the `core.profile._EDITED_BY_USER_TAG` tag and `from=[old id]`, with the old bucket
        untouched. Whether to accept the correction is decided by folding it, and the folding
        hand always belongs to the model. The notification is that new bucket itself
        (`core.profile.edited_by_user()` scans for that same tag on entries not yet folded;
        see the comments there).

        mind has no such entry point — and that is not enforced merely by the front-end not
        drawing a button; it is hard-checked here as well. A realization is the model's own
        judgement: the user may disagree with it, but changing it has to be the model's own
        act.
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        old_id = str(body.get("id") or "").strip()
        new_text = str(body.get("text") or "")
        if not old_id:
            return JSONResponse({"error": "缺 id"}, status_code=400)
        if not new_text.strip():
            return JSONResponse({"error": "text 不能为空——改完之后的完整正文"}, status_code=400)

        old = await sh.bucket_mgr.get(old_id)
        if not old:
            return JSONResponse({"error": f"查无此桶（也可能已归档）：{old_id}"}, status_code=404)
        old_meta = old.get("metadata", {}) or {}

        from core._rooms import is_event_room, is_mind_room
        old_room = str(old_meta.get("room") or "")
        if is_mind_room(old_room):
            return JSONResponse(
                {"error": "mind 不能从这儿改——mind 是我的判断，你可以不同意，"
                          "但得由我自己改（跟我说，我认同了自己 regrow）"},
                status_code=403)
        if not is_event_room(old_room):
            return JSONResponse(
                {"error": f"这不是一条 event（room={old_room or '未分房'}），"
                          "改错这个动作只对 event 开放"},
                status_code=409)

        try:
            from tools.grow.rooms_path import grow_event
            from core.profile import _EDITED_BY_USER_TAG
            # v/a are inherited from the old bucket: this is a factual correction, not a new
            # emotional experience, so nobody should be made to re-score the coordinates.
            old_v = old_meta.get("valence", 0.5)
            old_a = old_meta.get("arousal", 0.3)
            msg = await grow_event(
                items=[{"room": old_room, "text": new_text, "v": old_v, "a": old_a}],
                from_ids=[old_id],
            )
            m = re.search(r"📝([0-9a-f]{12})", msg)
            if not m:
                return JSONResponse({"error": f"新桶落盘失败：{msg}"}, status_code=500)
            new_id = m.group(1)
            # Apply _EDITED_BY_USER_TAG by merging, not replacing — the same reason as
            # rooms_path._backfill_one: the background-filled tags may not have landed yet,
            # and trace(tags=...) replaces the whole list, which would wipe them out. So this
            # reads the new bucket's current tags, merges into them, and goes through
            # bucket_mgr.update rather than trace.
            fresh = await sh.bucket_mgr.get(new_id)
            cur_tags = [str(t) for t in ((fresh or {}).get("metadata", {}).get("tags") or [])]
            merged_tags = list(dict.fromkeys(cur_tags + [_EDITED_BY_USER_TAG]))
            await sh.bucket_mgr.update(new_id, tags=merged_tags)
            return JSONResponse({"ok": True, "old_id": old_id, "new_id": new_id, "msg": msg})
        except Exception as e:
            logger.warning(f"[loci] event 改错失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # This panel's own password — the point being to depend on no other panel at all.
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/health", methods=["GET"])
    async def api_loci_health(request: Request) -> Response:
        """Our own health check. The upstream /api/system/diagnostics checks release
        compliance, not whether this memory is doing well."""
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_health())
        except Exception as e:
            logger.warning(f"[loci] health 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/auth/state", methods=["GET"])
    async def api_loci_auth_state(request: Request) -> Response:
        """Where this password currently lives and whether one needs to be set. Public, and
        carries no information about the password itself.

        WARNING: the `authed` field — whether a cookie session was logged in — was removed:
        /api/* is not authenticated at this layer any more, and there is no such thing as a
        session here. This password now governs exactly one thing: whether the remote MCP
        OAuth authorization page (bridge/oauth.py) accepts you. It no longer governs access
        to this panel screen.
        """
        from starlette.responses import JSONResponse
        try:
            has_file_password = sh._load_password_hash() is not None
        except sh.AuthPersistenceError as e:
            # An unreadable auth file is **not** "no password here". This route is public
            # and the gate overlay reads it; reporting "nothing set" would tell the owner
            # (and anyone else) that the panel is free to initialize.
            logger.error(f"[loci] auth 存储损坏，读不出口令 hash：{e}")
            has_file_password = True
        return JSONResponse({
            "setup_needed": sh._is_setup_needed(),
            # True = the password still comes from an environment variable, so it cannot be
            # changed here and the security question is unavailable
            "env_locked": bool(os.environ.get("LOCI_DASHBOARD_PASSWORD", "")),
            "has_file_password": has_file_password,
            "question": str(sh._load_auth_data().get("security_question") or ""),
        }, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/api/loci/auth/set-password", methods=["POST"])
    async def api_loci_set_password(request: Request) -> Response:
        """Write the password into a file (`.dashboard_auth.json`), taking over from the
        environment variable.

        Why this needed its own endpoint: both of the official routes were dead here.
          - `/auth/change-password` refuses outright while the environment variable exists.
          - `/auth/setup` accepts loopback only, but a request forwarded through Docker
            arrives with a container-network client IP, so **even opening localhost on your
            own machine gets a 403**.
        So a file-based password has to exist before the environment variable can be removed,
        or nobody can get in at all.

        WARNING: the original gate here was "you must already be logged in", meaning a valid
        cookie session. The strip-down removed cookie sessions entirely (/api/* is not
        authenticated at this layer), **and this gate must not loosen along with it** — this
        password is not the panel's door, it is the door to the remote MCP OAuth
        authorization page (bridge/oauth.py), and taking it over means being handed an MCP
        token that reads and writes every memory.
        The gate was rewritten not to depend on a session: **first-time setup**
        (`_is_setup_needed()`, meaning neither the file nor the environment holds a password)
        is allowed through; **when a password already exists**, the body must carry the
        correct `current_password`, verified through the same `_verify_password_for_rotation`
        the OAuth page uses, behind the same login rate limiting
        (`_login_retry_after` / `_reserve_global_login_attempt`), with a compare-and-swap
        write-back against concurrent changes. Hashing and persistence all go through
        _shared; no cryptography is implemented here.
        **The password travels from the browser straight into this endpoint and passes
        through no one's hands on the way.**
        """
        from starlette.responses import JSONResponse
        try:
            body = await _write_body(request)   # same-origin check plus Content-Type
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        pw = body.get("password", "")
        if not isinstance(pw, str):
            return JSONResponse({"error": "密码得是字符串"}, status_code=400)
        pw = pw.strip()
        if not 6 <= len(pw) <= 1024:
            return JSONResponse({"error": "密码 6~1024 位"}, status_code=400)

        proof = None
        if not sh._is_setup_needed():
            # A password is already standing guard: changing it requires proving knowledge of
            # the old one. There is no session to fall back on any more.
            retry = sh._login_retry_after(request)
            if retry:
                return JSONResponse({"error": f"尝试过于频繁，请 {retry} 秒后再试"},
                                    status_code=429, headers={"Retry-After": str(retry)})
            global_retry = sh._reserve_global_login_attempt()
            if global_retry:
                return JSONResponse({"error": f"登录服务繁忙，请 {global_retry} 秒后重试"},
                                    status_code=429, headers={"Retry-After": str(global_retry)})
            current = body.get("current_password", "")
            if not isinstance(current, str) or len(current) > 1024:
                sh._record_login_failure(request)
                return JSONResponse({"error": "current_password 格式无效"}, status_code=400)
            verified, queued_retry = await sh._run_public_password_verification(
                request, sh._verify_password_for_rotation, current
            )
            if queued_retry:
                return JSONResponse({"error": f"尝试过于频繁，请 {queued_retry} 秒后再试"},
                                    status_code=429, headers={"Retry-After": str(queued_retry)})
            if not verified:
                sh._record_login_failure(request)
                return JSONResponse({"error": "当前密码不对"}, status_code=401)
            sh._record_login_success(request)
            proof = verified  # CredentialProof, used for the compare-and-swap write-back against a concurrent change
        try:
            # PBKDF2 blocks the event loop for roughly 100ms. This is a button pressed a
            # handful of times in a lifetime, so that is accepted rather than introducing a
            # thread pool for it. The high-frequency login path uses the
            # _password_work_semaphore above instead.
            if proof is not None:
                ok = sh._save_password_hash(
                    pw, expected_hash=proof.value, expected_generation=proof.generation,
                )
            else:
                ok = sh._save_password_hash(pw)
        except Exception as e:
            logger.warning(f"[loci] 存密码失败: {e}")
            return JSONResponse({"error": f"写不进去：{e}"}, status_code=500)
        if not ok:
            return JSONResponse({"error": "写不进去（并发改动？再试一次）"}, status_code=409)
        return JSONResponse({
            "ok": True,
            "env_locked": bool(os.environ.get("LOCI_DASHBOARD_PASSWORD", "")),
            "next": ("密码已经存进文件了。现在去启动 Loci 的地方（docker-compose 文件，"
                     "或者你设环境变量的地方）删掉 LOCI_DASHBOARD_PASSWORD、重启，"
                     "新密码才真正接管（环境变量还在的时候它优先）。"),
        })

    # ---------------------------------------------------------
    # The profile — the note by the door
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/profile", methods=["GET"])
    async def api_loci_profile(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_profile())
        except Exception as e:
            logger.warning(f"[loci] profile 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # "Something comes back to you" — give me another
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/recollect", methods=["GET"])
    async def api_loci_recollect(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            n = int(request.query_params.get("n") or 2)
        except ValueError:
            n = 2
        try:
            return JSONResponse(await build_recollect(max(1, min(n, 5))))
        except Exception as e:
            logger.warning(f"[loci] recollect 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # Sources after the fact: the host's raw lines in, pending slices out (core/_slicer.py)
    # ---------------------------------------------------------
    @mcp.custom_route("/api/v2/slices", methods=["POST"])
    async def api_v2_slices_take(request: Request) -> Response:
        """The host hands over a stretch of raw lines before it lets go of them:
        {source: {system, instance, container}, day, lines: [{id, text, at?, speaker?}],
        revision?}. The side model slices them; the slices wait for the main model
        (recall(view="slices")). A `fingerprint_by` in the body is accepted and not used:
        a slice's fingerprint is Loci's own (core/_slicer.py). 400 for a malformed batch,
        502 when the side model fails — then nothing is stored, and the host may send the
        same batch again."""
        from starlette.responses import JSONResponse
        from core import _slicer as _sl
        from core import _sources as _src
        try:
            body = await sh._read_json_object(request)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"error": f"body: {e}"}, status_code=400)
        # A host delivers only material its credential reaches (`max_grant`); past it the
        # batch is refused whole, saying where.
        req = _request_of(request)
        top = req.host.max_grant if (req is not None and req.host is not None) else None
        if top is not None:
            src = body.get("source") if isinstance(body.get("source"), dict) else {}
            try:
                place = _src.Place.from_mapping(
                    {k: src.get(k) for k in ("system", "instance", "container")})
            except _src.SourceRecordError as e:
                return JSONResponse({"error": f"source: {e}"}, status_code=400)
            if not any(place.within(t) for t in top):
                return JSONResponse({"error": f"source {place.label()} is past this host's "
                                     "max_grant; nothing was stored"}, status_code=403)
        max_lines, threshold = _slices_config()
        try:
            out = await _sl.take_batch(sh.bucket_mgr, body,
                                       model=_sl.side_model(sh.dehydrator, sh.config),
                                       max_lines=max_lines, threshold=threshold)
        except _sl.BatchError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except _sl.SlicerError as e:
            logger.warning(f"[loci] slicing failed, nothing stored: {e}")
            return JSONResponse({"error": f"the side model failed, nothing was stored: {e}"},
                                status_code=502)
        return JSONResponse(out)

    @mcp.custom_route("/api/v2/slices", methods=["GET"])
    async def api_v2_slices_pending(request: Request) -> Response:
        """The pending slices, newest batch first, for the host's own prompt-building:
        {pending, batches: [{batch_id, source, day, revision, slices: [...]}], scope}.
        A scoped request sees the batches of containers its grant covers whole, and
        only the guesses it may read (tools/_slices.visible_batches)."""
        from starlette.responses import JSONResponse
        from tools import _slices
        refused = _scope_refusal(request)
        if refused is not None:
            return refused
        batches = await _slices.visible_batches()
        req = _request_of(request)
        return JSONResponse({"pending": await _slices.pending_seen(),
                             "batches": batches,
                             "scope": req.first_line() if req is not None else ""})

    # ---------------------------------------------------------
    # breath for a host's hook (tools/breath/awaken.py)
    # ---------------------------------------------------------
    @mcp.custom_route("/api/v2/breath", methods=["GET"])
    async def api_v2_breath(request: Request) -> Response:
        """The waking screen a host hands its model when a window opens — the second-tier
        hook in the README. Plain text by default, exactly what breath() returns;
        `?format=json` gives the object both are made from, under stable keys
        {core, prospective, recent, involuntary, invalidation, earliest}. Either way it is
        handed to a model, so a question in it counts as asked (stamp_asked), as it does
        through the tool. Like every read, it opens with the request's scope: the text's
        first line, the object's `scope`; a refused request gets the refusal and nothing
        else."""
        from starlette.responses import JSONResponse, PlainTextResponse
        from tools.breath.awaken import build_breath, record_shown, render_breath, stamp_asked
        fmt = str(request.query_params.get("format") or "text").strip().lower()
        if fmt not in ("text", "json"):
            return JSONResponse({"error": "format is text or json"}, status_code=400)
        refused = _scope_refusal(request)
        if refused is not None:
            return refused
        try:
            b = await build_breath()
            text = render_breath(b)
            await stamp_asked(b)
            record_shown(b, None if fmt == "json" else text)
        except Exception as e:
            logger.warning(f"[loci] breath failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        req = _request_of(request)
        line = req.first_line() if req is not None else ""
        if fmt == "json":
            return JSONResponse({**b, "scope": line} if line else b)
        return PlainTextResponse(f"{line}\n{text}" if line else text)

    # ---------------------------------------------------------
    # A host's source change, and the ledger read back by seq (core/_source_change.py,
    # core/_ledger.py)
    # ---------------------------------------------------------
    @mcp.custom_route("/api/v2/source/change", methods=["POST"])
    async def api_v2_source_change(request: Request) -> Response:
        """{change_id, source, host_seq, change, revision?, use?} from a host. 200 with
        `status` for every outcome of a well-formed change (applied, duplicate, conflict,
        stale, forbidden, unknown_source); 400 for a malformed one; 403 when the caller is
        the panel rather than a host. Resending the same change_id carries on a cleanup
        left pending."""
        from starlette.responses import JSONResponse
        from core import _source_change as _sc
        try:
            body = await sh._read_json_object(request)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"error": f"body: {e}"}, status_code=400)
        req = _request_of(request)
        host = req.host if req is not None else None
        try:
            status, out = await _sc.handle(sh.bucket_mgr, body, host,
                                           dehydrator=sh.dehydrator)
        except Exception as e:
            logger.warning(f"[loci] source change failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(out, status_code=status)

    @mcp.custom_route("/api/v2/changes", methods=["GET"])
    async def api_v2_changes(request: Request) -> Response:
        """The ledger past seq `since` (default 0), at most `limit` lines (default 1000,
        at most 5000): {since, next, more, changes}. A host with a ceiling sees only what
        its credential reaches; the panel and an open host see every line. Ask again from
        `next` while `more`."""
        from starlette.responses import JSONResponse
        from core import _ledger
        from core import scope as _scope
        try:
            since = int(request.query_params.get("since") or 0)
            limit = int(request.query_params.get("limit") or _ledger.CHANGES_LIMIT)
        except ValueError:
            return JSONResponse({"error": "since and limit are integers"}, status_code=400)
        if since < 0 or limit < 1:
            return JSONResponse({"error": "since >= 0 and limit >= 1"}, status_code=400)
        req = _request_of(request)
        host = req.host if req is not None else None
        if req is not None and req.host is None and req.mode == _scope.OPEN:
            host = _scope.Host("panel", scope_mode=_scope.OPEN)
        try:
            out = await _ledger.changes_since(sh.bucket_mgr, host, since, limit)
        except Exception as e:
            logger.warning(f"[loci] changes failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(out)

    # ---------------------------------------------------------
    # "Is it time to muse?" — the endpoint the host's wake-up leg asks
    # ---------------------------------------------------------
    @mcp.custom_route("/api/muse/pending", methods=["GET"])
    async def api_muse_pending(request: Request) -> Response:
        """Counts and ages, no content. **Deliberately not behind cookie auth.**

        The caller is the gateway's wake-up leg — a separate process with no access to a
        browser session — not a page the user is looking at. And what it hands back is three
        numbers and a boolean: **not one character of it is memory.**
        (On the same port, `/mcp` itself is reachable directly when `mcp_require_auth` is
         false. This opens no new hole; it simply does not add a gate.)
        """
        from starlette.responses import JSONResponse
        # Counts of the whole library: under a scope a count is a leak, so nothing.
        withheld = _scope_withholds(request, "发呆")
        if withheld is not None:
            return withheld
        try:
            return JSONResponse(await build_muse_pending())
        except Exception as e:
            logger.warning(f"[loci] muse/pending 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # The nudge endpoint: dreams (delivery) + muse cluster count (the nudge) + structured
    # recall scores, all answered in one call.
    # The gateway asks once at the start of each window (on the `newWindow` signal, and not
    # again within the window).
    # GET, no side effects — **deliberately not behind cookie auth**, for the same reason as
    # `/api/muse/pending` and `/api/dream/current`: the caller is the bridge, a separate
    # process, not a page the user is looking at.
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/subjects", methods=["GET"])
    async def api_loci_subjects(request: Request) -> Response:
        """The data behind the "who is in here" screen. **Read-only** — it counts, and never
        touches aliases.yaml.

        Before this existed, the only way to get these numbers was a hand-written script
        scanning the whole store, which the panel obviously cannot do on every page load.
        Merging and renaming are **write** operations and live on a separate endpoint, so
        that the rule holds: the system lays things out, and the merge is a human click.
        """
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_subjects())
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"[loci] subjects 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/setup", methods=["GET"])
    async def api_loci_setup(request: Request) -> Response:
        """The screen at the top of the settings page: five status rows plus read-only facts.
        **Read-only.**

        Why it exists: all five failures encountered in one day were silent, and not one of
        them raised an error. This endpoint's job is not to collect settings, it is to turn
        the silent things into visible ones.
        """
        from starlette.responses import JSONResponse
        try:
            return JSONResponse(await build_setup())
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"[loci] setup 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/subjects/action", methods=["POST"])
    async def api_loci_subjects_action(request: Request) -> Response:
        """The three actions on the subjects screen. **This is a write endpoint** — it writes
        buckets/aliases.yaml.

        Same principle as muse and fold: **the system lays things out, and which one to
        change is a human click.** So there is no automatic trigger path here, and no bulk
        "tidy all of this up for me" request is accepted: one call changes one name.

        The three actions reduce to two operations, because merging and renaming are the same
        thing — folding one name's entry into another (`_subjects.merge_names`: its key
        and aliases become the other's aliases, its links move, its entry goes):
          not_person  this is not a person -> record it on the blocklist so it is never
                      extracted again.
                      **Not one byte of the historical entries is touched.** Rewriting
                         historical metadata means writing into someone's memories, and
                         "this is what the model extracted at the time" is itself a fact. The
                         blocklist is enough, and it can be undone at any moment.
          merge       these two are one person -> fold `name` into `target`
          rename      give them a proper name -> the same, with `target` as the new canonical
                      name (created when the table does not have it)

        WARNING: all of it applies **going forward** only. Older entries keep their old names
           on disk; there is no migration script for this table. The panel screen merges them
           for display according to the table, which is why a row disappears the moment the
           click lands.
        """
        from starlette.responses import JSONResponse
        from tools import _subjects as subj
        try:
            body = await _write_body(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        action = str(body.get("action") or "").strip()
        name = body.get("name")
        target = body.get("target")
        try:
            if action == "not_person":
                changed = subj.mark_not_person(name)
                note = ("记下了，以后不再抽它（历史那几条没动）" if changed
                        else "它已经在黑名单里了")
            elif action in ("merge", "rename"):
                changed = subj.merge_names(name, target)
                note = ("写进别名表了 —— 只管以后，老条目盘上还是老名字" if changed
                        else "这条已经在表里了")
            else:
                return JSONResponse(
                    {"error": "action 只有三个：not_person / merge / rename"},
                    status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"[loci] subjects/action 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "changed": bool(changed), "note": note})

    @mcp.custom_route("/api/logs", methods=["GET"])
    async def api_logs(request: Request) -> Response:
        """Logs: read the tail of server.log. **Read-only.**

        Restored. This route used to live in `web/system.py` and went with it when the twenty
        upstream modules were cut — while the panel's entire log section kept calling it,
        getting HTML back from the 404, and blowing up in the front-end's `.json()`. What the
        user saw was "the response was not JSON". **The writing side was alive the whole
        time** (utils.setup_logging writes to <buckets>/.logs/server.log); nobody could read
        it.

        `level` filters upward by severity: choosing WARNING also returns ERROR and CRITICAL.
        Someone selecting "warnings" wants to know whether anything is wrong, not "warnings
        but please hide the errors".
        """
        from starlette.responses import JSONResponse
        q = request.query_params
        level = (q.get("level") or "WARNING").strip().upper()
        try:
            limit = max(1, min(2000, int(q.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        path = os.environ.get("LOCI_LOG_FILE", "").strip()
        if not path or not os.path.exists(path):
            return JSONResponse({
                "lines": [], "log_file": path,
                "note": "还没有日志文件（LOCI_LOG_FILE 没设，或者这次启动没开文件日志）。",
            })
        rank = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        floor = 0 if level == "ALL" else rank.get(level, 30)
        try:
            # Read only the tail: the log rotates at 5MB, and reading all of it is pure waste
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 1024 * 1024))
                raw = f.read().decode("utf-8", "replace")
            lines = raw.splitlines()
            if size > 1024 * 1024 and lines:
                lines = lines[1:]                    # drop the first line, which was cut in half
            keep = []
            for ln in lines:
                if floor:
                    hit = next((lv for lv in rank if f" {lv}:" in ln or f" {lv} " in ln), "")
                    if not hit or rank[hit] < floor:
                        continue
                keep.append(ln)
            return JSONResponse({
                "lines": keep[-limit:],
                "log_file": path,
                "level": level,
                "note": "" if keep else f"{level} 这一档下没有东西。",
            })
        except Exception as e:                       # noqa: BLE001
            logger.warning(f"[loci] logs 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/loci/pulse", methods=["GET"])
    async def api_loci_pulse(request: Request) -> Response:
        """Health: how many entries, how much space, are the engines alive. **Read-only;
        nothing is written to disk.**

        `pulse` was withdrawn from the MCP tool surface — the other nine tools are all "what
        am I doing to a memory", and this one alone is "is this machine healthy", which is
        not a memory action.
        The implementation is unchanged (`tools/pulse/`); only its entry point moved from the
        tool surface to this read-only route, which the panel uses to draw the health card.

        The `include_archive=1` query parameter includes the archive in the report.
        """
        from starlette.responses import PlainTextResponse
        from tools import pulse as _pulse
        inc = str(request.query_params.get("include_archive") or "").strip() in ("1", "true", "yes")
        try:
            return PlainTextResponse(await _pulse.pulse(include_archive=inc))
        except Exception as e:                       # noqa: BLE001 - the health endpoint must not take the panel down with it
            logger.warning(f"[loci] pulse 失败: {e}")
            return PlainTextResponse(f"pulse 失败：{e}", status_code=500)

    @mcp.custom_route("/api/loci/poke", methods=["GET"])
    async def api_loci_poke(request: Request) -> Response:
        """The read-only nudge endpoint. The store's fingerprint must be identical before and
        after a call: it sweeps no dreams, deletes no files, pushes no recollection forward,
        and writes nothing to disk.

        Query parameters: `query` (optional; `recall_scores` appears only when it is given)
        plus `when` / `room` / `tag` / `floor`, which mirror recall's parameters and are
        passed straight through to `recall_data()`.
        """
        from starlette.responses import JSONResponse
        withheld = _scope_withholds(request, "梦和发呆")
        if withheld is not None:
            return withheld
        q = request.query_params
        query = q.get("query") or ""
        when = q.get("when") or ""
        room = q.get("room") or ""
        tag = q.get("tag") or ""
        floor = None
        try:
            if (q.get("floor") or "").strip():
                floor = max(0.0, min(100.0, float(q.get("floor"))))
        except (TypeError, ValueError):
            floor = None
        try:
            return JSONResponse(await build_poke(
                query=query, when=when, room=room, tag=tag, floor=floor))
        except Exception as e:
            logger.warning(f"[loci] poke 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ---------------------------------------------------------
    # The demotion signal: the only way the whole-dream layer ever ends.
    # Nothing is added to or removed from the MCP tool surface — this is a new write endpoint
    # on a web route, in the same class as /api/loci/poke, /api/muse/pending and
    # /api/dream/current: the caller is the bridge, a process the gateway starts, not a
    # browser page. **Deliberately not behind cookie or same-origin auth** — that gate exists
    # to protect buttons on a page from cross-site requests, and a server-to-server request
    # from the bridge has no Origin to speak of.
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/dream/wake", methods=["POST"])
    async def api_loci_dream_wake(request: Request) -> Response:
        """The demotion signal, triggered by the user's next message after the dream has been
        handed into the window. Deciding which message that is belongs to the caller
        (lento-v2 `src/chat/梦桥.js`).
        It drops a still-live whole-dream layer to the fragment layer, and the old lifecycle
        — 30 minutes as a fragment, 60 minutes as a single sentence — starts from **this
        moment**.

        **Idempotent**: with no live whole layer it does nothing and still answers 200. If the
        gateway's state and this side ever disagree, a repeated call is completely harmless.
        The contract says to call it once, with idempotency as the safety net — and that net
        is implemented here, not by the bridge deduplicating for itself. The body is neither
        read nor validated; this endpoint takes no parameters.
        """
        from starlette.responses import JSONResponse
        withheld = _scope_withholds(request, "梦")
        if withheld is not None:
            return withheld
        try:
            from core import _dream as _D
            degraded = _D.degrade_on_wake()
        except Exception as e:
            logger.warning(f"[loci] dream/wake 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"降级了": degraded})

    # ---------------------------------------------------------
    # Fetching a dream: **the only endpoint that does so.** No new MCP tool is added for it.
    # ---------------------------------------------------------
    @mcp.custom_route("/api/dream/current", methods=["GET"])
    async def api_dream_current(request: Request) -> Response:
        """The current dream. If there is one: the current layer's content plus which layer
        it is. **If there is none: 204.**

        ------------------------------------------------------------
        Three rules. All three are principles, not implementation details.
        ------------------------------------------------------------
        1. **The whole dream lasts only until the user wakes** — the whole version is
           persisted (layer `完整`) and survives until the degrade signal
           (`/api/loci/dream/wake`); after that you get the fragment. After a while only a
           single sentence remains (`layer = "一句"`), and after that it is **really gone**:
           the file is deleted and a trace is left behind.
        2. **Each call counts as an act of recollection**: the expiry point moves out a
           little, **but by less each time** — recollection can delay a dream's fading, not
           prevent it. So this GET **does write to disk** (updating the expiry point and the
           recollection count). That is deliberate: if you do not want it, do not ask for it.
           Left alone, it disappears on its own.
        3. **There is exactly one way to keep it**: `grow` it into an event. That is what the
           "how to keep this" line in the response means — **the moment it is written down it
           stops being a dream and becomes a memory.**

        A nightmare is just one field (`nightmare: true`, low v and high a). **The part that
        speaks up is not here**: no push notifications, no buzzing anyone's phone. Saying
        something in chat in the middle of the night is the bridge's job — this must not
        become an app that pushes.

        **Deliberately not behind cookie auth**, for the same reason as `/api/muse/pending`:
        the caller is the bridge, a separate process with no browser session, and on the same
        port `/mcp` itself is already reachable without a token.
        """
        from starlette.responses import JSONResponse
        # A dream is woven from the whole library for the life line: a scoped request gets
        # none, and is told so (the agreed fourth first line).
        withheld = _scope_withholds(request, "梦")
        if withheld is not None:
            return withheld
        try:
            from core import _dream as _D
            got = await _D.current_dream()
        except Exception as e:
            logger.warning(f"[loci] dream/current 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
        if not got:
            # 204: **a night without dreams is normal** — below the threshold nothing is
            # woven. It is not an error.
            return Response(status_code=204)
        return JSONResponse(got)

    # ---------------------------------------------------------
    # Click through to the original text, reusing recall's direct-id-lookup rules: verbatim,
    # never truncated.
    # ---------------------------------------------------------
    @mcp.custom_route("/api/loci/bucket/{bucket_id}", methods=["GET"])
    async def api_loci_bucket(request: Request) -> Response:
        from starlette.responses import JSONResponse
        bucket_id = str(request.path_params.get("bucket_id") or "").strip()
        if not bucket_id:
            return JSONResponse({"error": "missing id"}, status_code=400)
        try:
            from tools.recall.core import _room_cn, _short_id
            from core._rooms import normalize_room, is_event_room
            b = await sh.bucket_mgr.get_including_archive(bucket_id)
            if not b:
                return JSONResponse(
                    {"error": f"查无此桶：{bucket_id}（可能已物理删除或打错，不做语义联想）"},
                    status_code=404)
            meta = b.get("metadata", {}) or {}
            room = normalize_room(meta.get("room")) or str(meta.get("room") or "")
            archived = (str(meta.get("type") or "") == "archived"
                        or bool(meta.get("tombstone")) or bool(meta.get("deleted_at")))
            return JSONResponse({
                "id": bucket_id,
                "short": _short_id(bucket_id),
                "name": str(meta.get("name") or ""),
                "summary": str(meta.get("summary") or ""),
                "content": str(b.get("content") or ""),   # verbatim, never truncated
                "room": room,
                "room_cn": _room_cn(meta.get("room")),
                "when": str(meta.get("when") or ""),
                "created": str(meta.get("created") or ""),
                "last_active": str(meta.get("last_active") or ""),
                "valence": meta.get("valence"),
                "arousal": meta.get("arousal"),
                "status": str(meta.get("status") or ""),
                "pinned": bool(meta.get("pinned")),
                "tags": [str(t) for t in (meta.get("tags") or [])],
                # Subjects are a third kind of tag, sitting alongside tags and aliases and
                # never mixed with them.
                "subjects": [str(s) for s in (meta.get("subjects") or [])],
                "from": read_from_ids(meta),
                "supersedes": _split_ids(meta.get("supersedes")),
                "superseded_by": str(meta.get("superseded_by") or ""),
                "archived": archived,
                # The `meaning` write path is retired, but **existing data on disk is still
                # displayed** — there are true things in there, and where they end up should
                # be decided after reading them, not before.
                "meaning": meta.get("meaning") or [],
                "why_remembered": str(meta.get("why_remembered") or ""),
                # Only an event may be corrected here; mind gets no such opening. Archived
                # buckets cannot be edited either — correcting one means restoring it first,
                # the same rule regrow follows.
                "can_edit": bool(is_event_room(room)) and not archived,
            })
        except Exception as e:
            logger.warning(f"[loci] bucket 失败: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
