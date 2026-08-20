"""
========================================
bridge/ — the outward-facing layer (extracted and regrouped)
========================================

This package holds the few things where the core engine talks to the outside world.
It is **not** the control panel and it is **not** the MCP tool surface: it is how a
remote MCP client proves who it is (``oauth.py``), how large a request body ``/mcp``
itself will accept (``request_limits.py``), and how the local Ollama child process is
kept alive alongside the server (``ollama_child.py``).

All three were moved out of ``web/``. They used to sit in the same files as a pile of
panel routes, but what they actually govern is *people and processes outside the box*,
which is a different concern from the panel UI. When the twenty upstream modules were
dropped (live store untouched, verified once in a throwaway container), these three
were lifted out of their original files and kept alive on their own:

- ``oauth.py``: the full OAuth 2.1 remote-auth flow for ``/mcp`` (discovery ->
  authorization page -> token exchange). "Auth is on by default" is a settled position
  for the open-source build; a deployment may switch it off in config, but the
  mechanism must not die along with the panel. ``_is_valid_mcp_token`` and
  ``_is_valid_static_mcp_token`` are the two validators that ``server.py`` imports
  directly for its startup-time MCP auth middleware.
- ``request_limits.py``: the body-size guard for ``/mcp`` and ``/api/*``. ``server_app.py``
  mounts it as middleware while assembling the HTTP app; it is not something any panel
  button can reach.
- ``ollama_child.py``: the docs state flatly that local embedding requires a local
  Ollama, so this is the machinery that starts that child process for the user and
  watches it (``server.py``'s lifespan calls ``ensure_child_on_boot`` / ``stop_child``).
  **The panel-side "one-click install wizard" half was cut** — downloading, verifying
  and unpacking an Ollama release could only ever be triggered by a panel button, so it
  became unreachable once the panel went. The keep-the-child-running half stays: a
  standalone-container deployment does not need it, but the mechanism has to remain.

Public surface: documented in each module's own docstring rather than repeated here.
========================================
"""
