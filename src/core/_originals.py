"""
========================================
core/_originals.py — the fourth joint: a source's original text, asked of its host
========================================

Loci keeps a source's identity, never its text (plan 二·五): the original lives with the
host. When the model asks for it (tools/recall/original.py), Loci asks the host serving
the source, once per source, and hands back what the host gave. Nothing fetched is
stored, cached or logged: the text goes into that one reply and nowhere else.

Which host: one in `hosts:` with a `fetch_url` whose `max_grant` covers the source; the
deepest covering place wins and config order breaks a tie. A host with no `max_grant`
(an open host that wrote none) is never asked: nothing says which sources are its. No
such host is NO_HOST: the original is not reachable from here.

The request: POST `fetch_url`, a JSON body, the host's own credential (the value of its
`token_env`) as `Authorization: Bearer <credential>`.

    {"v": 1,
     "source":    {"system": "lento", "instance": "home", "container": "private:U",
                   "id": "m_0012", "through": "m_0031"},
     "revision":  "r3",
     "span":      null,
     "scope":     {"v": 1, "entry": {"system": "telegram", "instance": "bot-a"},
                   "venue": "group", "audience": ["user:U"], "grant": [{...}]},
     "max_chars": 8000}

    source      the identity; `through` only for a run of lines (id = its first line)
    revision    the revision the memory was formed on; null = it names none
    span        {unit, start, end} for a fragment of one piece; null otherwise. A run
                never carries one: a run with a span is refused before anything is sent
    scope       this request's read scope as Loci resolved it (core/scope.py); null = the
                calling host reads the whole library (an open host that sent none)
    max_chars   how much text Loci will take; past it Loci cuts and says so

The host decides by the source's state, its use and that scope.

The answer: HTTP 200 and a JSON object of exactly these keys (anything else counts as no
answer):

    {"v": 1, "status": "given",
     "lines": [{"id": "m_0012", "revision": "r3", "text": "…"},
               {"id": "m_0013", "revision": null, "missing": "unavailable"},
               {"id": "m_0014", "revision": null, "text": "…", "cut": true}],
     "truncated_after": "m_0014"}
    {"v": 1, "status": "unavailable"}
    {"v": 1, "status": "not_allowed", "reason": "withdrawn"}

    status            given · unavailable (the host cannot reach it now) · not_allowed
    reason            not_allowed only, optional: withdrawn · deleted · out_of_scope
    lines             given only: the lines asked for, in the host's order, from the first
                      (`source.id`) on; a single piece is exactly one line
      id              the line's id
      revision        the revision actually given; null = the host has none
      text            its text (with a span: the fragment's own text)
      cut             optional; true = this line's text stops short
      missing         in place of text: withdrawn · deleted · out_of_scope · unavailable
    truncated_after   given only: null, or the id of the last line sent when the lines
                      after it were left out (it is then the last line listed)

How an answer is taken:

    given, nothing missing for good             GIVEN; lines missing for now are marked
    given, a line withdrawn / deleted /         NOT_ALLOWED for the whole source: a run is
      out_of_scope                              as bad as its worst line
    given, every line missing for now           UNAVAILABLE
    unavailable · no answer · a timeout · a     UNAVAILABLE: the memory itself is still
      status other than 200 · a redirect · an   allowed, so its own body stands in
      answer too big or not of this shape
    not_allowed · a status or missing word      NOT_ALLOWED: the memory is fed neither its
      this version does not know                body nor its summary in this reply

A source the registry holds as withdrawn or deleted is NOT_ALLOWED without asking. A
state the host reveals that Loci did not know (not_allowed for a source held active)
blocks the memory in this reply only: a source's state changes only by the host's ordered
change notices (POST /api/v2/source/change), never by a fetch.

Limits (`source_fetch:` in config, `Settings`): one fetch gets `timeout_seconds` in all;
an answer over `max_bytes` is refused unread; text past `max_chars` is cut here and said
so; a reply asks at most `max_sources` of one memory's sources. Redirects are not
followed and no proxy from the environment is used, so the text reaches Loci and nothing
else.

Exports: GIVEN · UNAVAILABLE · NOT_ALLOWED · NO_HOST · GONE_WORDS · Settings ·
         settings_from · Line · Answer · host_for · scope_wire · build_request ·
         parse_answer · fetch
========================================
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from typing import Mapping, Optional

import httpx

from . import _sources as _src

logger = logging.getLogger("loci_brain.originals")

GIVEN, UNAVAILABLE, NOT_ALLOWED, NO_HOST = "given", "unavailable", "not_allowed", "no_host"
VERSION = 1
GONE_WORDS = ("withdrawn", "deleted", "out_of_scope")
MISSING_NOW = "unavailable"
_ANSWER_KEYS = frozenset({"v", "status", "reason", "lines", "truncated_after"})
_LINE_KEYS = frozenset({"id", "revision", "text", "cut", "missing"})

# Why an answer is UNAVAILABLE (for the reply's wording and the log; never the text).
UNREACHABLE, TIMEOUT, REDIRECT, TOO_BIG, MALFORMED, HOST_SAYS, NO_TOKEN, ALL_MISSING = (
    "unreachable", "timeout", "redirect", "too_big", "malformed", "host_says", "no_token",
    "all_lines_missing")
# Why an answer is NOT_ALLOWED when the host's word is not one of GONE_WORDS.
UNKNOWN_WORD, REQUEST_REFUSED = "unknown_word", "request_refused"


@dataclass(frozen=True)
class Settings:
    timeout_seconds: float = 5.0
    max_chars: int = 8000
    max_bytes: int = 256 * 1024
    max_sources: int = 5


_BOUNDS = {"timeout_seconds": (float, 0.1, 60.0), "max_chars": (int, 100, 200_000),
           "max_bytes": (int, 1024, 8 * 1024 * 1024), "max_sources": (int, 1, 64)}


def settings_from(config: Optional[Mapping]) -> Settings:
    """`source_fetch:` from config; a missing or unreadable value keeps its default and
    one out of bounds is held to the bound."""
    raw = (config or {}).get("source_fetch") or {}
    if not isinstance(raw, Mapping):
        raw = {}
    base = Settings()
    out = {}
    for key, (cast, lo, hi) in _BOUNDS.items():
        try:
            value = cast(raw.get(key, getattr(base, key)))
        except (TypeError, ValueError):
            value = getattr(base, key)
        out[key] = min(max(value, lo), hi)
    return Settings(**out)


@dataclass(frozen=True)
class Line:
    """One line of what the host gave: its text, or why it is missing."""
    id: str
    revision: Optional[str] = None
    text: Optional[str] = None
    cut: bool = False
    missing: Optional[str] = None


@dataclass(frozen=True)
class Answer:
    """What one fetch came to.

    outcome           GIVEN · UNAVAILABLE · NOT_ALLOWED · NO_HOST
    source            the string form asked for (with the memory's revision)
    host              the host asked ("" when none was)
    lines             GIVEN: the lines, in the host's order
    truncated_after   GIVEN: the last line sent when later ones were left out
    cut_here          GIVEN: Loci cut the text at max_chars
    reason            NOT_ALLOWED: one of GONE_WORDS, UNKNOWN_WORD, REQUEST_REFUSED, or ""
                      (the host gave none)
    why               UNAVAILABLE: what went wrong (UNREACHABLE, TIMEOUT, …, or http_<code>)
    """
    outcome: str
    source: str = ""
    host: str = ""
    lines: tuple = ()
    truncated_after: Optional[str] = None
    cut_here: bool = False
    reason: str = ""
    why: str = ""


def _depth(place: _src.Place) -> int:
    return sum(1 for level in _src.PLACE_KEYS if getattr(place, level) is not None)


def host_for(hosts, identity) -> Optional[object]:
    """The host to ask for this source: one with a `fetch_url` whose `max_grant` covers
    it, the deepest covering place first, config order on a tie. None = none serves it."""
    sid = identity if isinstance(identity, _src.SourceId) else _src.record_id(identity)
    best, best_depth = None, -1
    for host in getattr(hosts, "hosts", {}).values():
        if not host.fetch_url or not host.max_grant:
            continue
        for place in host.max_grant:
            if _src.places_cover((place,), sid) and _depth(place) > best_depth:
                best, best_depth = host, _depth(place)
    return best


def scope_wire(request) -> Optional[dict]:
    """This request's read scope as it goes to the host (the Loci-Scope shape); None for
    the whole library or no request at all."""
    scope = getattr(request, "scope", None)
    if scope is None:
        return None
    entry = {k: v for k, v in zip(("system", "instance", "container"), scope.entry)
             if v is not None}
    grant = [{k: getattr(p, k) for k in _src.PLACE_KEYS if getattr(p, k) is not None}
             for p in scope.grant]
    return {"v": 1, "entry": entry, "venue": scope.venue, "audience": sorted(scope.audience),
            "grant": grant}


def build_request(record: dict, request, settings: Settings) -> dict:
    """The body sent to the host for one source record. Raises ValueError for a run with
    a span (not supported in this version)."""
    sid = _src.record_id(record)
    span = record.get("span") or None
    if span and sid.through:
        raise ValueError("a run of lines with a span is not supported: ask for the run, or "
                         "for one line with its span")
    source = {"system": sid.system, "instance": sid.instance, "container": sid.container,
              "id": sid.id}
    if sid.through:
        source["through"] = sid.through
    revision = record.get("revision")
    return {"v": VERSION, "source": source,
            "revision": revision if revision not in (None, "") else None,
            "span": dict(span) if span else None, "scope": scope_wire(request),
            "max_chars": settings.max_chars}


def _malformed(note: str) -> Answer:
    logger.info("[originals] answer not of the agreed shape: %s", note)
    return Answer(UNAVAILABLE, why=MALFORMED)


def _optional_text(value) -> bool:
    return value is None or isinstance(value, str)


def parse_answer(raw: bytes, identity, settings: Settings) -> Answer:
    """The host's HTTP 200 body -> an Answer (`source` and `host` are the caller's to
    fill in). See the module header for how each shape is taken."""
    sid = identity if isinstance(identity, _src.SourceId) else _src.record_id(identity)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _malformed("not JSON")
    if not isinstance(data, dict) or set(map(str, data)) - _ANSWER_KEYS:
        return _malformed("not an object of the agreed keys")
    if data.get("v") != VERSION or isinstance(data.get("v"), bool):
        return _malformed("v is not 1")
    status = data.get("status")
    if status == UNAVAILABLE:
        return Answer(UNAVAILABLE, why=HOST_SAYS)
    if status == NOT_ALLOWED:
        reason = data.get("reason")
        if reason is None:
            return Answer(NOT_ALLOWED)
        return Answer(NOT_ALLOWED, reason=reason if reason in GONE_WORDS else UNKNOWN_WORD)
    if status != GIVEN:
        return Answer(NOT_ALLOWED, reason=UNKNOWN_WORD)
    rows = data.get("lines")
    if not isinstance(rows, list) or not rows:
        return _malformed("given without lines")
    lines: list[Line] = []
    for row in rows:
        if not isinstance(row, dict) or set(map(str, row)) - _LINE_KEYS:
            return _malformed("a line is not an object of the agreed keys")
        lid, text, missing = row.get("id"), row.get("text"), row.get("missing")
        if not isinstance(lid, str) or not lid.strip():
            return _malformed("a line without an id")
        if (text is None) == (missing is None):
            return _malformed("a line needs exactly one of text and missing")
        if not (_optional_text(text) and _optional_text(missing)
                and _optional_text(row.get("revision"))):
            return _malformed("text, missing and revision are text or null")
        cut = row.get("cut", False)
        if not isinstance(cut, bool):
            return _malformed("cut is true or false")
        if missing is not None and missing not in (*GONE_WORDS, MISSING_NOW):
            return Answer(NOT_ALLOWED, reason=UNKNOWN_WORD)
        lines.append(Line(id=lid, revision=row.get("revision"), text=text, cut=cut,
                          missing=missing))
    ids = [ln.id for ln in lines]
    after = data.get("truncated_after")
    if len(set(ids)) != len(ids) or ids[0] != sid.id:
        return _malformed("lines repeat an id or do not start at the first line asked for")
    if sid.through is None:
        if len(lines) != 1 or after is not None:
            return _malformed("a single piece is exactly one line, never truncated after")
    elif after is None:
        if ids[-1] != sid.through:
            return _malformed("an untruncated run ends at its last line")
    elif after != ids[-1]:
        return _malformed("truncated_after is not the last line listed")
    gone = next((ln.missing for ln in lines if ln.missing in GONE_WORDS), None)
    if gone:
        return Answer(NOT_ALLOWED, reason=gone)
    if all(ln.text is None for ln in lines):
        return Answer(UNAVAILABLE, why=ALL_MISSING)
    return _within_budget(lines, after, settings.max_chars)


def _within_budget(lines: list[Line], after: Optional[str], budget: int) -> Answer:
    """Cut the text at `budget` characters: the line that crosses it is cut, the lines
    after it dropped, and the cut is said (`cut_here`)."""
    kept: list[Line] = []
    left = budget
    for ln in lines:
        size = len(ln.text or "")
        if size <= left:
            kept.append(ln)
            left -= size
            continue
        kept.append(replace(ln, text=(ln.text or "")[:left], cut=True))
        return Answer(GIVEN, lines=tuple(kept), truncated_after=ln.id, cut_here=True)
    return Answer(GIVEN, lines=tuple(kept), truncated_after=after)


class _TooBig(Exception):
    pass


async def _post(host, body: dict, settings: Settings, transport) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {host.token}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_seconds),
                                 follow_redirects=False, trust_env=False,
                                 transport=transport) as client:
        async with client.stream("POST", host.fetch_url, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                return resp.status_code, b""
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > settings.max_bytes:
                raise _TooBig
            got = bytearray()
            async for chunk in resp.aiter_bytes():
                got.extend(chunk)
                if len(got) > settings.max_bytes:
                    raise _TooBig
            return 200, bytes(got)


async def fetch(record: dict, *, hosts, request=None, settings: Settings = Settings(),
                registry=None, transport: Optional[httpx.AsyncBaseTransport] = None) -> Answer:
    """Ask the host serving this source record for its original. `request` is the call's
    resolved RequestScope (None outside a request), `registry` the source registry,
    `transport` an httpx transport for tests. Never raises for anything the host does."""
    sid = _src.record_id(record)
    label = _src.record_string(record)
    if request is not None and getattr(request, "refused", False):
        return Answer(NOT_ALLOWED, source=label, reason=REQUEST_REFUSED)
    if registry is not None:
        state = registry.state_of(sid)
        if state in (_src.WITHDRAWN, _src.DELETED):
            return Answer(NOT_ALLOWED, source=label, reason=state)
    host = host_for(hosts, sid)
    if host is None:
        return Answer(NO_HOST, source=label)
    if not host.token:
        return Answer(UNAVAILABLE, source=label, host=host.name, why=NO_TOKEN)
    body = build_request(record, request, settings)
    status, raw = 0, b""
    try:
        status, raw = await asyncio.wait_for(_post(host, body, settings, transport),
                                             timeout=settings.timeout_seconds)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        answer = Answer(UNAVAILABLE, why=TIMEOUT)
    except _TooBig:
        answer = Answer(UNAVAILABLE, why=TOO_BIG)
    except httpx.HTTPError:
        answer = Answer(UNAVAILABLE, why=UNREACHABLE)
    else:
        if 300 <= status < 400:
            answer = Answer(UNAVAILABLE, why=REDIRECT)
        elif status != 200:
            answer = Answer(UNAVAILABLE, why=f"http_{status}")
        else:
            answer = parse_answer(raw, sid, settings)
    answer = replace(answer, source=label, host=host.name)
    # Identity, outcome and sizes only: the text itself is never logged.
    logger.info("[originals] %s from %s: %s%s (%d bytes)", label, host.name, answer.outcome,
                f" {answer.reason or answer.why}" if (answer.reason or answer.why) else "",
                len(raw))
    return answer
