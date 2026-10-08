"""
========================================
bucket_manager.py — CRUD over memory buckets, plus the multi-dimensional index
========================================

A "memory bucket" is a Markdown file with YAML frontmatter. This file is responsible for
reading them, writing them back, and filtering them by domain, emotion coordinates and
fuzzy text matching.

Key behaviours:
- One bucket = one .md file, stored under permanent / dynamic / archive / feel
- Create, read, update, delete and move all go through BucketManager
- Retrieval = pre-filter by domain, then weighted ordering by emotion coordinates and text
  similarity
- The emotion coordinates are continuous values from Russell's circumplex model:
  valence 0~1 (negative -> positive), arousal 0~1 (calm -> aroused)
- create() / update(content=...) hit disk first and only then post to the embedding
  outbox; delete() cleans up the derived indexes
- Markdown is the source of truth for every memory type; a vectorisation failure never
  rolls back the text, and the background retries it
- create() accepts ``bucket_id_override`` (feel uses a readable minute-resolution id),
  plus ``source_tool`` / ``grow_batch_id`` for provenance

What it deliberately does not do:
- No decay scoring (that is decay_engine's job)
- No LLM calls and no vectorisation (that is dehydrator / embedding_engine)
- It exposes no MCP tool directly (tools/* reach it through core/runtime)

Where each part lives (BucketManager inherits one mixin from each):
- core/_bucket_write.py      create / update: how a write happens, step by step
- core/_bucket_search.py     search: how a query is scored (BM25 + vectors)
- core/_bucket_reads.py      get / is_live / find_exact_content / referenced_by / stats
- core/_bucket_cache.py      list_all's parsed cache, and edits made outside Loci
- core/_bucket_lifecycle.py  touch / sink / archive / delete / restore
- core/_bucket_clearing.py   clear_body and the invalidation records of a source change
- core/_bucket_files.py      directories, the id -> path index, load and commit a file
- core/_bucket_fields.py     field caps, clamps and normalisers (no disk)
This file keeps the class itself (construction, the ledger hand-off, the clock, the
per-bucket lease and the anchor quota), the lease `_filesystem_turn` that the rest of
the code takes too, and the search-scoring numbers.

Exports: the BucketManager class (create / get / update / delete / search /
list_by_type and friends)
========================================
"""
# ============================================================

import os
import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from .footprint import FootprintSnapshot


class _CrossLoopAsyncLock:
    """An async mutex that is safe across FastMCP event loops and threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self):
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        self._lock.release()


@asynccontextmanager
async def _filesystem_turn(base_dir: str, key: str, timeout_seconds: float = 30.0):
    """Cross-thread/cross-loop/process mutual exclusion via an OS file lease.

    A plain ``asyncio.Lock`` only serializes tasks scheduled on the same event
    loop; FastMCP may dispatch requests from different loops/threads (see the
    identical rationale in tools/_common.py's ``_filesystem_content_turn``), so
    quota check-then-write sequences (anchor's 24-cap) need an OS-level guard
    instead of an in-process one.

    The kernel owns the lease for as long as this context keeps its descriptor
    open.  This has two important properties that an mtime-based lock file does
    not: a slow live operation can never have its lock stolen after an arbitrary
    age, while a crashed process releases the lease automatically.  The key is
    hashed before it becomes a filename, so an untrusted bucket id cannot escape
    ``.locks`` or create nested paths.
    """
    if not base_dir:
        yield
        return
    lock_dir = Path(base_dir) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_id = hashlib.sha256(str(key).encode("utf-8", errors="surrogatepass")).hexdigest()
    lock_path = lock_dir / f"{lock_id}.lock"
    token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + timeout_seconds
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b", buffering=0)

    # Windows byte-range locks require the byte to exist.  Two processes may
    # both open a just-created zero-byte file; one can initialize and acquire
    # the byte before the other writes.  Recheck size after sharing/lock errors
    # instead of letting that first-use race escape as PermissionError.
    while True:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > 0:
            break
        try:
            handle.write(b"\0")
            break
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(
                    f"timed out initializing filesystem lease {lock_id}"
                )
            await asyncio.sleep(0.01)
    handle.seek(0)

    def _try_acquire() -> bool:
        try:
            if os.name == "nt":  # pragma: no branch - platform-specific
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised in Linux CI/container
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def _release() -> None:
        if os.name == "nt":  # pragma: no branch - platform-specific
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised in Linux CI/container
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    acquired = False
    try:
        while not acquired:
            acquired = _try_acquire()
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for filesystem lease {lock_id}")
            await asyncio.sleep(0.01)

        owner = json.dumps(
            {
                "state": "held",
                "token": token,
                "pid": os.getpid(),
                "thread": threading.get_ident(),
                "acquired_at": time.time(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        handle.seek(0)
        handle.write(owner)
        handle.truncate()
        yield
    finally:
        try:
            if acquired:
                released = json.dumps(
                    {
                        "state": "released",
                        "token": token,
                        "pid": os.getpid(),
                        "released_at": time.time(),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")
                handle.seek(0)
                handle.write(released)
                handle.truncate()
                _release()
        except OSError:
            pass
        finally:
            handle.close()


from pathlib import Path
from typing import Any, Optional

import frontmatter

from utils import (
    LEGACY_FROM_FIELD,
    PROV_FIELD,
    PROV_MAX_LINES,
    PROV_RELS,
    PROV_TARGET_MAX,
    atomic_write_text,
    busy_retry,
    generate_bucket_id,
    is_closed,
    read_from_ids,
    sanitize_name,
    safe_path,
    now_iso,
    parse_bool,
    parse_iso_datetime,
)
from ._rooms import is_event_room, is_mind_room
from ._sources import SourceRegistry, normalize_sources, note_written
from . import _ledger
from . import _usage
from ._cue_ledger import CueLedger
from ._slicer import PendingSlices
from .media_store import MediaStore
from .ledger_mirror import LedgerMirror

# BucketManager's methods live in the sibling modules below. The names imported into this
# file are part of its surface as well: callers and tests reach several of them as
# core.bucket_manager.<name> (read_from_ids, now_iso, V2_FIELDS, the clamps, _BM25Index),
# so they stay importable from here even where this file no longer uses them itself.
from ._bucket_cache import CacheMixin
from ._bucket_clearing import ClearingMixin
from ._bucket_fields import FieldsMixin
from ._bucket_files import FilesMixin
from ._bucket_lifecycle import LifecycleMixin
from ._bucket_reads import ReadsMixin
from ._bucket_search import SearchMixin, _BM25Index
from ._bucket_write import WriteMixin, _atomic_create_text
from ._bucket_fields import (
    DIRECTIONS_OF_FIT,
    EVIDENTIALS,
    HOLD_LEVELS,
    RECURRENCES,
    V2_FIELDS,
    _MAX_SUBJECT_CHARS,
    _METADATA_TEXT_LIMITS,
    _clamp01,
    _clamp_importance,
    _clamp_unit,
)

logger = logging.getLogger("loci_brain.bucket")


_atomic_write_text = atomic_write_text  # Backward-compatible private alias.


# ============================================================
# search scoring
# ------------------------------------------------------------
# rule.md §①: no bare magic numbers. search() (core/_bucket_search.py) reads these at
# call time. The cosine at which a hit counts as matching in meaning (`vector_match`,
# recall's 意思 mark) is a setting: `recall_meaning` in core/thresholds. After changing any
# of these, run tests/regression to verify the scoring behaviour.
# ============================================================
_VECTOR_TOPK = 50          # embedding prefetch top_k (a source for the semantic score only; it never narrows the candidate set)
_RESOLVED_RANK_PENALTY = 0.3   # a resolved bucket is demoted in ordering only
# What the three forgetting stages look like inside search: faded takes a discount, sunk
# takes a harsher one.
# "Old things do not come up by default, but if you really are looking for one and hit it
# squarely, it survives the discount and climbs anyway": a literal hit is floored by the
# recall layer's max(score, line) and is not blocked by either discount. These numbers get
# recalibrated once something has genuinely sunk.
_FADED_SEARCH_DISCOUNT = 0.85
_SUNK_SEARCH_DISCOUNT = 0.6
# There is no literal-match bonus: a literal hit carries a literal_hit flag and the recall
# layer floors it with max(score, the relevance line). Its name in the design table is "the
# literal-hit floor" — what it wants is not to be missed, not to come first; and a +25 bonus
# would hit the 100 ceiling where nothing can be told apart any more.


class BucketManager(
    WriteMixin,
    SearchMixin,
    ReadsMixin,
    CacheMixin,
    LifecycleMixin,
    ClearingMixin,
    FilesMixin,
    FieldsMixin,
):
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    """

    def __init__(self, config: dict, embedding_engine=None):
        # Keep raw config so create() can look up bucket_type_defaults at write time.
        self.config = config
        # --- Read storage paths from config ---
        self.base_dir = config["buckets_dir"]
        self.media_store = MediaStore(
            self.base_dir,
            str(config.get("media_dir") or os.path.join(self.base_dir, "_media")),
            max_bytes=int(config.get("media_max_bytes") or 25 * 1024 * 1024),
        )
        self.permanent_dir = os.path.join(self.base_dir, "permanent")
        self.dynamic_dir = os.path.join(self.base_dir, "dynamic")
        self.archive_dir = os.path.join(self.base_dir, "archive")
        self.feel_dir = os.path.join(self.base_dir, "feel")
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Search scoring weights ---
        # A search score is two terms: semantic (embedding cosine similarity, only when
        # embedding is enabled) and BM25 keyword matching.
        scoring = config.get("scoring_weights", {})
        self.w_semantic = scoring.get("semantic_weight", 2.5)
        # BM25: TF-IDF weighted keyword matching (rank_bm25 + jieba, both soft dependencies)
        self.w_bm25 = scoring.get("bm25_weight", 1.5)

        # --- The optional embedding engine, used to pre-filter the candidate set ---
        self.embedding_engine = embedding_engine
        self.embedding_outbox = None
        ledger_path = config.get("ledger_path") or os.path.join(
            self.base_dir, "_ledger", "events.jsonl"
        )
        self.ledger_mirror = LedgerMirror(ledger_path)
        # Each source's own state and the write-key claims (`<buckets>/_sources`).
        self.sources = SourceRegistry(self.base_dir, store=self)
        # Slices of the host's raw lines waiting for the main model (core/_slicer.py).
        self.slices = PendingSlices(self.base_dir)
        # What was shown, found and used as a source (core/_usage.py); not the ledger.
        self.usage = _usage.UsageLog(self.base_dir,
                                     (config.get("usage") or {}).get("retain_days"))
        # Which strong-reminder cards each host's window was offered and really loaded
        # (core/_cue_ledger.py).
        self.cues = CueLedger(self.base_dir)

        # The sparse BM25 index (marked dirty after a write, rebuilt lazily on search())
        self._bm25: "_BM25Index | None" = _BM25Index() if _BM25Index is not None else None
        self._bm25_dirty: bool = True
        self._bm25_rebuilding: bool = False  # Avoid concurrent duplicate rebuilds.

        # Active-bucket cache and its on-disk fingerprint are invalidated after writes.
        self._active_cache: "list[dict] | None" = None
        self._active_file_state: dict[str, tuple[int, int]] = {}
        self._active_cache_state_guard = threading.RLock()
        self._active_cache_generation = 0
        self._active_cache_lock = _CrossLoopAsyncLock()
        # Synchronous CRUD paths ask for IDs repeatedly.  A complete index
        # turns migration conflict/apply checks from O(imported × vault) into
        # one O(vault) build plus O(1) lookups.  Managed writes invalidate it;
        # external changes do so when the normal vault poll observes them.
        self._bucket_path_index_guard = threading.RLock()
        self._bucket_path_index: dict[str, str] = {}
        self._bucket_path_index_ready = False
        # See _bucket_turn: archive() / update() / delete() / touch() each independently do
        # find_file -> load -> mutate -> atomic_write, without telling one another. When two
        # of them hit the same bucket_id concurrently — the decay engine's background
        # archive() colliding with an update() from trace or hold, say — the later one
        # writes back based on the old file_path it read, and can "resurrect" a bucket with
        # stale content at the original path after the other has already moved the file into
        # archive/. Found during an adversarial review, and fixed with the same cross-loop,
        # cross-process file-lock scheme used by _quota_turn in tools/_common.py; see
        # _bucket_turn().
        storage_cfg = config.get("storage", {}) or {}
        try:
            self.external_change_poll_seconds = max(
                0.0, float(storage_cfg.get("external_change_poll_seconds", 1.0))
            )
        except (TypeError, ValueError):
            self.external_change_poll_seconds = 1.0
        self._last_file_state_check = 0.0
        self._external_changes_detected = 0
        self._last_external_change = ""

    def attach_embedding_outbox(self, outbox) -> None:
        """Attach the durable derived-index queue after both objects exist."""
        self.embedding_outbox = outbox

    def _record_ledger_event(
        self,
        event_type: str,
        bucket_id: str,
        bucket_type: str,
        content: str,
        metadata: dict | None,
        extra_payload: dict | None = None,
    ) -> None:
        # Names and identities only, never a value of text (core/_ledger.payload_of).
        payload = _ledger.payload_of(metadata, extra_payload)
        try:
            self.ledger_mirror.append_event(
                event_type=event_type,
                trace_id=bucket_id,
                trace_kind=bucket_type,
                payload=payload,
                body=content,
            )
        except Exception as exc:
            logger.warning(f"ledger mirror record failed for {event_type}:{bucket_id}: {exc}")

    def footprint_snapshot(self) -> FootprintSnapshot:
        """Read the legacy Ledger-compatible store and build a one-shot footprint snapshot
        for breath."""
        return FootprintSnapshot.from_events(self.ledger_mirror.iter_events())

    # ---------------------------------------------------------
    # The clock. Read through this module's names at call time: tests patch
    # core.bucket_manager.now_iso, and the exam clock (exam/clock.py) rebinds
    # core.bucket_manager.datetime. The methods in the sibling modules take the time
    # from these two, so those patches still reach them.
    # ---------------------------------------------------------
    @staticmethod
    def _now_iso() -> str:
        return now_iso()

    @staticmethod
    def _datetime_now() -> datetime:
        return datetime.now()

    # ---------------------------------------------------------
    # The per-bucket lease, and the one around a whole-store rename
    # ---------------------------------------------------------
    def _bucket_turn(self, bucket_id: str):
        """Serialize archive()/update()/delete()/touch() on the same bucket_id.
        Uses the same cross-loop/cross-process lock-file mechanism as
        ``tools/_common.py``'s ``_quota_turn`` rather than an ``asyncio.Lock``
        — FastMCP may dispatch requests from different event loops/threads,
        so an in-process lock would not actually serialize them.
        """
        return _filesystem_turn(str(self.base_dir), f"bucket-{bucket_id}")

    def human_name_change_turn(self):
        """Serialize config + vault human-name migrations as one transaction.

        Per-bucket turns protect each Markdown write, but they cannot by
        themselves stop two full-vault rename jobs from interleaving.  Routes
        that change or synchronize the human display name hold this outer
        process/cross-process lease from the config read through the last
        bucket replacement.
        """

        return _filesystem_turn(
            str(self.base_dir),
            "settings-human-name",
            timeout_seconds=300.0,
        )

    # ---------------------------------------------------------
    # anchor system — coordinate-system buckets, hard cap of 24
    # ---------------------------------------------------------
    ANCHOR_LIMIT = 24

    async def count_anchors(self) -> int:
        """Return current count of buckets with anchor=True."""
        # Counted through list_all; the scale is tiny (24 at most) so the scan costs nothing worth noting.
        all_b = await self.list_all(include_archive=False)
        return sum(1 for b in all_b if b.get("metadata", {}).get("anchor"))

    async def set_anchor(self, bucket_id: str, value: bool) -> dict:
        """
        Toggle the anchor flag on a bucket. Hard-rejects if cap reached.

        Returns: {"ok": bool, "anchor": bool, "count": int, "limit": int, "error": Optional[str]}
        """
        # The cap of 24 is enforced as a two-step "count, then write". Without this lock, two
        # concurrent set_anchor(True) calls would each read the same count<limit before the
        # other committed, both pass the check, and each update() — pushing the total past
        # the hard cap.
        async with _filesystem_turn(str(self.base_dir), "quota-anchor"):
            return await self._set_anchor_locked(bucket_id, value)

    async def _set_anchor_locked(self, bucket_id: str, value: bool) -> dict:
        bucket = await self.get(bucket_id)
        if not bucket:
            return {"ok": False, "error": "bucket not found", "count": 0, "limit": self.ANCHOR_LIMIT}
        current_value = parse_bool(
            bucket["metadata"].get("anchor", False), default=False
        )
        target = parse_bool(value)
        # Idempotent: same state → noop
        if current_value == target:
            count = await self.count_anchors()
            return {"ok": True, "anchor": target, "count": count, "limit": self.ANCHOR_LIMIT, "noop": True}
        if target is True:
            # pinned/protected and anchor are mutually exclusive: pinned means "always
            # surfaces at the top" (a core rule) while anchor means "deliberately does not
            # surface" (a coordinate system), and the two contradict each other outright.
            # Allowing both would make a pinned+anchor bucket appear as a core rule every
            # session, tempting the model to release it over and over without ever being able
            # to keep it down. This rejects outright, telling the caller to trace(pinned=0)
            # first and then set the coordinate system.
            if bucket["metadata"].get("pinned") or bucket["metadata"].get("protected"):
                return {
                    "ok": False,
                    "error": "这是 pinned 核心准则，不能同时设为 anchor（两者互斥）。要改成坐标系请先 trace(pinned=0)。",
                    "count": await self.count_anchors(),
                    "limit": self.ANCHOR_LIMIT,
                }
            count = await self.count_anchors()
            if count >= self.ANCHOR_LIMIT:
                return {
                    "ok": False,
                    "error": f"anchor 已达上限 {self.ANCHOR_LIMIT}。请先 release 一条再 anchor 新的。",
                    "count": count,
                    "limit": self.ANCHOR_LIMIT,
                }
        # Setting anchor also changes source_tool to "anchor", and releasing restores the
        # original source (kept in _pre_anchor_source_tool).
        # That way the dashboard's "filter by source" reflects a bucket's current state
        # correctly.
        update_kwargs: dict = {"anchor": target}
        bucket_meta = bucket.get("metadata", {})
        if target:
            # Save the current source_tool as _pre_anchor_source_tool first, then overwrite it with "anchor"
            original = bucket_meta.get("source_tool", "")
            update_kwargs["_pre_anchor_source_tool"] = original
            update_kwargs["source_tool"] = "anchor"
        else:
            # Release: restore the original source_tool and clear the temporary field
            original = bucket_meta.get("_pre_anchor_source_tool", "")
            update_kwargs["source_tool"] = original
            update_kwargs["_pre_anchor_source_tool"] = None  # delete the field
        ok = await self.update(bucket_id, **update_kwargs)
        if not ok:
            return {"ok": False, "error": "update failed", "count": 0, "limit": self.ANCHOR_LIMIT}
        new_count = await self.count_anchors()
        return {"ok": True, "anchor": target, "count": new_count, "limit": self.ANCHOR_LIMIT}

    async def list_anchors(self) -> list[dict]:
        """Return all buckets with anchor=True, sorted by created ascending."""
        all_b = await self.list_all(include_archive=False)
        anchors = [b for b in all_b if b.get("metadata", {}).get("anchor")]
        anchors.sort(key=lambda b: b.get("metadata", {}).get("created", ""))
        return anchors
