# -*- coding: utf-8 -*-
"""
tools/recall/_when_words.py — the `when` words recall accepts

_parse_when turns 48h / 7d / 今天 / 本周 / 2026-07 / 起..止 (and the English spellings)
into a span on the local calendar, or a plain error naming what it does understand.

tools/recall/core.py re-exports _parse_when.
"""

import re
from datetime import datetime, timedelta

from core import _when as _w          # "today" as the user lives it (local timezone) — never call datetime.now() directly


# ------------------------------------------------------------
# Parsing when: plain-language time scales (calendar scales are computed
# automatically; life-scale expressions wait on anchors)
# ------------------------------------------------------------

def _parse_when(when: str) -> tuple[datetime | None, datetime | None, str]:
    """Returns (start, end, error). An empty string = no time filter.

    ⚠️ Everything here goes through `core/_when` (local timezone), never the
    container's `datetime.now()`, which is UTC — ask for 「今天」 at 2 a.m. and the
    container would answer with the previous afternoon.
    """
    w = when.strip()
    if not w:
        return None, None, ""
    now = _w.now()
    today = _w.today()

    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hd])", w)
    if m:
        n = float(m.group(1))
        delta = timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)
        return now - delta, now, ""

    # Each stretch is defined once and then given every spelling that means it.
    #
    # 🔴 English spellings were added because the tool description, the README and every
    #    comment around them are in English while this table was not — so the very first
    #    thing a new reader reaches for, `when="today"`, failed. The tool said one thing
    #    and did another, which is the fault this whole file is careful about elsewhere.
    #    Nothing was taken away: every Chinese spelling still resolves exactly as before.
    _ranges = {
        "today": (today, now),
        "yesterday": (today - timedelta(days=1), today),
        "day before yesterday": (today - timedelta(days=2), today - timedelta(days=1)),
        "this week": (today - timedelta(days=today.weekday()), now),
        "last week": (today - timedelta(days=today.weekday() + 7),
                      today - timedelta(days=today.weekday())),
        "this month": (today.replace(day=1), now),
        "last month": ((today.replace(day=1) - timedelta(days=1)).replace(day=1),
                       today.replace(day=1)),
        "this year": (today.replace(month=1, day=1), now),
    }
    _spellings = {
        "今天": "today", "昨天": "yesterday", "前天": "day before yesterday",
        "本周": "this week", "上周": "last week",
        "本月": "this month", "上月": "last month", "今年": "this year",
        # Written without the space as well, because that is how it gets typed.
        "dayBeforeYesterday": "day before yesterday",
        "thisweek": "this week", "lastweek": "last week",
        "thismonth": "this month", "lastmonth": "last month", "thisyear": "this year",
    }
    words = {**_ranges, **{k: _ranges[v] for k, v in _spellings.items()}}
    if w in words:
        a, b = words[w]
        return a, b, ""

    # Everything below is "which day / which month" — calendar scales, computed
    # against the local calendar rather than a UTC instant
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", w)
    if m:
        try:
            a = _w.parse_date(m.group(1))
            b = _w.parse_date(m.group(2)) + timedelta(days=1)
            return a, b, ""
        except ValueError:
            pass
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}", w)
    if m:
        try:
            a = _w.parse_date(w)
            return a, a + timedelta(days=1), ""
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{4})-(\d{2})", w)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            a = datetime(y, mo, 1, tzinfo=_w.LOCAL_TZ)
            b = (datetime(y + 1, 1, 1, tzinfo=_w.LOCAL_TZ) if mo == 12
                 else datetime(y, mo + 1, 1, tzinfo=_w.LOCAL_TZ))
            return a, b, ""
        except ValueError:
            pass  # an impossible month like 2026-99 falls through to the "not understood" branch below

    return None, None, (
        f"when 看不懂：{w}。认识的写法：48h / 7d / "
        "今天 / 昨天 / 前天 / 本周 / 上周 / 本月 / 上月 / 今年（"
        "today / yesterday / this week / last week / this month / last month / this year 同义）/ "
        "2026-07 / 2026-07-15 / 2026-07-01..2026-07-15。"
        "「刚搬去那阵子」这类生活刻度要等锚点——先用 query 门扔关键词。"
    )
