"""
========================================
web/loci.py — where the standalone Loci dashboard's routes are assembled
========================================

The panel is one page, frontend/loci.html, a shell that loads its ES modules from
frontend/panel (a router, the API client, shared components, one module per page). The
handlers and the data builders behind them live in the sibling modules listed further
down. `register` adds every route in one ordered table, and the list here is the one list
of what the panel can reach.

    GET  /loci                        -> the page itself
    GET  /loci/panel/{path:path}      -> the panel's modules and stylesheet (frontend/panel,
                                         .js and .css only, public: the login page is one
                                         of them)
    GET  /api/loci/recall             -> recall's second skin (card + list), and `rows`:
                                         the text skin's lines, paged by offset / limit
    GET  /api/loci/graph              -> starfield: nodes + real edges + weak edges + constellations
    GET  /api/loci/similar            -> suspected-duplicate pairs + score distribution (adjustable threshold),
                                         less the pairs kept (core/similar_kept.py)
    GET  /api/loci/profile            -> the note by the door
    GET  /api/dream/current          -> the current dream (current layer + level; 204 when there is none, and it writes a recall state)
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
    GET  /api/loci/export/originals   -> 「导出原话」: a zip of imported conversations (one
                                         Markdown each) and sunk originals (one Markdown per
                                         local day written); nothing of a withdrawn or
                                         deleted source (core/export_originals.py)
    GET  /api/loci/version            -> the version that runs; `?check=1` adds the latest
                                         GitHub release against it (tag, day, notes, page).
                                         Read only: no update is installed from here
                                         (core/releases.py, web/loci_version.py)
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

🔴 THE WRITE SURFACE — fifteen POST routes here and five in the page blocks below
(entry/fix, names/action, trace, muse/nudge, embedding/backfill), and every one of them writes
something.

    POST /api/loci/similar/action     -> a human verdict on a suspected duplicate: keep
                                         both (writes _state/similar_kept.json: ids and
                                         version markers), or sink one (trace delete=True
                                         — a soft delete, always recoverable by direct id
                                         lookup)
    POST /api/loci/want/resolve       -> close something that was wanted (trace status)
    POST /api/loci/want/asked         -> record that it was asked about (trace)
    POST /api/loci/event/correct      -> regrow: writes a NEW VERSION of a memory (the
                                         content kind of entry/fix, under its old name)
    POST /api/loci/subjects/action    -> edits the alias table in the data volume (the
                                         same handler as names/action)
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
                                         slices them and the slices are stored as pending,
                                         with the batch's `report_at` when it carries one
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

The detail window, the name card and the names page (panel contract 「面板接口」 §三 §四
§五 name). Handlers in web/loci_detail.py (bucket, lineage, source, entry/fix) and
web/loci_names.py (names, names/pending, names/{name}, names/action); what they show is
assembled and worded in core/detail.py and core/census.py. Both POSTs write:

    GET  /api/loci/bucket/{id}        -> one entry verbatim, its title (the line every list
                                         shows for it), its metadata, the tag row, the 关联
                                         counts, its source layer and the edits offered
    GET  /api/loci/lineage/{id}       -> 关联: what came after it, its cue and live holds
    GET  /api/loci/source/{id}        -> 来源: its sources and their state (an import's
                                         with when its first and last line were said), what
                                         it stands on, how it is known; `?fetch=<n>` asks the
                                         host for source n's original (nothing stored or
                                         logged)
    GET  /api/loci/names              -> the names the table knows, by kind; paged
    GET  /api/loci/names/pending      -> the names it does not know yet, each with the
                                         entry it first appeared in; paged
    GET  /api/loci/names/{name}       -> one name's card and the entries it appears in
    POST /api/loci/entry/fix          -> 字写错了 (trace old_str/new_str) · 内容错了 (an
                                         event: a new version marked 人改的; a MIND entry:
                                         her `disputed` mark) · 删除 (trace delete=True)
    POST /api/loci/names/action       -> not_person / merge / rename / set_kind on one name
                                         (aliases.yaml)

The breath, surface, trace and regrow/fold pages (web/loci_mind.py). Lists page by
offset / limit / as_of. 🔴 POST /api/loci/trace writes.

    GET  /api/loci/breath/last        -> the last breath actually handed out, per host
                                         (`?host=`, else the most recent): its structure
                                         with titles read now, 近三天's card rendered now
                                         from the entries it named (`recent.text`; each
                                         item's `in_card`), each 依据变了的 item with why
                                         it is there in breath's words (`why`), and the
                                         scope line it was handed out under
                                         (core/breath_snapshot.py)
    GET  /api/loci/awake              -> the awake pool computed now, every reason each
                                         entry is awake in words (`?reason=` keeps one);
                                         core/profile.awake_pool
    GET  /api/loci/hanging            -> open promises, live holds and open cue conditions,
                                         `?part=surface` (awake) or `deep` (asleep), each
                                         row with its buttons; core/profile.hanging
    POST /api/loci/trace              -> a trace button ({id, action}: done / drop on a
                                         promise, withdraw on a hold or a cue), checked
                                         against the hanging list first, written through
                                         trace_core with closed_by="user"
    GET  /api/loci/changes/recent     -> new versions, merges, periods, pins, and what was
                                         done about a moved basis in the last two weeks,
                                         newest first, from the ledger with seq as each
                                         row's id (core/changes_feed.py)

What Loci handed out and took in — turns, recall's timeline, usage, grow, muse, dreams,
vectors (web/loci_activity.py; core/activity.py, core/grow_view.py, core/muse_view.py,
core/_nudge.py, core/_dream_archive.py, core/vector_view.py). turns and usage are host
reads too (panel_auth.HOST_READ_PATHS); the rest are the panel's alone. Two of them write.

    GET  /api/loci/turns/{window}     -> one window of a host (`?host=`), turn by turn, newest
                                         first: the cards each turn was handed and what became
                                         of them, what breath showed and recall found in it
                                         (with the query typed), the holds live on them
    GET  /api/loci/recall/timeline    -> the last three natural days: 「我搜 X」 from the usage
                                         log and 「你说 X」 card deliveries from the card
                                         ledger, mixed, newest first
    GET  /api/loci/usage              -> per memory since a day: how often shown, found, stood
                                         on; shown often and never stood on first
    GET  /api/loci/grow/today         -> the memories written since today began (`?since=`, a
                                         host's daily report; else the latest `report_at` a
                                         slices batch carried, `?host=` for one host's;
                                         else local midnight)
    GET  /api/loci/grow/slices        -> every batch of slices, handled and replaced ones
                                         included, each slice with its state and the guesses
                                         at or above the guess line
    GET  /api/loci/muse               -> `?part=clusters` the thoughts that look like one
                                         thing / `?part=days` the stretches without a name,
                                         each with its evidence and member ids
    POST /api/loci/muse/nudge         -> 「戳一下」 on a cluster ({cluster}): the next tool
                                         reply tells the model once (core/_nudge.py;
                                         writes _state/muse_nudges.json, not the ledger)
    GET  /api/loci/dreams             -> the last three natural days' dreams, whole text,
                                         state and thread candidates, from the panel's own
                                         copy no road of the model reads
                                         (core/_dream_archive.py)
    GET  /api/loci/embedding/missing  -> the memories with no vector and why (queued, keeps
                                         failing, not queued), from the embedding outbox
    POST /api/loci/embedding/backfill -> 「现在补」: queue what has no vector and make every
                                         waiting item due now (EmbeddingOutbox.reconcile +
                                         retry_now; writes the outbox file, not the ledger)

Host reads (panel_auth.HOST_READ_PATHS, panel contract §六): breath/last, awake, hanging,
names, names/{name}, recall, rooms, bucket, lineage, source, turns and usage answer a
host's credential too, under that host's scope; GET only. The panel's other routes, and
every write among them, stay the panel's: a host's credential is refused there.

Where each group lives (a new route goes into its group's module and gets its line in
`register`, which adds the routes in this order). A read that computes something over the
store is a core function plus a thin builder here: the builder reads the library, config
and engines off `web/_shared` at call time, hands them to the core function, and the
route turns its dict into JSON (core/starfield.py, core/census.py, core/similarity.py,
core/health.py, core/profile.py):

    web/loci_pages.py     /loci, /loci/panel, /loci/vendor
    web/loci_reads.py     recall, rooms, graph, profile, recollect, subjects, and the
                          builders behind them
    web/loci_similar.py   similar, similar/action
    web/loci_verdicts.py  want/resolve, want/asked, event/correct, subjects/action
    web/loci_password.py  auth/state, auth/set-password
    web/loci_health.py    health, setup, logs, pulse, and build_health / build_setup
    web/loci_dream.py     muse/pending, poke, dream/wake, dream/current, and
                          build_muse_pending / build_poke
    web/loci_detail.py    bucket, lineage, source, entry/fix (core/detail.py)
    web/loci_names.py     names, names/pending, names/{name}, names/action
                          (core/census.py)
    web/loci_mind.py      breath/last, awake, hanging, trace, changes/recent
                          (core/breath_snapshot.py, core/profile.py, core/changes_feed.py)
    web/loci_activity.py  turns, recall/timeline, usage, grow/today, grow/slices, muse,
                          muse/nudge, dreams, embedding/missing, embedding/backfill
                          (core/activity.py, core/grow_view.py, core/muse_view.py,
                          core/_nudge.py, core/_dream_archive.py, core/vector_view.py)
    web/host_api.py       /api/v2/* (a host's credential, not the panel's)
    web/library_api.py    export, export/originals, import-package, embedding/migration
    web/loci_version.py   version
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
    mcp.custom_route("/loci/panel/{path:path}", methods=["GET"])(loci_pages.loci_panel_asset)
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

    # ---------------------------------------------------------
    # The export package, bringing one back, and the recompute after a change of
    # embedding model. The handlers are web/library_api.py's; they are registered here so
    # this file's route list stays the one list of what the panel can reach.
    # ---------------------------------------------------------
    from . import library_api as _lib
    mcp.custom_route("/api/loci/export", methods=["GET"])(_lib.export)
    mcp.custom_route("/api/loci/export/originals", methods=["GET"])(_lib.export_originals)
    mcp.custom_route("/api/loci/import-package", methods=["POST"])(_lib.import_package)
    mcp.custom_route("/api/loci/import-package", methods=["GET"])(_lib.import_status)
    mcp.custom_route("/api/loci/embedding/migration", methods=["GET"])(_lib.reembed_status)
    mcp.custom_route("/api/loci/embedding/migration", methods=["POST"])(_lib.reembed_action)
    from . import loci_version as _version
    mcp.custom_route("/api/loci/version", methods=["GET"])(_version.api_loci_version)

    # ---------------------------------------------------------
    # The detail window, the name card and the names page (web/loci_detail.py,
    # web/loci_names.py). names/pending and names/action are registered before
    # names/{name}, which would otherwise take them as a name.
    # ---------------------------------------------------------
    from . import loci_detail as _detail
    from . import loci_names as _names
    mcp.custom_route("/api/loci/bucket/{bucket_id}", methods=["GET"])(_detail.api_loci_bucket)
    mcp.custom_route("/api/loci/lineage/{bucket_id}", methods=["GET"])(_detail.api_loci_lineage)
    mcp.custom_route("/api/loci/source/{bucket_id}", methods=["GET"])(_detail.api_loci_source)
    mcp.custom_route("/api/loci/entry/fix", methods=["POST"])(_detail.api_loci_entry_fix)
    mcp.custom_route("/api/loci/names", methods=["GET"])(_names.api_loci_names)
    mcp.custom_route("/api/loci/names/pending", methods=["GET"])(_names.api_loci_names_pending)
    mcp.custom_route("/api/loci/names/action", methods=["POST"])(_names.api_loci_names_action)
    mcp.custom_route("/api/loci/names/{name}", methods=["GET"])(_names.api_loci_name_card)

    # ---------------------------------------------------------
    # The breath, surface, trace and regrow/fold pages (web/loci_mind.py).
    # ---------------------------------------------------------
    from . import loci_mind as _mind
    mcp.custom_route("/api/loci/breath/last", methods=["GET"])(_mind.api_loci_breath_last)
    mcp.custom_route("/api/loci/awake", methods=["GET"])(_mind.api_loci_awake)
    mcp.custom_route("/api/loci/hanging", methods=["GET"])(_mind.api_loci_hanging)
    mcp.custom_route("/api/loci/trace", methods=["POST"])(_mind.api_loci_trace)
    mcp.custom_route("/api/loci/changes/recent", methods=["GET"])(_mind.api_loci_changes_recent)

    # ---------------------------------------------------------
    # What Loci handed out and took in: turns, recall's timeline, usage, grow, muse and
    # its poke, dreams, and the missing vectors. The handlers are web/loci_activity.py's.
    # ---------------------------------------------------------
    from . import loci_activity as _act
    mcp.custom_route("/api/loci/turns/{window}", methods=["GET"])(_act.api_loci_turns)
    mcp.custom_route("/api/loci/recall/timeline", methods=["GET"])(
        _act.api_loci_recall_timeline)
    mcp.custom_route("/api/loci/usage", methods=["GET"])(_act.api_loci_usage)
    mcp.custom_route("/api/loci/grow/today", methods=["GET"])(_act.api_loci_grow_today)
    mcp.custom_route("/api/loci/grow/slices", methods=["GET"])(_act.api_loci_grow_slices)
    mcp.custom_route("/api/loci/muse", methods=["GET"])(_act.api_loci_muse)
    mcp.custom_route("/api/loci/muse/nudge", methods=["POST"])(_act.api_loci_muse_nudge)
    mcp.custom_route("/api/loci/dreams", methods=["GET"])(_act.api_loci_dreams)
    mcp.custom_route("/api/loci/embedding/missing", methods=["GET"])(
        _act.api_loci_embedding_missing)
    mcp.custom_route("/api/loci/embedding/backfill", methods=["POST"])(
        _act.api_loci_embedding_backfill)

