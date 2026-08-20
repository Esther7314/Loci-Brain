"""
========================================
tools/_runtime.py — the runtime context shared by every tool module
========================================

This file solves one engineering problem: after the split, every tool submodule
needs access to config / bucket_mgr / dehydrator / decay_engine /
embedding_engine / logger — the global objects server.py creates — but a
submodule must not import server.py back (that would be a circular import).

The approach: once server.py has initialised every component it calls init(...)
to push the references in; every tool module then does
`from . import _runtime as rt` and reads `rt.bucket_mgr`.

Key behaviour:
- A lightweight container holding references to the shared objects
- init() writes once; tool modules only read afterwards, never write

What this file deliberately does not do:
- Creates no objects, loads no configuration, initialises no logging
- No thread-safety guards: the write happens once, during server.py startup

Exports: init() / config / bucket_mgr / dehydrator / decay_engine /
         embedding_engine / import_engine / logger / fire_webhook / mark_op
========================================
"""

from typing import Any, Awaitable, Callable, Optional

from locibrain.app.execution import ExecutionEnvelope

# --- Shared object references, injected by server.py at startup via init(...) ---
config: Any = None
bucket_mgr: Any = None
dehydrator: Any = None
decay_engine: Any = None
embedding_engine: Any = None
embedding_outbox: Any = None
import_engine: Any = None
logger: Any = None
v3_runtime: Any = None

# --- Shared helper callbacks (also injected by server.py, to avoid a back-import) ---
fire_webhook: Optional[Callable[[str, dict], Awaitable[None]]] = None
mark_op: Optional[Callable[..., None]] = None


def init(**kwargs: Any) -> None:
    """Called once by server.py, after every component exists, to write the
    references onto this module's globals.
    A test fixture may call this again to override individual fields; the effect
    is the same as monkeypatching."""
    g = globals()
    defaults = g.get("_DEFAULT_RUNTIME_HELPERS")
    if isinstance(defaults, dict):
        for name, fn in defaults.items():
            g[name] = fn
    for k, v in kwargs.items():
        g[k] = v


def _warn(message: str, *args: Any) -> None:
    log = globals().get("logger")
    warning = getattr(log, "warning", None)
    if callable(warning):
        try:
            warning(message, *args)
        except Exception:
            pass


def run_v3_capability(
    name: str,
    payload: Any,
    *,
    permissions: tuple[str, ...] = (),
    actor_name: str = "legacy-tool",
    source: str = "tools",
) -> Any:
    """Best-effort dispatch into the v3 capability registry."""
    runtime = globals().get("v3_runtime")
    dispatch = getattr(runtime, "dispatch_capability", None)
    if not callable(dispatch):
        return None
    try:
        return dispatch(
            name,
            payload,
            permissions=tuple(permissions),
            actor_name=actor_name,
            source=source,
        )
    except Exception as exc:
        _warn("v3 capability dispatch failed for %s: %s", name, exc)
        return None


def record_v3_tool_event(tool_name: str, payload: dict[str, Any] | None = None) -> Any:
    """Record a legacy tool call in the v3 side channel without affecting output."""
    runtime = globals().get("v3_runtime")
    recorder = getattr(runtime, "record_tool_event", None)
    if not callable(recorder):
        return None

    name = str(tool_name)
    event_payload = dict(payload or {})
    planner = getattr(runtime, "plan_legacy_command", None)
    if callable(planner):
        try:
            envelope = ExecutionEnvelope(
                module=f"tools.{name}",
                operation=name,
                payload=event_payload,
                actor_name="legacy-tool",
                source="tools.record_v3_tool_event",
                permissions=("mcp:call",),
            )
            plan = planner(envelope)
            event_payload["command_plan"] = (
                plan.to_dict() if hasattr(plan, "to_dict") else plan
            )
        except Exception as exc:
            _warn("v3 tool command planning failed for %s: %s", name, exc)
    try:
        return recorder(name, event_payload)
    except Exception as exc:
        _warn("v3 tool event record failed for %s: %s", name, exc)
        return None


def run_v3_operation(
    operation: str,
    payload: dict[str, Any] | None,
    handler: Callable[[], Any],
    *,
    module: str,
    permissions: tuple[str, ...] = (),
    required_permissions: tuple[str, ...] = (),
    actor_name: str = "legacy-tool",
    source: str = "tools",
    capability: str = "",
    writes_memory: bool = False,
    protected_paths: tuple[str, ...] = (),
    feature_flags: tuple[str, ...] = (),
) -> Any:
    runtime = globals().get("v3_runtime")
    runner = getattr(runtime, "run_operation", None)
    if not callable(runner):
        return handler()
    envelope = ExecutionEnvelope(
        module=module,
        operation=operation,
        payload=payload or {},
        actor_name=actor_name,
        source=source,
        permissions=permissions,
        required_permissions=required_permissions,
        capability=capability,
        writes_memory=writes_memory,
        protected_paths=protected_paths,
        feature_flags=feature_flags,
    )
    return runner(envelope, handler)


async def run_v3_async_operation(
    operation: str,
    payload: dict[str, Any] | None,
    handler: Callable[[], Awaitable[Any]],
    *,
    module: str,
    permissions: tuple[str, ...] = (),
    required_permissions: tuple[str, ...] = (),
    actor_name: str = "legacy-tool",
    source: str = "tools",
    capability: str = "",
    writes_memory: bool = False,
    protected_paths: tuple[str, ...] = (),
    feature_flags: tuple[str, ...] = (),
) -> Any:
    runtime = globals().get("v3_runtime")
    runner = getattr(runtime, "run_async_operation", None)
    if not callable(runner):
        return await handler()
    envelope = ExecutionEnvelope(
        module=module,
        operation=operation,
        payload=payload or {},
        actor_name=actor_name,
        source=source,
        permissions=permissions,
        required_permissions=required_permissions,
        capability=capability,
        writes_memory=writes_memory,
        protected_paths=protected_paths,
        feature_flags=feature_flags,
    )
    return await runner(envelope, handler)


_DEFAULT_RUNTIME_HELPERS = {
    "run_v3_capability": run_v3_capability,
    "record_v3_tool_event": record_v3_tool_event,
    "run_v3_operation": run_v3_operation,
    "run_v3_async_operation": run_v3_async_operation,
}
