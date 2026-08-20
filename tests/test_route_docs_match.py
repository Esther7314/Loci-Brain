# -*- coding: utf-8 -*-
"""The list of routes in `web/loci.py`'s header must match the routes it registers.

WHY THIS FILE EXISTS
    That header used to open with "this file is read-only — the single write endpoint is
    the similarity page's sink action", and listed two POST routes. There were seven, and
    all seven write: one rewrites a memory into a new version, one edits the alias table
    in the data volume, and one sets the password guarding remote access.

    Nothing was broken. The sentence was true the day it was written, and then five routes
    were added below it over the following weeks. Prose does not notice that.

    🔴 The cost is specific, not cosmetic. The one person most likely to read that header
       is someone deciding whether it is safe to expose this port — and they would have
       come away wrong by a factor of seven, wrong in the direction of "safer than it is",
       and wrong specifically about the password endpoint.

    A list written in prose rots the moment something is added to the code beside it. The
    fix is not to write more carefully; it is to make the list checkable.

WHAT THIS DOES NOT CHECK
    Whether the descriptions are accurate — only that the set of paths and methods lines
    up. A wrong description still needs a person. But "a route exists and nobody wrote it
    down" is now impossible, and that was the failure that actually happened.
"""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "src" / "web" / "loci.py"
TEXT = SOURCE.read_text(encoding="utf-8")

DOCSTRING = TEXT.split('"""')[1] if TEXT.startswith('"""') else ""

# What the code actually registers.
REGISTERED = {
    (m.group(2), m.group(1))
    for m in re.finditer(r'custom_route\(\s*"([^"]+)"\s*,\s*methods=\[\s*"(\w+)"\s*\]', TEXT)
}

# What the header claims, from its `GET /x -> ...` / `POST /x -> ...` lines.
DOCUMENTED = {
    (m.group(1), m.group(2))
    for m in re.finditer(r'^\s*(GET|POST)\s+(/\S+)', DOCSTRING, re.M)
}


def _norm(path: str) -> str:
    """`/api/loci/bucket/{id}` and `/api/loci/bucket/{bucket_id}` are the same route."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _pairs(rows):
    return {(method, _norm(path)) for method, path in rows}


def test_the_parsers_actually_found_something():
    # Criterion: a test that silently checks two empty sets passes forever and means
    # nothing. Both sides have to be non-trivial before any comparison is worth making.
    assert len(REGISTERED) > 8, f"only found {len(REGISTERED)} registered routes — check the regex"
    assert len(DOCUMENTED) > 8, f"only found {len(DOCUMENTED)} documented routes — check the docstring format"


def test_every_registered_route_is_documented():
    # Criterion: THE failure that happened. Five routes were added and the header was not
    # touched, so the header quietly understated what the surface could do.
    missing = _pairs(REGISTERED) - _pairs(DOCUMENTED)
    assert not missing, (
        "these routes exist in the code but not in the header list:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in sorted(missing)))


def test_every_documented_route_still_exists():
    # Criterion: the other direction. A header that advertises a route which was deleted
    # sends people looking for something that is not there, and makes the whole list
    # untrustworthy — which is worse than having no list.
    gone = _pairs(DOCUMENTED) - _pairs(REGISTERED)
    assert not gone, (
        "these routes are in the header list but no longer registered:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in sorted(gone)))


def test_the_header_does_not_call_the_file_read_only():
    # Criterion: it registers seven POST routes, every one of which writes. Any sentence
    # calling this file read-only is false regardless of how the list beneath it looks —
    # and that sentence is the one that would mislead the person sizing up the surface.
    writes = {p for m, p in REGISTERED if m == "POST"}
    assert len(writes) >= 7, "the write surface shrank — update this test and the header together"

    head = DOCSTRING[:600].lower()
    assert "read-only" not in head.replace("read-only call", ""), \
        "the header opens by calling this file read-only, and it registers seven writing routes"
