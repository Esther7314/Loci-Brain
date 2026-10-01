"""
========================================
core/_dates.py — a Chinese time phrase, read against the day it was said
========================================

The backfill's side model copies the time phrase out of the sentence word for word
(「下周一两点」「月底」「每年 8 月 7 号」); this file turns it into a date. The model never
does date math: what it copied is checked here against one table of forms, and a phrase
that fits none of them comes back as None — it is not guessed.

"Today" is the day the memory was written, on the local calendar (`LOCI_TZ`), never the
day the sweep happens to run: a promise written on Wednesday that says 「后天」 means
Friday even if the backfill only gets to it on Saturday.

Forms read (today = Wednesday 2026-10-07 in the examples):

  days       今天 明天 后天 大后天 · 昨天 前天 大前天 · 今早 今晚 明早 明晚 昨晚
  weeks      周X / 星期X / 礼拜X        the next one on or after today (周一 -> 10-12)
             这周X / 本周X              this week's, Monday first (这周一 -> 10-05)
             下周X / 下下周X / 上周X    one week on, two weeks on, one week back
             这周末 / 周末 / 下周末     that week's Saturday
  months     月底 / 下月底             the last day of this month / next month
             月初 / 下月初             the 1st of this month / next month
             X号 / 这个月X号 / 下个月X号
  dates      X月X号(日) · 今年/明年/去年X月X号 · YYYY年X月X日
  yearly     每年X月X号                 this year's occurrence, `yearly` set
  clock      两点 · 下午两点 · 晚上八点 · 十一点半 · 九点一刻 · 9:30
             A clock alone is today's. Without 上午/下午, 1-6 o'clock is afternoon
             (「两点开会」 is two in the afternoon) and 7-12 is as written.
  tail       前 / 之前 / 以前 / 左右 are read as the same day (「月底前」 = 月底)

Numbers may be digits or Chinese numerals (两、十一、二十三). An impossible day
(2月30号) is None, as is anything left over after the forms above have been read.

Exports: ResolvedTime · resolve_phrase(phrase, today, *, tz=None, yearly=False) · local_day
========================================
"""

from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from . import _when

_PHRASE_MAX = 40          # a time phrase is a few words; anything longer is a sentence

_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})"
_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
             "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7}
_SATURDAY = 6

# A relative day, and the part of the day it implies for a clock that follows it
# (明晚八点 is eight in the evening).
_RELATIVE_DAYS = {
    "大后天": (3, ""), "大前天": (-3, ""),
    "今天": (0, ""), "今日": (0, ""), "今儿": (0, ""), "今早": (0, "早上"), "今晚": (0, "晚上"),
    "明天": (1, ""), "明日": (1, ""), "明儿": (1, ""), "明早": (1, "早上"), "明晚": (1, "晚上"),
    "后天": (2, ""),
    "昨天": (-1, ""), "昨日": (-1, ""), "昨晚": (-1, "晚上"),
    "前天": (-2, ""),
}
_RELATIVE_RE = re.compile("^(" + "|".join(sorted(_RELATIVE_DAYS, key=len, reverse=True)) + ")")

_AFTERNOON = ("下午", "傍晚", "晚上", "夜里", "夜晚")
_MORNING = ("早上", "早晨", "清早", "上午", "凌晨", "半夜")
_PERIOD = r"(早上|早晨|清早|上午|中午|下午|傍晚|晚上|夜里|夜晚|半夜|凌晨)"
_CLOCK_RE = re.compile(
    "^" + _PERIOD + "?" + _NUM + r"(?:点钟|点|时)(半|一刻|三刻|" + _NUM + r"分?)?")
_COLON_RE = re.compile("^" + _PERIOD + r"?(\d{1,2})[:：](\d{2})")
_PERIOD_ONLY_RE = re.compile("^" + _PERIOD)
_TAIL_RE = re.compile(r"(之前|以前|前|左右)$")


@dataclass(frozen=True)
class ResolvedTime:
    """A phrase read as a day, a clock time on that day when it named one, and whether
    it comes round every year."""
    day: date
    clock: time | None = None
    yearly: bool = False

    def stamp(self, tz: tzinfo | None = None) -> str:
        """The form `when` stores: `YYYY-MM-DD` for a day, and with a clock time the
        local offset written out — a stored stamp without one is read as UTC."""
        if self.clock is None:
            return self.day.isoformat()
        moment = datetime.combine(self.day, self.clock).replace(tzinfo=tz or _when.LOCAL_TZ)
        return moment.isoformat(timespec="minutes")


def local_day(value, tz: tzinfo | None = None) -> date | None:
    """The local calendar day of a stored stamp (`created` is naive UTC), or None when it
    cannot be read."""
    stamp = _when.parse_stamp(value)
    if stamp is None:
        return None
    return stamp.astimezone(tz or _when.LOCAL_TZ).date()


def _number(token: str) -> int | None:
    """A digit string or a Chinese numeral up to 99 (两、十一、二十三)."""
    if token.isdigit():
        return int(token)
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _DIGITS.get(head, None) if head else 1
        ones = _DIGITS.get(tail, None) if tail else 0
        if tens is None or ones is None or "十" in tail:
            return None
        return tens * 10 + ones
    if len(token) == 1 and token in _DIGITS:
        return _DIGITS[token]
    return None


def _day(year: int, month: int | None, day: int | None) -> date | None:
    try:
        return date(year, month, day) if month and day else None
    except ValueError:
        return None


def _shift_month(d: date, months: int) -> tuple[int, int]:
    index = d.year * 12 + (d.month - 1) + months
    return index // 12, index % 12 + 1


def _read_day(s: str, today: date) -> tuple[date | None, str | None, str, bool]:
    """Read a day off the front of s. Returns (day, the rest, the part of the day it
    implies, whether it was a month-and-day form). day None = no day form here; rest
    None = a day form naming a day that does not exist."""
    m = re.match(r"^(\d{4}年|今年|明年|去年)?" + _NUM + "月" + _NUM + "[日号]", s)
    if m:
        named = m.group(1) or ""
        year = (int(named[:4]) if named[:1].isdigit()
                else today.year + {"明年": 1, "去年": -1}.get(named, 0))
        d = _day(year, _number(m.group(2)), _number(m.group(3)))
        return (d, s[m.end():], "", True) if d else (None, None, "", True)
    m = re.match(r"^(下下|下|上|这|本)?个?(?:周|星期|礼拜)([一二三四五六日天1-7])", s)
    if m:
        target = _WEEKDAYS[m.group(2)]
        wd = today.isoweekday()
        if m.group(1) is None:
            offset = (target - wd) % 7
        else:
            weeks = {"下下": 2, "下": 1, "上": -1, "这": 0, "本": 0}[m.group(1)]
            offset = target - wd + 7 * weeks
        return today + timedelta(days=offset), s[m.end():], "", False
    m = re.match(r"^(下下|下|这|本)?个?周末", s)
    if m:
        weeks = {"下下": 2, "下": 1}.get(m.group(1) or "", 0)
        return (today + timedelta(days=_SATURDAY - today.isoweekday() + 7 * weeks),
                s[m.end():], "", False)
    m = re.match(r"^(下|这|本)?个?月(底|初)", s)
    if m:
        year, month = _shift_month(today, 1 if m.group(1) == "下" else 0)
        last = monthrange(year, month)[1] if m.group(2) == "底" else 1
        return date(year, month, last), s[m.end():], "", False
    m = re.match("^(下|这|本)?个?月" + _NUM + "[日号]", s)
    if m:
        year, month = _shift_month(today, 1 if m.group(1) == "下" else 0)
        d = _day(year, month, _number(m.group(2)))
        return (d, s[m.end():], "", False) if d else (None, None, "", False)
    m = _RELATIVE_RE.match(s)
    if m:
        offset, period = _RELATIVE_DAYS[m.group(1)]
        return today + timedelta(days=offset), s[m.end():], period, False
    m = re.match("^" + _NUM + "[日号]", s)
    if m:
        d = _day(today.year, today.month, _number(m.group(1)))
        return (d, s[m.end():], "", False) if d else (None, None, "", False)
    return None, s, "", False


def _hour_of(period: str, hour: int) -> int | None:
    """A spoken hour with its part of the day -> 0-23, or None for one that does not
    exist (晚上十二点 is not guessed either way)."""
    if hour > 23:
        return None
    if hour > 12:
        return hour if not period or period in _AFTERNOON else None
    if period in _AFTERNOON:
        return None if hour == 12 else (hour + 12 if hour else hour)
    if period == "中午":
        return hour + 12 if 1 <= hour <= 2 else hour
    if period in _MORNING:
        return 0 if hour == 12 and period in ("凌晨", "半夜") else hour
    return hour + 12 if 1 <= hour <= 6 else hour


def _read_clock(s: str, implied: str) -> tuple[time | None, str, bool]:
    """Read a clock time off the front of s. Returns (time, the rest, ok); ok False
    means something that looks like a clock but is not a real one."""
    m = _COLON_RE.match(s)
    if m:
        hour = _hour_of(m.group(1) or implied, int(m.group(2)))
        minute = int(m.group(3))
        if hour is None or minute > 59:
            return None, s, False
        return time(hour, minute), s[m.end():], True
    m = _CLOCK_RE.match(s)
    if m:
        raw = _number(m.group(2))
        if raw is None:
            return None, s, False
        hour = _hour_of(m.group(1) or implied, raw)
        extra = m.group(3) or ""
        minute = {"": 0, "半": 30, "一刻": 15, "三刻": 45}.get(extra)
        if minute is None:
            minute = _number(m.group(4) or "")
        if hour is None or minute is None or minute > 59:
            return None, s, False
        return time(hour, minute), s[m.end():], True
    m = _PERIOD_ONLY_RE.match(s)
    if m:
        return None, s[m.end():], True
    return None, s, True


def resolve_phrase(phrase, today, *, tz: tzinfo | None = None,
                   yearly: bool = False) -> ResolvedTime | None:
    """Read `phrase` against `today` (a date, or a datetime that is first put on the local
    calendar of `tz`, naive read as UTC). `yearly` is the side model saying the phrase
    comes round every year; 每年 in the phrase says the same. A yearly phrase has to name
    a month and a day. None when the phrase fits no form."""
    if isinstance(today, datetime):
        today = _when.to_local(today).astimezone(tz or _when.LOCAL_TZ).date()
    if not isinstance(today, date):
        return None
    s = unicodedata.normalize("NFKC", str(phrase or ""))
    s = re.sub(r"\s+", "", s).replace("的", "")
    if not s or len(s) > _PHRASE_MAX:
        return None
    if s.startswith("每年"):
        yearly, s = True, s[2:]
    s = _TAIL_RE.sub("", s.removeprefix("在")) or s
    day, rest, implied, month_day = _read_day(s, today)
    if rest is None:
        return None
    clock, rest, ok = _read_clock(rest, implied)
    if not ok or rest or (day is None and clock is None):
        return None
    if yearly and (day is None or not month_day):
        return None
    return ResolvedTime(day or today, clock, yearly)
