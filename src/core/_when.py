# -*- coding: utf-8 -*-
"""
tools/_when.py — one definition of "today", and it is the local-calendar one.

**The problem**: the container has no TZ set, so `datetime.now()` returns UTC, while
the person reading the memories lives at +08. At 2 a.m. local it is still **6 p.m. the
previous day** inside the container — "today / yesterday / this week / this month" are
all shifted by eight hours. Anything written after midnight is missing from "today"
the following day, which for anyone who works late is a collision every single day.
That is exactly how it was found: something stored just after 1 a.m., gone by morning.

**Three conventions, do not mix them** (mixing is worse than leaving it broken — it
shifts the entire history by eight hours):

| field | what is on disk | how to read it |
|---|---|---|
| `created` / `last_active` | no suffix (`utils.now_iso()`: naive UTC on every machine) | **as UTC**, then converted to local |
| anything carrying `Z` / `+08:00` | says its own timezone | as it says |
| `when` as a bare date `YYYY-MM-DD` | that is "which day", not "which instant" | **as a local calendar day**, never as UTC |

⚠️ **The write side must stay UTC while it omits the suffix** (`utils.now_iso` asks for
UTC by name, so a host running at +08 writes the same stamp as the UTC container).
**If it ever switches to local time while still omitting the suffix, everything here
becomes wrong.** If that change is made, the write side must start
emitting the `+08:00` suffix in the same commit — with a suffix present, this side
can tell.

Exports: `LOCAL_TZ` · `now()` · `today()` · `parse_stamp()` · `parse_date()` · `to_local()`
"""

import os
import re
from datetime import datetime, timedelta, timezone

_TZ_NAME = os.environ.get("LOCI_TZ", "").strip() or "Asia/Shanghai"

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(_TZ_NAME)
except Exception:      # no tzdata in the image: fall back to a fixed +8 (China has had no DST since 1991)
    LOCAL_TZ = timezone(timedelta(hours=8), "UTC+8")

UTC = timezone.utc

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEADING_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def now() -> datetime:
    """Now: timezone-aware, local."""
    return datetime.now(LOCAL_TZ)


def today() -> datetime:
    """Midnight at the start of today, on the local calendar."""
    return now().replace(hour=0, minute=0, second=0, microsecond=0)


def to_local(dt: datetime) -> datetime:
    """Any datetime -> local aware. A naive one is always read as UTC (see the table above)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).astimezone(LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def parse_date(s: str) -> datetime:
    """`YYYY-MM-DD` -> midnight of that local day. **A date is a calendar, not an instant.**"""
    return datetime.fromisoformat(s[:10]).replace(tzinfo=LOCAL_TZ)


def parse_date_or_none(s) -> datetime | None:
    """The forgiving version of `parse_date`: unreadable input yields None, never raises.

    🔴 A whole family of bugs, all caught by the very first unit-test run, is contained
       here. The root cause: the regex `\\d{4}-\\d{2}-\\d{2}` only checks whether the
       string **looks like** a date, not whether that day **exists**. `2026-09-31` /
       `2026-13-45` sail straight through and only blow up down in `fromisoformat` —
       by which point we are already inside recall's loop.
    ⚠️ `parse_date` itself is **deliberately left strict**: its contract is "hand me a
       valid date string". Four of its seven call sites are immediately followed by
       `+ timedelta(days=1)`, so widening its return type would force each of those to
       invent its own answer to "what if there is no date". **Callers who want
       forgiveness ask for it by name.**
    """
    try:
        return parse_date(s)
    except (ValueError, TypeError):
        return None


def parse_stamp(value) -> datetime | None:
    """Read a stored timestamp string as a local aware datetime; None if unreadable.

    ⚠️ Never slice this with something like `s[:19]` — that lops off `Z` and `+08:00`
    along with everything after, turning a timestamp that stated its timezone into one
    that no longer does.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    if _DATE_ONLY.match(s):
        # This used to call parse_date bare — so `2026-09-31` (September has no 31st)
        # would **raise**, while the first line of this function's docstring promises
        # "None if unreadable". Worse, every caller upstream is written against
        # "None = this one has no time" and not one of them wraps it in a try: a single
        # bucket like that does not quietly drop out of the timeline, it **capsizes the
        # entire recall**. (The `_LEADING_DATE` branch below has always been wrapped —
        # one function, two tempers.)
        return parse_date_or_none(s)

    # ISO 8601: fromisoformat on Python 3.11+ accepts Z, and +08:00, and microseconds
    try:
        return to_local(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        pass

    # Prose hanging off the front (e.g. "2026-07-15, the day when..."): take only the leading date
    m = _LEADING_DATE.match(s)
    if m:
        try:
            return parse_date(m.group(0))
        except ValueError:
            return None
    return None


def year_week(dt: datetime) -> tuple[int, int]:
    """Calendar week (ISO). Used to cut the "one line per week" stretch."""
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def year_month(dt: datetime) -> tuple[int, int]:
    """Calendar month. Used to cut the "one line per month" stretch."""
    return (dt.year, dt.month)
