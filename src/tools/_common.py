"""
========================================
tools/_common.py — helper logic shared across tools
========================================

This file collects the small helpers reused by several tools that carry no
tool-specific meaning of their own: size and quota checks (per-bucket byte
ceiling, grow batch ceilings, pinned and high-importance quotas), the
cross-loop turns that serialize a check-then-write, and the short-id resolver
every tool that takes a bucket id goes through.

Key behaviour:
- check_content_size / check_grow_items_payload / check_pinned_quota: read
  config.limits and return a human-readable message string when a limit is
  exceeded
- _keyed_turn / _quota_turn: one writer at a time per key, across event loops
  and processes
- with_write_key: a resent write call (same write key) gets the first reply back
  instead of writing again
- resolve_bucket_id / resolve_bucket_ids: the 6-character handle a read tool
  prints is accepted by every write tool, as a unique prefix over the whole
  store

What this file deliberately does not do:
- Holds no global objects; every dependency comes from core/runtime
- Wraps no side effects beyond log formatting; the caller decides whether to await

- read_scope: the view core's gate takes (core/scope.ScopeView) — the request's read
  scope, or the whole library read against the source registry — loaded once per
  request; sees_whole_library: whether the
  call reads with no ceiling at all (what owner-only answers ask)

Exports: limits_cfg / max_bucket_bytes / max_pinned / check_content_size /
         check_grow_items_payload / count_pinned / check_pinned_quota /
         read_scope / sees_whole_library / not_found / resolve_bucket_id / resolve_bucket_ids / with_write_key
========================================
"""

import asyncio
from concurrent.futures import Future, InvalidStateError
from contextlib import asynccontextmanager
import contextvars
import math
import re
import threading

from core import scope as _scope
from core.visibility import LIVE, state_of
from utils import is_bucket_id, parse_bool

from core import runtime as rt

# ============================================================
# Named constants
# ------------------------------------------------------------
# No bare magic numbers. Collected here rather than spread across helper defaults
# and business logic, ① every tunable can be read at a glance and
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
_HIGH_IMP_EXEMPT_TYPES = frozenset({"feel", "archived"})

# --- The pinned soft threshold ---
_PINNED_SOFT_GAP = 2                   # "soft threshold = cap - GAP"; cap=20 -> soft=18

# --- Length of the content lock's hash key ---
_CONTENT_LOCK_KEY_HEX = 16             # a 64-bit space; collision probability is negligible
# How long a keyed turn waits for its holder: the slowest holder may be in a side-model
# call (dehydration.timeout_seconds), plus grace; never less than the minimum.
_CONTENT_TURN_WAIT_MIN_SECONDS = 180.0
_CONTENT_TURN_WAIT_GRACE_SECONDS = 60.0
_CONTENT_TURN_WAIT_EXTRA_SECONDS = 30.0

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
    """The cross-loop/process half of a keyed turn: an OS file lease on `.locks`
    (core.bucket_manager._filesystem_turn, the same one each bucket's writes take).

    The kernel holds the lease while this context keeps its descriptor open and drops it
    when the process dies, so a turn left by a killed process (a restart in the middle of
    a regrow) is free at once, and a slow live holder is never taken over by an age rule.
    The wait is as long as the slowest holder may take: a side-model call plus grace."""
    from core.bucket_manager import _filesystem_turn

    base_dir = str(getattr(rt.bucket_mgr, "base_dir", "") or "").strip()
    if not base_dir:
        yield
        return
    try:
        llm_timeout = float(
            (rt.config.get("dehydration") or {}).get("timeout_seconds", 120)
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        llm_timeout = 120.0
    if not math.isfinite(llm_timeout) or llm_timeout <= 0:
        llm_timeout = 120.0
    wait = (max(_CONTENT_TURN_WAIT_MIN_SECONDS, llm_timeout + _CONTENT_TURN_WAIT_GRACE_SECONDS)
            + _CONTENT_TURN_WAIT_EXTRA_SECONDS)
    async with _filesystem_turn(base_dir, f"content-{key}", timeout_seconds=wait):
        yield


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


async def with_write_key(key, do, *, op: str = ""):
    """Run one write tool call under its write key (core/_sources.SourceRegistry.run_once):
    the same key again returns the first reply and writes nothing. No key — today, until
    the request layer sets one from the host's turn — or no registry, and `do` just runs.
    The key's turn is a lease of its own, taken outside any lock the tool takes inside."""
    registry = getattr(rt.bucket_mgr, "sources", None)
    if not key or registry is None:
        return await do()
    return await registry.run_once(key, do, op=op)


@asynccontextmanager
async def _quota_turn(name: str):
    """Serialize a quota check-then-write so concurrent requests can't all pass
    the same stale pre-check before either commits (pinned/importance TOCTOU).

    Reuses the same cross-loop/cross-process lock-file machinery as
    ``_keyed_turn`` — FastMCP may dispatch requests from different event
    loops, so a plain ``asyncio.Lock`` here would not actually serialize them.
    """
    async with _keyed_turn(f"quota-{name}"):
        yield


# ============================================================
# Short ids: the handle a read tool prints is the handle a write tool accepts
# ------------------------------------------------------------
# breath and recall hand the model a 6-character handle (`core/_slicer.py::_short_id`
# cuts a 12-hex id to 6). A model that reads an item off breath and closes it
# with trace passes exactly that handle on, so every tool that takes an id runs
# it through here first, and every check downstream (exists? archived?
# superseded?) speaks of the full id. The rule is recall's read-by-id rule, in
# one place: a 6–11 hex prefix is matched over the whole store, archive
# included, and resolves only when exactly one id starts with it.
# ============================================================

_SHORT_ID_RE = re.compile(r"[0-9a-f]{6,11}")   # shorter than a 12-hex id, longer than noise
_SHORT_ID_CANDIDATES_SHOWN = 8                  # a collision lists this many, then stops
_FULL_ID_RE = re.compile(r"[0-9a-f]{12}|feel_\d{12}_V\d{3}(_\d+)?")   # what a full id looks like


# ============================================================
# The request's read scope, as tools hand it to core
# ------------------------------------------------------------
# The request layer sets the resolved request for the call (core/scope.request_scope);
# tools read it here and pass the view to core as an argument. One view per request: the
# library's metadata (archive included, for the walk to roots) is loaded once.
# ============================================================

_VIEW: contextvars.ContextVar = contextvars.ContextVar("loci_scope_view", default=None)


async def read_scope(fresh: bool = False):
    """This call's `ScopeView` (core/scope.view_of): its read scope, or — an open host
    that sent no scope, no request at all — the whole library, which narrows nothing but
    still reads the source registry. `fresh` reads the library and the registry again
    instead of the view this call already made (a check after waiting on something
    outside). A call outside any request builds its view each time."""
    req = _scope.current_request()
    cached = _VIEW.get()
    if req is not None and cached is not None and cached[0] is req and not fresh:
        return cached[1]
    view = await _scope.view_of(rt.bucket_mgr, req)
    if req is not None:
        _VIEW.set((req, view))
    return view


def sees_whole_library() -> bool:
    """Does this call read the library with no ceiling at all: no request (a direct call,
    a background job), or an open host carrying no `max_grant` that sent no scope (the
    owner's own clients; the one implicit host when there is no `hosts:` table)? Decided
    by the request's scope, not by the host's name: the same host sending a scope reads
    under it and is not the whole library."""
    req = _scope.current_request()
    if req is None:
        return True
    return req.whole_library and req.host is not None and req.host.max_grant is None


def not_found(q: str) -> str:
    """What every id-taking tool says of an id that names nothing it may see."""
    return f"查无此桶：{q}（id 形状但没匹配——可能已物理删除或打错）。"


async def resolve_bucket_id(bucket_id) -> tuple[str, str]:
    """Return (full id, error). A 6–11 hex prefix becomes the one id it names;
    anything else — a full 12-hex id, a readable `feel_…` id, an empty string —
    passes through untouched, so the caller's own existence check still answers
    for exactly what it was given.

    The archive is searched too: an archived entry has to resolve so the caller
    can say "it is in the archive" rather than "no such bucket". On a collision
    nothing is chosen and the candidates are listed; the caller writes nothing.

    Under a read scope only what the request may read resolves, a full id included:
    an id out of scope gets the same answer as an id that names nothing, and a
    collision lists only candidates in scope. "A full id" there is anything shaped like
    an id of the library (`utils.is_bucket_id`: 12 hex, or a readable `feel_…` id of any
    shape) or naming an entry of it — a missing id of that shape gets the same answer, so
    the two cannot be told apart. Anything else (a host's line id in `from`) passes on.
    """
    q = str(bucket_id or "").strip()
    view = await read_scope()
    if _scope.narrows(view) and q and (_FULL_ID_RE.fullmatch(q) or is_bucket_id(q)
                                       or q in view.metas):
        return (q, "") if view.permits_id(q) else (q, not_found(q))
    if not _SHORT_ID_RE.fullmatch(q):
        return q, ""
    allb = await rt.bucket_mgr.list_all(include_archive=True)
    cand = sorted({
        cid for cid in (
            str((b.get("metadata") or {}).get("id") or b.get("id") or "") for b in allb
        )
        if cid.startswith(q) and (view is None or view.permits_id(cid))
    })
    if len(cand) == 1:
        return cand[0], ""
    if cand:
        return q, ("半截 id 撞了 " + str(len(cand)) + " 个："
                   + " / ".join(cand[:_SHORT_ID_CANDIDATES_SHOWN]) + "。给完整的。")
    return q, not_found(q)


async def resolve_bucket_ids(ids: list, label: str) -> tuple[list[str], str]:
    """Resolve every id in a list parameter (`from`, `folds`, `folds_append`).
    Returns (full ids, error); the first id that fails names the parameter and
    the handle, so the caller can tell which of several went wrong."""
    out: list[str] = []
    for raw in ids:
        full, err = await resolve_bucket_id(raw)
        if err:
            return list(ids), f"{label} 里 {str(raw).strip()} —— {err}"
        out.append(full)
    return out, ""


def _push_warning_safe(code: str, msg: str) -> None:
    """Call errors.push_warning safely; degrade silently if the import fails.

    Why: push_warning is called four times across the two quota helpers, and each
    call site would otherwise repeat the same three-layer try/except import dance.
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
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if number < 0:
        return default
    # 🔴 0 is a real setting here: it means "no ceiling". Which makes truncation
    #    dangerous in exactly one place — `int(0.5)` is 0, so a decimal typed where an
    #    integer was meant does not shrink the gate, it **removes** it, silently.
    #    Anything in (0, 1) is therefore treated as a mistake rather than as "off".
    if 0 < number < 1:
        return default
    try:
        return int(number)
    except (ValueError, OverflowError):
        return default


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
            # 🔴 `text` is the field that actually arrives — `rooms_path` reads
            #    `item["text"]`. Reading only `content` measured every dict item as
            #    0 bytes, so this ceiling never once fired on a real grow call, and it
            #    failed **open and silently**: no error, the batch just went in.
            #    `content` is not an alternative spelling either; it is the parameter
            #    that was withdrawn (see the tombstone comment in grow/__init__.py).
            #    It stays here only so anyone still calling the old way keeps working.
            value = item.get("text", item.get("content", ""))
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
    """Whether metadata represents an archived/deleted terminal memory — the gate's
    `state_of` (core/visibility.py), so a quota counts exactly what a road shows."""
    if not isinstance(metadata, dict):
        return False
    return state_of(metadata) != LIVE


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
