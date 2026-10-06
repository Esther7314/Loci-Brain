"""
========================================
web/ — the panel's HTTP route layer (only what this one screen needs)
========================================

What is not really "panel" (remote MCP OAuth validation, the ``/mcp`` body-size guard,
keeping the local Ollama child process up) lives in ``bridge/`` instead.

**The route modules**:
- ``config_api``: engine settings (``/api/config`` GET+POST, ``/api/test/dehydration``,
  ``/api/test/embedding``, ``/api/models``).
- ``import_api``: the import routes (preflight / upload / status / batches / pause /
  withdraw): an export stored as a source, drafted, withdrawn whole.
- ``loci``: the current panel itself — the four rooms, the breath/recall preview, the
  archive, musing, password setup. Its handlers live in ``loci_*``, ``host_api`` and
  ``library_api``; ``loci.register`` assembles them.

Shared dependencies (config, password and login rate-limit helpers) live in
``web/_shared.py`` (the counterpart of ``core/runtime.py``).

Note that ``_shared.py`` carries no **cookie sessions or authentication** — the panel's
``/api/*`` routes are not authenticated at that layer; the gate lives in
``panel_auth`` and is wrapped on at registration time (see ``_Gated`` below). The password
primitives left in ``_shared`` serve the MCP remote-OAuth authorization page in
``bridge/oauth.py``, which is a separate concern.

Public surface: ``register_all(mcp)`` — registers every web route module.
========================================
"""

from . import _shared
from . import config_api
from . import import_api
from . import loci
from . import panel_auth


_WEB_MODULES = (
    # The gate registers first: its own four routes are on the allowlist, so it cannot
    # lock itself out.
    ("web.panel_auth", panel_auth.register),
    ("web.config_api", config_api.register),
    ("web.loci", loci.register),
    # The import routes (core/import_memory.py behind them).
    ("web.import_api", import_api.register),
)


class _Gated:
    """Wrap `mcp` so that **every** web route automatically sits behind the panel gate.

    Why the wrapping happens here, at registration, rather than a line added inside each
    route: there are twenty-odd routes, and relying on a human to remember the line means
    one gets missed eventually — and the missed one does not raise. It is just **quietly
    unprotected**. That failure mode has shown up too many times to leave to memory.
    Wrapping once here means a newly added route is inside the gate by default, unless it
    is explicitly written onto the allowlist.

    The allowlist has only two kinds of entry (see panel_auth.PUBLIC_PATHS): what the gate
    itself needs, and routes whose caller is not a browser (the bridge is a separate
    process; it has no cookies).
    """

    def __init__(self, mcp):
        self._mcp = mcp

    def __getattr__(self, name):
        return getattr(self._mcp, name)

    def custom_route(self, path, methods=None, **kw):
        inner = self._mcp.custom_route(path, methods=methods, **kw)
        if panel_auth.is_public(path):
            return inner

        # The bridge-facing routes: no cookie required (the bridge has none), but a key is
        # required whenever the gate is locked. The reasoning is written above
        # panel_auth.HOOK_PATHS. Who is calling (a host, or the panel) and
        # what that request may read is resolved here once and set for the call
        # (core/scope.request_scope); each route decides what a refused or scoped request
        # gets.
        if panel_auth.is_hook(path):
            def hook_deco(fn):
                import functools
                from starlette.responses import JSONResponse
                from core import scope as _scope

                @functools.wraps(fn)
                async def hook_guarded(request):
                    ok, why, caller = panel_auth.hook_caller(request)
                    if not ok:
                        return JSONResponse({"error": why}, status_code=401)
                    req = panel_auth.request_scope_of(request, caller)
                    request.state.loci_request = req
                    with _scope.request_scope(req):
                        return await fn(request)

                return inner(hook_guarded)

            return hook_deco

        def deco(fn):
            import functools
            from starlette.responses import JSONResponse

            @functools.wraps(fn)
            async def guarded(request):
                # 401 is the **agreed signal**: the front-end pops the gate open when it
                # sees one (`if (r.status === 401){ openGate(); }` in the page), so this
                # is what makes that line fire. A `hosts:` table adds two refusals (a host's
                # credential is never the panel; an unlocked panel stays shut), both in
                # panel_auth.panel_refusal.
                refused = panel_auth.panel_refusal(request)
                if refused is not None:
                    status, why = refused
                    return JSONResponse({"error": why}, status_code=status)
                return await fn(request)

            return inner(guarded)

        return deco


def register_all(mcp) -> None:
    """Register every route module migrated into web/. One more line per module."""
    gated = _Gated(mcp)
    for _name, register in _WEB_MODULES:
        register(gated)
