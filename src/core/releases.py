# -*- coding: utf-8 -*-
"""
========================================
core/releases.py — 「检查更新」: the latest published release, asked of GitHub on request
========================================

The setting page shows the version that runs (utils.get_version: src/VERSION, else the
root VERSION) and, when the person presses 「检查更新」, the latest release published on
GitHub for this project (`REPO`): its tag, the day it came out, its notes (「看这次改了
什么」) and its page. Nothing is installed from here: there is no update button, no
download and no shell command; updating is the person's own step (pull the new image or
code and restart).

One request to the public GitHub API per check, without a key, kept for `CACHE_SECONDS`
(a failure for `FAILURE_CACHE_SECONDS`), so pressing the button again does not ask again.
Nothing is asked in the background. When GitHub cannot be reached, is rate-limited or
answers something unreadable, the answer says it could not check — an ordinary reply,
never an error page.

`FETCH(url, headers)` -> (HTTP status, decoded JSON or None) is the one network call;
tests put a stub there.

Exports: REPO · RELEASES_PAGE · CACHE_SECONDS · FAILURE_CACHE_SECONDS · FETCH ·
         parse_version · check · forget
========================================
"""

from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Optional

from . import _when as _w

REPO = "Esther7314/Loci-Brain"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
CACHE_SECONDS = 600
FAILURE_CACHE_SECONDS = 60
TIMEOUT_SECONDS = 8.0
NOTES_MAX = 20000

_VERSION = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)")


async def _fetch(url: str, headers: dict) -> tuple[int, Any]:
    import httpx
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body


# The one network call. Tests replace it.
FETCH: Callable[[str, dict], Awaitable[tuple[int, Any]]] = _fetch

_cache: dict = {"until": 0.0, "answer": None}


def forget() -> None:
    """Drop the kept answer (the next check asks GitHub again)."""
    _cache["until"], _cache["answer"] = 0.0, None


def parse_version(text) -> Optional[tuple[int, int, int]]:
    """`1.4.0` / `v1.4.0` / `1.4.0-rc1` -> (1, 4, 0); None when it does not start with one."""
    m = _VERSION.match(str(text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _day(stamp) -> str:
    parsed = _w.parse_stamp(stamp)
    return parsed.date().isoformat() if parsed else ""


def _could_not(why: str) -> dict:
    return {"ok": False, "latest": None,
            "words": f"没查到更新：{why}。过一会儿再点一次，或者直接去发布页看。"}


async def _ask(user_agent: str) -> dict:
    """What GitHub says about the latest release, independent of the running version:
    {ok, latest: {tag, name, date, notes, notes_cut, url} | None, words?}."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": user_agent,
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        status, body = await FETCH(LATEST_URL, headers)
    except Exception as exc:                         # noqa: BLE001 - said, never raised
        kind = type(exc).__name__
        return _could_not("连不上 GitHub（超时了）" if "Timeout" in kind
                          else f"连不上 GitHub（{kind}）")
    if status == 404:
        return {"ok": True, "latest": None, "words": "这个项目在 GitHub 上还没有发布过版本。"}
    if status in (403, 429):
        return _could_not("GitHub 这会儿限流了")
    if status != 200 or not isinstance(body, dict) or not body.get("tag_name"):
        return _could_not(f"GitHub 回的东西读不懂（HTTP {status}）")
    notes = str(body.get("body") or "")
    return {"ok": True, "latest": {
        "tag": str(body.get("tag_name")),
        "name": str(body.get("name") or ""),
        "date": _day(body.get("published_at") or body.get("created_at")),
        "notes": notes[:NOTES_MAX],
        "notes_cut": len(notes) > NOTES_MAX,
        "url": str(body.get("html_url") or RELEASES_PAGE),
    }}


def _compare(running: str, answer: dict) -> dict:
    latest = answer.get("latest")
    if not answer.get("ok") or latest is None:
        return {**answer, "newer": None}
    mine, theirs = parse_version(running), parse_version(latest["tag"])
    if mine is None or theirs is None:
        newer, words = None, f"最新发布的是 {latest['tag']}（{latest['date']}），跟这份对不上号。"
    elif theirs > mine:
        newer, words = True, f"有新版本 {latest['tag']}（{latest['date']} 发布），这份是 {running}。"
    elif theirs == mine:
        newer, words = False, f"已经是最新的了（{latest['tag']}）。"
    else:
        newer, words = False, f"这份 {running} 比最新发布的 {latest['tag']} 还新。"
    return {**answer, "newer": newer, "words": words}


async def check(running: str, *, now: Optional[float] = None) -> dict:
    """The latest release against the running version: {ok, latest, newer, words,
    checked_at}. `newer` is True / False, or None when it could not be told."""
    clock = time.monotonic() if now is None else now
    answer = _cache["answer"]
    if answer is None or clock >= _cache["until"]:
        answer = await _ask(f"Loci-Brain/{running or 'unknown'}")
        answer["checked_at"] = _w.now().isoformat(timespec="seconds")
        _cache["answer"] = answer
        _cache["until"] = clock + (CACHE_SECONDS if answer.get("ok") else FAILURE_CACHE_SECONDS)
    return _compare(running, dict(answer))
