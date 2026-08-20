# -*- coding: utf-8 -*-
"""Every `$("#id")` in the panel must point at an element that exists.

WHY THIS FILE EXISTS
    The panel is one 2800-line HTML file with no test coverage of any kind, and it had a
    live bug of exactly this shape for weeks:

        an element was deliberately removed from the markup — the explanation line under
        the password field, dropped once the panel had its own lock back and the password
        no longer needed explaining — and the JavaScript that filled it was left behind.

    `document.querySelector("#pwWhat")` returns `null`, and `null.textContent = x` throws.
    The throw landed in the surrounding `catch`, which wrote **"cannot read the current
    config"** into the settings page. So every single load of that page reported a failure
    to read a config that had been read perfectly.

    🔴 The cost is not the crash. Nothing crashed. The cost is that the interface said
       something untrue about its own state, which is worse than an honest failure —
       there is nothing to go and fix, and the real message is now noise.

    This test is cheap, it needs no browser, and it turns "somebody has to remember to
    delete both halves" into something a machine checks.

WHAT IT CANNOT SEE
    Ids created at runtime (`el.id = ...`, template strings). Those are collected too,
    but a lookup built from a variable is invisible to any static pass. So a clean run
    means "no dangling literal lookup", not "every lookup resolves".
"""
import re
from pathlib import Path

import pytest

PANEL = Path(__file__).resolve().parent.parent / "frontend" / "loci.html"
SRC = PANEL.read_text(encoding="utf-8")

# `$("#thing")` — the panel's own one-character helper for querySelector.
LOOKUPS = [(m.start(), m.group(1))
           for m in re.finditer(r'\$\(\s*["\']#([\w-]+)["\']\s*\)', SRC)]

DECLARED = (set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']', SRC))
            | set(re.findall(r'\.id\s*=\s*["\']([^"\']+)["\']', SRC))
            | set(re.findall(r'id\s*:\s*["\']([^"\']+)["\']', SRC)))


def _line(pos: int) -> int:
    return SRC.count("\n", 0, pos) + 1


def test_the_panel_is_where_this_test_thinks_it_is():
    # Criterion: if the file ever moves, this suite must fail loudly rather than pass by
    # finding nothing to check. A test that silently checks an empty set is worse than no
    # test, because it reports success.
    assert PANEL.is_file()
    assert len(LOOKUPS) > 50, "found almost no lookups — the helper was probably renamed"
    assert len(DECLARED) > 50, "found almost no ids — the markup probably moved"


def test_no_lookup_points_at_an_element_that_does_not_exist():
    # Criterion: THE assertion. One dangling lookup is enough to make a whole handler
    # report the wrong thing, because these lookups sit inside try/catch blocks that
    # attribute any throw to whatever the block was nominally doing.
    dangling = sorted({(_line(pos), name) for pos, name in LOOKUPS if name not in DECLARED})
    assert not dangling, "\n".join(
        f'loci.html:{ln}  $("#{name}") — no element with this id'
        for ln, name in dangling)


@pytest.mark.parametrize("element_id", ["pwState", "pwMsg", "cfgSaveMsg"])
def test_the_elements_from_the_incident_are_all_still_there(element_id):
    # Criterion: names the three the bug actually involved. If one of them is removed
    # later, the failure should point straight at this history instead of at a generic
    # dangling-id message.
    assert element_id in DECLARED
