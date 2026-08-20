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
- `github_sync.py` / `import_memory.py` / `migrate_engine.py` / `migration_engine.py`:
  background engines that sit at the same level as bucket_manager (GitHub backup sync,
  external-note import, store migration, embedding-backend migration). Each is built
  exactly once by server.py at startup, which makes it the same kind of thing as the
  five above; they used to be scattered at top level and were collected in here.
- `_fold.py` / `_muse.py` / `_dream.py` / `_bigevent.py` / `_when.py` / `_rooms.py`:
  engine pieces moved out of `tools/` — the bones of fold/gist, the rule for when to
  muse, the rule for when to dream, how a period draws its circle in time, the
  calendar and timezone conventions, the whitelist of the four rooms. They lived
  under `tools/` only because the MCP tools happened to reach them first, but a rule
  belongs to no single tool. (Splitting a rule across two homes has cost us twice: a
  room rename that had to be made in both copies, and a `weight=0` falsy-fallback bug
  that got fixed on one side and left standing on the other.)
- `profile.py`: the single source for `door_note()` / `event_pool()`. It used to live
  in `tools/breath/awaken.py`, but that rule is not breath's private property — the
  profile page in `web/loci.py` and the awakening in `tools/breath/awaken.py` have to
  read the same one, so it moved here and both sides import it instead of each
  keeping its own copy.

⚠️ **Moved, not rewritten**: the contents of this layer are word for word what they
were before the move; only import paths changed. Cracking open monoliths like
`bucket_manager.py` / `recall/core.py` is explicitly out of scope here — refactoring
rides along with deletions and moves, it does not get a front of its own.

Dependency direction: `core` does not depend on `tools` / `web` / `bridge` — *mostly*.
`dehydrator.py` uses `normalize_subjects` from `tools/_subjects.py`, and
`import_memory.py` uses a few validators from `tools/_common.py`. Those two backward
edges predate the move and were left alone on purpose: `_subjects.py` / `_common.py`
are really about "how a tool validates its input", and dragging them down here would
only stir unrelated things together. They are known, not overlooked.

What is exported: each file's own docstring says so, not repeated here. `server.py` is
the only place that imports these engine classes directly in order to construct them;
`tools/_runtime.py` and `web/_shared.py` hold the constructed instances, and the rest
of the code reaches them through those rather than doing `import core.bucket_manager`
and friends.
========================================
"""
