"""
========================================
web/loci_version.py — the setting page's version block
========================================

    GET /api/loci/version            the version that runs, and where releases are published
    GET /api/loci/version?check=1    the same, plus the latest release on GitHub against it
                                     (core/releases.py): tag, day, notes, page, whether it is
                                     newer, and a sentence saying so; a failed check is a
                                     reply saying it could not check, with status 200

Read only: nothing is downloaded or installed and no command runs. Registered in
web/loci.py, behind the panel gate.

Exports: api_loci_version
========================================
"""

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh


def _running() -> str:
    from utils import get_version
    return str(getattr(sh, "version", "") or get_version())


async def api_loci_version(request: Request) -> Response:
    from core import releases
    running = _running()
    out = {"version": running, "repo": releases.REPO, "releases_url": releases.RELEASES_PAGE}
    if str(request.query_params.get("check") or "") in ("1", "true", "yes"):
        out["check"] = await releases.check(running)
    return JSONResponse(out)
