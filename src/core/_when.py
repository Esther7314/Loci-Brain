# -*- coding: utf-8 -*-
"""
tools/_when.py — 统一的「她的今天」（2026-08-04，codex 复核 #4）

**问题**：容器里没有 TZ，`datetime.now()` 给的是 UTC。她在 +08。
所以北京时间凌晨 2 点，容器那边还是**前一天下午 6 点** ——
「今天/昨天/这周/这个月」整体偏 8 小时。
她是个夜猫子，**凌晨存的东西第二天在「今天」里看不见**，这是每天都会撞上的。
（发现的那一刻正好是 2026-08-04 凌晨 1:47，她刚存完东西。）

**三条口径，别混**（混了比不改还糟，会把历史数据整体挪 8 小时）：

| 字段 | 落盘长什么样 | 怎么解释 |
|---|---|---|
| `created` / `last_active` | 无后缀（容器里 `datetime.now().isoformat()`） | **按 UTC**，再转本地 |
| 任何带 `Z` / `+08:00` 的 | 自己说了是哪个时区 | 按它自己说的 |
| `when` 是纯日期 `YYYY-MM-DD` | 那是「哪一天」，不是「哪一刻」 | **按本地日历**，别当 UTC |

⚠️ **写入端要是哪天改成写本地时间但仍然不带后缀，这里就会错。**
真要改，写入端必须同时开始带 `+08:00` 后缀 —— 带了后缀这边就认得出来。

对外：`LOCAL_TZ` · `now()` · `today()` · `parse_stamp()` · `parse_date()` · `to_local()`
"""

import os
import re
from datetime import datetime, timedelta, timezone

_TZ_NAME = os.environ.get("LOCI_TZ", "").strip() or "Asia/Shanghai"

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(_TZ_NAME)
except Exception:      # 镜像里没有 tzdata 就退回固定 +8（中国 1991 年后没有夏令时）
    LOCAL_TZ = timezone(timedelta(hours=8), "UTC+8")

UTC = timezone.utc

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEADING_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def now() -> datetime:
    """当下，带时区，本地。"""
    return datetime.now(LOCAL_TZ)


def today() -> datetime:
    """本地日历里今天的零点。"""
    return now().replace(hour=0, minute=0, second=0, microsecond=0)


def to_local(dt: datetime) -> datetime:
    """任何 datetime → 本地 aware。不带时区的一律当 UTC（见上面那张表）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).astimezone(LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def parse_date(s: str) -> datetime:
    """`YYYY-MM-DD` → 本地那一天的零点。**日期是日历，不是时刻。**"""
    return datetime.fromisoformat(s[:10]).replace(tzinfo=LOCAL_TZ)


def parse_date_or_none(s) -> datetime | None:
    """`parse_date` 的宽容版：读不懂给 None，不抛。

    🔴 2026-08-19 单元测试第一跑逮到的那一族 bug 就收在这儿。
       病根：`\\d{4}-\\d{2}-\\d{2}` 这个正则只管**长得像不像**日期，
       不管**是不是真有那一天**。`2026-09-31` / `2026-13-45` 一路畅通，
       到 `fromisoformat` 那儿才炸 —— 而那时候已经在 recall 的循环里了。
    ⚠️ `parse_date` 本身**故意不动**：它的契约是「给我一个合法日期串」，
       七个调用点里有四个紧跟着 `+ timedelta(days=1)`，改它的返回类型
       等于逼那四处各自编一个「拿不到日期怎么办」。**要宽容的自己点名要。**
    """
    try:
        return parse_date(s)
    except (ValueError, TypeError):
        return None


def parse_stamp(value) -> datetime | None:
    """把落盘的时间字符串读成本地 aware datetime；读不懂给 None。

    ⚠️ 不要再对它做 `s[:19]` 那种切片 —— 那会把 `Z` 和 `+08:00` 一起切掉，
    等于把一个说清楚了时区的时间戳硬掰成「不知道哪个时区」（codex #4 点名的一处）。
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    if _DATE_ONLY.match(s):
        # 2026-08-19：这儿原来是裸调 parse_date —— 于是 `2026-09-31`（9 月没有 31 号）
        # 会**抛异常**，而这个函数的第一句 docstring 写着「读不懂给 None」。
        # 更要命的是上游全都按「None = 这条没有时间」写的，没有一处 try 住它：
        # 一条这样的桶不是自己安静地掉出时间轴，是**把整趟 recall 掀翻**。
        # （下面 `_LEADING_DATE` 那支一直是 try 住的 —— 同一个函数两种脾气。）
        return parse_date_or_none(s)

    # ISO 8601：Python 3.11+ 的 fromisoformat 认 Z，也认 +08:00 和微秒
    try:
        return to_local(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        pass

    # 前面挂了别的字（比如「2026-07-15 那天…」）：只取开头那个日期
    m = _LEADING_DATE.match(s)
    if m:
        try:
            return parse_date(m.group(0))
        except ValueError:
            return None
    return None


def year_week(dt: datetime) -> tuple[int, int]:
    """自然周（ISO）。用来分「按周一句」那一段。"""
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def year_month(dt: datetime) -> tuple[int, int]:
    """自然月。用来分「按月一句」那一段。"""
    return (dt.year, dt.month)
