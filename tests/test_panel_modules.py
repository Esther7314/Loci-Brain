# -*- coding: utf-8 -*-
"""The panel's modules must fit together, and their route must serve them and nothing else.

WHY THIS FILE EXISTS
    The panel is plain ES modules with no build step (frontend/panel; frontend/loci.html is
    only the shell). Nothing compiles them, so a module importing a name another module
    no longer exports, or a file that moved, breaks only in a browser: the page stays blank
    and says nothing. That is the panel's version of the dangling lookup — one half of a
    rename left behind — and a static pass catches it without a browser:

      · the shell loads the entry module and the stylesheet, and both exist;
      · every relative import (static or `import()`) names a file that exists;
      · every `{ name }` imported from a sibling module is exported by it.

    The route that serves them (GET /loci/panel/{path}) is public, because the login page
    is one of the modules. So it must serve .js and .css from frontend/panel and nothing
    else: no other extension, nothing outside the folder.

WHAT IT CANNOT SEE
    Whether the code runs. A clean pass means "the pieces are where the imports say",
    not "the page works"; scripts/dev_panel.py is for looking at it.
"""
import asyncio
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELL = ROOT / "frontend" / "loci.html"
PANEL = ROOT / "frontend" / "panel"
MODULES = sorted(PANEL.rglob("*.js"))

STATIC = re.compile(r'^\s*import\s+(?:(\{[^}]*\})|\*\s+as\s+\w+|\w+)?\s*(?:from\s+)?["\'](\.{1,2}/[^"\']+)["\']', re.M)
DYNAMIC = re.compile(r'import\(\s*["\'](\.{1,2}/[^"\']+)["\']\s*\)')
EXPORTS = re.compile(r'^\s*export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([\w$]+)', re.M)


def _exports(path: Path) -> set:
    text = path.read_text(encoding="utf-8")
    names = set(EXPORTS.findall(text))
    if re.search(r'^\s*export\s+default\b', text, re.M):
        names.add("default")
    return names


def test_the_shell_loads_the_entry_and_the_stylesheet():
    html = SHELL.read_text(encoding="utf-8")
    assert '<script type="module" src="/loci/panel/app.js">' in html
    assert 'href="/loci/panel/panel.css"' in html
    assert (PANEL / "app.js").is_file() and (PANEL / "panel.css").is_file()
    assert "{{AI_NAME}}" in html, "the server fills the AI's name into the shell"


def test_there_are_modules_to_check():
    # A pass over an empty folder would report success and mean nothing.
    names = {p.relative_to(PANEL).as_posix() for p in MODULES}
    assert {"app.js", "router.js", "api.js", "ui.js", "detail.js"} <= names
    assert any(n.startswith("pages/") for n in names)


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.relative_to(PANEL).as_posix())
def test_every_import_resolves_and_every_imported_name_is_exported(module):
    text = module.read_text(encoding="utf-8")
    wrong = []
    for names, rel in STATIC.findall(text):
        target = (module.parent / rel).resolve()
        if not target.is_file():
            wrong.append(f"imports {rel}: no such file")
            continue
        if names:
            wanted = {n.split(" as ")[0].strip() for n in names.strip("{}").split(",") if n.strip()}
            missing = wanted - _exports(target)
            if missing:
                wrong.append(f"imports {sorted(missing)} from {rel}, which does not export them")
    for rel in DYNAMIC.findall(text):
        target = (module.parent / rel).resolve()
        if not target.is_file():
            wrong.append(f"import({rel}): no such file")
        elif "default" not in _exports(target):
            wrong.append(f"import({rel}): no default export")
    assert not wrong, "\n".join(wrong)


# ── the route ────────────────────────────────────────────────────────────────

def _serve(path: str):
    from starlette.requests import Request
    from web import _shared, loci_pages
    req = Request({"type": "http", "method": "GET", "path": f"/loci/panel/{path}", "headers": [],
                   "query_string": b"", "path_params": {"path": path}})
    before = _shared.repo_root
    _shared.repo_root = str(ROOT)        # server.py injects it at startup
    try:
        return asyncio.run(loci_pages.loci_panel_asset(req))
    finally:
        _shared.repo_root = before


def test_the_route_serves_the_modules_and_the_stylesheet_uncached():
    js = _serve("app.js")
    assert js.status_code == 200 and js.media_type == "text/javascript"
    assert js.body == (PANEL / "app.js").read_bytes()
    assert "no-cache" in js.headers["cache-control"]
    css = _serve("panel.css")
    assert css.status_code == 200 and css.media_type == "text/css"
    assert _serve("pages/breath.js").status_code == 200


@pytest.mark.parametrize("path", [
    "../loci.html", "../../src/server.py", "..\\..\\README.md", "../../gateway/auto_attach.js",
    "missing.js", "app.js.map", "panel.css.bak", "", "pages",
])
def test_the_route_serves_nothing_else(path):
    assert _serve(path).status_code == 404


def test_the_modules_are_public_and_registered():
    from web import loci, panel_auth
    assert panel_auth.is_public("/loci/panel/app.js")
    assert not panel_auth.is_public("/loci/panelx")
    assert '"/loci/panel/{path:path}"' in Path(loci.__file__).read_text(encoding="utf-8")
