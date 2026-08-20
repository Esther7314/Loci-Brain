"""
========================================
tools/_common.py — helper logic shared across tools
========================================

This file collects the small helpers reused by several tools that carry no
tool-specific meaning of their own: quota checks (per-bucket byte ceiling /
pinned count ceiling), merge-or-create (shared by hold and grow), the
suspected-duplicate scan for a new bucket, and the automatic plan-closing check
triggered by a new event.

Key behaviour:
- check_content_size / check_pinned_quota: read config.limits and return a
  human-readable message string when a limit is exceeded
- merge_or_create: semantic retrieval first to find a near bucket; above the
  threshold it merges (hold concatenates the original text, grow compresses with
  an LLM), otherwise it creates; after writing it posts to the embedding queue
  and refreshes the dehydration cache
- merge_or_create accepts ``source_tool`` / ``grow_batch_id`` and writes them
  into the frontmatter when creating; when merging it leaves the existing
  bucket's source_tool alone and only appends ``last_merged_by``
- check_duplicate_for: fire-and-forget marking of suspected duplicate pairs
  (never merges automatically)
- check_plan_resolution: fire-and-forget prefiltering through both the keyword
  and vector channels plus a conservative LLM judgement, to mark a finished
  active plan as resolved

What this file deliberately does not do:
- Holds no global objects; every dependency comes from _runtime
- Wraps no side effects beyond log formatting; the caller decides whether to await

Exports: limits_cfg / max_bucket_bytes / max_pinned / check_content_size /
         count_pinned / check_pinned_quota / merge_or_create /
         check_duplicate_for / check_plan_resolution
========================================
"""

from typing import Tuple
import asyncio
from copy import deepcopy
from concurrent.futures import Future, InvalidStateError
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import math
import os
from pathlib import Path
import threading
import time
import uuid

from utils import is_closed, parse_bool
from locibrain.domain.plan_history import append_plan_change_log as append_plan_change_log

from . import _runtime as rt

_EMBED_WARN = (
    "向量暂未完成，该桶当前仅支持关键词匹配；正文已保存。"
    "请检查向量队列与 embedding 提供商配置后重试补齐。"
)

# ============================================================
# Named constants
# ------------------------------------------------------------
# No bare magic numbers. These used to be scattered across helper defaults and
# business logic; collected here, ① every tunable can be read at a glance and
# ② the thresholds that encode a principle (the importance>=9 ceiling) are
# traceable. Before changing any of them, remember what the ceiling is for:
# importance only means anything while it is scarce.
# ============================================================

# --- Bucket and quota defaults ---
_DEFAULT_MAX_BUCKET_BYTES = 50 * 1024  # 50 KB ceiling per bucket (above that, split it up with grow)
_DEFAULT_MAX_PINNED = 20               # ceiling on pinned buckets (a principle, not a technical limit: importance must stay scarce); kept in sync with limits.max_pinned in config.example.yaml
_DEFAULT_MAX_GROW_INPUT_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_QUERY_BYTES = 16 * 1024
_DEFAULT_MAX_METADATA_BYTES = 16 * 1024
_DEFAULT_MAX_GROW_ITEMS = 100

# --- The importance>=9 quota (scarcity is the point) ---
_HIGH_IMP_THRESHOLD = 9                # importance at or above this counts as "high importance"
_HIGH_IMP_HARD_CAP = 24                # hard ceiling on high-importance buckets
_HIGH_IMP_SOFT_WARN = 22               # from here on, push the OB-W003 reminder
_HIGH_IMP_DEGRADE_TO = 8               # the importance an over-quota bucket is degraded to
_HIGH_IMP_EXEMPT_TYPES = frozenset({"feel", "plan", "letter", "archived"})

# --- The pinned soft threshold ---
_PINNED_SOFT_GAP = 2                   # "soft threshold = cap - GAP"; cap=20 -> soft=18

# --- check_duplicate_for / check_plan_resolution ---
_DUP_DEFAULT_THRESHOLD = 0.95          # vector similarity >= this -> mark as a suspected duplicate
_DUP_TOPK = 10                         # how many top candidates to retrieve when judging duplicates
_PLAN_VECTOR_TOPK = 20                 # the vector prefilter width for plan judgement
_PLAN_VECTOR_THRESHOLD = 0.7           # only above this does the LLM get asked whether it is done
_PLAN_LLM_CONFIDENCE_MIN = 0.7         # floor on the LLM's judgement.confidence
_SAME_EVENT_CONFIDENCE_MIN = 0.85      # automatic merging demands high confidence; when in doubt, create
_PLAN_FALLBACK_CAP = 10                # ceiling on plans sent straight to the LLM without vectors (guards against a flood of LLM calls)

# --- Field truncation lengths (downstream storage / log readability) ---
_RESOLUTION_REASON_MAX = 200           # ceiling on the reason written into a bucket's frontmatter
_LOG_REASON_PREVIEW = 60               # how much of the reason to preview in the log

# --- Length of the content lock's hash key ---
_CONTENT_LOCK_KEY_HEX = 16             # a 64-bit space; collision probability is negligible
_CONTENT_LOCK_POLL_SECONDS = 0.01
_CONTENT_LOCK_STALE_MIN_SECONDS = 180.0
_CONTENT_LOCK_STALE_GRACE_SECONDS = 60.0
_CONTENT_LOCK_WAIT_GRACE_SECONDS = 30.0

# Per-content turns use concurrent futures rather than asyncio.Lock. FastMCP may
# dispatch independent HTTP sessions from different event loops/threads;
# asyncio.Lock is not a cross-loop primitive and allowed two first writes to race.
_merge_content_tails: dict[str, Future[None]] = {}
_merge_content_tails_guard = threading.Lock()


def _complete_content_turn(key: str, turn: Future[None]) -> None:
    # Future callbacks may complete the next cancelled turn in the same
    # thread.  Never call ``set_result`` while holding the non-reentrant tail
    # guard, or that callback chain deadlocks trying to reacquire it.
    with _merge_content_tails_guard:
        if _merge_content_tails.get(key) is turn:
            _merge_content_tails.pop(key, None)
    if not turn.done():
        try:
            turn.set_result(None)
        except InvalidStateError:
            # Another completion/cancellation won the race after ``done``.
            pass


@asynccontextmanager
async def _filesystem_content_turn(key: str):
    """Use atomic lock-file creation as a cross-loop/process final guard."""
    base_dir = str(getattr(rt.bucket_mgr, "base_dir", "") or "").strip()
    if not base_dir:
        yield
        return

    lock_dir = Path(base_dir) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"content-{key}.lock"
    token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    try:
        llm_timeout = float(
            (rt.config.get("dehydration") or {}).get("timeout_seconds", 120)
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        llm_timeout = 120.0
    if not math.isfinite(llm_timeout) or llm_timeout <= 0:
        llm_timeout = 120.0
    stale_seconds = max(
        _CONTENT_LOCK_STALE_MIN_SECONDS,
        llm_timeout + _CONTENT_LOCK_STALE_GRACE_SECONDS,
    )
    deadline = time.monotonic() + stale_seconds + _CONTENT_LOCK_WAIT_GRACE_SECONDS
    acquired = False

    while not acquired:
        try:
            descriptor = os.open(
                lock_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_seconds:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for identical-content write lock")
            await asyncio.sleep(_CONTENT_LOCK_POLL_SECONDS)
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token)
            acquired = True

    try:
        yield
    finally:
        try:
            if lock_path.read_text(encoding="utf-8") == token:
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass


@asynccontextmanager
async def _keyed_turn(key: str):
    """Serialize operations sharing ``key`` across tasks, loops, and request threads."""
    turn: Future[None] = Future()
    with _merge_content_tails_guard:
        previous = _merge_content_tails.get(key)
        _merge_content_tails[key] = turn

    acquired = previous is None
    try:
        if previous is not None:
            # Do not let cancellation of this waiter cancel its predecessor's
            # shared Future; later turns still depend on that predecessor as
            # the serialization barrier.
            await asyncio.shield(asyncio.wrap_future(previous))
            acquired = True
        async with _filesystem_content_turn(key):
            yield
    finally:
        if acquired:
            _complete_content_turn(key, turn)
        elif previous is not None:
            # A waiter can be cancelled before its predecessor finishes.  Its
            # turn must still be completed once the predecessor releases;
            # otherwise every later waiter for this key blocks forever on the
            # abandoned Future.
            previous.add_done_callback(
                lambda _completed: _complete_content_turn(key, turn)
            )


@asynccontextmanager
async def _content_turn(content: str):
    """Serialize identical writes across tasks, loops, and request threads."""
    key = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:_CONTENT_LOCK_KEY_HEX]
    async with _keyed_turn(key):
        yield


@asynccontextmanager
async def _quota_turn(name: str):
    """Serialize a quota check-then-write so concurrent requests can't all pass
    the same stale pre-check before either commits (pinned/importance TOCTOU).

    Reuses the same cross-loop/cross-process lock-file machinery as
    ``_content_turn`` — FastMCP may dispatch requests from different event
    loops, so a plain ``asyncio.Lock`` here would not actually serialize them.
    """
    async with _keyed_turn(f"quota-{name}"):
        yield


def _push_warning_safe(code: str, msg: str) -> None:
    """Call errors.push_warning safely; degrade silently if the import fails.

    Why: push_warning is called four times across the two quota helpers, and each
    call site used to repeat the same three-layer try/except import dance.
    Centralised here:
      ① the calling code becomes one clean line;
      ② the import fallback logic exists in exactly one place;
      ③ a test only needs to patch this function.

    Path priority:
      1. from errors        —— production/test environments where src/ is at the
                               top of sys.path
      2. from ..errors      —— the in-package relative import as a fallback
      3. both fail -> skip silently (a failure to deliver a warning must never
         make the actual operation fail)
    """
    try:
        from core.errors import push_warning  # type: ignore
    except ImportError:
        try:
            from ..core.errors import push_warning  # type: ignore
        except Exception:  # pragma: no cover
            return
    try:
        push_warning(code, msg)
    except Exception:  # pragma: no cover
        # Even if the warning channel breaks, it must not drag the real path down
        pass


def limits_cfg() -> dict:
    """Read the config.limits section; the defaults are 50KB per bucket / 20 pinned."""
    config = rt.config if isinstance(rt.config, dict) else {}
    return config.get("limits", {}) or {}


def _configured_limit(name: str, default: int) -> int:
    raw = limits_cfg().get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if value >= 0 else default


def max_bucket_bytes() -> int:
    return _configured_limit("max_bucket_bytes", _DEFAULT_MAX_BUCKET_BYTES)


def max_pinned() -> int:
    return _configured_limit("max_pinned", _DEFAULT_MAX_PINNED)


def max_grow_input_bytes() -> int:
    return _configured_limit("max_grow_input_bytes", _DEFAULT_MAX_GROW_INPUT_BYTES)


def max_query_bytes() -> int:
    return _configured_limit("max_query_bytes", _DEFAULT_MAX_QUERY_BYTES)


def max_metadata_bytes() -> int:
    return _configured_limit("max_metadata_bytes", _DEFAULT_MAX_METADATA_BYTES)


def max_grow_items() -> int:
    return _configured_limit("max_grow_items", _DEFAULT_MAX_GROW_ITEMS)


def check_content_size(content: str) -> str | None:
    """Returns a message string when the per-bucket ceiling is exceeded, else None."""
    cap = max_bucket_bytes()
    if cap <= 0:
        return None
    size = len(content.encode("utf-8"))
    if size > cap:
        return (
            f"内容过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
            "请改用 grow 拆分存入，或在 config.limits.max_bucket_bytes 调高上限。"
        )
    return None


def check_grow_input_size(content: str) -> str | None:
    cap = max_grow_input_bytes()
    if cap <= 0:
        return None
    size = len(str(content or "").encode("utf-8"))
    if size > cap:
        return (
            f"grow 输入过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
            "请分批调用，或调整 config.limits.max_grow_input_bytes。"
        )
    return None


def check_query_size(query: str) -> str | None:
    cap = max_query_bytes()
    if cap <= 0:
        return None
    size = len(str(query or "").encode("utf-8"))
    if size > cap:
        return (
            f"查询过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB）。"
            "请缩短查询，或调整 config.limits.max_query_bytes。"
        )
    return None


def check_metadata_size(**fields: object) -> str | None:
    cap = max_metadata_bytes()
    if cap <= 0:
        return None
    try:
        size = sum(len(str(value or "").encode("utf-8")) for value in fields.values())
    except Exception:
        return "元数据参数无法安全序列化。"
    if size > cap:
        labels = ", ".join(fields)
        return (
            f"元数据过大（{size / 1024:.1f} KB > 上限 {cap / 1024:.0f} KB；字段: {labels}）。"
            "请缩短标签、名称或筛选条件。"
        )
    return None


def check_grow_items_payload(items: list) -> str | None:
    item_cap = max_grow_items()
    if item_cap > 0 and len(items) > item_cap:
        return f"grow items 过多（{len(items)} > 上限 {item_cap}）。请分批调用，或调整 config.limits.max_grow_items。"

    byte_cap = max_grow_input_bytes()
    if byte_cap <= 0:
        return None
    total = 0
    for item in items:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("content", "")
        else:
            continue
        try:
            total += len(str(value or "").encode("utf-8"))
        except Exception:
            return "grow items 包含无法安全序列化的 content。"
        if total > byte_cap:
            return f"grow items 正文总量过大（{total / 1024:.1f} KB > 上限 {byte_cap / 1024:.0f} KB）。请分批调用。"
    return None


async def count_pinned() -> int:
    """Count the pinned buckets right now. On failure it returns 0 (conservative:
    never block).

    The single source of truth for the quota is metadata.pinned. type=permanent
    is a first-class bucket type; it is not the same as pinned=True and does not
    consume the pinned quota.
    """
    try:
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
        seen_ids: set[str] = set()
        count = 0
        for bucket in all_b:
            bucket_id = str(bucket.get("id") or "").strip()
            if bucket_id:
                if bucket_id in seen_ids:
                    continue
                seen_ids.add(bucket_id)
            metadata = bucket.get("metadata", {})
            if is_terminal_memory_metadata(metadata):
                continue
            if isinstance(metadata, dict) and parse_bool(
                metadata.get("pinned"), default=False
            ):
                count += 1
        return count
    except Exception as e:
        warning = getattr(getattr(rt, "logger", None), "warning", None)
        if callable(warning):
            warning(f"count_pinned failed: {e}")
        return 0


def _is_pinned_orphan(meta: dict) -> bool:
    """Return True only for confidently repairable pinned/type desync.

    `type == "permanent"` is now a first-class bucket type, not just the
    storage side effect of `pinned=True`.  Metadata alone cannot safely
    distinguish a legacy unpinned-pinned bucket from an intentionally permanent
    bucket, so automatic demotion is intentionally disabled.
    """
    return False


async def repair_pinned_desync(bucket_mgr, apply: bool = False) -> dict:
    """Scan for pinned/type desync; permanent is currently never auto-demoted.

    type=permanent is now a first-class bucket type. Metadata alone cannot safely
    distinguish the residue of a historical unpin from a permanent bucket the
    user created deliberately, so automatic demotion is disabled.

    Returns a dict: {total, pinned, orphans:[{id,name,importance}], applied,
    demoted, failed}."""
    buckets = await bucket_mgr.list_all(include_archive=False)
    unique_buckets: list[dict] = []
    seen_ids: set[str] = set()
    for bucket in buckets:
        bucket_id = str(bucket.get("id") or "").strip()
        if bucket_id:
            if bucket_id in seen_ids:
                continue
            seen_ids.add(bucket_id)
        unique_buckets.append(bucket)
    pinned_now = [
        bucket
        for bucket in unique_buckets
        if isinstance(bucket.get("metadata"), dict)
        and not is_terminal_memory_metadata(bucket["metadata"])
        and parse_bool(bucket["metadata"].get("pinned"), default=False)
    ]
    orphans = [
        b for b in unique_buckets
        if _is_pinned_orphan(b.get("metadata", {}))
    ]

    result: dict = {
        "total": len(unique_buckets),
        "pinned": len(pinned_now),
        "orphans": [
            {
                "id": b["id"],
                "name": b.get("metadata", {}).get("name") or "",
                "importance": b.get("metadata", {}).get("importance"),
            }
            for b in orphans
        ],
        "applied": apply,
        "demoted": 0,
        "failed": 0,
    }
    if not apply or not orphans:
        return result

    for b in orphans:
        try:
            ok = await bucket_mgr.update(b["id"], pinned=False)
            if ok:
                result["demoted"] += 1
            else:
                result["failed"] += 1
                rt.logger.warning(f"repair_pinned_desync: update returned False for {b['id']}")
        except Exception as e:
            result["failed"] += 1
            rt.logger.warning(f"repair_pinned_desync: update failed for {b['id']}: {e}")
    return result


async def check_pinned_quota() -> str | None:
    """Returns a message string once the pinned ceiling is reached, else None.

    (store_pinned uses this for a hard rejection in strict mode; the newer
    "automatic degradation" path should use enforce_pinned_quota instead, which
    returns (False, msg) at the ceiling so the caller falls back to an ordinary
    bucket.)"""
    cap = max_pinned()
    if cap <= 0:
        return None
    cur = await count_pinned()
    if cur >= cap:
        return (
            f"pinned 桶已达上限（{cur}/{cap}），建议先用 trace(bucket_id, pinned=0) "
            "清理低优先级钉选；或在 config.limits.max_pinned 调高上限。"
        )
    return None


# ============================================================
# Quota helpers (the unified error scheme: OB-W003/W004 + OB-I001/I002)
# ------------------------------------------------------------
# Design: "quota warning" and "automatic degradation" are two separate steps,
# corresponding to W and I respectively.
# Calling code gets the message from the former and it is automatically delivered
# to the end of the MCP response via _push_warning_safe.
# The threshold constants live in the "Named constants" block at the top of this
# file, next to the importance ceiling they encode.
# ============================================================


def is_terminal_memory_metadata(metadata: dict | None) -> bool:
    """Whether metadata represents an archived/deleted terminal memory."""
    if not isinstance(metadata, dict):
        return False
    return bool(
        metadata.get("deleted_at")
        or parse_bool(metadata.get("tombstone"), default=False)
        or str(metadata.get("type") or "").strip().lower() == "archived"
    )


def is_importance_audit_candidate(
    metadata: dict | None,
    minimum: int,
) -> bool:
    """Shared visible ordinary-memory scope for importance audit and quota."""
    if not isinstance(metadata, dict):
        return False
    try:
        importance = int(metadata.get("importance") or 0)
    except (OverflowError, TypeError, ValueError):
        return False
    if importance < minimum:
        return False
    if parse_bool(metadata.get("dont_surface"), default=False):
        return False
    if is_terminal_memory_metadata(metadata):
        return False
    bucket_type = str(metadata.get("type") or "dynamic").strip().lower()
    return bucket_type not in _HIGH_IMP_EXEMPT_TYPES


def occupies_high_importance_quota_slot(metadata: dict | None) -> bool:
    """Whether one logical bucket occupies the ordinary importance>=9 pool.

    This intentionally matches the auditable ``breath_advanced(importance_min=9)``
    candidate scope, then additionally excludes pinned/protected buckets because
    those have their own quota.  Explicit unpinned ``permanent`` buckets remain
    ordinary candidates and therefore still count.
    """
    if not is_importance_audit_candidate(metadata, _HIGH_IMP_THRESHOLD):
        return False
    assert isinstance(metadata, dict)
    if parse_bool(metadata.get("pinned"), default=False):
        return False
    if parse_bool(metadata.get("protected"), default=False):
        return False
    return True


async def count_high_importance(bucket_mgr=None) -> int:
    """Count unique logical buckets in the ordinary importance>=9 pool."""
    manager = bucket_mgr if bucket_mgr is not None else rt.bucket_mgr
    try:
        all_b = await manager.list_all(include_archive=False)
        seen_ids: set[str] = set()
        duplicates = 0
        count = 0
        for bucket in all_b:
            bucket_id = str(bucket.get("id") or "").strip()
            if bucket_id:
                if bucket_id in seen_ids:
                    duplicates += 1
                    continue
                seen_ids.add(bucket_id)
            if occupies_high_importance_quota_slot(bucket.get("metadata", {})):
                count += 1
        if duplicates:
            warning = getattr(getattr(rt, "logger", None), "warning", None)
            if callable(warning):
                warning(
                    "count_high_importance ignored %s duplicate physical bucket rows",
                    duplicates,
                )
        return count
    except Exception as e:
        warning = getattr(getattr(rt, "logger", None), "warning", None)
        if callable(warning):
            warning(f"count_high_importance failed: {e}")
        return 0


async def enforce_high_importance_quota(
    importance: int,
    *,
    bucket_mgr=None,
) -> int:
    """The importance>=9 quota check plus automatic degradation.

    - current count >= hard cap -> push OB-I001 and lower importance to
      _HIGH_IMP_DEGRADE_TO
    - current count >= soft threshold -> push OB-W003 (a reminder only; no data
      is touched)
    Returns the importance that actually takes effect.
    """
    if importance < _HIGH_IMP_THRESHOLD:
        return importance
    cur = (
        await count_high_importance()
        if bucket_mgr is None
        else await count_high_importance(bucket_mgr=bucket_mgr)
    )
    if cur >= _HIGH_IMP_HARD_CAP:
        info = getattr(getattr(rt, "logger", None), "info", None)
        if callable(info):
            info(
                f"op=quota phase=branch branch=imp_degrade requested={importance} "
                f"current={cur} cap={_HIGH_IMP_HARD_CAP} degraded_to={_HIGH_IMP_DEGRADE_TO}"
            )
        _push_warning_safe(
            "OB-I001",
            f"当前已有 {cur} 条 importance≥{_HIGH_IMP_THRESHOLD}（硬上限 {_HIGH_IMP_HARD_CAP}），新桶 importance 自动降级为 {_HIGH_IMP_DEGRADE_TO}",
        )
        return _HIGH_IMP_DEGRADE_TO
    if cur >= _HIGH_IMP_SOFT_WARN:
        _push_warning_safe(
            "OB-W003",
            f"当前已有 {cur} 条 importance≥{_HIGH_IMP_THRESHOLD}（硬上限 {_HIGH_IMP_HARD_CAP}），接近上限",
        )
    return importance


async def enforce_pinned_quota(pinned: bool) -> bool:
    """The pinned quota check plus automatic bail-out.

    - current count >= hard cap -> push OB-I002 and return False (fall back to an
      ordinary bucket)
    - current count >= soft threshold -> push OB-W004 (a reminder only; no data is
      touched)
    Passing pinned=False returns False immediately.
    """
    if not pinned:
        return False
    cap = max_pinned()
    cur = await count_pinned()
    # soft threshold = cap - GAP; cap=20, GAP=2 -> soft=18. If cap is too small
    # (<= GAP) this degenerates to the hard cap.
    soft = max(1, cap - _PINNED_SOFT_GAP) if cap > _PINNED_SOFT_GAP else cap
    if cap > 0 and cur >= cap:
        rt.logger.info(
            f"op=quota phase=branch branch=pinned_degrade current={cur} cap={cap}"
        )
        _push_warning_safe(
            "OB-I002",
            f"当前已有 {cur} 条 pinned（硬上限 {cap}），本次未钉成功，已保留为普通桶",
        )
        return False
    if cap > 0 and cur >= soft:
        _push_warning_safe(
            "OB-W004",
            f"当前已有 {cur} 条 pinned（硬上限 {cap}），接近上限",
        )
    return True


async def merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    raw_merge: bool = False,
    why_remembered: str = "",
    source_tool: str = "",
    grow_batch_id: str = "",
    meaning: str = "",
    media: list | str | None = None,
    test_data: bool = False,
) -> Tuple[str, bool, str]:
    """
    Look for a similar bucket to merge into; merge if there is one, create if
    there is not. Returns (bucket id or name, whether it merged, embedding
    warning text).

    raw_merge=True (hold): append the original text; no LLM compression.
    raw_merge=False (grow): the LLM compresses old + new content together.

    Provenance tracking:
    - source_tool: "hold" | "grow", written as the new bucket's source_tool; on
      the merge path the existing bucket's source_tool is left untouched and
      last_merged_by=source_tool is written instead.
    - grow_batch_id: only ever passed on the grow path, written on creation; the
      merge path does not overwrite the existing bucket's batch_id (that bucket
      may come from an earlier grow or hold, and overwriting would lose which
      batch it originally belonged to).

    Note: meaning/media are my own anchors to the experience, not a summary. On
    creation they are written directly; when merging into an existing bucket both
    meanings are kept (concatenated) and media is appended rather than replaced.

    The whole search->create path runs serialised under a per-content-hash lock.
    On concurrent calls with the same content the later coroutine blocks, and
    once the first has written it takes the merge branch — so no duplicate bucket
    is produced.
    """
    async with _content_turn(content):
        return await _merge_or_create_inner(
            content=content, tags=tags, importance=importance, domain=domain,
            valence=valence, arousal=arousal, name=name, raw_merge=raw_merge,
            why_remembered=why_remembered, source_tool=source_tool,
            grow_batch_id=grow_batch_id, meaning=meaning, media=media,
            test_data=test_data,
        )


async def _merge_or_create_inner(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    raw_merge: bool = False,
    why_remembered: str = "",
    source_tool: str = "",
    grow_batch_id: str = "",
    meaning: str = "",
    media: list | str | None = None,
    test_data: bool = False,
) -> Tuple[str, bool, str]:
    """The actual search->merge/create logic, called by merge_or_create under the
    protection of the lock."""
    exact_storage_match = False
    try:
        existing = await rt.bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        rt.logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    # Cache invalidation and a concurrent list_all() refresh can cross: an old
    # parsed snapshot may briefly hide a bucket that is already durable on disk.
    # Before any create, let Markdown truth override search/cache results.
    exact_finder = getattr(rt.bucket_mgr, "find_exact_content", None)
    if callable(exact_finder):
        try:
            # Byte-identical source text is the same write even when concurrent
            # Flash analyses choose different domains/tags. Metadata is a
            # derived classification and must not split one identical event.
            exact = exact_finder(content, domain_filter=None)
        except Exception as exc:
            rt.logger.warning(f"Exact-content storage check failed: {exc}")
        else:
            if exact:
                exact = dict(exact)
                exact["score"] = float("inf")
                existing = [exact]
                exact_storage_match = True

    merge_threshold = rt.config.get("merge_threshold") or 75
    if (
        not test_data
        and existing
        and existing[0].get("score", 0) > merge_threshold
    ):
        candidate_id = str(existing[0].get("id") or "").strip()
        merge_key = hashlib.sha256(
            candidate_id.encode("utf-8", errors="replace")
        ).hexdigest()[:_CONTENT_LOCK_KEY_HEX]
        try:
            # Different new texts can resolve to the same target bucket.  The
            # content-hash lock above cannot serialize that fan-in, so reserve
            # the logical target too and optimistically retry regular edits.
            async with _keyed_turn(f"merge-target-{merge_key}"):
                for _attempt in range(3):
                    bucket = await rt.bucket_mgr.get(candidate_id)
                    if not bucket:
                        break
                    metadata = bucket.get("metadata", {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    if parse_bool(metadata.get("pinned"), default=False) or parse_bool(
                        metadata.get("protected"), default=False
                    ) or is_terminal_memory_metadata(metadata):
                        break
                    snapshot_content = str(bucket.get("content") or "")
                    snapshot_metadata = deepcopy(metadata)

                    if not exact_storage_match:
                        judge = getattr(rt.dehydrator, "judge_same_event", None)
                        if not callable(judge):
                            rt.logger.warning(
                                "Same-event judge unavailable; creating new bucket / "
                                "同一事件判定器不可用，保守新建"
                            )
                            break
                        judgement = await judge(snapshot_content, content)
                        same_event = parse_bool(
                            judgement.get("same_event", False), default=False
                        )
                        try:
                            confidence = float(judgement.get("confidence", 0.0))
                        except (TypeError, ValueError):
                            confidence = 0.0
                        if not same_event or confidence < _SAME_EVENT_CONFIDENCE_MIN:
                            rt.logger.info(
                                "op=merge_or_create phase=branch branch=separate_event "
                                f"bucket_id={candidate_id} confidence={confidence:.3f} "
                                f"reason={str(judgement.get('reason', ''))[:_LOG_REASON_PREVIEW]}"
                            )
                            break

                    if raw_merge or exact_storage_match:
                        old_text = snapshot_content.rstrip()
                        new_text = content.strip()
                        if new_text and new_text not in old_text:
                            merged = (
                                f"{old_text}\n\n---\n{new_text}"
                                if old_text
                                else new_text
                            )
                        else:
                            merged = old_text or new_text
                    else:
                        merged = await rt.dehydrator.merge(
                            snapshot_content, content
                        )

                    old_v = metadata.get("valence") or 0.5
                    old_a = metadata.get("arousal") or 0.3
                    merged_valence = (
                        round((old_v + valence) / 2, 2)
                        if 0 <= valence <= 1
                        else old_v
                    )
                    merged_arousal = (
                        round((old_a + arousal) / 2, 2)
                        if 0 <= arousal <= 1
                        else old_a
                    )
                    merged_importance = max(
                        metadata.get("importance") or 5,
                        importance,
                    )
                    update_kwargs = {
                        "content": merged,
                        "tags": list(set((metadata.get("tags") or []) + tags)),
                        "importance": merged_importance,
                        "domain": list(
                            set((metadata.get("domain") or []) + domain)
                        ),
                        "valence": merged_valence,
                        "arousal": merged_arousal,
                    }
                    if source_tool:
                        update_kwargs["last_merged_by"] = source_tool
                    if meaning:
                        update_kwargs["meaning_append"] = meaning
                    if media:
                        update_kwargs["media_append"] = media

                    async with AsyncExitStack() as commit_stack:
                        if importance >= _HIGH_IMP_THRESHOLD:
                            await commit_stack.enter_async_context(
                                _quota_turn("high_importance")
                            )
                        bucket_turn = getattr(rt.bucket_mgr, "_bucket_turn", None)
                        update_locked = getattr(
                            rt.bucket_mgr, "_update_locked", None
                        )
                        use_locked_update = callable(bucket_turn) and callable(
                            update_locked
                        )
                        if use_locked_update:
                            await commit_stack.enter_async_context(
                                bucket_turn(candidate_id)
                            )

                        locked_bucket = await rt.bucket_mgr.get(candidate_id)
                        if not locked_bucket:
                            break
                        locked_metadata = locked_bucket.get("metadata", {})
                        if not isinstance(locked_metadata, dict):
                            locked_metadata = {}
                        if is_terminal_memory_metadata(locked_metadata) or (
                            str(locked_bucket.get("content") or "")
                            != snapshot_content
                            or locked_metadata != snapshot_metadata
                        ):
                            continue

                        projected_metadata = dict(locked_metadata)
                        projected_metadata["importance"] = merged_importance
                        if (
                            occupies_high_importance_quota_slot(
                                projected_metadata
                            )
                            and not occupies_high_importance_quota_slot(
                                locked_metadata
                            )
                        ):
                            update_kwargs["importance"] = (
                                await enforce_high_importance_quota(
                                    merged_importance
                                )
                            )

                        update_method = (
                            update_locked
                            if use_locked_update
                            else rt.bucket_mgr.update
                        )
                        committed = await update_method(
                            candidate_id,
                            allow_embedding_fallback=(
                                raw_merge and source_tool == "hold"
                            ),
                            bump_active=True,
                            **update_kwargs,
                        )
                        if not committed:
                            break

                    try:
                        rt.dehydrator.invalidate_cache(snapshot_content)
                    except Exception:
                        pass
                    rt.logger.info(
                        "op=merge_or_create phase=branch branch=merge "
                        f"bucket_id={candidate_id} raw_merge={int(raw_merge)} "
                        f"source_tool={source_tool or '_'} "
                        f"score={existing[0].get('score', 0):.3f}"
                    )
                    return candidate_id, True, ""
                else:
                    rt.logger.warning(
                        "Merge target changed repeatedly; creating a new bucket "
                        "instead of overwriting concurrent edits: %s",
                        candidate_id,
                    )
        except Exception as e:
            rt.logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    async def create_bucket(final_importance: int) -> str:
        return await rt.bucket_mgr.create(
            content=content,
            tags=tags,
            importance=final_importance,
            domain=domain,
            valence=valence,
            arousal=arousal,
            name=name or None,
            why_remembered=why_remembered,
            source_tool=source_tool,
            grow_batch_id=grow_batch_id,
            meaning=meaning,
            media=media,
            test_data=test_data,
            # hold's iron rule: the body reaches disk first. Tagging and embedding
            # may degrade, but a memory is never compressed or taken back.
            allow_embedding_fallback=(raw_merge and source_tool == "hold"),
        )

    if importance >= _HIGH_IMP_THRESHOLD:
        # The quota turn must include the durable create, not just the count.
        # Releasing it after enforce() recreates the original TOCTOU window:
        # several distinct-content holds can all observe one remaining slot.
        async with _quota_turn("high_importance"):
            importance = await enforce_high_importance_quota(importance)
            bucket_id = await create_bucket(importance)
    else:
        bucket_id = await create_bucket(importance)
    # create() already posted to the embedding outbox once the original text hit
    # disk; there is no need to generate again here.
    # Under the managed runtime, queued is a normal success state and must not be
    # misreported as "embedding failed" before the network request has actually
    # completed; only a compatibility runtime without an outbox checks the result
    # of the synchronous attempt.
    embed_warn = ""
    embedding_state = "disabled"
    outbox = getattr(rt.bucket_mgr, "embedding_outbox", None)
    engine = rt.embedding_engine
    if outbox is not None:
        try:
            pending = bool(outbox.is_pending(bucket_id))
        except Exception as pending_exc:
            pending = False
            rt.logger.warning(
                "embedding outbox pending check failed for %s: %s",
                bucket_id,
                pending_exc,
            )
        if pending:
            embedding_state = "queued"
        else:
            existing = None
            lookup_error = None
            if engine and getattr(engine, "enabled", False):
                try:
                    existing = await engine.get_embedding(bucket_id)
                except Exception as exc:
                    lookup_error = exc
            if existing is not None:
                embedding_state = "indexed"
            else:
                # Defensive repair: a stale reconcile/path-index race must not
                # turn a transiently lost task into a permanent unindexed row
                # or tell the user to delete and recreate valid Markdown.
                repair_content = content
                try:
                    stored_bucket = await rt.bucket_mgr.get(bucket_id)
                    if stored_bucket is not None:
                        repair_content = str(
                            stored_bucket.get("content") or repair_content
                        )
                except Exception as read_exc:
                    rt.logger.warning(
                        "embedding repair could not reload bucket %s: %s",
                        bucket_id,
                        read_exc,
                    )
                try:
                    ensure_pending = getattr(outbox, "ensure_pending", None)
                    if callable(ensure_pending):
                        repaired = bool(ensure_pending(
                            bucket_id,
                            repair_content,
                        ))
                    else:
                        repaired = bool(outbox.enqueue(
                            bucket_id,
                            repair_content,
                            reset_retry=False,
                        ))
                except Exception as enqueue_exc:
                    try:
                        repaired = bool(outbox.is_pending(bucket_id))
                    except Exception:
                        repaired = False
                    rt.logger.warning(
                        "embedding outbox repair enqueue failed for %s: %s",
                        bucket_id,
                        enqueue_exc,
                    )
                if repaired:
                    embedding_state = "queued_repair"
                    rt.logger.warning(
                        "Requeued missing embedding task after create: %s%s",
                        bucket_id,
                        (
                            f" lookup_error={type(lookup_error).__name__}"
                            if lookup_error is not None else ""
                        ),
                    )
                else:
                    embedding_state = "missing"
                    embed_warn = _EMBED_WARN
                    rt.logger.info(
                        "op=merge_or_create phase=branch "
                        "branch=embed_degrade bucket_id=%s "
                        "reason=outbox_requeue_failed",
                        bucket_id,
                    )
    elif engine and getattr(engine, "enabled", False):
        try:
            existing = await engine.get_embedding(bucket_id)
            if existing is None:
                embedding_state = "missing"
                embed_warn = _EMBED_WARN
                rt.logger.info(
                    f"op=merge_or_create phase=branch branch=embed_degrade bucket_id={bucket_id} "
                    f"reason=no_embedding_after_create"
                )
            else:
                embedding_state = "indexed"
        except Exception as _embed_exc:
            embedding_state = "missing"
            embed_warn = _EMBED_WARN
            rt.logger.info(
                f"op=merge_or_create phase=branch branch=embed_degrade bucket_id={bucket_id} "
                f"reason={type(_embed_exc).__name__}"
            )
    rt.logger.info(
        f"op=merge_or_create phase=branch branch=create bucket_id={bucket_id} "
        f"source_tool={source_tool or '_'} grow_batch_id={grow_batch_id or '_'} "
        f"embedding_state={embedding_state}"
    )
    return bucket_id, False, embed_warn


async def check_duplicate_for(new_bucket_id: str, new_text: str, threshold: float = _DUP_DEFAULT_THRESHOLD) -> None:
    """fire-and-forget: after a new bucket is written, any older bucket whose
    vector similarity exceeds threshold is marked as a suspected duplicate.

    Nothing is merged automatically. Both sides get dup_candidate=<the other id>
    + dup_score=<0~1>, the Dashboard shows a "suspected duplicate" note in the
    bucket detail view, and a human decides whether to merge them.
    """
    try:
        if not rt.embedding_engine or not getattr(rt.embedding_engine, "enabled", False):
            return
        sims = await rt.embedding_engine.search_similar(new_text, top_k=_DUP_TOPK)
        for bid, score in sims:
            if bid == new_bucket_id:
                continue
            if score < threshold:
                continue
            try:
                await rt.bucket_mgr.update(
                    new_bucket_id, dup_candidate=bid, dup_score=round(float(score), 4)
                )
                await rt.bucket_mgr.update(
                    bid, dup_candidate=new_bucket_id, dup_score=round(float(score), 4)
                )
                rt.logger.info(
                    f"duplicate candidate: {new_bucket_id} ↔ {bid} (sim={score:.3f})"
                )
            except Exception as e:
                rt.logger.warning(f"dup mark failed: {e}")
            break  # only mark the single most similar pair
    except Exception as e:
        rt.logger.warning(f"check_duplicate_for outer error: {e}")


async def _rank_active_plans_by_query(
    new_event_text: str,
    active_plans: list[dict],
) -> list[dict]:
    """Rank active plans through BucketManager's keyword/BM25 channel; no vectors."""
    active_by_id = {str(plan.get("id") or ""): plan for plan in active_plans}
    try:
        ranked = await rt.bucket_mgr.search(
            new_event_text,
            limit=max(len(active_plans), _PLAN_FALLBACK_CAP),
            vector_scores={},
        )
    except Exception as exc:
        rt.logger.warning(f"plan resolution: keyword pre-filter failed: {exc}")
        return []
    return [
        active_by_id[bucket_id]
        for bucket in ranked
        if (bucket_id := str(bucket.get("id") or "")) in active_by_id
    ]


async def check_plan_resolution(new_event_text: str, source_bucket_id: str = "") -> None:
    """A new event triggers keyword/vector recall over active plans, and an LLM
    then judges conservatively whether any of them has been closed."""
    try:
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
        active_plans = [
            b for b in all_b
            if b["metadata"].get("type") == "plan"
            and b["metadata"].get("status", "active") == "active"
        ]
        if not active_plans:
            return
        keyword_candidates = await _rank_active_plans_by_query(
            new_event_text, active_plans
        )
        vector_candidates = []
        if rt.embedding_engine and getattr(rt.embedding_engine, "enabled", False):
            try:
                sims = await rt.embedding_engine.search_similar(new_event_text, top_k=_PLAN_VECTOR_TOPK)
                sim_map = {bid: sc for bid, sc in sims}
                for p in active_plans:
                    if sim_map.get(p["id"], 0.0) > _PLAN_VECTOR_THRESHOLD:
                        vector_candidates.append(p)
            except Exception as e:
                rt.logger.warning(f"plan resolution: vector pre-filter failed, falling back: {e}")
        # Keywords are the indispensable base recall; vectors only add semantic
        # candidates. Even after deduplication the number of small-model calls is
        # capped, so that a single write cannot trigger unbounded API requests
        # when there are many active plans.
        plan_candidates = []
        seen_plan_ids: set[str] = set()
        for candidate in keyword_candidates + vector_candidates + active_plans:
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id in seen_plan_ids:
                continue
            seen_plan_ids.add(candidate_id)
            plan_candidates.append(candidate)
            if len(plan_candidates) >= _PLAN_FALLBACK_CAP:
                break
        for p in plan_candidates:
            try:
                judgement = await rt.dehydrator.judge_plan_resolution(
                    p["content"], new_event_text
                )
                if judgement.get("resolved") and judgement.get("confidence", 0.0) >= _PLAN_LLM_CONFIDENCE_MIN:
                    await rt.bucket_mgr.update(
                        p["id"],
                        status="resolved",
                        resolution_reason=judgement.get("reason", "")[:_RESOLUTION_REASON_MAX],
                        resolved_by=source_bucket_id or "",
                    )
                    rt.logger.info(
                        f"plan auto-resolved: {p['id']} — {judgement.get('reason', '')[:_LOG_REASON_PREVIEW]}"
                    )
            except Exception as e:
                rt.logger.warning(f"plan resolution judgement failed for {p['id']}: {e}")
    except Exception as e:
        rt.logger.warning(f"check_plan_resolution outer error: {e}")


# ============================================================
# Explicit plan -> bucket propagation (the human / AI path)
# ------------------------------------------------------------
# When a plan bucket is marked resolved *explicitly*, by a human or by the AI,
# the two ordinary buckets it points at (related_bucket / resolved_by) are also
# marked status="resolved".
# This is the principle made concrete: a plan is a promise, and when a promise is
# set down, the event buckets carrying it should stop surfacing too.
#
# The path that does NOT propagate: check_plan_resolution (the LLM's automatic
# second judgement) — an automatic verdict is less trustworthy than an explicit
# human or AI action, and this avoids accidentally sinking a live event bucket.
#
# Not done in reverse: bucket trace(resolved=1) does not propagate to the plan (a
# plan is an independent promise, and one event ending is not the promise being
# kept).
# ============================================================
async def cascade_plan_resolved_to_buckets(plan_meta: dict, plan_id: str) -> list[str]:
    """Mark the ordinary buckets pointed at by related_bucket / resolved_by in
    plan_meta as resolved.

    In: the plan bucket's metadata + plan_id (used only for logging).
    Out: the list of bucket_ids actually propagated to (existing, not deleted, and
    not already resolved).
    Errors: one bucket failing does not affect the others; an outer exception is
    only logged, and the list propagated so far is returned.
    """
    linked: list[str] = []
    if not isinstance(plan_meta, dict):
        return linked
    candidates: list[str] = []
    for key in ("related_bucket", "resolved_by"):
        val = (plan_meta.get(key) or "").strip() if isinstance(plan_meta.get(key), str) else ""
        # resolved_by may be "manual" / "llm_judge" rather than a bucket_id: skip
        if not val or val in ("manual", "llm_judge"):
            continue
        if val not in candidates:
            candidates.append(val)
    for bid in candidates:
        try:
            b = await rt.bucket_mgr.get(bid)
            if not b:
                continue
            meta = b.get("metadata", {})
            # Already closed: do not act again (avoids a pointless touch)
            if is_closed(meta):
                continue
            # A plan never propagates to a plan; letters are skipped too (kept forever)
            if meta.get("type") in ("plan", "letter"):
                continue
            # An ending is recorded in status only; the resolved boolean is never
            # newly written any more
            ok = await rt.bucket_mgr.update(bid, status="resolved")
            if ok:
                linked.append(bid)
                rt.logger.info(
                    f"plan→bucket cascade: plan={plan_id} → bucket={bid} status=resolved"
                )
        except Exception as e:
            rt.logger.warning(
                f"plan→bucket cascade failed: plan={plan_id} bucket={bid} err={e}"
            )
    return linked


# Backward compatibility: keep the underscore aliases (some historical call sites
# use the _ prefix)
_check_duplicate_for = check_duplicate_for
_check_plan_resolution = check_plan_resolution
