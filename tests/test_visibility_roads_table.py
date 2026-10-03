# -*- coding: utf-8 -*-
"""
tests/test_visibility_roads_table.py — every road of the gate keeps off exactly what its
row of the table says, column by column.

core/visibility.py states the roads as a table (its module docstring) and builds them as
`ROADS`. This file holds the same table as the contract, feeds every road one entry per
column — a deliberate dont_surface, an old version, a covered entry, an entry under a live
`defer` and under a live `avoid` hold, a hold itself, an open entry due later today — and
checks the road keeps it off exactly when its column is ticked. The docstring table is read
back too, so the table a reader trusts cannot drift from the one the code runs.
"""

from datetime import datetime

import pytest

from core import _holds as H
from core import _when as W
from core import visibility as V

NOW = datetime(2026, 10, 1, 10, 0, 0, tzinfo=W.LOCAL_TZ)

# road: (kind, dont_surface, old version, covered, holds, hold entry, later today)
TABLE = {
    V.PROSPECTIVE: (V.SURFACE, True, True, False, {"defer", "avoid"}, True, True),
    V.REVIEW: (V.SURFACE, True, True, False, {"avoid"}, False, True),
    V.RECENT: (V.SURFACE, False, True, False, set(), False, True),
    V.SUDDEN: (V.SURFACE, True, True, True, {"defer", "avoid"}, True, True),
    V.EDITED: (V.SURFACE, False, False, True, set(), False, True),
    V.INVALIDATION: (V.SURFACE, False, True, False, set(), False, True),
    V.DOOR: (V.SURFACE, False, False, True, set(), False, False),
    V.REMIND: (V.SURFACE, True, True, False, {"defer", "avoid"}, True, False),
    V.MUSE: (V.SURFACE, False, False, False, set(), False, False),
    V.DREAM: (V.SURFACE, True, True, False, {"avoid"}, True, False),
    V.DREAM_HANDOUT: (V.SURFACE, True, False, False, {"avoid"}, False, False),
    V.CUE: (V.SURFACE, True, True, False, {"defer", "avoid"}, False, False),
    V.RECONSOLIDATION: (V.SURFACE, True, True, True, {"defer", "avoid"}, True, False),
    V.CASE_RECALL: (V.SURFACE, True, True, False, {"avoid"}, True, False),
    V.LIST: (V.LOOKUP, False, True, False, set(), False, False),
    V.READ: (V.LOOKUP, False, False, False, set(), False, False),
}

TARGET = {"id": "t" * 12, "room": "EVENT/SELF", "created": "2026-09-30T10:00:00"}


def _hold(level: str) -> dict:
    return {"id": f"{level[0]}" * 12, "room": "EVENT/SELF", "exception_of": TARGET["id"],
            "hold": level, "direction_of_fit": "telic", "created": "2026-09-30T10:00:00"}


def _verdict(road: str, meta: dict, *, holds=(), covered=None) -> V.Verdict:
    return V.visible_for(meta, road=road, now=NOW, holds=H.hold_index(list(holds)),
                         covered=covered)


@pytest.mark.parametrize("road", list(TABLE))
def test_each_road_keeps_off_what_its_row_says(road):
    kind, faded, old, covered, holds, hold_entry, later = TABLE[road]
    assert V.ROADS[road].kind == kind
    assert _verdict(road, TARGET, covered=False).shown, "a plain live entry is shown"
    got = {
        "dont_surface": V.DONT_SURFACE in _verdict(road, {**TARGET, "dont_surface": True},
                                                   covered=False).reasons,
        "old version": V.SUPERSEDED in _verdict(road, {**TARGET, "superseded_by": "n" * 12},
                                                covered=False).reasons,
        "covered": V.COVERED in _verdict(road, TARGET, covered=True).reasons,
        "defer": V.HELD in _verdict(road, TARGET, holds=[TARGET, _hold("defer")],
                                    covered=False).reasons,
        "avoid": V.HELD in _verdict(road, TARGET, holds=[TARGET, _hold("avoid")],
                                    covered=False).reasons,
        "hold entry": V.HOLD_ENTRY in _verdict(road, _hold("defer"), covered=False).reasons,
        "later today": V.LATER_TODAY in _verdict(
            road, {**TARGET, "when": "2026-10-01T19:00+08:00"}, covered=False).reasons,
    }
    want = {"dont_surface": faded, "old version": old, "covered": covered,
            "defer": "defer" in holds, "avoid": "avoid" in holds, "hold entry": hold_entry,
            "later today": later}
    assert got == want, road


def test_every_road_is_in_the_table():
    assert set(V.ROADS) == set(TABLE)


def _docstring_rows() -> dict:
    rows = {}
    for line in (V.__doc__ or "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or len(cells) < 8 or cells[0] in ("road", ""):
            continue
        if set(cells[0]) <= {"-"}:
            continue
        rows[cells[0]] = cells
    return rows


def test_the_docstring_table_is_the_table_the_code_runs():
    rows = _docstring_rows()
    assert set(rows) == set(TABLE) - {V.READ}, "read has its own wording in the table"
    for road, cells in rows.items():
        kind, faded, old, covered, holds, hold_entry, later = TABLE[road]
        written = {h.strip() for h in cells[5].split(",") if h.strip()}
        assert (cells[1], cells[2] == "✔", cells[3] == "✔", cells[4] == "✔", written,
                cells[6] == "✔", cells[7] == "✔") == (kind, faded, old, covered, holds,
                                                       hold_entry, later), road
