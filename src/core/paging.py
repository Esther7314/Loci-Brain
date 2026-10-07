"""
========================================
core/paging.py — how every panel list pages: offset / limit / as_of
========================================

The panel contract (「面板接口」 §一.2) fixes one convention for every list, and this
module is its one reading:

    request   offset   from 0 (default 0)
              limit    1..PAGE_MAX (default PAGE_LIMIT); a larger one is held to PAGE_MAX
              as_of    the first page's `as_of`, sent back as it came; absent -> now, to
                       the second
    reply     {items, total, offset, limit, next_offset, as_of}; next_offset is None on
              the last page

Lists are newest first, and a row stamped after `as_of` is left out, so rows arriving
while someone pages do not shift the pages. Times are compared to the second, the
precision `as_of` is handed out in: a row of the first page's own second stays on every
page (`past`).

An argument that does not read raises `BadPage` (a ValueError): the route answers 400
with its words. An `as_of` whose offset "+" was sent unescaped arrives with a space in its
place ("…T21:04:11 08:00") and is read as the "+" it was.

Exports: PAGE_LIMIT · PAGE_MAX · BadPage · page_args · args_of · stamp · past · cut · page
========================================
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Iterable, Optional

from . import _when as _w

PAGE_LIMIT = 5
PAGE_MAX = 50

# "…T21:04:11 08:00" (or with a fraction): the offset's "+" arrived as a space.
_SPACED_OFFSET = re.compile(r"T(\d{2}:\d{2}:\d{2}(?:\.\d+)?) (\d{2}:\d{2})$")


class BadPage(ValueError):
    """A paging argument that does not read; its message is the words the 400 carries."""


def page_args(offset=None, limit=None, as_of=None,
              now: Optional[datetime] = None) -> tuple[int, int, datetime]:
    """(offset, limit, as_of) from a request's raw values. Absent or empty values take
    their defaults; `now` is the moment a first page is cut at (the clock when None).
    Raises BadPage for a non-integer offset or limit, an offset below 0, a limit below 1
    or an as_of that does not read."""
    try:
        off = int(offset or 0)
        lim = int(limit or PAGE_LIMIT)
    except (TypeError, ValueError):
        raise BadPage("offset 和 limit 要是整数") from None
    if off < 0 or lim < 1:
        raise BadPage("offset 不能小于 0，limit 至少 1")
    raw = _SPACED_OFFSET.sub(r"T\1+\2", str(as_of or "").strip())
    cut_at = _w.parse_stamp(raw) if raw else _w.to_local(now or _w.now())
    if cut_at is None:
        raise BadPage(f"as_of 读不懂：{raw}（照第一页回给你的原样带回来）")
    return off, min(lim, PAGE_MAX), cut_at.replace(microsecond=0)


def args_of(query, now: Optional[datetime] = None) -> tuple[int, int, datetime]:
    """`page_args` over a request's query parameters (anything with `.get`)."""
    return page_args(query.get("offset"), query.get("limit"), query.get("as_of"), now=now)


def stamp(dt: Optional[datetime]) -> Optional[str]:
    """A moment as the panel reads it: local, with its offset, to the second."""
    if dt is None:
        return None
    return _w.to_local(dt).isoformat(timespec="seconds")


def past(at: Optional[datetime], as_of: Optional[datetime]) -> bool:
    """Was `at` stamped after `as_of`? Compared to the second. A row with no moment, or
    no `as_of` at all, is never past."""
    return at is not None and as_of is not None and at.replace(microsecond=0) > as_of


def cut(rows: Iterable, as_of: Optional[datetime],
        at: Callable[[object], Optional[datetime]]) -> list:
    """The rows not stamped after `as_of`, in their order; `at(row)` reads a row's moment
    (None when it has none, and such a row is kept)."""
    return [r for r in rows if not past(at(r), as_of)]


def page(rows: list, offset: int, limit: int, as_of: Optional[datetime]) -> dict:
    """One page of rows already ordered newest first and cut at as_of."""
    total = len(rows)
    end = offset + limit
    return {"items": rows[offset:end], "total": total, "offset": offset, "limit": limit,
            "next_offset": end if end < total else None, "as_of": stamp(as_of)}
