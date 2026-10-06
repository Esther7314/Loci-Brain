"""
========================================
web/loci.py — where the standalone Loci dashboard's routes are assembled
========================================

Four pages: the main pool, the starfield, the similarity check, and the profile. The
handlers and the data builders behind them live in the sibling modules listed further
down; all rendering lives in frontend/loci.html. `register` adds every route in one
ordered table, and the list here is the one list of what the panel can reach.

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
                                         stamps a question it hands out as asked; `?peek=1`
                                         reads without stamping or recording it as shown
    GET  /api/loci/export             -> the export package: every entry, a vector snapshot, the
                                         library's state, sunk originals, attachments, the
                                         schema note and a manifest with what was filtered
                                         out and what could not travel; never a secret
                                         (core/export_package.py)
    GET  /api/loci/import-package     -> where bringing a package back stands: parsed with
                                         its collisions, applying, done (with what of the
                                         library's state was restored or merged), error
    GET  /api/loci/embedding/migration -> the recompute after a change of embedding model:
                                         progress, failure, whether it can be resumed
                                         (core/embedding_switch.py)
    GET  /api/v2/changes              -> the ledger past a point: ids, kinds, source identities
                                         and hashes, never text; without "was merely touched";
                                         only what the host's credential reaches. An open host
                                         reads by the ledger's numbers (`?since=N&limit=`); a
                                         host with a max_grant by an opaque cursor
                                         (`?cursor=…&limit=`) and never sees those numbers
                                         (hook key; core/_ledger.py)

🔴 THE WRITE SURFACE — fifteen POST routes, and every one of them writes something.

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
    POST /api/loci/import-package     -> bring an export package back: a multipart `file`
                                         is checked and parsed (nothing written; the
                                         collisions come back); then {job_id, decisions,
                                         default} writes its entries and restores the
                                         library's state (core/package_import.py), or
                                         {job_id, cancel: true} drops the parse
    POST /api/loci/embedding/migration -> {action: "resume"} carries on a recompute that
                                         failed or was interrupted; {action: "skip",
                                         ids?} leaves the entries that keep failing for
                                         the new model to compute after the swap and
                                         carries on; {action: "abandon"} throws it away
                                         (the old model stays)
    POST /api/v2/slices               -> the host hands over a day's raw lines; a side model
                                         slices them and the slices are stored as pending
                                         (hook key; the raw text is not kept)
    POST /api/v2/source/change        -> a host's change to one piece of its material: the
                                         source registry, the block on what stood on it, and
                                         for withdrawn / deleted the clearing of every place
                                         its text reached. Only the source's declared change
                                         authority may send it (host credential;
                                         core/_source_change.py)
    POST /api/v2/source/lines         -> a host registers which lines a run holds, in order,
                                         with the revision of each as delivered under its
                                         watermark: the source registry's line orders (host
                                         credential; core/_source_change.handle_lines)
    POST /api/v2/cue                  -> the owner's message as it arrives ({text, window,
                                         turn}): at most three cards to paste after it; the
                                         same turn again gets the same cards, less any no
                                         longer good. Writes the card ledger (the window's
                                         opening, the offer) and the usage log (hook key;
                                         core/_cue.py)
    POST /api/v2/cue/delivered        -> the host's word that cards were placed in the model's
                                         input ({window, turn} when all of the turn's were /
                                         {window, cards} for exactly those); only then are
                                         they not handed to that window again (hook key)
    POST /api/v2/cue/dropped          -> the host's word that cards left the input ({window,
                                         cards} / {window, turns} / {window, all: true}); they
                                         may be handed again (hook key)

Where each group lives (a new route goes into its group's module and gets its line in
`register`, which adds the routes in this order):

    web/loci_pages.py     /loci, /loci/vendor
    web/loci_reads.py     recall, rooms, graph, profile, recollect, subjects, bucket,
                          and the builders behind them
    web/loci_similar.py   similar, similar/action, and the pair cache
    web/loci_verdicts.py  want/resolve, want/asked, event/correct, subjects/action
    web/loci_password.py  auth/state, auth/set-password
    web/loci_health.py    health, setup, logs, pulse, and build_health / build_setup
    web/loci_dream.py     muse/pending, poke, dream/wake, dream/current, and
                          build_muse_pending / build_poke
    web/host_api.py       /api/v2/* (a host's credential, not the panel's)
    web/library_api.py    export, import-package, embedding/migration
    web/_guards.py        the same-origin write check and the hook-scope refusals

Rules: do not use Optional[simple type] for parameters; run all three smoke suites after
changing anything here.

Public surface: register(mcp). Re-exported for the callers that reach them through this
module: the builders build_rooms / build_graph / build_subjects / build_profile /
build_recollect / build_setup / build_health, and the write guards `_origin_reject` /
`_write_body` (web/library_api.py, web/import_api.py). build_poke and
build_muse_pending are not re-exported: build_poke looks build_muse_pending up in
web.loci_dream, so that is where a test swaps it.
========================================
"""

from . import host_api
from . import loci_dream
from . import loci_health
from . import loci_pages
from . import loci_password
from . import loci_reads
from . import loci_similar
from . import loci_verdicts
from ._guards import _origin_reject, _write_body
from .loci_health import build_health, build_setup
from .loci_reads import (
    build_graph, build_profile, build_recollect, build_rooms, build_subjects,
)

__all__ = [
    "register",
    "build_rooms", "build_graph", "build_subjects", "build_profile", "build_recollect",
    "build_setup", "build_health",
    "_origin_reject", "_write_body",
]


def register(mcp) -> None:
    mcp.custom_route("/loci", methods=["GET"])(loci_pages.loci_page)
    mcp.custom_route("/loci/vendor/{path:path}", methods=["GET"])(loci_pages.loci_vendor)
    mcp.custom_route("/api/loci/recall", methods=["GET"])(loci_reads.api_loci_recall)
    mcp.custom_route("/api/loci/rooms", methods=["GET"])(loci_reads.api_loci_rooms)
    mcp.custom_route("/api/loci/graph", methods=["GET"])(loci_reads.api_loci_graph)
    mcp.custom_route("/api/loci/similar", methods=["GET"])(loci_similar.api_loci_similar)
    mcp.custom_route("/api/loci/similar/action", methods=["POST"])(
        loci_similar.api_loci_similar_action)
    mcp.custom_route("/api/loci/want/resolve", methods=["POST"])(
        loci_verdicts.api_loci_want_resolve)
    mcp.custom_route("/api/loci/want/asked", methods=["POST"])(loci_verdicts.api_loci_want_asked)
    mcp.custom_route("/api/loci/event/correct", methods=["POST"])(
        loci_verdicts.api_loci_event_correct)
    mcp.custom_route("/api/loci/health", methods=["GET"])(loci_health.api_loci_health)
    mcp.custom_route("/api/loci/auth/state", methods=["GET"])(loci_password.api_loci_auth_state)
    mcp.custom_route("/api/loci/auth/set-password", methods=["POST"])(
        loci_password.api_loci_set_password)
    mcp.custom_route("/api/loci/profile", methods=["GET"])(loci_reads.api_loci_profile)
    mcp.custom_route("/api/loci/recollect", methods=["GET"])(loci_reads.api_loci_recollect)
    mcp.custom_route("/api/v2/slices", methods=["POST"])(host_api.api_v2_slices_take)
    mcp.custom_route("/api/v2/slices", methods=["GET"])(host_api.api_v2_slices_pending)
    mcp.custom_route("/api/v2/breath", methods=["GET"])(host_api.api_v2_breath)
    mcp.custom_route("/api/v2/source/change", methods=["POST"])(host_api.api_v2_source_change)
    mcp.custom_route("/api/v2/source/lines", methods=["POST"])(host_api.api_v2_source_lines)
    mcp.custom_route("/api/v2/cue", methods=["POST"])(host_api.api_v2_cue)
    mcp.custom_route("/api/v2/cue/delivered", methods=["POST"])(host_api.api_v2_cue_delivered)
    mcp.custom_route("/api/v2/cue/dropped", methods=["POST"])(host_api.api_v2_cue_dropped)
    mcp.custom_route("/api/v2/changes", methods=["GET"])(host_api.api_v2_changes)
    mcp.custom_route("/api/muse/pending", methods=["GET"])(loci_dream.api_muse_pending)
    mcp.custom_route("/api/loci/subjects", methods=["GET"])(loci_reads.api_loci_subjects)
    mcp.custom_route("/api/loci/setup", methods=["GET"])(loci_health.api_loci_setup)
    mcp.custom_route("/api/loci/subjects/action", methods=["POST"])(
        loci_verdicts.api_loci_subjects_action)
    mcp.custom_route("/api/logs", methods=["GET"])(loci_health.api_logs)
    mcp.custom_route("/api/loci/pulse", methods=["GET"])(loci_health.api_loci_pulse)
    mcp.custom_route("/api/loci/poke", methods=["GET"])(loci_dream.api_loci_poke)
    mcp.custom_route("/api/loci/dream/wake", methods=["POST"])(loci_dream.api_loci_dream_wake)
    mcp.custom_route("/api/dream/current", methods=["GET"])(loci_dream.api_dream_current)
    mcp.custom_route("/api/loci/bucket/{bucket_id}", methods=["GET"])(loci_reads.api_loci_bucket)

    # ---------------------------------------------------------
    # The export package, bringing one back, and the recompute after a change of
    # embedding model. The handlers are web/library_api.py's; they are registered here so
    # this file's route list stays the one list of what the panel can reach.
    # ---------------------------------------------------------
    from . import library_api as _lib
    mcp.custom_route("/api/loci/export", methods=["GET"])(_lib.export)
    mcp.custom_route("/api/loci/import-package", methods=["POST"])(_lib.import_package)
    mcp.custom_route("/api/loci/import-package", methods=["GET"])(_lib.import_status)
    mcp.custom_route("/api/loci/embedding/migration", methods=["GET"])(_lib.reembed_status)
    mcp.custom_route("/api/loci/embedding/migration", methods=["POST"])(_lib.reembed_action)

