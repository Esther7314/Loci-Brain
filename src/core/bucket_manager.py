"""
========================================
bucket_manager.py — CRUD over memory buckets, plus the multi-dimensional index
========================================

A "memory bucket" is a Markdown file with YAML frontmatter. This file is responsible for
reading them, writing them back, and filtering them by domain, emotion coordinates and
fuzzy text matching.

Key behaviours:
- One bucket = one .md file, stored under permanent / dynamic / archive / feel /
  letters
- Create, read, update, delete and move all live here
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
- It exposes no MCP tool directly (tools/* reach it through _runtime)

Exports: the BucketManager class (create / get / update / delete / search /
list_by_type and friends)
========================================
"""
# ============================================================

import os
import re
import asyncio
import hashlib
import json
import logging
import math
import threading
import time
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime

from locibrain.eventsourcing.footprint import FootprintSnapshot

# The unified error system: clamping an out-of-range value reports OB-W001/OB-W002
# (rule.md §11)
try:
    from errors import push_warning as _ob_push_warning  # type: ignore
except Exception:
    try:
        from .errors import push_warning as _ob_push_warning  # type: ignore
    except Exception:
        def _ob_push_warning(*_a, **_kw):  # type: ignore
            return None


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


def _clamp_importance(v, source: str) -> int:
    """importance out of range -> clamp into [1,10], and raise an OB-W001 notice."""
    try:
        iv = int(v)
    except (TypeError, ValueError, OverflowError):
        _ob_push_warning("OB-W001", f"importance={v!r} 无法解析，回退为 5（{source}）")
        return 5
    if iv < 1 or iv > 10:
        clamped = max(1, min(10, iv))
        _ob_push_warning("OB-W001", f"importance={iv} 超出 [1,10]，已修正为 {clamped}（{source}）")
        return clamped
    return iv


def _clamp_unit(v, field: str, source: str) -> float:
    """valence/arousal out of range -> clamp into [0.0,1.0], and raise an OB-W002 notice."""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        _ob_push_warning("OB-W002", f"{field}={v!r} 无法解析，回退为 0.5（{source}）")
        return 0.5
    if not math.isfinite(fv):
        _ob_push_warning("OB-W002", f"{field}={v!r} 不是有限数，回退为 0.5（{source}）")
        return 0.5
    if fv < 0.0 or fv > 1.0:
        clamped = max(0.0, min(1.0, fv))
        _ob_push_warning("OB-W002", f"{field}={fv} 超出 [0.0,1.0]，已修正为 {clamped}（{source}）")
        return clamped
    return fv


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
from ._slicer import PendingSlices
from locibrain.storage.media_store import MediaStore
from locibrain.eventsourcing.ledger_mirror import LedgerMirror

try:
    # ⚠️ A mine caught during acceptance: after the move into core, the old top-level path
    #    `from bm25_index import` failed with a silent ImportError -> the entire BM25
    #    dimension died without a word. That is the trap in a soft dependency: it fails
    #    without erroring, and retrieval quietly goes blind.
    #    This try is only meant to cover rank_bm25/jieba being absent. It must not cover an
    #    import path of our own being wrong.
    from .bm25_index import BM25Index as _BM25Index
except ImportError:
    _BM25Index = None  # type: ignore

logger = logging.getLogger("loci_brain.bucket")


_atomic_write_text = atomic_write_text  # Backward-compatible private alias.


def _atomic_create_text(path: str, text: str) -> None:
    """Publish a complete text file atomically, refusing an existing target.

    ``atomic_write_text`` intentionally replaces its destination, which is the
    right behavior for updates but unsafe for creation races.  Build the full
    file beside the destination and publish it with a hard link: link creation
    is atomic and fails with ``FileExistsError`` instead of overwriting.
    """

    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(target)}.create.",
        dir=parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


# ============================================================
# Tunable constants
# ------------------------------------------------------------
# rule.md §①: no bare magic numbers. Retrieval scoring and the field truncation caps are
# gathered here.
# After changing any of these, run tests/regression to verify the scoring behaviour.
# ============================================================

# --- Default metadata values (kept in step with dehydrator/import_memory) ---
_DEFAULT_VALENCE = 0.5
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_PINNED_IMPORTANCE = 10           # the importance a pinned/protected bucket is locked to
_DEFAULT_DOMAIN_NAME = "未分类"     # the placeholder used when no domain was supplied
_EDITABLE_BUCKET_TYPES = frozenset(
    {"dynamic", "permanent", "feel", "letter", "i", "self"}
)
# The write-layer fields of v2 (part 1 of the plan document). Absent is the default reading of each:
# no direction_of_fit = thetic (recording what is), no evidential = not marked,
# no internally_generated = it happened out there. So only a marked value is stored.
DIRECTIONS_OF_FIT = frozenset({"thetic", "telic"})
EVIDENTIALS = frozenset({"inference", "assumption"})
# RFC 5545 RRULE, the subset this version reads: yearly, on the date in `when`.
RECURRENCES = frozenset({"FREQ=YEARLY"})
# A hold (core/_holds.py) is a short telic entry hung on a standing one: exception_of =
# the id it is hung on, hold = how far it reaches. The two come together or not at all.
HOLD_LEVELS = frozenset({"defer", "avoid"})
# cue = "when I meet this, remember that": {condition, phrasings}. Writing one declares
# the entry is waiting on something, so a cue without a condition is no cue. phrasings
# are the ways it might be said, filled in later by the backfill; [] until then.
# card_of = the name this MIND entry is the card of (the names table's key, normalised by
# the tool that writes it). Only a MIND entry is a card; which MIND room, and one live card
# per name, are the write tools' checks (tools/grow/rooms_path.check_card).
# sources = the pieces of the host's material this memory was formed from, one record each
# (core/_sources.py: identity, revision, fingerprint, span, use). Whether a source may be
# used is the registry's question, asked by the write tools before they get here.
# looks_like_promise = the backfill read a promise in a sentence the main model did not mark
# telic. Telic stays the main model's switch; the mark is what the read side turns into a
# question. Stored only as true.
V2_FIELDS = ("direction_of_fit", "bound", "evidential", "internally_generated",
             "recurrence", "backfilled", "cue", "exception_of", "hold", "review_after",
             "card_of", "sources", "looks_like_promise")
_CUE_CONDITION_MAX = 200
_CUE_PHRASINGS_MAX_ITEMS = 16
_CUE_PHRASING_MAX = 200
_HOLD_TARGET_MAX = 64
_REVIEW_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- Field truncation lengths, so the frontmatter cannot bloat ---
_SOURCE_TOOL_MAX = 32
_GROW_BATCH_ID_MAX = 64
_WHY_REMEMBERED_MAX = 500
# prov (where this came from) is not truncated: a cut target is half an id pointing at
# nothing. PROV_MAX_LINES lines of at most PROV_TARGET_MAX characters (utils), and
# anything past either is refused by _normalize_prov.
# --- Truncation lengths for three later fields ---
# room    = the room. The caller decides it and tools/_rooms.py validates it; this file
#           only stores it and has no opinion on its meaning.
# summary = the one-sentence summary. It is the foundation of "distant things get only
#           their summary", and the dehydrator backfills it in the background.
# when    = when the event happened (an ISO date string). recall's time gate reads it;
#           left empty it means the moment of storing.
_ROOM_MAX = 64
_SUMMARY_MAX = 200
_WHEN_MAX = 32
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024
_MAX_TAGS = 64
_MAX_TAG_CHARS = 128
_MAX_DOMAINS = 16
_MAX_DOMAIN_CHARS = 128
# --- subjects: the third of the three kinds of label ---
# Not many names appear in one memory, people and things together; the cap exists to stop a
# model having a fit and splitting an entire passage into names.
_MAX_SUBJECTS = 16
_MAX_BOUND = 8
_MAX_SUBJECT_CHARS = 64

# --- meaning / media: hold's experience-anchoring extension ---
# meaning is stored as list[str]: the same memory may be touched again at different
# moments, and each hold passes in the new entry, which is appended rather than
# overwriting what is there (see merge_or_create in tools/_common.py).
_MEANING_ITEM_MAX = 2000        # length cap on one meaning entry
_MEANING_LIST_MAX_ITEMS = 50    # how many meanings one bucket may accumulate
_MEDIA_MAX_ITEMS = 20           # how many media references one memory may carry
_MEDIA_PATH_MAX = 500
_MEDIA_TITLE_MAX = 200
_MEDIA_TYPE_MAX = 32
_MEDIA_NOTE_MAX = 500

# --- invalidation: marks that a basis of this memory changed under it (W3C PROV) ---
# A list of records {kind, of, by, at, confirmed_at?}, appended by regrow(mode="overturn")
# on every descendant of the overturned version, and by the confirm gesture (core/
# _invalidation.py) for a source revision or a panel edit looked at and kept. The memory
# keeps surfacing; the mark is shown wherever it is read by id. `confirmed_at` (a day) =
# looked at on that day and kept as it is: the record no longer counts as open. Capped so
# a basis overturned again and again cannot grow the frontmatter without bound: the newest
# stay. `of` / `by` hold a memory id or a source's string form (core/_sources.STRING_MAX).
_INVALIDATION_MAX_ITEMS = 32
_INVALIDATION_KIND_MAX = 32
_INVALIDATION_ID_MAX = 128
_INVALIDATION_AT_MAX = 32

_METADATA_TEXT_LIMITS = {
    "status": 32,
    "type": 32,
    # Provenance stamps for name/summary: a single short enum-ish word ("fallback").
    "name_source": 32,
    "summary_source": 32,
    "resolution_reason": 500,
    "resolved_by": 128,
    "related_bucket": 128,
    # Closing a want records who closed it. The field name is deliberately kept separate
    # from the resolved_by above, which older data uses for a bucket_id or a
    # source label, while this one holds a person's name.
    "closed_by": 50,
    "author": 120,
    "user_name": 120,
    "title": 120,
    "letter_date": 64,
    "why_remembered": _WHY_REMEMBERED_MAX,
    "source_tool": _SOURCE_TOOL_MAX,
    "grow_batch_id": _GROW_BATCH_ID_MAX,
    "last_merged_by": _SOURCE_TOOL_MAX,
    "_pre_anchor_source_tool": _SOURCE_TOOL_MAX,
}

# ⚰️ The three temporal-ripple constants were deleted along with the function — the
#    reasoning is on the memorial above touch().
#    (Leaving unused constants behind makes a reader believe the mechanism is still alive.)
_MAX_METADATA_DEPTH = 16
_MAX_METADATA_NODES = 10_000

# --- search scoring ---
_VECTOR_TOPK = 50          # embedding prefetch top_k (a source for the semantic score only; it never narrows the candidate set)
_VECTOR_RECALL_THRESHOLD = 0.65  # the cosine at which a hit counts as matching in meaning (`vector_match`, recall's 意思 mark)
_RESOLVED_RANK_PENALTY = 0.3   # a resolved bucket is demoted in ordering only
# What the three forgetting stages look like inside search: faded takes a discount, sunk
# takes a harsher one.
# "Old things do not come up by default, but if you really are looking for one and hit it
# squarely, it survives the discount and climbs anyway": a literal hit is floored by the
# recall layer's max(score, line) and is not blocked by either discount. These numbers get
# recalibrated once something has genuinely sunk.
_FADED_SEARCH_DISCOUNT = 0.85
_SUNK_SEARCH_DISCOUNT = 0.6
# _LITERAL_MATCH_BONUS was deleted: a literal hit went from "+25 to the score" to "the
# result carries a literal_hit flag and the recall layer floors it with max(score, the
# relevance line)". Its name in the design table is "the literal-hit floor" — what it wants
# is not to be missed, not to come first; and adding 25 hits the 100 ceiling where nothing
# can be told apart any more.

# The pure functions for the topic/emotion/time/touch scoring dimensions, and their weight
# constants, live in locibrain.retrieval.bucket_scoring. Nothing here imports them any
# more: search() scores on semantic+bm25 only, and the _calc_*_score wrapper methods were
# deleted as dead code on 2026-08-25 (they had no remaining callers, in tests or elsewhere).


def _clamp01(value, default: float) -> float:
    """Clamp any input into [0.0, 1.0]; on failure return `default`.

    This exists for the ``max(0.0, min(1.0, float(x)))`` boilerplate scattered through the
    body (model_valence, weight, bucket_type_defaults.weight and so on).
    The philosophical valence/arousal go through _clamp_unit instead, which pushes an
    OB-W002.
    This helper clamps silently, and suits the case where the caller already guarantees the
    range and this is at most a belt-and-braces check.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return max(0.0, min(1.0, numeric))


class BucketManager:
    """
    Memory bucket manager — entry point for all bucket CRUD operations.
    Buckets are stored as Markdown files with YAML frontmatter for metadata
    and body for content. Natively compatible with Obsidian browsing/editing.
    """

    def __init__(self, config: dict, embedding_engine=None, v3_runtime=None):
        # Keep raw config so create() can look up bucket_type_defaults at write time.
        self.config = config
        self.v3_runtime = v3_runtime
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
        self.letter_dir = os.path.join(self.base_dir, "letters")
        self.fuzzy_threshold = config.get("matching", {}).get("fuzzy_threshold", 50)
        self.max_results = config.get("matching", {}).get("max_results", 5)

        # --- Search scoring weights ---
        scoring = config.get("scoring_weights", {})
        self.w_topic = scoring.get("topic_relevance", 4.0)
        self.w_emotion = scoring.get("emotion_resonance", 2.0)
        self.w_time = scoring.get("time_proximity", 1.5)
        self.w_importance = scoring.get("importance", 1.0)
        self.content_weight = scoring.get("content_weight", 1.0)  # body×1, per spec
        # Two additional dimensions, touch and semantic:
        # touch:    the more it has been deliberately recalled, the higher the score
        #           (normalised, capped at 10 recalls)
        # semantic: embedding cosine similarity (only when embedding is enabled)
        self.w_touch = scoring.get("touch_weight", 1.0)
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

    def attach_v3_runtime(self, runtime) -> None:
        self.v3_runtime = runtime

    def attach_embedding_outbox(self, outbox) -> None:
        """Attach the durable derived-index queue after both objects exist."""
        self.embedding_outbox = outbox

    def _record_v3_bucket_event(
        self,
        action: str,
        bucket_id: str,
        bucket_type: str,
        content: str,
        metadata: dict | None,
    ) -> None:
        runtime = getattr(self, "v3_runtime", None)
        recorder = getattr(runtime, "record_bucket_event", None)
        if not callable(recorder):
            return
        try:
            recorder(
                action=action,
                bucket_id=bucket_id,
                bucket_type=bucket_type,
                content=content,
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.warning(f"v3 bucket event record failed for {action}:{bucket_id}: {exc}")

    def _record_ledger_event(
        self,
        event_type: str,
        bucket_id: str,
        bucket_type: str,
        content: str,
        metadata: dict | None,
        extra_payload: dict | None = None,
    ) -> None:
        payload = dict(metadata or {})
        if extra_payload:
            payload.update(extra_payload)
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
    # Internal helpers — heavily reused inside this file, not a public API
    # Directory walking, primary-domain paths, loading; behaviour and cost identical to
    # the originals they were extracted from
    # ---------------------------------------------------------
    @property
    def _active_dirs(self) -> list[str]:
        """The active bucket directories, archive excluded (used by list_all, _collect_all_tags and lookups). The order must not be shuffled: feel/letter come after dynamic to preserve the original scan order."""
        return [self.permanent_dir, self.dynamic_dir,
                self.feel_dir, self.letter_dir]

    def _iter_md_files(self, dirs: list[str]):
        """Recursively walk several directories for *.md, yielding (root, filename, full_path).

        Behaves exactly like the five copies of ``for root, _, files in os.walk(…): for f in files: if not f.endswith('.md'): continue`` it replaced.
        No filtering logic is added here; the caller decides what to skip.
        """
        for dir_path in dirs:
            if not os.path.exists(dir_path):
                continue
            for root, _, files in os.walk(dir_path):
                for fname in files:
                    if not fname.endswith(".md"):
                        continue
                    yield root, fname, os.path.join(root, fname)

    @staticmethod
    def _primary_domain(domain: list[str] | str | None) -> str:
        """Take domain[0] as the primary-domain subdirectory name; empty or missing falls
        back to ``未分类``.

        Used in three places: create, _move_bucket and archive. It has to go through
        sanitize_name before it can be used as a path.
        """
        if isinstance(domain, str):
            primary = domain.strip()
        elif domain:
            primary = str(domain[0]).strip()
        else:
            primary = ""
        return sanitize_name(primary) if primary else _DEFAULT_DOMAIN_NAME

    def _max_bucket_bytes(self) -> int:
        raw = (self.config.get("limits") or {}).get(
            "max_bucket_bytes", _DEFAULT_MAX_BUCKET_BYTES
        )
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            return _DEFAULT_MAX_BUCKET_BYTES
        return value if value >= 0 else _DEFAULT_MAX_BUCKET_BYTES

    def _validate_bucket_content(self, content: str) -> None:
        cap = self._max_bucket_bytes()
        if cap <= 0:
            return
        size = len(content.encode("utf-8"))
        if size > cap:
            raise ValueError(
                f"内容过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
                "请拆分后存入，或调整 config.limits.max_bucket_bytes。"
            )

    def _v2_fields(self, **given) -> dict:
        """The v2 write-layer fields in their stored form; raises ValueError on a value
        outside the enums. A field at its default comes back as None, which update()
        reads as "remove the field" and create() drops."""
        out: dict = {}
        if "direction_of_fit" in given:
            v = str(given["direction_of_fit"] or "").strip()
            if v and v not in DIRECTIONS_OF_FIT:
                raise ValueError(f"direction_of_fit must be thetic or telic, got {v!r}")
            out["direction_of_fit"] = "telic" if v == "telic" else None
        if "bound" in given:
            names = self._normalize_metadata_list(
                given["bound"] or [], max_items=_MAX_BOUND, max_chars=_MAX_SUBJECT_CHARS)
            out["bound"] = names or None
        if "evidential" in given:
            v = str(given["evidential"] or "").strip()
            if v and v not in EVIDENTIALS:
                raise ValueError(f"evidential must be inference or assumption, got {v!r}")
            out["evidential"] = v or None
        if "internally_generated" in given:
            out["internally_generated"] = (
                True if parse_bool(given["internally_generated"], default=False) else None)
        if "recurrence" in given:
            v = str(given["recurrence"] or "").strip().upper()
            if v and v not in RECURRENCES:
                raise ValueError(f"recurrence: only FREQ=YEARLY is read, got {v!r}")
            out["recurrence"] = v or None
        if "backfilled" in given:
            fields = self._normalize_metadata_list(
                given["backfilled"] or [], max_items=16, max_chars=32)
            out["backfilled"] = fields or None
        if "cue" in given:
            out["cue"] = self._normalize_cue(given["cue"])
        if "exception_of" in given:
            v = self._sanitize_text(str(given["exception_of"] or "")).strip()
            if v and (len(v) > _HOLD_TARGET_MAX or re.search(r"\s", v)):
                raise ValueError(f"exception_of must be one bucket id, got {v[:_HOLD_TARGET_MAX + 1]!r}")
            out["exception_of"] = v or None
        if "hold" in given:
            v = str(given["hold"] or "").strip()
            if v and v not in HOLD_LEVELS:
                raise ValueError(f"hold must be defer or avoid, got {v!r}")
            out["hold"] = v or None
        if "review_after" in given:
            v = str(given["review_after"] or "").strip()
            if v:
                try:
                    real_day = bool(_REVIEW_DATE_RE.match(v)) and bool(
                        datetime.strptime(v, "%Y-%m-%d"))
                except ValueError:
                    real_day = False
                if not real_day:
                    raise ValueError(f"review_after must be one YYYY-MM-DD day, got {v!r}")
            out["review_after"] = v or None
        if "card_of" in given:
            v = self._sanitize_text(str(given["card_of"] or "")).strip()
            if v and (len(v) > _MAX_SUBJECT_CHARS or "\n" in v):
                raise ValueError(f"card_of must be one name, got {v[:_MAX_SUBJECT_CHARS + 1]!r}")
            out["card_of"] = v or None
        if "sources" in given:
            out["sources"] = normalize_sources(given["sources"]) or None
        if "looks_like_promise" in given:
            out["looks_like_promise"] = (
                True if parse_bool(given["looks_like_promise"], default=False) else None)
        return out

    @classmethod
    def _normalize_cue(cls, cue) -> Optional[dict]:
        """The stored form of `cue`: {condition, phrasings}. Empty (None, "", {}) is no
        cue, which update() reads as "remove it". A plain string is the condition. A cue
        that carries anything but has no condition raises ValueError: the condition is
        what makes it a cue."""
        if cue is None or cue == "" or cue == {}:
            return None
        if isinstance(cue, str):
            cue = {"condition": cue}
        if not isinstance(cue, dict):
            raise ValueError(f"cue must be {{condition, phrasings}}, got {type(cue).__name__}")
        condition = cls._sanitize_text(str(cue.get("condition") or "")).strip()
        if not condition:
            raise ValueError("cue needs a non-empty condition")
        return {
            "condition": condition[:_CUE_CONDITION_MAX],
            "phrasings": cls._normalize_metadata_list(
                cue.get("phrasings") or [], max_items=_CUE_PHRASINGS_MAX_ITEMS,
                max_chars=_CUE_PHRASING_MAX),
        }

    @classmethod
    def _normalize_metadata_list(
        cls,
        values,
        *,
        max_items: int,
        max_chars: int,
    ) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        elif not isinstance(values, (list, tuple, set)):
            values = [values]
        normalized: list[str] = []
        for value in values:
            text = cls._sanitize_text(str(value)).strip()[:max_chars]
            if text and text not in normalized:
                normalized.append(text)
            if len(normalized) >= max_items:
                break
        return normalized

    @classmethod
    def _normalize_meaning_item(cls, text) -> str:
        """Trim one meaning entry. This is not summarisation, only a length cap."""
        if not text:
            return ""
        return cls._sanitize_text(str(text)).strip()[:_MEANING_ITEM_MAX]

    @classmethod
    def _normalize_meaning_list(cls, values) -> list[str]:
        """For wholesale replacement: trim each entry, drop the empty ones, and cap the count.

        It deliberately does not deduplicate: the same sentence written at two different
        moments is itself information, and deduplicating would erase that gap in time.
        """
        if not values:
            return []
        if isinstance(values, str):
            values = [values]
        normalized: list[str] = []
        for v in values:
            item = cls._normalize_meaning_item(v)
            if item:
                normalized.append(item)
            if len(normalized) >= _MEANING_LIST_MAX_ITEMS:
                break
        return normalized

    @classmethod
    def _normalize_invalidation(cls, records) -> list[dict]:
        """Keep each record to its four short text fields, plus `confirmed_at` when it is
        set; a record with no `kind` says nothing and is dropped. Order is kept, the newest
        `_INVALIDATION_MAX_ITEMS` win."""
        if not records:
            return []
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, (list, tuple)):
            return []
        out: list[dict] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            kind = cls._sanitize_text(str(rec.get("kind") or "")).strip()[:_INVALIDATION_KIND_MAX]
            if not kind:
                continue
            row = {
                "kind": kind,
                "of": cls._sanitize_text(str(rec.get("of") or "")).strip()[:_INVALIDATION_ID_MAX],
                "by": cls._sanitize_text(str(rec.get("by") or "")).strip()[:_INVALIDATION_ID_MAX],
                "at": cls._sanitize_text(str(rec.get("at") or "")).strip()[:_INVALIDATION_AT_MAX],
            }
            confirmed = cls._sanitize_text(str(rec.get("confirmed_at") or "")).strip()
            if confirmed:
                row["confirmed_at"] = confirmed[:_INVALIDATION_AT_MAX]
            out.append(row)
        return out[-_INVALIDATION_MAX_ITEMS:]

    @classmethod
    def _normalize_prov(cls, lines) -> list[dict]:
        """The stored form of `prov`: [{rel, target}], order kept, identical lines once.
        Raises ValueError on a rel outside PROV_RELS, a target that is empty, holds
        whitespace or is longer than PROV_TARGET_MAX, or more than PROV_MAX_LINES lines.
        Nothing is cut to fit: a shortened target or a dropped line would be a chain that
        silently says less than it was given. Whether a target exists is the tools'
        question, not this one."""
        if not lines:
            return []
        if isinstance(lines, dict):
            lines = [lines]
        if not isinstance(lines, (list, tuple)):
            raise ValueError(f"prov must be a list of {{rel, target}}, got {type(lines).__name__}")
        out: list[dict] = []
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError(f"prov line must be {{rel, target}}, got {type(line).__name__}")
            rel = str(line.get("rel") or "").strip()
            if rel not in PROV_RELS:
                raise ValueError(f"prov rel must be one of {', '.join(PROV_RELS)}, got {rel!r}")
            target = cls._sanitize_text(str(line.get("target") or "")).strip()
            if not target or len(target) > PROV_TARGET_MAX or re.search(r"\s", target):
                raise ValueError(f"prov target must be one id of at most {PROV_TARGET_MAX} "
                                 f"characters, got {target[:PROV_TARGET_MAX + 1]!r}")
            entry = {"rel": rel, "target": target}
            if entry not in out:
                out.append(entry)
        if len(out) > PROV_MAX_LINES:
            raise ValueError(f"prov holds at most {PROV_MAX_LINES} lines, got {len(out)}")
        return out

    @classmethod
    def _normalize_media(cls, media) -> list[dict]:
        """Validate persisted media metadata; `path` must already have been stabilised by
        MediaStore."""
        if not media:
            return []
        if not isinstance(media, list):
            media = [media]
        normalized: list[dict] = []
        for item in media:
            if not isinstance(item, dict):
                continue
            path = cls._sanitize_text(str(item.get("path") or "")).strip()[:_MEDIA_PATH_MAX]
            if not path:
                continue
            entry: dict = {"path": path}
            title = item.get("title")
            if title:
                entry["title"] = cls._sanitize_text(str(title)).strip()[:_MEDIA_TITLE_MAX]
            media_type = item.get("type")
            if media_type:
                entry["type"] = cls._sanitize_text(str(media_type)).strip()[:_MEDIA_TYPE_MAX]
            note = item.get("note")
            if note:
                entry["note"] = cls._sanitize_text(str(note)).strip()[:_MEDIA_NOTE_MAX]
            digest = str(item.get("sha256") or "").lower()
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                entry["sha256"] = digest
            try:
                size = int(item.get("size"))
            except (TypeError, ValueError, OverflowError):
                size = -1
            if size >= 0:
                entry["size"] = size
            if item.get("stored") is True:
                entry["stored"] = True
            normalized.append(entry)
            if len(normalized) >= _MEDIA_MAX_ITEMS:
                break
        return normalized

    # ---------------------------------------------------------
    # Internal: keep embedding index in sync with markdown storage
    # ---------------------------------------------------------
    async def _sync_embedding(self, bucket_id: str, content: str) -> bool:
        """Best-effort inline indexing for runtimes without a queue worker."""
        if not self.embedding_engine or not getattr(self.embedding_engine, "enabled", False):
            return False
        if not content or not content.strip():
            return True
        return bool(
            await self.embedding_engine.generate_and_store(bucket_id, content)
        )

    async def _sync_meaning_embedding(self, bucket_id: str, meaning_list: list[str]) -> None:
        """Best-effort: embed the most recent meaning entry, separate from content.

        It takes the last entry in the list: the most recent feeling is usually the closest
        to the current context. There is no dedicated outbox or retry queue — a failed
        meaning vector does not affect the fact that the memory itself is already on disk,
        and the next hold or trace that appends a new meaning tries again.
        """
        if not meaning_list:
            return
        engine = self.embedding_engine
        if not engine or not getattr(engine, "enabled", False):
            return
        store_meaning = getattr(engine, "generate_and_store_meaning", None)
        if not callable(store_meaning):
            return
        try:
            await store_meaning(bucket_id, meaning_list[-1])
        except Exception as exc:
            logger.warning(f"meaning embedding failed for {bucket_id}: {exc}")

    async def _index_after_write(self, bucket_id: str, content: str) -> None:
        """Queue derived indexing after Markdown is safely on disk.

        The server runtime starts a durable outbox worker, so this returns
        without waiting for network I/O.  Standalone/stdio users still get one
        inline attempt; failures remain queued for a later managed startup.
        """
        outbox = self.embedding_outbox
        queued = False
        if outbox is not None:
            try:
                queued = bool(outbox.enqueue(bucket_id, content))
            except Exception as exc:
                logger.error(
                    "Failed to persist embedding outbox item for %s: %s",
                    bucket_id,
                    exc,
                )
            if queued and getattr(outbox, "running", False):
                return

        try:
            indexed = await self._sync_embedding(bucket_id, content)
        except Exception as exc:
            indexed = False
            logger.warning(
                "Inline embedding attempt failed; memory remains queued / "
                "同步向量尝试失败，记忆已保留待后台重试: %s: %s",
                bucket_id,
                exc,
            )
        if indexed and outbox is not None:
            try:
                outbox.discard(bucket_id)
            except Exception:
                logger.warning("Failed to acknowledge embedding outbox item: %s", bucket_id)
        elif not indexed:
            logger.warning(
                "Memory saved without vector; pending retry / 记忆已落盘，向量待重试: %s",
                bucket_id,
            )

    def _invalidate_bm25(self) -> None:
        """Called after a write: mark BM25 for rebuild and clear the active-bucket cache,
        since the set has changed and the cache is void.

        The name is historical (every write path already calls it); what it really is, is
        the single invalidation hook for "the set changed".
        """
        with self._active_cache_state_guard:
            self._active_cache_generation += 1
            self._bm25_dirty = True
            self._active_cache = None
            self._active_file_state = {}
            self._last_file_state_check = 0.0
        # This used to clear _bucket_path_index as well. Why that was removed: after every
        # managed write, the next create()'s collision check triggered a full re-parse of
        # every frontmatter in the store (886 files on a Windows bind mount ≈5.4s a time —
        # which was the hidden half of hold being slow and grow timing out). The path index
        # does not need wholesale invalidation for a **managed** write:
        #   ① create() inserts its own new entry after writing (under the same guard);
        #   ② every hit is verified with os.path.isfile, so a moved or deleted file drops
        #      its entry on the spot and marks the index not-ready, falling through to the
        #      filename scan and then a full rebuild;
        #   ③ a managed filename carries the id, so passing verification while pointing at a
        #      different bucket is impossible.
        # Invalidation for external edits (Obsidian, a hand-edit through git) goes through
        # list_all's file-state polling instead (the other _bucket_path_index_ready=False),
        # and is left exactly as it was — that is the case that genuinely needs a full
        # rebuild.

    def _cache_bump(
        self,
        bucket_id: str,
        *,
        last_active=None,
        activation_count=None,
        file_path: str = "",
    ) -> None:
        """touch only changes one bucket's activation fields and leaves the set unchanged,
        so the cache is updated in place rather than cleared wholesale."""
        with self._active_cache_state_guard:
            # Even with no published cache, a concurrent builder may have
            # parsed the pre-touch file.  Bumping the generation makes its
            # eventual publish fail the CAS and forces a rescan.
            self._active_cache_generation += 1
            if self._active_cache is None:
                return
            for b in self._active_cache:
                if b.get("id") == bucket_id:
                    m = b.get("metadata")
                    if isinstance(m, dict):
                        if last_active is not None:
                            m["last_active"] = last_active
                        if activation_count is not None:
                            m["activation_count"] = activation_count
                    break
            if file_path:
                self._refresh_cached_file_state(file_path)

    def _refresh_cached_file_state(self, file_path: str) -> None:
        """Acknowledge an internal in-place write without invalidating the cache."""
        with self._active_cache_state_guard:
            if self._active_cache is None:
                return
            normalized = os.path.normcase(os.path.abspath(file_path))
            try:
                stat = os.stat(file_path)
                self._active_file_state[normalized] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                self._active_file_state.pop(normalized, None)
            self._last_file_state_check = time.monotonic()

    def _scan_active_file_state(self) -> dict[str, tuple[int, int]]:
        """Return a cheap metadata fingerprint for every active Markdown file.

        🔴 It uses os.scandir, not walk + os.stat.
           Listing a directory already carries each entry's mtime and size (DirEntry caches
           them itself), so calling os.stat afterwards **asks the same question twice** — and
           on a Windows Docker bind mount one stat is one round trip across the boundary.
           Measured over 1000 files: 2.45 seconds, versus 1.05 seconds with scandir.
           Not one field of the fingerprint changed (mtime_ns + size); only the way it is
           fetched did.
        """
        state: dict[str, tuple[int, int]] = {}
        stack = [str(d) for d in self._active_dirs]
        while stack:
            current = stack.pop()
            try:
                scanner = os.scandir(current)
            except OSError:
                continue
            with scanner:
                for entry in scanner:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.name.endswith(".md"):
                            continue
                        stat = entry.stat()
                    except OSError:
                        continue
                    state[os.path.normcase(os.path.abspath(entry.path))] = (
                        stat.st_mtime_ns,
                        stat.st_size,
                    )
        return state

    def external_change_status(self) -> dict[str, Any]:
        with self._active_cache_state_guard:
            return {
                "poll_seconds": self.external_change_poll_seconds,
                "detected": self._external_changes_detected,
                "last_detected": self._last_external_change,
                "cached_files": len(self._active_file_state),
            }

    def _reconcile_external_changes(
        self,
        previous: list[dict],
        current: list[dict],
    ) -> None:
        """Propagate externally-created/edited/deleted Markdown to derived state."""
        old_by_id = {str(bucket.get("id") or ""): bucket for bucket in previous}
        new_by_id = {str(bucket.get("id") or ""): bucket for bucket in current}
        old_by_id.pop("", None)
        new_by_id.pop("", None)

        added_ids = set(new_by_id) - set(old_by_id)
        removed_ids = set(old_by_id) - set(new_by_id)
        content_changed_ids = {
            bucket_id
            for bucket_id in set(old_by_id) & set(new_by_id)
            if str(old_by_id[bucket_id].get("content") or "")
            != str(new_by_id[bucket_id].get("content") or "")
        }
        updated_ids = {
            bucket_id
            for bucket_id in set(old_by_id) & set(new_by_id)
            if bucket_id in content_changed_ids
            or (old_by_id[bucket_id].get("metadata") or {})
            != (new_by_id[bucket_id].get("metadata") or {})
        }

        outbox = self.embedding_outbox
        if outbox is not None:
            for bucket_id in sorted(added_ids | content_changed_ids):
                try:
                    outbox.enqueue(
                        bucket_id,
                        str(new_by_id[bucket_id].get("content") or ""),
                    )
                except Exception as exc:
                    logger.warning(
                        "external edit embedding enqueue failed for %s: %s",
                        bucket_id,
                        exc,
                    )
        for bucket_id in sorted(removed_ids):
            # Moving a file to archive is not physical deletion; keep its
            # derived vector. Only remove the index when the ID vanished from
            # every managed directory.
            if self._find_bucket_file(bucket_id) is not None:
                continue
            if outbox is not None:
                try:
                    outbox.discard(bucket_id)
                except Exception:
                    pass
            if self.embedding_engine is not None:
                try:
                    self.embedding_engine.delete_embedding(bucket_id)
                except Exception as exc:
                    logger.warning(
                        "external delete embedding cleanup failed for %s: %s",
                        bucket_id,
                        exc,
                    )

        for bucket_id in sorted(added_ids):
            bucket = new_by_id[bucket_id]
            self._record_v3_bucket_event(
                "external_create",
                bucket_id,
                str((bucket.get("metadata") or {}).get("type") or "dynamic"),
                str(bucket.get("content") or ""),
                dict(bucket.get("metadata") or {}),
            )
        for bucket_id in sorted(updated_ids):
            bucket = new_by_id[bucket_id]
            self._record_v3_bucket_event(
                "external_update",
                bucket_id,
                str((bucket.get("metadata") or {}).get("type") or "dynamic"),
                str(bucket.get("content") or ""),
                dict(bucket.get("metadata") or {}),
            )
        for bucket_id in sorted(removed_ids):
            bucket = old_by_id[bucket_id]
            self._record_v3_bucket_event(
                "external_delete",
                bucket_id,
                str((bucket.get("metadata") or {}).get("type") or "dynamic"),
                str(bucket.get("content") or ""),
                dict(bucket.get("metadata") or {}),
            )

        logger.info(
            "External vault change reconciled / 外部记忆文件变更已对账: "
            "added=%s changed=%s removed=%s",
            len(added_ids),
            len(updated_ids),
            len(removed_ids),
        )

    def _sunk_orig_path(self, bucket_id: str) -> str:
        """Where a sunk bucket's original text lives: archive/原文/{id}.txt.

        ⚠️ It must be .txt and never .md: with the same id owning an md file in two places,
        _find_bucket_file would collide.
        """
        return os.path.join(self.archive_dir, "原文", f"{bucket_id}.txt")

    def _bm25_source(self, bucket: dict) -> dict:
        """The bucket whose text BM25 indexes. A sunk bucket has only its summary left in
        the main store, but search still matches against the original text (you simply
        cannot see its details any more), so the original is read back from
        archive/原文/{id}.txt. Called from inside to_thread, never on the event loop."""
        meta = bucket.get("metadata", {}) or {}
        if str(meta.get("decay_stage") or "") != "sunk":
            return bucket
        txt = self._sunk_orig_path(str(meta.get("id") or bucket.get("id") or ""))
        try:
            with open(txt, encoding="utf-8") as f:
                return {**bucket, "content": f.read()}
        except OSError:
            return bucket  # if the original is gone, match on the summary; do not blow up the whole index over it

    def _build_bm25_index(self, buckets: list):
        """Build a **brand new** BM25 index and return it (jieba segmenting the whole store
        is slow: run it in a thread)."""
        idx = _BM25Index()  # type: ignore[operator]
        idx.build(buckets, self._bm25_source)
        return idx

    async def _sync_bm25(self, buckets: list, generation: int | None) -> None:
        """Bring BM25 level with `buckets`, read at store generation `generation`, in a
        thread. Only what changed is tokenised again, so after a write this costs tens of
        milliseconds, not a rebuild. The index stays dirty when a write landed after
        `buckets` was read (or when nobody knows when it was read): the next search
        syncs again."""
        await asyncio.to_thread(self._bm25.sync, buckets, self._bm25_source, generation)
        with self._active_cache_state_guard:
            if generation is not None and generation == self._active_cache_generation:
                self._bm25_dirty = False

    async def _rebuild_bm25_async(self, buckets: list, generation: int | None = None) -> None:
        """The first build, in the background: jieba over the whole store takes about a
        second per ~1700 entries (longer on a slow disk), too long to hold a search for.
        Searches until it lands score without BM25."""
        try:
            await self._sync_bm25(buckets, generation)
        except Exception as e:
            logger.warning(f"[bm25] 后台建索引失败，这次先不用字面分: {e}")
        finally:
            self._bm25_rebuilding = False

    # ---------------------------------------------------------
    # Create a new bucket
    # Write content and metadata into a .md file
    # ---------------------------------------------------------
    async def create(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        importance: int = 5,
        domain: Optional[list[str]] = None,
        valence: float = 0.5,
        arousal: float = 0.3,
        bucket_type: str = "dynamic",
        name: Optional[str] = None,
        pinned: bool = False,
        protected: bool = False,
        why_remembered: str = "",
        # Where this came from: [{rel, target}] (utils, `prov`). The caller has already
        # picked each rel from its route and checked what has to exist.
        prov: Optional[list[dict]] = None,
        weight: Optional[float] = None,
        source_tool: str = "",
        grow_batch_id: str = "",
        bucket_id_override: str = "",
        allow_embedding_fallback: bool = False,
        meaning: str = "",
        media: Any = None,
        test_data: bool = False,
        room: str = "",
        summary: str = "",
        when: str = "",
        subjects: Optional[list[str]] = None,
        direction_of_fit: str = "",
        bound: Optional[list[str]] = None,
        evidential: str = "",
        internally_generated: bool = False,
        recurrence: str = "",
        backfilled: Optional[list[str]] = None,
        cue: Any = None,
        exception_of: str = "",
        hold: str = "",
        review_after: str = "",
        card_of: str = "",
        sources: Any = None,
        looks_like_promise: bool = False,
    ) -> str:
        """
        Create a new memory bucket, return bucket ID.

        pinned/protected=True: bucket won't be merged, decayed, or have importance changed.
        Importance is locked to 10 for pinned/protected buckets.

        Provenance:
        - source_tool: "hold" | "grow" — which tool created it. feel goes through the hold
          branch, so a feel bucket has source_tool="hold" and is told apart by bucket_type.
        - grow_batch_id: every bucket split out of one grow call shares a batch_id, so the
          dashboard can group them by batch.
        - bucket_id_override: a readable id supplied by the caller (feel uses
          ``feel_202605011423_V085``). On a collision with an existing bucket a
          second-resolution suffix is appended automatically.
          Empty -> fall back to ``generate_bucket_id()`` (12 hex characters).
        """
        # ``allow_embedding_fallback`` is retained for API compatibility.
        # All memory types now write first; embedding is a derived index.

        # F-04: strip dangerous control characters and bidi overrides out of content, tags and name
        content = self._sanitize_text(content)
        self._validate_bucket_content(content)
        if name:
            name = self._sanitize_text(name)

        # Candidate selection is finalized immediately before the no-overwrite
        # write while holding that exact ID's normal bucket turn.  The value
        # here is provisional so metadata normalization can include a useful
        # source label without doing an unsafe check-then-write.
        preferred_bucket_id = (
            sanitize_name(bucket_id_override) or generate_bucket_id()
            if bucket_id_override
            else generate_bucket_id()
        )
        bucket_id = preferred_bucket_id
        # The bucket name is "YYYY-MM-DD HH-MM-SS [title generated by the LLM]", or just
        # the timestamp when there is no title.
        # Hyphens are used instead of colons, so that a later sanitize_name pass cannot
        # strip the colons and wreck readability.
        # The name and filename use the **local** time: something stored after midnight
        # carrying yesterday afternoon's name grates every time it is read. Only this naming
        # layer changed; created/last_active remain suffix-less UTC (the three conventions
        # in tools/_when — changing those would shift the whole history by eight hours).
        try:
            from ._when import now as _local_now
            _ts = _local_now().strftime("%Y-%m-%d %H-%M-%S")
        except Exception:
            _ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        _clean = sanitize_name(name) if name else ""
        bucket_name = (f"{_ts} {_clean}" if (_clean and _clean != "unnamed") else _ts)[:80]
        # feel buckets are allowed to have empty domain; others default to ["未分类"]
        if bucket_type == "feel":
            domain = domain if domain is not None else []
        else:
            domain = domain or [_DEFAULT_DOMAIN_NAME]
        domain = self._normalize_metadata_list(
            domain,
            max_items=_MAX_DOMAINS,
            max_chars=_MAX_DOMAIN_CHARS,
        )
        if bucket_type != "feel" and not domain:
            domain = [_DEFAULT_DOMAIN_NAME]
        tags = self._normalize_metadata_list(
            tags,
            max_items=_MAX_TAGS,
            max_chars=_MAX_TAG_CHARS,
        )
        linked_content = content  # wikilink injection disabled; LLM adds [[]] via prompt

        # --- Pinned/protected buckets: lock importance to 10 ---
        if pinned or protected:
            importance = _PINNED_IMPORTANCE

        # --- Build the YAML frontmatter metadata ---
        # Out-of-range values are not clamped silently: they raise an OB-W001/OB-W002 that
        # travels to the end of the MCP return value
        metadata = {
            "id": bucket_id,
            "name": bucket_name,
            "tags": tags,
            "domain": domain,
            "valence": _clamp_unit(valence, "valence", f"create:{bucket_id}"),
            "arousal": _clamp_unit(arousal, "arousal", f"create:{bucket_id}"),
            "importance": _clamp_importance(importance, f"create:{bucket_id}"),
            "type": bucket_type,
            "created": now_iso(),
            "last_active": now_iso(),
            "activation_count": 0,
        }
        if test_data:
            metadata["provenance"] = {
                "kind": "test",
                "created_by": str(source_tool or "developer")[:_SOURCE_TOOL_MAX],
                "erasable": True,
            }
        if pinned:
            metadata["pinned"] = True
        if protected:
            metadata["protected"] = True
        if bucket_type == "permanent" or pinned:
            metadata["type"] = "permanent"

        # --- The source tool and the grow batch ---
        # An empty source_tool means the caller did not declare one (older callers), and
        # nothing is written into the frontmatter.
        # Only the grow path passes grow_batch_id; hold and feel never carry that field.
        if source_tool:
            metadata["source_tool"] = str(source_tool).strip()[:_SOURCE_TOOL_MAX]
        if grow_batch_id:
            metadata["grow_batch_id"] = str(grow_batch_id).strip()[:_GROW_BATCH_ID_MAX]

        # --- Let a memory carry "why this is worth remembering" ---
        # A free-text field, written by the model or by hand. It takes no part in scoring;
        # it only appears in display and search.
        # An empty string means no reason was given, and the dashboard simply omits the row.
        if why_remembered:
            metadata["why_remembered"] = str(why_remembered).strip()[:_WHY_REMEMBERED_MAX]
        # --- Where this came from: `prov` (refused whole, before anything is written) ---
        prov_lines = self._normalize_prov(prov)
        if prov_lines:
            metadata[PROV_FIELD] = prov_lines
        # --- room / summary / when ---
        # The semantic validation of `room` lives in tools/_rooms.py and the caller decides
        # it; this file only stores it and has no opinion on its meaning.
        # `summary` is backfilled in the background by the dehydrator (it is on update's
        # whitelist too).
        # `when` is when the event happened; left empty it means the moment of storing, so
        # `created` can simply be read and nothing redundant is written.
        if room:
            metadata["room"] = self._sanitize_text(str(room)).strip()[:_ROOM_MAX]
        # --- subjects: the **third kind** of label, namely who ---
        # 🔴 It is a field of its own and must never be mixed in: mixed into tags it would
        #    break the guarantee that a tag appears literally in the body; mixed into
        #    aliases it would enter BM25 scoring, and who a memory is about should not move
        #    relevance.
        # The model extracts it and it is normalised through the alias table, so no extra
        # field has to be written by hand.
        if subjects:
            metadata["subjects"] = self._normalize_metadata_list(
                subjects, max_items=_MAX_SUBJECTS, max_chars=_MAX_SUBJECT_CHARS)
        if summary:
            metadata["summary"] = self._sanitize_text(str(summary)).strip()[:_SUMMARY_MAX]
        if when:
            metadata["when"] = str(when).strip()[:_WHEN_MAX]
        # --- meaning / media: why I myself think this memory is worth being recalled ---
        # meaning is a list[str]: on creation it holds one entry (the sentence this hold
        # passed in), and every later hold or trace appends to the same list. media is an
        # opaque list of external references.
        # Both are optional, neither depends on the other, and neither takes part in scoring.
        meaning_item = self._normalize_meaning_item(meaning)
        if meaning_item:
            metadata["meaning"] = [meaning_item]
        # --- v2 write-layer fields (validated here too: a bad value never reaches disk) ---
        metadata.update({k: v for k, v in self._v2_fields(
            direction_of_fit=direction_of_fit, bound=bound, evidential=evidential,
            internally_generated=internally_generated, recurrence=recurrence,
            backfilled=backfilled, cue=cue, exception_of=exception_of, hold=hold,
            review_after=review_after, card_of=card_of,
            sources=sources, looks_like_promise=looks_like_promise).items() if v is not None})
        if bool(metadata.get("exception_of")) != bool(metadata.get("hold")):
            raise ValueError("exception_of and hold come together: a hold names what it "
                             "is hung on and how far it reaches")
        if metadata.get("card_of") and is_event_room(metadata.get("room")):
            raise ValueError("card_of is for MIND entries: an event is not the card of a name")
        # --- "weight of the promise", 0.0-1.0, which is not importance ---
        # importance = how important this thing is; weight = how heavily it presses on me.
        # It belongs to what is wanted (telic).
        if metadata.get("direction_of_fit") == "telic" and weight is not None:
            metadata["weight"] = _clamp01(weight, _DEFAULT_VALENCE)
        # --- bucket_type_defaults: per-type default values ---
        # config.bucket_type_defaults may hold {letter: {weight: 1.0, dont_surface: false}, ...}
        # and is applied only where the caller passed no explicit value. A letter defaults to
        # weight=1.0, expressing that a letter has weight by its nature.
        # An older config without that section is skipped silently.
        try:
            type_defaults = (self.config.get("bucket_type_defaults") or {}).get(bucket_type, {})
            if type_defaults:
                if "weight" in type_defaults and "weight" not in metadata and weight is None:
                    metadata["weight"] = _clamp01(type_defaults["weight"], _DEFAULT_VALENCE)
                if "dont_surface" in type_defaults and "dont_surface" not in metadata:
                    if parse_bool(type_defaults["dont_surface"], default=False):
                        metadata["dont_surface"] = True
                if "why_remembered" in type_defaults and not why_remembered:
                    metadata["why_remembered"] = str(type_defaults["why_remembered"]).strip()[:_WHY_REMEMBERED_MAX]
        except Exception as e:
            logger.warning(f"bucket_type_defaults apply failed / 类型默认值应用失败: {e}")
        # --- The deliberate-forgetting switch, defaulting to False. A new bucket does not
        #     write it into the frontmatter, to save space ---
        # It only appears there after an update(dont_surface=True).
        # --- first_of_kind, decided automatically ---
        # The rule: this bucket's tags share nothing at all with the tags already in the
        # store -> this is a "first time".
        # Only buckets that carry tags are judged; a bucket with no tags is never marked.
        if tags:
            try:
                existing_tags = self._collect_all_tags()
                if existing_tags is not None and not (set(tags) & existing_tags):
                    metadata["first_of_kind"] = True
            except Exception as e:
                # A failure here must not block the main write path
                logger.warning(f"first_of_kind check failed / 首次标记检测失败: {e}")

        # --- Choose directory by type + primary domain ---
        if bucket_type == "permanent" or pinned:
            type_dir = self.permanent_dir
        elif bucket_type == "feel":
            type_dir = self.feel_dir
        elif bucket_type == "letter":
            type_dir = self.letter_dir
        else:
            type_dir = self.dynamic_dir
        if bucket_type == "feel":
            primary_domain = "沉淀物"  # feel subfolder name
        elif bucket_type == "letter":
            primary_domain = "history"
        else:
            primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)

        def _candidate_ids():
            yield preferred_bucket_id
            if bucket_id_override:
                yield f"{preferred_bucket_id}_{datetime.now().strftime('%S')}"
                for _attempt in range(5):
                    yield f"{preferred_bucket_id}_{uuid.uuid4().hex[:2]}"
            while True:
                yield generate_bucket_id()

        # Finalize the ID, persist any ID-keyed media and publish the Markdown
        # while holding the same turn used by update/migrate/delete.  The
        # second existence check closes the former create-vs-migrate TOCTOU.
        collision_count = 0
        for candidate_id in _candidate_ids():
            async with self._bucket_turn(candidate_id):
                # scan_if_missing=False: this call **expects to find nothing** (the id was
                # just generated), and taking the fallback scan would mean walking the whole
                # store for nothing on every single grow.
                # A genuine collision still has two more layers below: os.path.exists, and a
                # no-overwrite write.
                if self._find_bucket_file(candidate_id, scan_if_missing=False):
                    collision_count += 1
                    continue

                if bucket_name and bucket_name != candidate_id:
                    filename = f"{bucket_name}_{candidate_id}.md"
                else:
                    filename = f"{candidate_id}.md"
                candidate_path = safe_path(target_dir, filename)
                if os.path.exists(candidate_path):
                    collision_count += 1
                    continue

                bucket_id = candidate_id
                metadata["id"] = bucket_id
                metadata.pop("media", None)
                persisted_media = await self.media_store.persist(bucket_id, media)
                normalized_media = self._normalize_media(persisted_media)
                if normalized_media:
                    metadata["media"] = normalized_media

                post = frontmatter.Post(  # type: ignore[arg-type]
                    linked_content,
                    **metadata,
                )
                try:
                    # Publish the file and its ID lookup entry under the same
                    # path-index guard.  The collision check above can leave a
                    # complete, ready index that does not yet contain this new
                    # ID; without this hand-off an outbox worker can observe
                    # the committed Markdown as "missing" and discard its
                    # embedding task while create() awaits meaning indexing.
                    with self._bucket_path_index_guard:
                        _atomic_create_text(
                            candidate_path, frontmatter.dumps(post)
                        )
                        self._bucket_path_index[bucket_id] = candidate_path
                except FileExistsError:
                    # An unmanaged writer may not honor the bucket turn.  The
                    # no-overwrite publish still protects its file; choose a
                    # fresh ID instead of replacing it.
                    collision_count += 1
                    continue
                except OSError as e:
                    logger.error(
                        "Failed to write bucket file / 写入桶文件失败: "
                        "%s: %s",
                        candidate_path,
                        e,
                    )
                    raise
                break

        if collision_count > 6:
            logger.warning(
                "bucket_id_override %r repeatedly conflicted; used random id %s",
                preferred_bucket_id,
                bucket_id,
            )

        # Markdown becomes the visible source of truth before any network or
        # derived-index await.  This also changes the path index from the
        # precise hand-off above to a normal lazy rebuild for later lookups.
        self._invalidate_bm25()
        # The file is on disk: a keyed write (core/_sources.run_once) claims this id.
        note_written(bucket_id)

        logger.info(
            f"Created bucket / 创建记忆桶: {bucket_id} ({bucket_name}) → {primary_domain}/"
            + (" [PINNED]" if pinned else "") + (" [PROTECTED]" if protected else "")
        )

        # Markdown is committed before any derived-index work. The managed
        # server enqueues and returns immediately; standalone mode tries once.
        await self._index_after_write(bucket_id, linked_content)
        # meaning gets an embedding of its own rather than being concatenated into content
        # and embedded together. Concatenating would let a long content dominate the vector
        # and dilute the signal of a one-sentence meaning; stored separately, retrieval takes
        # the higher of the two similarities and a single sentence of feeling can be hit on
        # its own.
        # Best effort: a failure only logs a warning and does not affect the fact that the
        # bucket is already on disk.
        await self._sync_meaning_embedding(bucket_id, metadata.get("meaning") or [])
        self._record_v3_bucket_event(
            "create",
            bucket_id,
            str(metadata.get("type") or bucket_type),
            linked_content,
            metadata,
        )
        self._record_ledger_event(
            "TraceCreated",
            bucket_id,
            str(metadata.get("type") or bucket_type),
            linked_content,
            metadata,
        )

        return bucket_id

    # ---------------------------------------------------------
    # Read bucket content
    # Returns {"id", "metadata", "content", "path"} or None
    # ---------------------------------------------------------
    async def get(self, bucket_id: str) -> Optional[dict]:
        """
        Read a single bucket by ID.
        F-10: a soft-deleted bucket (one carrying deleted_at) is invisible to ordinary
        callers and comes back as None.
        """
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None
        data = self._load_bucket(file_path)
        # F-10: a soft-deleted bucket must not be visible through get()
        if data and data.get("metadata", {}).get("deleted_at"):
            return None
        return data

    def is_live(self, bucket_id: str) -> bool:
        """Is this id a bucket in the active store? Archived, soft-deleted (both live in
        archive/) and missing ids are not. Synchronous and file-level: a cover check runs
        once per covered entry inside read loops, and must not parse YAML."""
        if not bucket_id or not isinstance(bucket_id, str):
            return False
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False
        path = os.path.normcase(os.path.abspath(file_path))
        archive = os.path.normcase(os.path.abspath(self.archive_dir))
        try:
            return os.path.commonpath((path, archive)) != archive
        except ValueError:
            return True

    async def get_including_archive(self, bucket_id: str) -> Optional[dict]:
        """Read one bucket by ID without hiding its archived/tombstoned state."""
        if not bucket_id or not isinstance(bucket_id, str):
            return None
        file_path = self._find_bucket_file(bucket_id)
        return self._load_bucket(file_path) if file_path else None

    def find_exact_content(
        self,
        content: str,
        domain_filter: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """Read Markdown directly for an exact match, bypassing derived caches."""
        expected = self._sanitize_text(content)
        filter_set = {
            str(domain).strip().lower()
            for domain in (domain_filter or [])
            if str(domain).strip()
        }
        for _root, _fname, file_path in self._iter_md_files(self._active_dirs):
            bucket = self._load_bucket(file_path)
            if not bucket or bucket.get("content") != expected:
                continue
            metadata = bucket.get("metadata", {})
            if metadata.get("deleted_at"):
                continue
            domains = metadata.get("domain") or []
            if isinstance(domains, str):
                domains = [domains]
            if filter_set and not {
                str(domain).strip().lower() for domain in domains
            } & filter_set:
                continue
            return bucket
        return None

    # ---------------------------------------------------------
    # Move bucket between directories
    # ---------------------------------------------------------
    def _move_bucket(self, file_path: str, target_type_dir: str, domain: Optional[list[str]] = None) -> str:
        """
        Move a bucket file to a new type directory, preserving domain subfolder.
        Returns new file path.
        """
        primary_domain = self._primary_domain(domain)
        target_dir = os.path.join(target_type_dir, primary_domain)
        os.makedirs(target_dir, exist_ok=True)
        filename = os.path.basename(file_path)
        new_path = safe_path(target_dir, filename)
        if os.path.normpath(file_path) != os.path.normpath(new_path):
            os.rename(file_path, new_path)
            logger.info(f"Moved bucket / 移动记忆桶: {filename} → {target_dir}/")
        return str(new_path)

    def _bucket_target_path(
        self,
        file_path: str,
        bucket_type: str,
        domain: Optional[list[str]] = None,
        status: str = "",
    ) -> str:
        """Return the canonical storage path for an editable bucket type.

        The frontmatter ``type`` is the logical source of truth, but the vault
        layout is part of the public Obsidian contract too.  Keeping this
        mapping in one place prevents dashboard edits from changing metadata
        without moving the Markdown file.
        """
        normalized_type = str(bucket_type or "dynamic").strip().lower()
        if normalized_type not in _EDITABLE_BUCKET_TYPES:
            raise ValueError(f"unsupported editable bucket type: {normalized_type}")

        if normalized_type == "permanent":
            type_dir = self.permanent_dir
            subdir = self._primary_domain(domain)
        elif normalized_type == "feel":
            type_dir = self.feel_dir
            subdir = "沉淀物"
        elif normalized_type == "letter":
            type_dir = self.letter_dir
            subdir = "history"
        else:
            # ``i`` / ``self`` are private logical channels, not separate
            # physical stores.  They intentionally live under dynamic/<domain>.
            type_dir = self.dynamic_dir
            subdir = self._primary_domain(domain)

        target_dir = os.path.join(type_dir, subdir)
        os.makedirs(target_dir, exist_ok=True)
        return str(safe_path(target_dir, os.path.basename(file_path)))

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )

    def _commit_bucket_update(
        self,
        file_path: str,
        target_path: str,
        serialized: str,
        bucket_id: str = "",
    ) -> str:
        """Commit an update without leaving type/path split-brain on failure.

        In-place edits use the normal atomic writer.  A directory migration is
        copy-on-commit: atomically write the complete new file at its canonical
        destination, then remove the untouched source.  If source removal
        fails, delete the new copy and keep the original as the sole truth.
        Existing destination files are never overwritten.

        A managed move has to keep the ID -> path index in step. _invalidate_bm25 no longer
        clears that index wholesale, so a file moved here without updating the mapping would
        cause the next hit on the old path to mark the index not-ready -> another full
        re-parse of the store (5s+).
        The caller passes bucket_id where it can; without it this degrades to removing the
        old mapping, which is fail-safe.
        """
        if self._same_path(file_path, target_path):
            _atomic_write_text(file_path, serialized)
            return file_path

        if os.path.exists(target_path):
            raise FileExistsError(
                f"bucket migration target already exists: {target_path}"
            )

        _atomic_write_text(target_path, serialized)
        try:
            os.remove(file_path)
        except Exception:
            try:
                os.remove(target_path)
            except OSError as rollback_error:
                logger.critical(
                    "Failed to roll back bucket migration target %s: %s",
                    target_path,
                    rollback_error,
                )
            raise

        self._path_index_moved(file_path, target_path, bucket_id)

        logger.info(
            "Moved bucket / 移动记忆桶: %s → %s/",
            os.path.basename(file_path),
            os.path.dirname(target_path),
        )
        return target_path

    def _path_index_moved(self, old_path: str, new_path: str, bucket_id: str = "") -> None:
        """Maintain the ID -> path index after a managed move or archive (see the comments
        in _commit_bucket_update).

        With a bucket_id known, update the mapping in place; without one, look the old path
        up in reverse and remove that entry.
        It only maintains incrementally and never marks the index not-ready — wholesale
        invalidation for external edits goes through list_all's polling.
        """
        with self._bucket_path_index_guard:
            if bucket_id:
                self._bucket_path_index[bucket_id] = new_path
                return
            stale_keys = [
                k for k, v in self._bucket_path_index.items()
                if self._same_path(v, old_path)
            ]
            for k in stale_keys:
                if new_path:
                    self._bucket_path_index[k] = new_path
                else:
                    self._bucket_path_index.pop(k, None)

    def _path_index_removed(self, bucket_id: str, path: str = "") -> None:
        """Remove the entry after a managed delete. It does not mark the index not-ready:
        the file really is gone, so a miss IS the correct answer."""
        with self._bucket_path_index_guard:
            if bucket_id:
                self._bucket_path_index.pop(bucket_id, None)
            if path:
                for k in [k for k, v in self._bucket_path_index.items()
                          if self._same_path(v, path)]:
                    self._bucket_path_index.pop(k, None)
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

    async def replace_text_fields(self, old: str, new: str) -> dict[str, int]:
        """Replace a display term through managed per-bucket transactions.

        This is used when the configured human name changes.  It deliberately
        does not bump ``last_active``: a display-name migration is not a memory
        activation.  Each bucket is re-read while holding the normal
        cross-process bucket lock and then committed through ``_update_locked``
        so atomic writes, derived-index updates, ledger/projection events and
        concurrent edits retain the same guarantees as every other mutation.
        """

        if not old or not new or old == new:
            return {"buckets_changed": 0, "replacements": 0}

        pattern = re.compile(re.escape(old))
        bucket_ids: list[str] = []
        seen: set[str] = set()
        directories = list(self._active_dirs) + [self.archive_dir]
        for _root, _filename, file_path in self._iter_md_files(directories):
            bucket = self._load_bucket(file_path)
            bucket_id = str((bucket or {}).get("id") or "").strip()
            if bucket_id and bucket_id not in seen:
                seen.add(bucket_id)
                bucket_ids.append(bucket_id)

        changed = 0
        total = 0
        for bucket_id in bucket_ids:
            async with self._bucket_turn(bucket_id):
                file_path = self._find_bucket_file(bucket_id)
                if not file_path:
                    continue
                try:
                    post = frontmatter.load(file_path)
                except Exception as exc:
                    logger.warning(
                        "Failed to load bucket for text replacement %s: %s",
                        bucket_id,
                        exc,
                    )
                    continue

                replacements = 0
                updates: dict[str, str] = {}
                # A callable replacement is literal.  Passing ``new`` directly
                # would interpret user-controlled ``\\1``/``\\g<name>`` syntax
                # as regular-expression group references.
                content, count = pattern.subn(lambda _match: new, post.content or "")
                if count:
                    updates["content"] = content
                    replacements += count
                for field in ("name", "why_remembered", "user_name"):
                    value = post.get(field)
                    if not isinstance(value, str) or not value:
                        continue
                    replaced, count = pattern.subn(lambda _match: new, value)
                    if count:
                        updates[field] = replaced
                        replacements += count

                if not updates:
                    continue
                try:
                    committed = await self._update_locked(bucket_id, **updates)
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Text replacement rejected for bucket %s: %s",
                        bucket_id,
                        exc,
                    )
                    continue
                if committed:
                    changed += 1
                    total += replacements

        return {"buckets_changed": changed, "replacements": total}

    async def update_content_fragment(
        self,
        bucket_id: str,
        *,
        old_str: str,
        new_str: str,
        **kwargs,
    ) -> dict[str, Any]:
        """Atomically replace one unique literal fragment in a bucket body.

        The match and the write deliberately happen under the same per-bucket
        cross-process lock.  Computing the replacement from an earlier
        ``get()`` result would let a concurrent trace/update be overwritten by
        a stale full-body snapshot.

        ``new_str`` may be empty (delete the matched fragment).  Zero matches
        and multiple matches are both non-mutating results so callers never
        have to guess which occurrence was intended.
        """
        old_text = str(old_str)
        replacement = str(new_str)
        if not old_text:
            return {"ok": False, "error": "empty_old_str", "matches": 0}
        if "content" in kwargs:
            return {"ok": False, "error": "content_conflict", "matches": 0}

        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return {"ok": False, "error": "not_found", "matches": 0}
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                logger.warning(
                    "Failed to load bucket for content patch %s: %s",
                    bucket_id,
                    exc,
                )
                return {"ok": False, "error": "read_failed", "matches": 0}

            current_content = str(post.content or "")
            # ``str.count`` ignores overlapping occurrences ("aa" in "aaa"),
            # which could silently patch the first of two valid match starts.
            # Only 0/1/many matters, so stop at the second start rather than
            # scanning every pathological overlapping match.
            first_match = current_content.find(old_text)
            if first_match < 0:
                return {
                    "ok": False,
                    "error": "old_str_not_found",
                    "matches": 0,
                }
            second_match = current_content.find(old_text, first_match + 1)
            if second_match >= 0:
                return {
                    "ok": False,
                    "error": "old_str_ambiguous",
                    "matches": 2,
                }

            updated_content = self._sanitize_text(
                current_content.replace(old_text, replacement, 1)
            )
            if updated_content == current_content:
                return {"ok": False, "error": "unchanged", "matches": 1}
            if not updated_content.strip():
                return {
                    "ok": False,
                    "error": "invalid_content",
                    "matches": 1,
                    "message": "替换后正文不能为空；如需移除整个桶，请使用归档。",
                }

            updates = dict(kwargs)
            updates["content"] = updated_content
            try:
                committed = await self._update_locked(bucket_id, **updates)
            except ValueError as exc:
                return {
                    "ok": False,
                    "error": "invalid_content",
                    "matches": 1,
                    "message": str(exc),
                }
            if committed:
                note_written(bucket_id)
            return {
                "ok": bool(committed),
                "error": "" if committed else "update_failed",
                "matches": 1,
            }

    # ---------------------------------------------------------
    # Update bucket
    # Supports: content, tags, importance, valence, arousal, name, resolved
    # ---------------------------------------------------------
    async def update(
        self,
        bucket_id: str,
        *,
        allow_embedding_fallback: bool = False,
        bump_active: bool = False,
        **kwargs,
    ) -> bool:
        """
        Update bucket content or metadata fields.

        bump_active=False (the default): a pure metadata or content edit — trace,
        anchor, a background auto-resolve, an import — which does **not** refresh
        last_active and does not touch activation_count.
        bump_active=True: treat this write as a genuine activation (hold/grow merging into a
        neighbouring bucket, say), refreshing last_active and incrementing
        activation_count, with the same meaning as touch().
        """
        async with self._bucket_turn(bucket_id):
            committed = await self._update_locked(
                bucket_id,
                allow_embedding_fallback=allow_embedding_fallback,
                bump_active=bump_active,
                **kwargs,
            )
        # What this call wrote, for a keyed write's claim (core/_sources.run_once).
        if committed:
            note_written(bucket_id)
        return committed

    async def _update_locked(
        self,
        bucket_id: str,
        *,
        allow_embedding_fallback: bool = False,
        bump_active: bool = False,
        **kwargs,
    ) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # Normalize public/migration inputs at the storage boundary.  A quoted
        # YAML value such as "false" must never be persisted as true merely
        # because Python considers non-empty strings truthy.
        for field in (
            "resolved",
            "pinned",
            "digested",
            "dont_surface",
            "first_of_kind",
            "anchor",
        ):
            if field in kwargs:
                kwargs[field] = parse_bool(kwargs[field])

        if "content" in kwargs:
            kwargs["content"] = self._sanitize_text(kwargs["content"])
            self._validate_bucket_content(kwargs["content"])
        if "tags" in kwargs:
            kwargs["tags"] = self._normalize_metadata_list(
                kwargs["tags"],
                max_items=_MAX_TAGS,
                max_chars=_MAX_TAG_CHARS,
            )
        if "aliases" in kwargs:
            # The expansion words: they feed the bm25 index only and never appear on the tag
            # row a human reads
            kwargs["aliases"] = self._normalize_metadata_list(
                kwargs["aliases"],
                max_items=_MAX_TAGS,
                max_chars=_MAX_TAG_CHARS,
            )
        if "subjects" in kwargs:
            # subjects, the third kind of label. It enters neither tags nor aliases; see create().
            kwargs["subjects"] = self._normalize_metadata_list(
                kwargs["subjects"],
                max_items=_MAX_SUBJECTS,
                max_chars=_MAX_SUBJECT_CHARS,
            )
        if "domain" in kwargs:
            kwargs["domain"] = self._normalize_metadata_list(
                kwargs["domain"],
                max_items=_MAX_DOMAINS,
                max_chars=_MAX_DOMAIN_CHARS,
            ) or [_DEFAULT_DOMAIN_NAME]
        if "media" in kwargs:
            # media is a wholesale overwrite (trace's media_replace). An empty list clears the field.
            kwargs["media"] = self._normalize_media(
                await self.media_store.persist(bucket_id, kwargs["media"])
            )
        if "media_append" in kwargs:
            # media_append appends (trace's media_append, and every hold call).
            kwargs["media_append"] = self._normalize_media(
                await self.media_store.persist(bucket_id, kwargs["media_append"])
            )
        if "meaning" in kwargs:
            # meaning is a wholesale overwrite (trace's meaning_replace, for corrections and cleanup).
            kwargs["meaning"] = self._normalize_meaning_list(kwargs["meaning"])
        if "meaning_append" in kwargs:
            # meaning_append appends one new meaning (trace's meaning_append, and every hold call).
            kwargs["meaning_append"] = self._normalize_meaning_item(kwargs["meaning_append"])
        if "invalidation" in kwargs:
            # The whole list is written each time; the caller appends to what it read.
            kwargs["invalidation"] = self._normalize_invalidation(kwargs["invalidation"])
        if PROV_FIELD in kwargs:
            # The whole list is written each time, like invalidation. A bad line refuses
            # the whole update rather than reaching disk.
            try:
                kwargs[PROV_FIELD] = self._normalize_prov(kwargs[PROV_FIELD])
            except ValueError as exc:
                logger.warning(f"update() refused {bucket_id}: {exc}")
                return False

        try:
            post = frontmatter.load(file_path)
        except Exception as e:
            logger.warning(f"Failed to load bucket for update / 加载桶失败: {file_path}: {e}")
            return False

        # Work out the final pin/type state before mutating the post.  Type is
        # also a physical-storage decision, so unsupported values must fail
        # here instead of being written into frontmatter and reported as a
        # successful edit.
        was_pinned = parse_bool(post.get("pinned", False), default=False)
        is_protected = parse_bool(post.get("protected", False), default=False)
        current_type = str(post.get("type") or "dynamic").strip().lower()
        if (
            current_type == "archived"
            or post.get("deleted_at")
            or parse_bool(post.get("tombstone"), default=False)
        ):
            # Archive is a terminal lifecycle transition.  Allowing an
            # ordinary update here is unsafe even when ``type`` was omitted:
            # pinned=True forces type=permanent below and could otherwise
            # resurrect an archived file into the active tree.
            logger.warning(
                "update() rejected mutation on terminal bucket=%s",
                bucket_id,
            )
            return False
        will_be_pinned = parse_bool(
            kwargs.get("pinned", was_pinned), default=was_pinned
        )

        requested_type: str | None = None
        if "type" in kwargs:
            requested_type = str(kwargs["type"] or "").strip().lower()
            if requested_type not in _EDITABLE_BUCKET_TYPES:
                logger.warning(
                    "update() rejected unsupported type=%r for bucket=%s",
                    requested_type,
                    bucket_id,
                )
                return False
        forced_type: str | None = None
        if will_be_pinned:
            forced_type = "permanent"
        elif "pinned" in kwargs and was_pinned and not is_protected:
            # A true pinned bucket demotes when explicitly unpinned.  Explicit
            # permanent memories (was_pinned=False) remain permanent.
            forced_type = "dynamic"

        if forced_type is not None:
            if requested_type is not None and requested_type != forced_type:
                logger.warning(
                    "update() rejected incompatible pinned/type transition "
                    "bucket=%s pinned=%s type=%s",
                    bucket_id,
                    will_be_pinned,
                    requested_type,
                )
                return False
            kwargs["type"] = forced_type
            requested_type = forced_type

        if (
            requested_type is not None
            and requested_type != current_type
            and is_protected
            and requested_type != "permanent"
        ):
            logger.warning(
                "update() rejected protected bucket type transition "
                "bucket=%s type=%s",
                bucket_id,
                requested_type,
            )
            return False

        # pinned/protected buckets lock importance at 10.  An atomic
        # pinned=False + importance=N transition is allowed, because the final
        # state is no longer pinned; this is needed for quota-safe unpinning.
        if will_be_pinned or is_protected:
            kwargs.pop("importance", None)
        elif forced_type == "dynamic" and "importance" not in kwargs:
            # 🔴 **Unpinning drops importance back down with it** (bug ④).
            # The old behaviour: after unpinning, importance stayed at 10 — and a pinned
            # bucket does not occupy the importance>=9 pool, so the moment it is unpinned it
            # falls into that pool at full marks and fills it up (the pool caps at 24).
            # Meanwhile the importance parameter was withdrawn entirely: **there is no
            # entry point anywhere that can bring it back down**, so unpinning became a
            # one-way door. The "quota-safe unpinning" path mentioned in the comment above
            # has been there all along; it simply had no caller able to reach it any more —
            # so this branch walks it on their behalf.
            # Landing on 8 is not an arbitrary pick: it is the number the system already
            # degrades to when the hard cap is hit (_HIGH_IMP_DEGRADE_TO), and two paths
            # arriving at the same place is what stops them fighting each other.
            kwargs["importance"] = 8

        # --- Update only fields that were passed in ---
        if "content" in kwargs:
            post.content = kwargs["content"]  # wikilink injection disabled; LLM adds [[]] via prompt
        if "tags" in kwargs:
            post["tags"] = kwargs["tags"]
        if "aliases" in kwargs:
            if kwargs["aliases"]:
                post["aliases"] = kwargs["aliases"]
            else:
                post.metadata.pop("aliases", None)
        if "importance" in kwargs:
            post["importance"] = _clamp_importance(kwargs["importance"], f"update:{bucket_id}")
        if "domain" in kwargs:
            post["domain"] = kwargs["domain"]
        if "valence" in kwargs:
            post["valence"] = _clamp_unit(kwargs["valence"], "valence", f"update:{bucket_id}")
        if "arousal" in kwargs:
            post["arousal"] = _clamp_unit(kwargs["arousal"], "arousal", f"update:{bucket_id}")
        if "name" in kwargs:
            post["name"] = sanitize_name(kwargs["name"])
        if "resolved" in kwargs:
            post["resolved"] = kwargs["resolved"]
        if "pinned" in kwargs:
            post["pinned"] = kwargs["pinned"]
            if kwargs["pinned"]:
                post["importance"] = _PINNED_IMPORTANCE  # pinned → lock importance to 10
                post.metadata.pop("anchor", None)  # pinned and anchor are mutually exclusive: pinning it as a core rule clears the coordinate-system mark
        if "digested" in kwargs:
            post["digested"] = kwargs["digested"]
        if "model_valence" in kwargs:
            post["model_valence"] = _clamp01(kwargs["model_valence"], _DEFAULT_VALENCE)
        if "media" in kwargs:
            # A wholesale overwrite (trace's media_replace); an empty list clears the field.
            if kwargs["media"]:
                post["media"] = kwargs["media"]
            else:
                post.metadata.pop("media", None)
        if "media_append" in kwargs and kwargs["media_append"]:
            # Appends, deduplicating any earlier reference with the same path (trace's
            # media_append, and every hold call).
            existing_media = post.get("media") or []
            existing_paths = {m.get("path") for m in existing_media if isinstance(m, dict)}
            appended = existing_media + [
                m for m in kwargs["media_append"] if m.get("path") not in existing_paths
            ]
            post["media"] = appended[:_MEDIA_MAX_ITEMS]
        if "meaning" in kwargs:
            # A wholesale overwrite (trace's meaning_replace, for corrections and cleanup);
            # an empty list clears the field.
            if kwargs["meaning"]:
                post["meaning"] = kwargs["meaning"]
            else:
                post.metadata.pop("meaning", None)
        if "meaning_append" in kwargs and kwargs["meaning_append"]:
            # Appends one new meaning without overwriting what is there (trace's
            # meaning_append, and every hold call).
            existing_meaning = post.get("meaning") or []
            if isinstance(existing_meaning, str):
                existing_meaning = [existing_meaning]
            post["meaning"] = (list(existing_meaning) + [kwargs["meaning_append"]])[:_MEANING_LIST_MAX_ITEMS]
        # --- v2 write-layer fields: stored form; None (= the default) removes the field ---
        _v2_given = {k: kwargs[k] for k in V2_FIELDS if k in kwargs}
        if _v2_given:
            try:
                kwargs.update(self._v2_fields(**_v2_given))
            except ValueError as exc:
                logger.warning(f"update() refused {bucket_id}: {exc}")
                return False
        # --- Pass-through fields for the letter lifecycle and the rest ---
        # These fields have no validation or conversion logic: whatever is given is written.
        # A new field only has to be added to this tuple.
        for k in ("status", "type", "resolution_reason", "resolved_by",
                  "related_bucket", "author", "user_name", "title", "letter_date",
                  # Everything below passes through unconverted, weight included.
                  # weight only means anything on something wanted; its type is not checked in this
                  # loop, and server.py above guarantees the range it passes in.
                  "why_remembered", "dont_surface", "first_of_kind",
                  "weight",
                  # prov: where this came from, normalised above. Writing it retires the
                  # older `from` string on the same entry, so the two can never disagree;
                  # an empty list removes the field.
                  PROV_FIELD,
                  # subjects. The dehydrator's backfill comes through this path.
                  "subjects",
                  # anchor: a bool that takes no part in scoring, hard-capped at 24.
                  # The cap is checked in the anchor branch below (counting only on a
                  # False->True transition). set_anchor() remains the preferred entry point;
                  # update() is only here as a fallback for bulk migration scripts.
                  "anchor",
                  # Provenance fields:
                  # source_tool / grow_batch_id are normally fixed at create() time, and the
                  # pass-through here serves migration scripts backfilling older buckets.
                  # last_merged_by is written by _common.merge_or_create after a merge and
                  # says whether hold or grow triggered the last merge.
                  # _pre_anchor_source_tool holds the original source_tool saved when
                  # anchoring, restored automatically on release; None deletes the field.
                  "source_tool", "grow_batch_id", "last_merged_by", "_pre_anchor_source_tool",
                  # room (semantic validation in tools/_rooms.py), summary (the
                  # background-backfilled one-sentence summary), when (when the event
                  # happened).
                  "room", "summary", "when",
                  # regrow's version chain. supersedes = who I replaced; superseded_by = who
                  # replaced me (any value means "an old version": recall and surfacing skip
                  # it, while a direct lookup by id still sees it).
                  "supersedes", "superseded_by",
                  # The two ends of fold / gist.
                  # cover = who this gist covers (a list[str], always a definite list of ids
                  #   — a time range is only a convenient way to write the input, and what
                  #   lands on disk must already be resolved);
                  # covered_by = who covers me (any value means "stops surfacing on its own",
                  #   while search, direct id lookup and drilling down are unaffected).
                  # 🔴 Writing both ends is **deliberate**: the surfacing pools rescan the
                  #   whole store every round, and querying the single field covered_by is
                  #   enough to filter, with no reverse index to maintain.
                  "cover", "covered_by",
                  # The three forgetting stages alive/faded/sunk.
                  # alive/faded are written here by the decay loop (pure metadata); sunk goes
                  # through sink_bucket(), because it has to move the original text and a
                  # plain update() would recompute the vector when content changes.
                  "decay_stage",
                  # Closing a want records who closed it, plus a timestamp of when the user
                  # was last asked about it. 🔴 The hole this filled: both fields were already
                  # assembled into the updates dict in trace_core.py, and closed_by even had
                  # a length cap in _METADATA_TEXT_LIMITS, but neither had ever been listed on
                  # this pass-through whitelist — and update() writes only whitelisted keys,
                  # so a field missing from here is **silently discarded**. The result was
                  # that the close button and the "asked about it" stamp appeared to succeed
                  # while not one character reached disk.
                  "closed_by", "last_asked",
                  # When this entry was last dreamt about. Weaving a dream records this and
                  # nothing else, and ingredient selection discounts by it — "do not keep
                  # having the same dream". **weight is not touched.**
                  # 🔴 Writing that field walked **straight back into the trap the comment
                  #    above describes**: it was added only as `update(last_dreamt=...)` in
                  #    `_dream.py` and never put on this list, so it was silently dropped, the
                  #    smoke test reported "last_dreamt=None", and that looked like a bug
                  #    somewhere else entirely.
                  #    **The very thing one comment warned about happened again within the
                  #    hour** — which says that "remember to add it to the list" does not work
                  #    as a method.
                  #    -> What cures it is not remembering harder, it is the assertion in
                  #      smoke_dream that last_dreamt really was written: that one goes red
                  #      when the list entry is missing.
                  "last_dreamt",
                  # Where `name` / `summary` came from. Present with the value
                  # "fallback" = that field is not the model's answer, it is a slice of
                  # the caller's own body standing in for one that never arrived; absent
                  # = the ordinary case. Written and cleared by grow's background
                  # backfill, and the only reason they are persisted at all is so a
                  # re-tagging pass can still **find** those buckets — a fallback fills
                  # the field in, which makes every "this one is unfinished" check stop
                  # matching it.
                  # 📌 And note where this line sits: right under the `last_dreamt`
                  #    epitaph, which is the same mistake written down — a field added at
                  #    one end, never listed here, silently dropped by update(), and the
                  #    symptom looking like a bug somewhere else entirely. What catches
                  #    it is not remembering, it is the assertion that the stamp really
                  #    reached disk.
                  "name_source", "summary_source",
                  # invalidation: the records saying a basis of this memory changed under
                  # it (regrow's overturn writes them on every descendant). Normalised
                  # above; an empty list removes the field.
                  "invalidation",
                  # v2 write-layer fields, already normalised just above this loop.
                  *V2_FIELDS):
            if k in kwargs:
                if k == "weight" and kwargs[k] is not None:
                    post[k] = _clamp01(kwargs[k], _DEFAULT_VALENCE)
                elif k == "invalidation":
                    if kwargs[k]:
                        post[k] = kwargs[k]
                    else:
                        post.metadata.pop(k, None)
                elif k == PROV_FIELD:
                    post.metadata.pop(LEGACY_FROM_FIELD, None)
                    if kwargs[k]:
                        post[k] = kwargs[k]
                    else:
                        post.metadata.pop(k, None)
                elif k == "room":
                    post[k] = self._sanitize_text(str(kwargs[k])).strip()[:_ROOM_MAX]
                elif k == "summary":
                    post[k] = self._sanitize_text(str(kwargs[k])).strip()[:_SUMMARY_MAX]
                elif k == "when":
                    post[k] = str(kwargs[k]).strip()[:_WHEN_MAX]
                elif k == "dont_surface":
                    post[k] = kwargs[k]
                elif k == "first_of_kind":
                    post[k] = kwargs[k]
                elif k == "anchor":
                    # anchor is a bool; a False deletes the field outright to keep the
                    # frontmatter clean.
                    # The fix: the pass-through path used to bypass ANCHOR_LIMIT, so a bulk
                    # script or the front end calling update(anchor=True) directly could push
                    # the anchor total past the cap of 24. A check is added here, counting
                    # only on a False->True transition; setting anchor again on a bucket that
                    # already has it does not count.
                    if kwargs[k]:
                        already_anchor = parse_bool(
                            post.get("anchor", False), default=False
                        )
                        if not already_anchor:
                            # FIX (RED-02): count_anchors is async and must be awaited, or
                            # `coroutine >= int` raises TypeError and the whole cap check
                            # stops working.
                            current = await self.count_anchors()
                            if current >= self.ANCHOR_LIMIT:
                                logger.warning(
                                    f"update() 拒绝 anchor=True：已达上限 "
                                    f"{self.ANCHOR_LIMIT}（当前 {current}）。bucket={bucket_id}"
                                )
                                return False
                        post["anchor"] = True
                    else:
                        post.metadata.pop("anchor", None)
                else:
                    if kwargs[k] is None:
                        # None explicitly deletes that frontmatter field (used when an anchor
                        # release clears its temporary field)
                        post.metadata.pop(k, None)
                    elif k in _METADATA_TEXT_LIMITS:
                        post[k] = self._sanitize_text(str(kwargs[k])).strip()[
                            :_METADATA_TEXT_LIMITS[k]
                        ]
                    else:
                        post[k] = kwargs[k]
        # A hold is both halves or neither: half of one is a pointer that holds nothing,
        # or a level hung on nothing.
        if bool(post.get("exception_of")) != bool(post.get("hold")):
            logger.warning(f"update() refused {bucket_id}: exception_of and hold come together")
            return False
        if post.get("card_of") and is_event_room(post.get("room")):
            logger.warning(f"update() refused {bucket_id}: card_of is for MIND entries")
            return False

        # --- Activation time and activation count ---
        # last_active means "the last genuine activation or recall" and nothing else, and it
        # is the input to decay's recency scoring.
        # A metadata edit — trace, anchor, a background auto-resolve — **does not
        # count as activity**: refreshing it unconditionally here would reset the forgetting
        # clock, and would also leave activation_count and last_active permanently out of
        # step (the count static while the timestamp keeps getting newer). Only a genuine new
        # event write treats the memory as activated again, which bump_active=True triggers
        # explicitly (hold/grow merging into a neighbouring bucket, say), refreshing
        # last_active and incrementing activation_count with the same meaning as touch().
        if bump_active:
            post["last_active"] = now_iso()
            post["activation_count"] = int(post.get("activation_count") or 0) + 1

        final_type = str(post.get("type") or current_type).strip().lower()
        target_path = file_path
        if final_type in _EDITABLE_BUCKET_TYPES:
            target_path = self._bucket_target_path(
                file_path,
                final_type,
                post.get("domain") or [_DEFAULT_DOMAIN_NAME],
                str(post.get("status") or "active"),
            )

        try:
            committed_path = self._commit_bucket_update(
                file_path,
                target_path,
                frontmatter.dumps(post),
                bucket_id=bucket_id,
            )
        except (OSError, ValueError) as e:
            logger.error(
                "Failed to commit bucket update / 提交桶更新失败: "
                "%s -> %s: %s",
                file_path,
                target_path,
                e,
            )
            return False

        if bump_active:
            self._cache_bump(
                bucket_id,
                last_active=post["last_active"],
                activation_count=post["activation_count"],
                file_path=committed_path,
            )

        logger.info(f"Updated bucket / 更新记忆桶: {bucket_id}")

        # Content is already committed. Queue the derived vector without
        # turning provider failure into a false "memory write failed" result.
        if "content" in kwargs:
            await self._index_after_write(bucket_id, post.content or "")
        # meaning has an embedding of its own, so a change to content and a change to
        # meaning each trigger their own regeneration.
        if "meaning" in kwargs or "meaning_append" in kwargs:
            await self._sync_meaning_embedding(bucket_id, post.get("meaning") or [])
        self._invalidate_bm25()
        self._record_v3_bucket_event(
            "update",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        self._record_ledger_event(
            "TraceUpdated",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
            {"changed_fields": sorted(str(k) for k in kwargs.keys())},
        )

        return True

    async def hard_delete_test_bucket(self, bucket_id: str, *, reason: str = "") -> dict:
        """Erase only a bucket born as test data, with an explicit audit reason."""
        async with self._bucket_turn(bucket_id):
            return await self._hard_delete_test_bucket_locked(bucket_id, reason=reason)

    async def _hard_delete_test_bucket_locked(
        self,
        bucket_id: str,
        *,
        reason: str = "",
    ) -> dict:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return {"ok": False, "error": "not_found"}
        try:
            post = frontmatter.load(file_path)
        except Exception as exc:
            return {"ok": False, "error": f"read_failed: {exc}"}
        provenance = post.get("provenance")
        if not (isinstance(provenance, dict)
                and provenance.get("kind") == "test"
                and provenance.get("erasable") is True):
            return {"ok": False, "error": "not_erasable_test_data"}
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            return {"ok": False, "error": "missing_delete_reason"}
        if len(normalized_reason) > 500:
            return {"ok": False, "error": "delete_reason_too_long"}
        bucket_type = str(post.get("type") or "dynamic")
        try:
            os.remove(file_path)
        except OSError as exc:
            return {"ok": False, "error": f"delete_failed: {exc}"}
        self._path_index_removed(bucket_id, file_path)
        if self.embedding_outbox is not None:
            try:
                self.embedding_outbox.discard(bucket_id)
            except Exception:
                pass
        if self.embedding_engine is not None:
            try:
                self.embedding_engine.delete_embedding(bucket_id)
            except Exception as exc:
                logger.warning("hard delete embedding cleanup failed for %s: %s", bucket_id, exc)
        self._invalidate_bm25()
        self._record_ledger_event(
            "TraceHardDeleted", bucket_id, bucket_type, "",
            {"provenance": {"kind": "test", "erasable": True}},
            {"reason": normalized_reason, "content_erased": True},
        )
        logger.warning("Physically erased test bucket: %s", bucket_id)
        return {"ok": True, "deleted": bucket_id}

    # ---------------------------------------------------------
    # Wikilink injection — DISABLED
    # Now handled by LLM prompts (the model adds [[]] around proper nouns)
    # ---------------------------------------------------------
    # def _apply_wikilinks(self, content, tags, domain, name): ...
    # def _collect_wikilink_keywords(self, content, tags, domain, name): ...
    # def _normalize_keywords(self, keywords): ...
    # def _extract_auto_keywords(self, content): ...

    # ---------------------------------------------------------
    # Delete bucket
    # ---------------------------------------------------------
    async def delete(self, bucket_id: str) -> bool:
        """
        Soft-delete a memory bucket: move to archive/ and stamp `deleted_at`.
        F-10: a memory does not disappear, it fades. Nothing is physically deleted; the file
        moves into archive/ and a deleted_at timestamp is written into its frontmatter. The
        embedding is still cleaned up, to save space.
        """
        async with self._bucket_turn(bucket_id):
            deleted = await self._delete_locked(bucket_id)
        if deleted:
            note_written(bucket_id)
        return deleted

    async def restore_archived(self, bucket_id: str) -> dict:
        """Restore an archived/tombstoned Markdown bucket to its original channel.

        Discovery never calls this method.  It is deliberately exposed only
        through an explicit ``trace(..., restore=True)`` decision.
        """
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return {"ok": False, "error": "not_found"}
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                return {"ok": False, "error": f"read_failed: {exc}"}

            normalized_path = os.path.normcase(os.path.abspath(file_path))
            normalized_archive = os.path.normcase(os.path.abspath(self.archive_dir))
            try:
                stored_in_archive = (
                    os.path.commonpath((normalized_path, normalized_archive))
                    == normalized_archive
                )
            except ValueError:
                stored_in_archive = False
            archived_state = (
                stored_in_archive
                or str(post.get("type") or "").strip().lower() == "archived"
                or bool(post.get("deleted_at"))
                or parse_bool(post.get("tombstone"), default=False)
            )
            if not archived_state:
                # A sunk bucket (decay_stage=sunk) keeps its shell in the main store with
                # only the summary as its body, and restore=True is its one and only door
                # back — this stage is reversible by design.
                if str(post.get("decay_stage") or "") == "sunk":
                    return await self._unsink_locked(bucket_id, file_path, post)
                return {"ok": False, "error": "not_archived"}

            original_kind = self.footprint_snapshot().original_kind(
                bucket_id, dict(post.metadata)
            )
            if original_kind not in _EDITABLE_BUCKET_TYPES:
                original_kind = "dynamic"
            if parse_bool(post.get("pinned"), default=False) or parse_bool(
                post.get("protected"), default=False
            ):
                original_kind = "permanent"

            post["type"] = original_kind
            for field in (
                "deleted_at", "tombstone", "tombstoned_at", "erasure_mode"
            ):
                post.metadata.pop(field, None)
            try:
                target_path = self._bucket_target_path(
                    file_path,
                    original_kind,
                    post.get("domain") or [_DEFAULT_DOMAIN_NAME],
                    str(post.get("status") or "active"),
                )
                committed_path = self._commit_bucket_update(
                    file_path, target_path, frontmatter.dumps(post),
                    bucket_id=bucket_id,
                )
            except (OSError, ValueError) as exc:
                logger.error("Failed to restore archived bucket %s: %s", bucket_id, exc)
                return {"ok": False, "error": f"restore_failed: {exc}"}

            self._invalidate_bm25()
            await self._index_after_write(bucket_id, post.content or "")
            await self._sync_meaning_embedding(bucket_id, post.get("meaning") or [])
            self._record_v3_bucket_event(
                "restore", bucket_id, original_kind, post.content or "", dict(post.metadata)
            )
            self._record_ledger_event(
                "TraceRestored",
                bucket_id,
                original_kind,
                post.content or "",
                dict(post.metadata),
            )
            logger.info("Restored archived bucket: %s -> %s", bucket_id, committed_path)
            return {"ok": True, "restored": bucket_id, "type": original_kind}

    # ---------------------------------------------------------
    # The reverse chain: whose sources include this memory.
    # The forward chain is `prov` (its sources read through utils.read_from_ids); the
    # reverse one is computed on the spot by scanning the parse cache — the store holds a
    # few hundred entries, so with a warm cache it is one in-memory pass, and no separate
    # index is built (building one would be a second source of truth).
    # ---------------------------------------------------------
    async def referenced_by(self, bucket_id: str) -> list[str]:
        """Who stands on this memory: whose sources (utils.read_from_ids) include it.
        This is what "cited by" in a direct id lookup and regrow's overturn walk use."""
        out: list[str] = []
        for b in await self.list_all(include_archive=False):
            meta = b.get("metadata", {}) or {}
            if bucket_id in read_from_ids(meta):
                out.append(str(meta.get("id") or b.get("id") or ""))
        return out

    async def mind_from_ids(self) -> set[str]:
        """The set of ids among the sources of any insight-type bucket (the MIND branch, or
        the older type=feel/i).

        This is the "not digested" test: once an insight has been distilled out of
        something, that thing is digested; only what refuses to distil keeps turning over in
        dreams.
        """
        out: set[str] = set()
        for b in await self.list_all(include_archive=False):
            meta = b.get("metadata", {}) or {}
            # Never write `"/MIND/" in room`: the new room name MIND/TRAITS has no leading slash
            if (not is_mind_room(meta.get("room"))
                    and str(meta.get("type") or "") not in ("feel", "i")):
                continue
            out.update(read_from_ids(meta))
        return out

    # ---------------------------------------------------------
    # Sinking and restoring: forgetting is not "the whole entry disappears", it is
    # "resolution drops"
    # ---------------------------------------------------------
    async def sink_bucket(self, bucket_id: str) -> bool:
        """Sink a bucket: the original text moves to archive/原文/{id}.txt and the body in
        the main store is replaced by its summary.

        🔴 The vector is left exactly as it is and never recomputed — the semantic
        fingerprint is still the one computed from the original text.
        **This is what a person is actually like: remembering what something felt like while
        being unable to reproduce the words.**
        BM25 reads the original back out of the txt (_bm25_source), so
        "matching against the original text" is not lost.
        Reversible: trace(restore=True) goes through _unsink_locked and fetches it back.
        """
        async with self._bucket_turn(bucket_id):
            file_path = self._find_bucket_file(bucket_id)
            if not file_path:
                return False
            try:
                post = frontmatter.load(file_path)
            except Exception as exc:
                logger.warning(f"sink_bucket 读取失败 {bucket_id}: {exc}")
                return False
            if (str(post.get("type") or "") == "archived" or post.get("deleted_at")
                    or parse_bool(post.get("tombstone"), default=False)):
                return False
            if str(post.get("decay_stage") or "") == "sunk":
                return True  # idempotent: it has already sunk
            summary = str(post.get("summary") or "").strip()
            orig = post.content or ""
            if not summary or not orig.strip():
                # Without a summary it cannot sink, since sinking IS "only the summary is
                # left". Wait for the backfill to supply one and sink on the next round.
                return False
            txt = self._sunk_orig_path(bucket_id)
            try:
                os.makedirs(os.path.dirname(txt), exist_ok=True)
                with open(txt, "w", encoding="utf-8", newline="") as f:
                    f.write(orig)
            except OSError as exc:
                logger.warning(f"sink_bucket 原文落盘失败 {bucket_id}: {exc}")
                return False
            post.content = summary
            post["decay_stage"] = "sunk"
            try:
                self._commit_bucket_update(
                    file_path, file_path, frontmatter.dumps(post), bucket_id=bucket_id,
                )
            except (OSError, ValueError) as exc:
                logger.error(f"sink_bucket 提交失败 {bucket_id}: {exc}")
                return False
            # ⚠️ _index_after_write is deliberately NOT called: leaving the vector alone is
            #    the design, not an oversight
            self._invalidate_bm25()
            self._record_ledger_event(
                "MemorySunk", bucket_id, str(post.get("type") or "dynamic"),
                post.content or "", dict(post.metadata), {"orig_path": txt},
            )
            logger.info(f"Memory sunk / 沉底: {bucket_id}（原文 → {txt}）")
            return True

    async def _unsink_locked(self, bucket_id: str, file_path: str, post) -> dict:
        """Restore a sunk bucket (called by restore_archived while it holds the lock).

        The original is read back out of the txt into the body and decay_stage is cleared.
        Restoring means it was recalled, so last_active is refreshed — otherwise the next
        decay cycle would immediately sink it again. The vector was computed from the
        original text in the first place and needs no recomputation.
        """
        txt = self._sunk_orig_path(bucket_id)
        try:
            with open(txt, encoding="utf-8") as f:
                orig = f.read()
        except OSError:
            return {"ok": False, "error": f"orig_missing: {txt}（沉底原文找不到了）"}
        post.content = orig
        post.metadata.pop("decay_stage", None)
        post["last_active"] = now_iso()
        post["activation_count"] = int(post.get("activation_count") or 0) + 1
        try:
            self._commit_bucket_update(
                file_path, file_path, frontmatter.dumps(post), bucket_id=bucket_id,
            )
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"restore_failed: {exc}"}
        self._invalidate_bm25()
        self._record_ledger_event(
            "MemoryUnsunk", bucket_id, str(post.get("type") or "dynamic"),
            post.content or "", dict(post.metadata),
        )
        logger.info(f"Memory unsunk / 沉底还原: {bucket_id}")
        return {"ok": True, "restored": bucket_id, "type": str(post.get("type") or "dynamic"),
                "unsunk": True}

    async def _delete_locked(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        # --- Read the file, write deleted_at, move it into archive/ ---
        try:
            post = frontmatter.load(file_path)
            tombstone_at = now_iso()
            post["deleted_at"] = tombstone_at
            post["tombstone"] = True
            post["tombstoned_at"] = tombstone_at
            post["erasure_mode"] = "tombstone_only"
            os.makedirs(self.archive_dir, exist_ok=True)
            dest = os.path.join(self.archive_dir, os.path.basename(file_path))
            # If archive/ already holds a file of the same name (very rare), append the
            # bucket_id as a suffix so nothing is overwritten
            if os.path.exists(dest) and dest != file_path:
                dest = os.path.join(
                    self.archive_dir,
                    f"{os.path.splitext(os.path.basename(file_path))[0]}_{bucket_id}.md",
                )
            self._commit_bucket_update(
                file_path,
                str(dest),
                frontmatter.dumps(post),
                bucket_id=bucket_id,
            )
        except OSError as e:
            logger.error(f"Failed to soft-delete bucket / 软删除桶文件失败: {file_path}: {e}")
            return False

        # The embedding is still cleaned up, so orphaned vectors do not take up space
        if self.embedding_outbox is not None:
            try:
                self.embedding_outbox.discard(bucket_id)
            except Exception as e:
                logger.warning(f"discard embedding outbox failed for {bucket_id}: {e}")
        if self.embedding_engine is not None:
            try:
                self.embedding_engine.delete_embedding(bucket_id)
            except Exception as e:
                logger.warning(f"delete embedding failed for {bucket_id}: {e}")

        self._invalidate_bm25()
        logger.info(f"Soft-deleted bucket (moved to archive) / 软删除记忆桶: {bucket_id}")
        self._record_v3_bucket_event(
            "delete",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        self._record_ledger_event(
            "TraceDeletedToArchive",
            bucket_id,
            str(post.get("type") or "dynamic"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # touch: set the "last recalled" timestamp to now
    # ---------------------------------------------------------
    async def touch(self, bucket_id: str, ripple: bool = False) -> None:
        """Set this memory's `last_active` to now. **The forgetting engine reads this one
        field and nothing else.**

        🔴 **What counts as "being recalled" — the facts first, the rule below**:
           There are exactly three places that call this, and all three are "**I actually
           did something with this memory**":
             · wrote an insight that cites it (`grow`'s from)
             · changed a version, with it as the source (`regrow`)
             · folded it into a summarising sentence (`fold`)
           **`recall` finding it -> no call.** Scrolling past it while browsing -> no call.

        📌 The rule, which overturned an earlier proposal that a search hit should extend
           its life:
           "things recalled repeatedly are remembered firmly — **but that case is the
           minority**", together with **"a faded entry still has its summary; it is not
           forgotten outright"**.
           The second half is the key: **fading is not deletion.** It means "it stops
           surfacing on its own; go looking and it is still there, merely discounted."
           So the question "should this get its life extended" carries far less weight than
           it sounds like it does — not enough to justify importing the whole "look something
           up often and it lives longer" model, which is a practice curve, not memory.

        ⚠️ The old English comment here said `Called on every recall hit` (inherited from
           upstream). **That sentence had been false for a long time**, and was corrected
           along with this.

        ⚰️ The `ripple` parameter is kept only so the call sites do not have to change;
           **it does nothing whatever you pass** (temporal ripple was deleted — the
           reasoning is on the memorial below).
        """
        async with self._bucket_turn(bucket_id):
            await self._touch_locked(bucket_id)

    async def _touch_locked(self, bucket_id: str) -> datetime | None:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return None

        try:
            post = frontmatter.load(file_path)
            post["last_active"] = now_iso()
            post["activation_count"] = int(post.get("activation_count") or 0) + 1  # type: ignore[call-overload]

            _atomic_write_text(file_path, frontmatter.dumps(post))
            self._cache_bump(
                bucket_id,
                last_active=post["last_active"],
                activation_count=post["activation_count"],
                file_path=file_path,
            )

            current_time = parse_iso_datetime(
                post.get("created", post.get("last_active", ""))
            )
            self._record_ledger_event(
                "TraceTouched",
                bucket_id,
                str(post.get("type") or "dynamic"),
                post.content or "",
                dict(post.metadata),
            )
            return current_time
        except Exception as e:
            logger.warning(f"Failed to touch bucket / 触碰桶失败: {bucket_id}: {e}")
            return None

    async def touch_many(self, bucket_ids: list, ripple: bool = False) -> None:
        """touch in bulk. One failure does not affect the others.

        ⚰️ `ripple` does nothing whatever you pass (temporal ripple was deleted); it is kept
        only so the call sites do not have to change.
        """
        for bid in bucket_ids:
            try:
                await self.touch(bid)
            except Exception as e:
                logger.warning(f"touch_many: 触碰 {bid} 失败: {e}")

    # ⚰️ **Temporal ripple was deleted.**
    #    What it did: every time a bucket was touched, it added 0.3 to the
    #    `activation_count` of the handful of buckets **adjacent to it in time** — "recall
    #    one thing and you incidentally wake the things around it".
    #
    #    🔴 Why it went: **it was dead, and it was not cheap.**
    #    · Dead: nothing had read `activation_count` since the count factor was cut out of
    #      the forgetting formula, and the rule became "things recalled often do not sink,
    #      and last_active handles that". From that day the number only went up and was
    #      never read, so the 0.3 the ripple added was **visible nowhere at all**.
    #    · Not cheap: every ripple meant a `list_all()` pass over the whole store (hundreds
    #      of files), in order to change a number nobody read.
    #
    #    📌 The deeper reason for deleting it — deeper than "it was dead" — is worth keeping:
    #      "things recalled repeatedly are remembered firmly — **but that case is the
    #      minority**".
    #      That is the practice-makes-permanent model (Ebbinghaus), and it is **not the
    #      memory we are after**. What we are after is "what mattered, what moved something,
    #      lasts", not "what got looked up most lasts".
    #      The ripple was the last remaining leg of that other model, and the rest of it had
    #      already walked out.
    #
    #    ⚠️ The `activation_count` field itself **stays for now**: it costs nothing sitting
    #       in the frontmatter, and deleting it would mean touching thousands of files.
    #       **But nothing reads it any more** — anyone who sees it and thinks of building a
    #       rule on it should come back and read this first.

    async def search(
        self,
        query: str,
        limit: Optional[int] = None,
        domain_filter: Optional[list[str]] = None,
        query_valence: Optional[float] = None,
        query_arousal: Optional[float] = None,
        vector_scores: Optional[dict[str, float]] = None,
        include_archive: bool = False,
    ) -> list[dict]:
        """
        Multi-dimensional indexed search for memory buckets.

        domain_filter: pre-filter by domain (None = search all)
        query_valence/arousal: emotion coordinates for resonance scoring
        """
        if not query or not query.strip():
            return []

        limit = limit or self.max_results
        # Literal recall: keep the query as it stands (lowercased, trimmed) for substring
        # matching, so a word that was explicitly searched for is always recalled
        q_norm = query.strip().lower()
        # Read before the list: BM25 is synced to the list, and a write that lands after
        # this read has to leave the index dirty (see _sync_bm25).
        generation = self._active_cache_generation
        all_buckets = await self.list_all(include_archive=include_archive)

        if not all_buckets:
            return []

        # --- Layer 0: the direct bucket-id channel (pure lookup, short-circuits) ---
        # A bucket id is random hex and carries **no meaning**, so it has no business in the
        # vector, BM25 or fuzzy channels — putting it there only pollutes the semantic
        # space. This does an exact full-id match on its own: if the query string equals some
        # visible bucket's full id, return that bucket at full marks and bypass semantic
        # ordering. This is the "I know exactly which one I want" lookup.
        # Only a full id counts (no prefix matching), so an ordinary keyword cannot trip it;
        # soft-deleted and archived buckets are not in all_buckets, so a deleted bucket
        # cannot be found by id either, which matches get()'s visibility.
        q_exact = query.strip()
        if q_exact:
            for b in all_buckets:
                if str(b.get("id")) == q_exact:
                    hit = dict(b)
                    hit["score"] = 1.0
                    return [hit]

        # --- Layer 1: domain pre-filter (fast scope reduction) ---
        if domain_filter:
            filter_set = {d.lower() for d in domain_filter}
            candidates = [
                b for b in all_buckets
                if {d.lower() for d in b["metadata"].get("domain", [])} & filter_set
            ]
            # Fall back to full search if pre-filter yields nothing
            if not candidates:
                candidates = all_buckets
        else:
            candidates = all_buckets

        # --- Layer 1.5: the embedding semantic score. It is a scoring dimension only and no
        #     longer narrows the candidate set. ---
        # This used to replace the candidate set with "the buckets present in
        # embeddings.db", which meant:
        #   - any bucket lacking an embedding (the embed key failed at write time, or an old
        #     script bulk-imported without backfilling vectors) was filtered out wholesale as
        #     soon as the query matched any vector at all -> breath's retrieval counts stopped
        #     agreeing with pulse.
        # The fix: keep vector_scores for Layer 2's semantic dimension, but leave `candidates`
        # alone. A bucket with no embedding scores semantic_score=0 and can still be hit on
        # topic/emotion/time/importance.
        # ``None`` means this caller wants BucketManager to query the engine.
        # An explicit dict (including {}) lets an orchestration layer perform
        # the query once and reuse the same scores for ranking and recall.
        vector_scores_provided = vector_scores is not None
        if vector_scores is None:
            vector_scores = {}
        else:
            vector_scores = dict(vector_scores)
        if (
            not vector_scores_provided
            and self.embedding_engine
            and self.embedding_engine.enabled
        ):
            try:
                vector_results = await self.embedding_engine.search_similar(query, top_k=_VECTOR_TOPK)
                if vector_results:
                    vector_scores = {bid: score for bid, score in vector_results}
            except Exception as e:
                logger.warning(f"Embedding score failed, using fuzzy only / embedding 评分失败: {e}")

        # --- BM25 scoring. What was just written has to be literally findable on the next
        #     search, while its vector may still be queued. ---
        # Built and dirty -> synced right here before scoring: only the entries that changed
        # are tokenised again (tens of milliseconds), and an index built from another
        # process's writes catches up the same way once list_all has seen them.
        # Never built -> the first build (the whole store through jieba) goes to a background
        # thread, and this query scores without BM25; vectors and whole-query literal hits
        # still carry it. The decay cycle builds it a few seconds after start.
        # The archive is never indexed: a search that includes it scores BM25 as it stands.
        bm25_scores: dict[str, float] = {}
        if self._bm25 is not None:
            if not self._bm25.built:
                if not self._bm25_rebuilding and not include_archive:
                    self._bm25_rebuilding = True
                    asyncio.create_task(self._rebuild_bm25_async(all_buckets, generation))
            elif self._bm25_dirty and not include_archive:
                try:
                    await self._sync_bm25(all_buckets, generation)
                except Exception as e:
                    logger.warning(f"[bm25] 同步失败，本次用旧索引: {e}")
            try:
                bm25_scores = self._bm25.score(query)
            except Exception as e:
                logger.warning(f"[bm25] score 失败，本次跳过 BM25 维度: {e}")
                bm25_scores = {}

        # --- Layer 2: two-dimension scoring (semantic 2.5 + bm25 1.5); a literal hit only
        #     sets a flag ---
        # weight_sum is constantly w_semantic + w_bm25: a bucket with no vector scores
        # semantic=0 (a sunk bucket's vector was computed from the original text and is left
        # untouched, so it can still be hit on its semantic fingerprint even with the body
        # gone — that is the design).
        # The score runs 0~100; the relevance line lives over in recall, one number for
        # everything.
        scored = []
        weight_sum = self.w_semantic + self.w_bm25
        for bucket in candidates:
            meta = bucket.get("metadata", {})

            try:
                # A literal hit: the query string appears as-is in name, tags or body.
                # (domain is a folder name the model invented and was taken out of literal
                # matching; aliases only feed bm25 and take no part in it either — the floor
                # recognises only characters that genuinely appear in this memory.)
                literal_hit = False
                if q_norm:
                    hay = " ".join([
                        str(meta.get("name", "")),
                        " ".join(str(t) for t in (meta.get("tags") or [])),
                        bucket.get("content", "") or "",
                    ]).lower()
                    literal_hit = q_norm in hay

                semantic_score = vector_scores.get(bucket["id"], 0.0) or 0.0
                bm25_score = bm25_scores.get(bucket["id"], 0.0) if bm25_scores else 0.0
                total = semantic_score * self.w_semantic + bm25_score * self.w_bm25
                normalized = (total / weight_sum) * 100 if weight_sum > 0 else 0.0

                # The ticket in: any dimension has signal, or there was a literal hit (a word
                # searched for explicitly has to be reachable).
                # The above-the-line / below-the-line decision is not made here — recall does
                # its own filtering with the relevance line, and floors a literal hit up to
                # that line (max(score, line), never a bonus).
                if normalized > 0 or literal_hit:
                    # Resolved buckets get ranking penalty (but still reachable)
                    if is_closed(meta):  # only `status` marks an ending; the old booleans stay read-only for compatibility
                        normalized *= _RESOLVED_RANK_PENALTY
                    # The three forgetting stages: faded takes a discount, sunk a harsher one.
                    # It happens quietly and is not flagged in the output — a literal hit is
                    # still floored up to the line by the recall layer.
                    _stage = str(meta.get("decay_stage") or "")
                    if _stage == "faded":
                        normalized *= _FADED_SEARCH_DISCOUNT
                    elif _stage == "sunk":
                        normalized *= _SUNK_SEARCH_DISCOUNT
                    bucket["score"] = round(normalized, 2)
                    # How it matched, for the reader to see after the score: the whole query
                    # as written (literal_hit), some of its words (bm25_hit), its meaning
                    # (vector_match: cosine at or above the vector line). Any of them can
                    # hold at once.
                    bucket["literal_hit"] = literal_hit
                    bucket["bm25_hit"] = bm25_score > 0
                    bucket["vector_match"] = semantic_score >= _VECTOR_RECALL_THRESHOLD
                    scored.append(bucket)
            except Exception as e:
                logger.warning(
                    f"Scoring failed for bucket {bucket.get('id', '?')} / "
                    f"桶评分失败: {e}"
                )
                continue

        # In the ordering, a literal hit is lifted at least to the median of the current
        # pool, so that "the word I searched for is right there, yet it sits behind a pile of
        # vaguely related things" cannot happen. The real max(score, line) is done in the
        # recall layer, since the line arrives with each request and is out of reach here.
        scored.sort(key=lambda x: (x["score"], bool(x.get("literal_hit"))), reverse=True)
        return scored[:limit]

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

    # ---------------------------------------------------------
    # List all buckets
    # ---------------------------------------------------------
    async def list_all(self, include_archive: bool = False) -> list[dict]:
        """
        Recursively walk directories (including domain subdirs), list all buckets.
        """
        if include_archive:
            buckets = []
            dirs = list(self._active_dirs) + [self.archive_dir]
            for _root, _fname, file_path in self._iter_md_files(dirs):
                bucket = self._load_bucket(file_path)
                if bucket:
                    buckets.append(bucket)
            return buckets

        # Active buckets use a parsed cache, but Obsidian/Git/manual edits may
        # bypass BucketManager.  The build mutex is cross-loop; the short state
        # guard and generation form a CAS with every managed write.  File state
        # is scanned both before and after parsing so an external editor cannot
        # make us publish old content paired with a new fingerprint.
        async with self._active_cache_lock:
            previous_cache: list[dict] | None = None
            while True:
                now = time.monotonic()
                with self._active_cache_state_guard:
                    generation = self._active_cache_generation
                    cached = self._active_cache
                    if cached is not None:
                        poll_due = (
                            self.external_change_poll_seconds == 0
                            or now - self._last_file_state_check
                            >= self.external_change_poll_seconds
                        )
                        if not poll_due:
                            return [dict(bucket) for bucket in cached]
                        cached_state = dict(self._active_file_state)
                    else:
                        poll_due = False
                        cached_state = {}

                if cached is not None and poll_due:
                    current_state = self._scan_active_file_state()
                    with self._active_cache_state_guard:
                        if generation != self._active_cache_generation:
                            previous_cache = None
                            continue
                        # 🔴 What is recorded is **the moment the scan finished**, not the
                        #    `now` from before it started.
                        #    Scanning 1000 files on a Windows bind mount takes 1~2.6 seconds
                        #    while the interval is 1 second — so with the pre-scan timestamp,
                        #    every single call necessarily decides "time to rescan" and
                        #    **the cache never hits**.
                        #    Measured: one recall calls list_all four times internally, all
                        #    four rescanned, and 10.4 of its 10.5 seconds went here.
                        #    "Check at most once per second" is supposed to be measured on the
                        #    wall clock; how long the check itself takes must not turn the
                        #    check into something that happens every time.
                        self._last_file_state_check = time.monotonic()
                        if current_state == cached_state:
                            current_cache = self._active_cache
                            if current_cache is not None:
                                return [dict(bucket) for bucket in current_cache]
                            continue

                        previous_cache = [dict(bucket) for bucket in cached]
                        self._active_cache_generation += 1
                        generation = self._active_cache_generation
                        self._active_cache = None
                        self._active_file_state = {}
                        self._bm25_dirty = True
                        self._external_changes_detected += 1
                        self._last_external_change = now_iso()
                        with self._bucket_path_index_guard:
                            self._bucket_path_index_ready = False
                            self._bucket_path_index = {}

                with self._active_cache_state_guard:
                    generation = self._active_cache_generation

                state_before = self._scan_active_file_state()
                buckets = []
                for _root, _fname, file_path in self._iter_md_files(
                    self._active_dirs
                ):
                    bucket = self._load_bucket(file_path)
                    if bucket:
                        buckets.append(bucket)
                state_after = self._scan_active_file_state()

                if state_before != state_after:
                    await asyncio.sleep(0)
                    continue

                with self._active_cache_state_guard:
                    if generation != self._active_cache_generation:
                        previous_cache = None
                        continue
                    self._active_cache = [dict(bucket) for bucket in buckets]
                    self._active_file_state = state_after
                    self._last_file_state_check = time.monotonic()

                if previous_cache is not None:
                    self._reconcile_external_changes(previous_cache, buckets)
                return buckets

    # ---------------------------------------------------------
    # Statistics (counts per category + total size)
    # ---------------------------------------------------------
    async def get_stats(self) -> dict:
        """
        Return memory bucket statistics (including domain subdirs).
        """
        stats: dict[str, Any] = {
            "permanent_count": 0,
            "dynamic_count": 0,
            "archive_count": 0,
            "feel_count": 0,
            "letter_count": 0,
            "total_size_kb": 0.0,
            "domains": {},
        }

        for subdir, key in [
            (self.permanent_dir, "permanent_count"),
            (self.dynamic_dir, "dynamic_count"),
            (self.archive_dir, "archive_count"),
            (self.feel_dir, "feel_count"),
            (self.letter_dir, "letter_count"),
        ]:
            if not os.path.exists(subdir):
                continue
            for root, _, files in os.walk(subdir):
                for f in files:
                    if f.endswith(".md"):
                        stats[key] += 1
                        fpath = os.path.join(root, f)
                        try:
                            stats["total_size_kb"] += os.path.getsize(fpath) / 1024
                        except OSError:
                            pass
                        # Per-domain counts
                        domain_name = os.path.basename(root)
                        if domain_name != os.path.basename(subdir):
                            stats["domains"][domain_name] = stats["domains"].get(domain_name, 0) + 1

        return stats

    # ---------------------------------------------------------
    # Archive bucket (move from permanent/dynamic into archive)
    # Called by decay engine to simulate "forgetting"
    # ---------------------------------------------------------
    async def archive(self, bucket_id: str) -> bool:
        """
        Move a bucket into the archive directory (preserving domain subdirs).
        """
        async with self._bucket_turn(bucket_id):
            return await self._archive_locked(bucket_id)

    async def _archive_locked(self, bucket_id: str) -> bool:
        file_path = self._find_bucket_file(bucket_id)
        if not file_path:
            return False

        try:
            # Read once, get domain info and update type
            post = frontmatter.load(file_path)
            domain: list[str] = post.get("domain") or [_DEFAULT_DOMAIN_NAME]  # type: ignore[assignment]
            primary_domain = self._primary_domain(domain)
            archive_subdir = os.path.join(self.archive_dir, primary_domain)
            os.makedirs(archive_subdir, exist_ok=True)

            dest = safe_path(archive_subdir, os.path.basename(file_path))
            # Collision guard: when archive/ already holds a file of the same name, append
            # the bucket_id as a suffix, so an earlier archived memory cannot be quietly
            # overwritten (matching delete()'s soft-delete protection).
            if os.path.exists(dest) and os.path.abspath(dest) != os.path.abspath(file_path):
                stem = os.path.splitext(os.path.basename(file_path))[0]
                dest = safe_path(archive_subdir, f"{stem}_{bucket_id}.md")

            # Commit the archived metadata at the destination before removing
            # the untouched source.  A failed move must not leave an
            # ``type=archived`` file stranded in an active directory.
            post["type"] = "archived"
            self._commit_bucket_update(
                file_path,
                str(dest),
                frontmatter.dumps(post),
                bucket_id=bucket_id,
            )
        except Exception as e:
            logger.error(
                f"Failed to archive bucket / 归档桶失败: {bucket_id}: {e}"
            )
            return False

        self._invalidate_bm25()
        logger.info(f"Archived bucket / 归档记忆桶: {bucket_id} → archive/{primary_domain}/")
        self._record_v3_bucket_event(
            "archive",
            bucket_id,
            str(post.get("type") or "archived"),
            post.content or "",
            dict(post.metadata),
        )
        self._record_ledger_event(
            "TraceArchived",
            bucket_id,
            str(post.get("type") or "archived"),
            post.content or "",
            dict(post.metadata),
        )
        return True

    # ---------------------------------------------------------
    # Collect all tags currently in the vault (excluding archive), for the first_of_kind check
    # Returns set[str]; an empty vault gives an empty set; on an exception it returns None,
    # telling the caller to give up
    # ---------------------------------------------------------
    def _collect_all_tags(self) -> Optional[set]:
        tags = set()
        # archive_dir is excluded — archived buckets are "the past", they
        # shouldn't block a tag from being marked first_of_kind today.
        for _root, _fname, full_path in self._iter_md_files(self._active_dirs):
            try:
                post = frontmatter.load(full_path)
                for t in list(post.get("tags") or []):  # type: ignore[call-overload]
                    if t:
                        tags.add(str(t))
            except Exception:
                # One bucket failing to parse does not affect the whole; first_of_kind is a soft feature
                continue
        return tags

    # ---------------------------------------------------------
    # Internal: find bucket file across all three directories
    # ---------------------------------------------------------
    def _ensure_bucket_path_index(self) -> None:
        """Build the complete ID → path index once for bulk conflict checks."""

        with self._bucket_path_index_guard:
            if self._bucket_path_index_ready:
                return
            # archive is included: a soft-deleted bucket still has to be findable by internal path lookup.
            dirs = [
                self.permanent_dir,
                self.dynamic_dir,
                self.archive_dir,
                self.feel_dir,
                self.letter_dir,
            ]
            index: dict[str, str] = {}
            for _root, fname, full_path in self._iter_md_files(dirs):
                stem = fname[:-3]
                index.setdefault(stem, full_path)
                # The root cause once tracked down: a managed filename looks like
                # `<name>_<id>.md`, and only the whole stem used to be indexed as a key — so
                # when the frontmatter failed to parse (stored_id="") the bare id could not be
                # found, the index was ready but missing that entry, the bucket became an
                # orphan, and it fell through to the filename-scan fallback in
                # `_find_bucket_file()` (which also logs a loud warning).
                # Same rule as `_scan_bucket_file_by_name()`'s `stem.endswith(f"_{bucket_id}")`
                # test: parse the `_<id>` suffix out and index that as a key too, so nothing
                # depends on whether the frontmatter can be read.
                if "_" in stem:
                    index.setdefault(stem.rsplit("_", 1)[-1], full_path)
                try:
                    post = frontmatter.load(full_path)
                    stored_id = str(post.get("id") or "")
                except Exception:
                    stored_id = ""
                if stored_id:
                    index.setdefault(stored_id, full_path)
            self._bucket_path_index = index
            self._bucket_path_index_ready = True

    def _scan_bucket_file_by_name(self, bucket_id: str) -> Optional[str]:
        """Scan once by **filename** for this id (a managed filename is either `<id>.md` or
        `<name>_<id>.md`).

        It uses `os.walk` alone and **parses not one frontmatter** — that is the crucial
        difference from `_ensure_bucket_path_index()`, which has to read the YAML of the
        entire store and takes seconds on a Windows bind mount. A hit is backfilled into the
        index on the way out.
        """
        dirs = [
            self.permanent_dir,
            self.dynamic_dir,
            self.archive_dir,
            self.feel_dir,
            self.letter_dir,
        ]
        for _root, fname, full_path in self._iter_md_files(dirs):
            stem = fname[:-3]
            if stem == bucket_id or stem.endswith(f"_{bucket_id}"):
                with self._bucket_path_index_guard:
                    self._bucket_path_index[bucket_id] = full_path
                return full_path
        return None

    def _find_bucket_file(
        self, bucket_id: str, *, scan_if_missing: bool = True
    ) -> Optional[str]:
        """
        Recursively search permanent/dynamic/archive for a bucket file
        matching the given ID.

        ------------------------------------------------------------
        🔴 The path-index fallback
        ------------------------------------------------------------
        **When the index is ready but the lookup misses, it may not simply answer "no".**
        Fall back to one scan by filename, and on a hit put the entry back into the index
        before returning.

        Why: during acceptance testing this was walked into three times in a row — "index
        ready, entry missing -> `update()` silently returns False -> the write is lost
        without a word" (just after the container started, or while the store was being
        scanned concurrently). The likely root cause is that while building the table
        `_ensure_bucket_path_index()` **skips any file that reads empty or fails to parse**
        (the `stored_id=""` branch), while the other key it `setdefault`s is the **filename
        stem** (`<name>_<id>`) rather than the bare id — so that bucket effectively does not
        exist in the index, yet `_bucket_path_index_ready` is True, and "not found" got
        treated as "does not exist".
        ⚠️ **Only the fallback was added**; not one line of the index construction was
        rewritten.

        `scan_if_missing=False` exists for `create()`'s collision check: that caller
        **expects to find nothing** (the id was just generated), and scanning the whole store
        every time would add a walk to every single grow.
        That path has two other layers of protection: `os.path.exists(candidate_path)`, and a
        no-overwrite `_atomic_create_text` on write (a collision raises FileExistsError and
        another id is drawn).
        """
        if not bucket_id:
            return None

        with self._bucket_path_index_guard:
            path = self._bucket_path_index.get(bucket_id)
            if path and os.path.isfile(path):
                return path
            if path:
                # An unmanaged move/delete can invalidate a hit before the
                # periodic full-vault poll.  Drop it; the next poll rebuilds
                # the complete index and discovers any replacement location.
                self._bucket_path_index.pop(bucket_id, None)
                self._bucket_path_index_ready = False
            index_ready = self._bucket_path_index_ready
        if index_ready:
            # ▼▼▼ The fallback: **ready does not mean complete.** Scan once by filename
            #     before saying "no".
            #     The cost was measured (a copied store of 964 buckets / 2371 .md files, on a
            #     Windows bind mount):
            #       one filename scan      0.3s   <- this branch
            #       full index rebuild     5.1s   <- deliberately **not** taken here (the
            #                                        branch below takes it)
            #     "Genuinely absent" (no such bucket, a collision check) is the common case,
            #     and turning the common case into a seconds-long job would be a new disease.
            if not scan_if_missing:
                return None
            hit = self._scan_bucket_file_by_name(bucket_id)
            if hit:
                # 🔴 **It must make a sound**: this warning is the only evidence that the
                #    index dropped an entry. Recovering silently would bury the root cause —
                #    and the reason this bug took three collisions to catch is precisely that
                #    it was silent the whole way.
                logger.warning(
                    "path index was ready but missing %s; recovered by filename "
                    "scan and backfilled / 索引 ready 却缺条目，已按文件名找回并补回索引: %s",
                    bucket_id,
                    hit,
                )
            return hit
            # ▲▲▲ End of the fallback.

        # Preserve the cheap common path for ordinary single-bucket CRUD: most
        # managed filenames contain the ID, so there is no reason to parse the
        # full vault merely to serve one get/update.  Bulk migration explicitly
        # prewarms the complete index via _ensure_bucket_path_index().
        hit = self._scan_bucket_file_by_name(bucket_id)
        if hit:
            return hit

        # Imported/renamed files may not encode their ID in the filename.
        self._ensure_bucket_path_index()
        with self._bucket_path_index_guard:
            path = self._bucket_path_index.get(bucket_id)
            return path if path and os.path.isfile(path) else None

    # ---------------------------------------------------------
    # Internal: load bucket data from .md file
    # ---------------------------------------------------------
    @staticmethod
    def _sanitize_text(text: str) -> str:
        """F-04 fix: strip NUL, dangerous control characters, and Unicode bidi
        override/isolate characters.

        \\n (LF), \\r (CR) and \\t (Tab) are preserved.
        What is stripped:
          U+0000~U+0008, U+000B, U+000C, U+000E~U+001F, U+007F (C0/C1 control characters)
          U+202A~U+202E bidi controls (LRE / RLE / PDF / LRO / RLO)
          U+2066~U+2069 bidi isolates (LRI / RLI / FSI / PDI)
        Emoji and CJK are unaffected.
        """
        _ctrl_table = {
            c: None
            for c in list(range(0x00, 0x09))    # 0x00..0x08
            + [0x0B, 0x0C]                       # VT, FF
            + list(range(0x0E, 0x20))            # 0x0E..0x1F
            + [0x7F]                             # DEL
            + list(range(0x202A, 0x202F))        # bidi controls 0x202A..0x202E
            + list(range(0x2066, 0x206A))        # bidi isolates 0x2066..0x2069
        }
        return str(text).translate(_ctrl_table)

    @staticmethod
    def _sanitize_float_field(value, default: float) -> float:
        """Extract a float from whatever format it arrives in (older ones such as `'V0.9'`,
        `'[我的视角:V0.3]'` and plain 0.9 are all accepted)."""
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return default
            return max(0.0, min(1.0, numeric))
        try:
            nums = re.findall(r'[-+]?\d*\.?\d+', str(value))
            if not nums:
                return default
            numeric = float(nums[0])
            if not math.isfinite(numeric):
                return default
            return max(0.0, min(1.0, numeric))
        except Exception:
            return default

    @classmethod
    def _normalize_metadata_value(
        cls,
        value,
        *,
        _depth: int = 0,
        _seen: set[int] | None = None,
        _budget: list[int] | None = None,
    ):
        """Return bounded, alias-free JSON-safe YAML metadata.

        SafeLoader blocks object construction but still permits recursive and
        exponentially shared aliases.  Reject repeated containers and cap the
        expansion before rebuilding untrusted frontmatter into ordinary lists.
        """
        if _depth > _MAX_METADATA_DEPTH:
            raise ValueError("bucket metadata exceeds nesting-depth limit")
        if _seen is None:
            _seen = set()
        if _budget is None:
            _budget = [_MAX_METADATA_NODES]
        _budget[0] -= 1
        if _budget[0] < 0:
            raise ValueError("bucket metadata exceeds node limit")
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            # RFC 8259/JSON has no NaN or infinity.  Normalize YAML's .nan and
            # .inf scalars to null; known numeric fields below then apply their
            # documented defaults instead of poisoning dashboard responses.
            return value if math.isfinite(value) else None
        if isinstance(value, (bytes, bytearray, memoryview, set, frozenset)):
            raise ValueError(
                f"bucket metadata contains non-JSON-safe value: {type(value).__name__}"
            )
        if isinstance(value, dict):
            identity = id(value)
            if identity in _seen:
                raise ValueError("bucket metadata contains recursive/shared aliases")
            _seen.add(identity)
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(key, datetime):
                    normalized_key = key.isoformat()
                elif isinstance(key, date):
                    normalized_key = key.isoformat()
                elif key is None or isinstance(key, (str, bool, int)):
                    normalized_key = str(key)
                elif isinstance(key, float) and math.isfinite(key):
                    normalized_key = str(key)
                else:
                    raise ValueError(
                        "bucket metadata contains a non-JSON mapping key"
                    )
                if normalized_key in normalized:
                    raise ValueError(
                        "bucket metadata contains colliding normalized keys"
                    )
                normalized[normalized_key] = cls._normalize_metadata_value(
                    item,
                    _depth=_depth + 1,
                    _seen=_seen,
                    _budget=_budget,
                )
            return normalized
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in _seen:
                raise ValueError("bucket metadata contains recursive/shared aliases")
            _seen.add(identity)
            return [
                cls._normalize_metadata_value(
                    v,
                    _depth=_depth + 1,
                    _seen=_seen,
                    _budget=_budget,
                )
                for v in value
            ]
        raise ValueError(
            f"bucket metadata contains unsupported scalar: {type(value).__name__}"
        )

    def _load_bucket(self, file_path: str) -> Optional[dict]:
        """
        Parse a Markdown file and return structured bucket data.
        """
        try:
            post = frontmatter.load(file_path)
            # Normalize the metadata object as one graph so aliases shared by
            # different top-level keys cannot reset the node/depth budgets.
            metadata = self._normalize_metadata_value(dict(post.metadata))
            domain_value = metadata.get("domain")
            if isinstance(domain_value, str):
                metadata["domain"] = [domain_value] if domain_value.strip() else []
            elif domain_value is None:
                metadata["domain"] = []
            elif not isinstance(domain_value, list):
                metadata["domain"] = list(domain_value) if isinstance(domain_value, tuple) else [str(domain_value)]
            # Older buckets may have stored string forms such as `'V0.9'` or `'[我的视角:V0.3]'`
            for field, default in (
                ("valence", 0.5),
                ("arousal", 0.3),
                ("model_valence", 0.5),
                ("weight", 0.5),
            ):
                if field in metadata:
                    metadata[field] = self._sanitize_float_field(metadata[field], default)
            # YAML is an external input boundary (manual files, migration ZIP,
            # GitHub restore).  Never let arbitrary scalar strings reach JSON
            # consumers that treat these fields as numbers.
            metadata["importance"] = _clamp_importance(
                metadata.get("importance", 5), f"load:{Path(file_path).name}"
            )
            try:
                activation_count = float(metadata.get("activation_count", 0) or 0)
                if not math.isfinite(activation_count) or activation_count < 0:
                    raise ValueError("invalid activation_count")
                metadata["activation_count"] = (
                    int(activation_count)
                    if activation_count.is_integer()
                    else round(activation_count, 3)
                )
            except (TypeError, ValueError, OverflowError):
                metadata["activation_count"] = 0
            # Defense in depth: future scalar branches must not accidentally
            # reintroduce NaN/bytes/set values to web JSON consumers.
            json.dumps(metadata, ensure_ascii=False, allow_nan=False)
            return {
                "id": post.get("id", Path(file_path).stem),
                "metadata": metadata,
                "content": post.content,
                "path": file_path,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load bucket file / 加载桶文件失败: {file_path}: {e}"
            )
            return None
