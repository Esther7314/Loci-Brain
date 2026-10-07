"""
========================================
web/host_api.py — /api/v2/*: what a host calls with its own credential, not the panel
========================================

Every route here sits behind the hook guard (web/__init__ `_Gated`, panel_auth.HOOK_PATHS):
the host's key, and the scope its credential reaches. The routes and what each one writes
are listed in web/loci.py's header, the one list of what can be reached.
========================================
"""

import json

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from ._guards import _request_of, _scope_refusal

logger = sh.logger


def _slices_config() -> tuple[int, float]:
    """(max lines per batch from `slices:`, the guess line from core/thresholds)."""
    from core import _slicer as _sl
    from core import thresholds as _T
    cfg = (sh.config or {}).get("slices") or {}
    try:
        max_lines = int(cfg.get("max_lines_per_batch") or _sl.DEFAULT_MAX_LINES_PER_BATCH)
    except (TypeError, ValueError):
        max_lines = _sl.DEFAULT_MAX_LINES_PER_BATCH
    return max(1, max_lines), _T.value(_T.SLICE_GUESS, sh.config or {})


# ---------------------------------------------------------
# Sources after the fact: the host's raw lines in, pending slices out (core/_slicer.py)
# ---------------------------------------------------------
async def api_v2_slices_take(request: Request) -> Response:
    """The host hands over a stretch of raw lines before it lets go of them:
    {source: {system, instance, container}, day, lines: [{id, text, at?, speaker?, revision?}],
    revision?}. The side model slices them; the slices wait for the main model
    (recall(view="slices")). A `fingerprint_by` in the body is accepted and not used:
    a slice's fingerprint is Loci's own (core/_slicer.py). 400 for a malformed batch
    or one holding a line the registry reads as withdrawn, deleted or held; 403 past the
    host's max_grant or for lines it is neither authority nor registrar for; 502 when
    the side model fails — then nothing is stored, and the host may send the same batch
    again.

    A line that becomes withdrawn, deleted or held while the side model is slicing (a
    source change applied meanwhile) drops the whole batch: 400 with
    {"error": "…", "note": "source_changed_while_slicing", "lines": {line id: state}}
    — no slice, gist or line order is stored, and the same batch sent again is refused
    as above. The host sends the batch again without those lines."""
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
    from . import panel_auth as _pa
    try:
        out = await _sl.take_batch(sh.bucket_mgr, body,
                                   model=_sl.side_model(sh.dehydrator, sh.config),
                                   max_lines=max_lines, threshold=threshold,
                                   host=req.host if req is not None else None,
                                   hosts=_pa.hosts())
    except _sl.BatchForbidden as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except _sl.BatchStale as e:
        return JSONResponse({"error": str(e), "note": "source_changed_while_slicing",
                             "lines": e.lines}, status_code=400)
    except _sl.BatchError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except _sl.SlicerError as e:
        logger.warning(f"[loci] slicing failed, nothing stored: {e}")
        return JSONResponse({"error": f"the side model failed, nothing was stored: {e}"},
                            status_code=502)
    return JSONResponse(out)


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
async def api_v2_breath(request: Request) -> Response:
    """The waking screen a host hands its model when a window opens — the second-tier
    hook in the README. Plain text by default, exactly what breath() returns;
    `?format=json` gives the object both are made from, under stable keys
    {core, prospective, recent, involuntary, invalidation, earliest}. Either way it is
    handed to a model, so a question in it counts as asked (stamp_asked), as it does
    through the tool. Like every read, it opens with the request's scope: the text's
    first line, the object's `scope`; a refused request gets the refusal and nothing
    else.

    `?peek=1` is the read-only form, for a host reading the screen for itself rather
    than handing it to a model (a bridge polling what is on its mind): the same text or
    object, but no ask-once question is stamped as asked, nothing is recorded as shown
    in the usage log and it does not become the host's last breath on the panel, so the
    model still gets those questions the next time its window opens. The breath tool
    itself always stamps: the model calling it is the model being asked."""
    from starlette.responses import JSONResponse, PlainTextResponse
    from tools.breath.awaken import build_breath, handed_out, render_breath, stamp_asked
    fmt = str(request.query_params.get("format") or "text").strip().lower()
    if fmt not in ("text", "json"):
        return JSONResponse({"error": "format is text or json"}, status_code=400)
    peek = str(request.query_params.get("peek") or "0").strip().lower()
    if peek not in ("0", "1", "true", "false"):
        return JSONResponse({"error": "peek is 1 or 0"}, status_code=400)
    peek = peek in ("1", "true")
    refused = _scope_refusal(request)
    if refused is not None:
        return refused
    try:
        b = await build_breath()
        text = render_breath(b)
        if not peek:
            await stamp_asked(b)
            handed_out(b, None if fmt == "json" else text)
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
    from . import panel_auth as _pa
    req = _request_of(request)
    host = req.host if req is not None else None
    try:
        status, out = await _sc.handle(sh.bucket_mgr, body, host,
                                       dehydrator=sh.dehydrator, hosts=_pa.hosts())
    except Exception as e:
        logger.warning(f"[loci] source change failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(out, status_code=status)


async def api_v2_source_lines(request: Request) -> Response:
    """{source: run, revision?, lines: [id | {id, revision?}]} from a host: which lines
    the run holds, in order. 200 with `status` (recorded, known, conflict, forbidden);
    400 for a malformed body; 403 when the caller is the panel rather than a host."""
    from starlette.responses import JSONResponse
    from core import _source_change as _sc
    try:
        body = await sh._read_json_object(request)
    except (ValueError, json.JSONDecodeError) as e:
        return JSONResponse({"error": f"body: {e}"}, status_code=400)
    from . import panel_auth as _pa
    req = _request_of(request)
    host = req.host if req is not None else None
    try:
        status, out = await _sc.handle_lines(sh.bucket_mgr, body, host, hosts=_pa.hosts())
    except Exception as e:
        logger.warning(f"[loci] source lines failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(out, status_code=status)


# ---------------------------------------------------------
# The strong reminder: cards with the owner's message, and the host's word on what
# reached the model (core/_cue.py, core/_cue_ledger.py)
# ---------------------------------------------------------
async def _cue_route(request: Request, handler) -> Response:
    from starlette.responses import JSONResponse
    try:
        body = await sh._read_json_object(request)
    except (ValueError, json.JSONDecodeError) as e:
        return JSONResponse({"error": f"body: {e}"}, status_code=400)
    try:
        status, out = await handler(sh.bucket_mgr, body, _request_of(request))
    except Exception as e:
        logger.warning(f"[loci] cue failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(out, status_code=status)


async def api_v2_cue(request: Request) -> Response:
    """{text, window, turn} -> {window, turn, cards: [{card, kind, id, short, why,
    text}], text, scope}. `text` is the cards to paste after the owner's message, one
    line each; `card` is what the host confirms or strikes. Nothing counts as delivered
    until the host says so. The same turn again gets the same cards back, less any whose
    entry is no longer good, and nothing new. A refused request gets its refusal, as every read does."""
    from core import _cue
    return await _cue_route(request, _cue.handle_cue)


async def api_v2_cue_delivered(request: Request) -> Response:
    """{window, turn} (every card that turn was offered) and/or {window, cards: [card]}
    -> {window, delivered, unknown}. A card the window was never offered is unknown and
    not recorded."""
    from core import _cue
    return await _cue_route(request, _cue.handle_delivered)


async def api_v2_cue_dropped(request: Request) -> Response:
    """{window, cards: [card]} / {window, turns: [turn]} / {window, all: true} ->
    {window, dropped, cleared}. `all` also forgets what the window's breath listed in
    依据变了的 when it opened."""
    from core import _cue
    return await _cue_route(request, _cue.handle_dropped)


async def api_v2_changes(request: Request) -> Response:
    """At most `limit` lines (default 1000, at most 5000) past a point. The panel and an
    open host: past seq `since` (default 0), {since, next, more, changes}, every line.
    A host with a ceiling: past `cursor` (none = from the start), {cursor, next, more,
    changes}, only what its credential reaches, no seq anywhere; a `since` from it is
    refused. Ask again from `next` while `more`."""
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
    cursor = str(request.query_params.get("cursor") or "").strip() or None
    req = _request_of(request)
    host = req.host if req is not None else None
    if req is not None and req.host is None and req.mode == _scope.OPEN:
        host = _scope.Host("panel", scope_mode=_scope.OPEN)
    if _ledger.host_view(host):
        if since:
            return JSONResponse({"error": "a host with a max_grant reads by cursor "
                                          "(?cursor=<next>), not by seq"}, status_code=400)
    elif cursor is not None:
        return JSONResponse({"error": "an open host reads by seq (?since=N)"},
                            status_code=400)
    try:
        out = await _ledger.changes_since(sh.bucket_mgr, host, since, limit, cursor=cursor)
    except _ledger.CursorError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning(f"[loci] changes failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(out)
