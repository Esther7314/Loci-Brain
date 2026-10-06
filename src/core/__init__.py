"""
========================================
core/ — the engine layer: storage, forgetting, fold, muse, dream
========================================

Sorted by layer, not by feature. This is the foundation of the memory system:
whichever tool or panel a call arrives from, there is exactly ONE implementation
of persistence, indexing, decay, dedup, summarisation and dream-weaving.

What lives here:
- `bucket_manager.py` / `decay_engine.py` / `dehydrator.py` / `bm25_index.py` /
  `embedding_engine.py`: the engines that actually touch disk, compute decay, call
  the summariser LLM, score BM25, and keep the vectors.
- `errors.py`: the error types that run through all of the above — everyone has to
  recognise the same error shapes.
- `import_memory.py` / `package_import.py` / `reembed.py`:
  background engines that sit at the same level as bucket_manager (conversation
  import, export-package import, recomputing the vectors after an embedding-model change). Each is built
  exactly once by server.py at startup, which makes it the same kind of thing as the
  five above.
- `ledger_mirror.py` / `footprint.py` / `media_store.py` / `backup_archive.py` /
  `embedding_outbox.py` / `provider_detect.py`: the append-only ledger and the footprint
  line breath reads from it, attachment storage, the archive format exports and imports
  share, the write-behind queue of the vector index, and provider detection for the
  summariser and embedding clients.
- `memory_messages.py`: the shared wording trace returns when an entry sinks or is
  reactivated.
- `_fold.py` / `_muse.py` / `_dream.py` / `_bigevent.py` / `_when.py` / `_rooms.py`:
  engine pieces — the bones of fold/gist, the rule for when to muse, the rule for when
  to dream, how a period draws its circle in time, the calendar and timezone
  conventions, the whitelist of the four rooms. They are here, not under `tools/`,
  because a rule belongs to no single tool. (A rule split across two homes drifts: a
  room rename has to be made in both copies, a falsy-fallback fix lands on one side
  and not the other.)
- `_holds.py`: what a hold is and when it holds (a short exception hung on a standing
  entry); breath's roads, the dream pools and the decay sweep all ask it.
- `visibility.py`: the one gate — may this memory be put in front of the model, on this
  road. Every road that shows a memory (breath, recall, muse, dreams) asks it.
- `profile.py`: the single source for `door_note()` / `event_pool()`. That rule is not
  breath's private property — the profile page in `web/loci_reads.py` and the
  awakening in `tools/breath/awaken.py` have to read the same one, so both import it
  from here instead of each keeping its own copy. Awake / asleep (`is_accessible`) and breath's 惦记的事 and
  忽然想起 (`prospective`, `involuntary`) live beside them.
- `_invalidation.py`: breath's 依据变了的 — which memories stand on ground that moved
  (an overturned basis, a source revised or withdrawn, a panel correction), card them one
  layer at a time, and the keep-as-is gesture trace writes.

Dependency direction: `core` does not depend on `tools` / `web` / `bridge` — *mostly*.
`dehydrator.py` uses `normalize_subjects` from `tools/_subjects.py`. That backward
edge is left alone on purpose: `_subjects.py` is really about
"how a tool validates its input", and dragging it down here would only stir unrelated
things together. It is known, not overlooked.

What is exported: each file's own docstring says so, not repeated here. `server.py` is
the only place that imports these engine classes directly in order to construct them;
`tools/_runtime.py` and `web/_shared.py` hold the constructed instances, and the rest
of the code reaches them through those rather than doing `import core.bucket_manager`
and friends.
========================================
"""
