"""
========================================
web/loci_dream.py — dreams and musing, for the bridge
========================================

    GET  /api/muse/pending            -> is it time to muse? (cluster count + age + worth_poking)
    GET  /api/loci/poke               -> dream (delivery) + muse cluster count (nudge) + structured recall scores, all in one read-only call
    POST /api/loci/dream/wake         -> the demotion signal: drop a live "whole" dream
                                         layer down to the fragment layer (idempotent)
    GET  /api/dream/current           -> the current dream (current layer + level; 204 when there is none, and it writes a recall state)

`build_poke` looks `build_muse_pending` up as this module's global, so a test that swaps
`build_muse_pending` patches it here (web.loci does not re-export either).
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from ._guards import _scope_withholds

logger = sh.logger


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
                # The weaver's thread candidates, when it had any: the main model decides
                # whether one becomes a cue (core/_dream.handout_cues).
                **_D.handout_cues(rec),
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


# ---------------------------------------------------------
# "Is it time to muse?" — the endpoint the host's wake-up leg asks
# ---------------------------------------------------------
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
