"""
========================================
web/loci_reads.py — the panel's reads, and the builders behind them
========================================

    GET  /api/loci/recall             -> recall's second skin (card + list)
    GET  /api/loci/rooms              -> the four rooms and what is in them
    GET  /api/loci/graph              -> starfield: nodes + real edges + weak edges + constellations
    GET  /api/loci/profile            -> the note by the door
    GET  /api/loci/recollect          -> pull a faded or sunk memory back up
    GET  /api/loci/subjects           -> the "who is in here" screen
    GET  /api/loci/bucket/{id}        -> one bucket, verbatim, plus its metadata
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from core import _when as _w      # "today" in the user's local timezone — never call datetime.now() directly
from core import census as _census
from core import starfield as _starfield
from core.profile import _PROFILE_TAG
from core.starfield import split_ids
from utils import read_from_ids

logger = sh.logger

# `_REMIND_DAYS` (30 days) and `_is_closed` live with the note by the door's contract source
# (`tools/breath/awaken.py`) — **do not put a second 30 here**. Two 30s are two rulesets, and
# that is exactly how one gets changed and the other forgotten.


# ============================================================
# The builders are **deliberately apart from the route handlers**, so each can be run on
# its own without faking a login session
# (`python -c "asyncio.run(loci.build_graph())"`). The routes handle only auth and the JSON
# envelope. Where a read computes something over the store, the computing is a core
# function (core/starfield.py, core/census.py, core/profile.py) and the builder lists the
# store off `sh` and hands it over.
# ============================================================

async def build_rooms() -> dict:
    """The directory both doors open onto (core/census.rooms)."""
    return _census.rooms(await sh.bucket_mgr.list_all(include_archive=False))


async def build_subjects() -> dict:
    """"Who is in here" (core/census.subjects). **Read-only; nothing is written to disk.**"""
    return _census.subjects(await sh.bucket_mgr.list_all(include_archive=False))


async def build_graph() -> dict:
    """Starfield (core/starfield.build): the live store, in the local `now`."""
    all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
    return _starfield.build(all_buckets, _w.now())


def _collect_events(all_buckets: list) -> list[dict]:
    """The pool behind the "something comes back to you" section of the waking screen.
    **The rule is not here** — it is in the contract source.

    It does exactly one thing: call `core.profile.event_pool()` and reshape the dict into
    what the front-end wants. (Copying the same line into two files is not sharing a
    source.)
    WARNING: the direction is fixed at **web -> core/tools**, never the reverse. The MCP
       surface must not depend on the panel.
    WARNING: a parallel implementation drifts: a room rename has to be made in both copies,
       a gate added on one side lets entries keep surfacing on the other.
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
    this function directly some day, cannot get around the contract.
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

    There is one rule and one place: `core.profile.door_note()`, the same one the waking
    screen reads. This function only turns the dict into JSON.

    WARNING: there is no "things that keep coming up about a person" section. Ranking by
    how often something is mentioned is wrong: **what gets mentioned most is not what is
    most true**. Reading a dossier of "what this person is like" on waking and then
    treating them according to the dossier turns a person into a character sheet. The
    rule is about timing instead: **only what there is no time to look up before speaking
    belongs by the door.**
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
        # No freq_i / freq_you keys: a front-end still reading them gets undefined rather
        # than an empty array, so the section simply disappears. That is intended:
        # half-rendering an empty section makes it much harder to notice it should not be
        # there than having it vanish outright.
        "reminders": reminders,
        "heavy": heavy,
        "edited": edited,     # the notification pool: user-edited entries not yet handled
        "big_events": big,
    }


# ---------------------------------------------------------
# The main pool: recall's two skins — the card above, for the model, and the list below,
# for a person.
# ---------------------------------------------------------
async def api_loci_recall(request: Request) -> Response:
    from starlette.responses import JSONResponse
    q = request.query_params
    # There is no `by`; `view="scene"` does that job.
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
        # Not recall_data() and recall_core() separately: each runs its own _collect —
        #    **the same search computed twice** (measured with a query: 3 seconds on the
        #    tool surface, 8.6 seconds here).
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
async def api_loci_graph(request: Request) -> Response:
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_graph())
    except Exception as e:
        logger.warning(f"[loci] graph 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------
# The profile — the note by the door
# ---------------------------------------------------------
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


# ---------------------------------------------------------
# Click through to the original text, reusing recall's direct-id-lookup rules: verbatim,
# never truncated.
# ---------------------------------------------------------
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
            "supersedes": split_ids(meta.get("supersedes")),
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
