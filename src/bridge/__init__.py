"""
========================================
bridge/ — the outward-facing layer
========================================

This package holds the few things where the core engine talks to the outside world.
It is **not** the control panel and it is **not** the MCP tool surface: it is how a
remote MCP client proves who it is (``oauth.py``), which public origin it is checked
against (``public_origin.py``), how large a request body ``/mcp`` itself will accept
(``request_limits.py``), and how the local Ollama child process is kept alive alongside
the server (``ollama_child.py``).

None of the four is in ``web/``: what they govern is *people and processes outside the
box*, which is a different concern from the panel UI.

- ``oauth.py``: the full OAuth 2.1 remote-auth flow for ``/mcp`` (discovery ->
  authorization page -> token exchange). "Auth is on by default" is a settled position
  for the open-source build; a deployment may switch it off in config, but the
  mechanism must not die along with the panel. ``_is_valid_mcp_token`` and
  ``_is_valid_static_mcp_token`` are the two validators that ``server.py`` imports
  directly for its startup-time MCP auth middleware.
- ``public_origin.py``: the one parser of the externally visible origin. OAuth grants are
  bound to it and ``server_app.py``'s MCP auth middleware validates against the same
  value; the panel's settings (``web/config_api.py``, ``web/deployment_profile.py``)
  parse it here too.
- ``request_limits.py``: the body-size guard for ``/mcp`` and ``/api/*``. ``server_app.py``
  mounts it as middleware while assembling the HTTP app; it is not something any panel
  button can reach.
- ``ollama_child.py``: the docs state flatly that local embedding requires a local
  Ollama, so this is the machinery that starts that child process for the user and
  watches it (``server.py``'s lifespan calls ``ensure_child_on_boot`` / ``stop_child``).
  It does not download or install Ollama. A standalone-container deployment does not
  need it, but the mechanism has to remain.

Public surface: documented in each module's own docstring rather than repeated here.
========================================
"""
