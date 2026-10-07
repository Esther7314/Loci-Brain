"""
========================================
web/loci_pages.py — the panel page itself, its modules, and the three.js it loads
========================================

    GET  /loci                        -> frontend/loci.html, with the AI's name filled in
    GET  /loci/panel/{path:path}      -> the panel's ES modules and stylesheet (frontend/panel)
    GET  /loci/vendor/{path:path}     -> three.js, served locally, which the starfield page needs

The two asset routes are public (panel_auth._PUBLIC_PREFIXES): the login page is itself one
of the panel's modules, so they have to load before anyone is logged in. They hold code and
no data.
========================================
"""

import os

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh


async def loci_page(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    path = os.path.join(sh.repo_root, "frontend", "loci.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>loci.html not found</h1>", status_code=404)
    # The page hardcodes nobody's name: `{{AI_NAME}}` is filled in at serve time.
    # The shipped copy has to be blank, so that whoever clones it sees their own AI's
    # name rather than someone else's. The name comes from utils.get_ai_name() — the
    # AI_NAME environment variable, falling back to "AI".
    from utils import get_ai_name
    html = html.replace("{{AI_NAME}}", get_ai_name())
    return HTMLResponse(
        html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


_PANEL_TYPES = {".js": "text/javascript", ".css": "text/css"}


def _inside(root: str, rel: str) -> str | None:
    """The real path of `rel` under `root`, or None when it resolves outside it. No string
    from a request is trusted as a path: after realpath it must still be inside `root`."""
    root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


async def loci_panel_asset(request: Request) -> Response:
    """The panel's own code: frontend/panel/**, `.js` and `.css` only.

    Served no-cache, like the page: the modules import one another by plain relative
    paths with no version in them, so a cached module from before an update would run
    against the new ones. The same traversal rule as `loci_vendor`."""
    from starlette.responses import JSONResponse
    rel = str(request.path_params.get("path") or "")
    ext = os.path.splitext(rel)[1].lower()
    media = _PANEL_TYPES.get(ext)
    target = _inside(os.path.join(sh.repo_root, "frontend", "panel"), rel) if media else None
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        with open(target, "rb") as f:
            return Response(f.read(), media_type=media,
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except OSError:
        return JSONResponse({"error": "not found"}, status_code=404)


async def loci_vendor(request: Request) -> Response:
    """Locally served three.js, which the starfield page needs.

    The memory-starmap this was based on pulls three from a CDN at page load, so with no
    network it is just a black screen. The whole memory system runs on the user's own
    machine, and the starfield should not be the one place that breaks when the network
    does — so the library was vendored locally.

    Security: only .js is served, and no string from the request is ever concatenated
    into a path directly. After realpath it must still be inside the vendor directory,
    or this becomes the ?path=../../../etc/passwd kind of traversal.
    """
    from starlette.responses import Response as _Resp, JSONResponse
    rel = str(request.path_params.get("path") or "")
    if not rel.endswith(".js"):
        return JSONResponse({"error": "not found"}, status_code=404)
    root = os.path.realpath(os.path.join(sh.repo_root, "frontend", "vendor"))
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        with open(target, "rb") as f:
            return _Resp(f.read(), media_type="text/javascript",
                         headers={"Cache-Control": "public, max-age=604800"})
    except OSError:
        return JSONResponse({"error": "not found"}, status_code=404)
