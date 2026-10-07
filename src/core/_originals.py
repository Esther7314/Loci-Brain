"""
========================================
core/_originals.py — the fourth joint: a source's original text, asked of its host
========================================

Loci keeps a source's identity, never its text (plan 二·五): the original lives with the
host. When the model asks for it (tools/recall/original.py), Loci asks the host serving
the source, once per source, and hands back what the host gave. Nothing fetched is
stored, cached or logged: the text goes into that one reply and nowhere else.

An imported conversation (`system: import`) is the one exception: Loci holds its lines
itself and answers from them (core/import_memory.original_of), in the same shapes, as the
host `loci`.

Which host: the one declared as serving the source (`provides:` in `hosts:`,
core/scope.Hosts.provider_for — the deepest declared place covering it; two hosts at the
same depth name neither), with a `fetch_url`. What a host may touch (`max_grant`) never
decides it. No declared provider is NO_HOST: the original cannot be fetched from here, and
the memory's own body stands in as for UNAVAILABLE.

The request: POST `fetch_url`, a JSON body, Loci's own credential toward that host (the
value of its `fetch_token_env`, never the host's inbound credential) as
`Authorization: Bearer <credential>`. A provider with no such credential is not asked
(UNAVAILABLE, `no_token`).

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
     "lines": [{"id": "m_0012", "revision": "r3", "text": "…", "speaker": "小周",
                "at": "2026-10-06T21:04:11+08:00"},
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
                      (for now) · not_found (the host has no record of it — not the same
                      word as deleted)
      speaker         optional: who said the line, as a display name the audience may see;
                      left out when the host does not know
      at              optional: when it was said, ISO 8601 with its offset; left out when
                      the host does not know
                      A speaker or an at that does not read (not text, empty, too long, a
                      control character; a time with no offset) is taken as absent: it
                      never makes the answer unreadable
    truncated_after   given only: null, or the id of the last line sent when the lines
                      after it were left out (it is then the last line listed)

How an answer is taken:

    given, nothing missing                      GIVEN
    given, some lines unavailable / not_found,  GIVEN and `partial`: shown as partial, the
      or cut, or truncated                      missing lines marked
    given, every line unavailable / not_found   UNAVAILABLE
    unavailable · a timeout · no connection     UNAVAILABLE
      (refused, no such name)
    HTTP 502 / 503 / 504 (the host's side is    UNAVAILABLE
      down or slow for now)
    given, a line withdrawn / deleted /         NOT_ALLOWED for the whole source: a run is
      out_of_scope                              as bad as its worst line
    not_allowed                                 NOT_ALLOWED
    HTTP 401 / 403 (the host refused Loci)      NOT_ALLOWED
    HTTP 410                                    NOT_ALLOWED (`http_410`): the endpoint says
                                                it is gone, which says nothing of any one
                                                source — logged as the endpoint's fault
    the TLS handshake or the host's certificate NOT_ALLOWED (`tls_failed`): a host that
      failing                                   cannot prove who it is is not asked around
    any other status than 200 · a redirect ·    NOT_ALLOWED: an answer Loci cannot read lets
      an answer too big, not of this shape, or  nothing through
      with a word this version does not know

NOT_ALLOWED feeds the memory neither its body nor its summary in this reply. UNAVAILABLE,
NO_HOST and a partial GIVEN let the memory's own body stand in — only when, checked again
after the answer came back, the request may still read the memory and nothing it rests on
is withdrawn, deleted or held (the caller's check, tools/recall/original.py); they never
override a withdrawal.

What a NOT_ALLOWED does to state, by why:
    out_of_scope, HTTP 410, a TLS failure (or a       this call only; nothing is recorded,
      refusal, an unreadable answer)                  and the next read asks again
    withdrawn / deleted, said in a 200 answer         `holds` names each source (or line
      (`not_allowed` with that reason, or a line      of a run) the host said is gone: the
      missing with that word)                         caller holds it (core/_source_change.
                                                      hold), so breath, recall, cards and
                                                      dreams stop using what rests on it
                                                      until the ordered change settles it
The registry's state itself changes only by the host's ordered change notices (POST
/api/v2/source/change), never by a fetch.

Before asking: a source the registry holds as withdrawn, deleted or held is NOT_ALLOWED
without asking, and so is a run whose lines are not registered (`order_unknown`). After
the answer comes back the registry is read again, so a withdrawal that landed while the
fetch was in flight wins over whatever the host gave.

Limits (`source_fetch:` in config, `Settings`): one fetch gets `timeout_seconds` in all;
an answer over `max_bytes` is refused unread; text past `max_chars` is cut here and said
so; a reply asks at most `max_sources` of one memory's sources. Redirects are not
followed and no proxy from the environment is used, so the text reaches Loci and nothing
else.

Exports: GIVEN · UNAVAILABLE · NOT_ALLOWED · NO_HOST · GONE_WORDS · MISSING_NOW ·
         HOLD_WORDS · GONE_HTTP · TLS_FAILED · TEMPORARY_STATUSES · Settings · settings_from · Line · Answer · host_for · scope_wire ·
         build_request · parse_answer · speaker_of · at_of · SPEAKER_MAX · fetch ·
         deployment_hosts() · source_records_of(meta) · hold_what_hosts_said(answers, store)
========================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from dataclasses import dataclass, replace
from typing import Mapping, Optional

import httpx

from utils import WAS_QUOTED_FROM, read_prov

from . import _sources as _src
from . import _when as _w
from . import runtime as rt
from . import scope as _scope
from .scope import IMPORT_SYSTEM, LOCI

logger = logging.getLogger("loci_brain.originals")

GIVEN, UNAVAILABLE, NOT_ALLOWED, NO_HOST = "given", "unavailable", "not_allowed", "no_host"
VERSION = 1
GONE_WORDS = ("withdrawn", "deleted", "out_of_scope")
# A line missing for now, or one the host has no record of: neither blocks the source.
MISSING_NOW = ("unavailable", "not_found")
NOT_FOUND = "not_found"
# The host's words that a source is gone for good: the caller holds it.
HOLD_WORDS = ("withdrawn", "deleted")
_ANSWER_KEYS = frozenset({"v", "status", "reason", "lines", "truncated_after"})
_LINE_KEYS = frozenset({"id", "revision", "text", "cut", "missing", "speaker", "at"})
# A speaker is a display name: past this many characters it is not one.
SPEAKER_MAX = 64

# Why an answer is UNAVAILABLE (for the reply's wording and the log; never the text).
UNREACHABLE, TIMEOUT, HOST_SAYS, NO_TOKEN, ALL_MISSING = (
    "unreachable", "timeout", "host_says", "no_token", "all_lines_missing")
# Why an answer is NOT_ALLOWED when the host's word is not one of GONE_WORDS: an answer
# Loci cannot read, a refusal, or a word this version does not know.
REDIRECT, TOO_BIG, MALFORMED, UNKNOWN_WORD, REQUEST_REFUSED, ORDER_UNKNOWN = (
    "redirect", "too_big", "malformed", "unknown_word", "request_refused", "order_unknown")
GONE_HTTP = "http_410"
TLS_FAILED = "tls_failed"
# The statuses that say the host's side is down or slow for now: UNAVAILABLE, `why`
# http_<code>.
TEMPORARY_STATUSES = (502, 503, 504)


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
    """One line of what the host gave: its text, or why it is missing; who said it and
    when (local ISO 8601) when the host knows."""
    id: str
    revision: Optional[str] = None
    text: Optional[str] = None
    cut: bool = False
    missing: Optional[str] = None
    speaker: Optional[str] = None
    at: Optional[str] = None


@dataclass(frozen=True)
class Answer:
    """What one fetch came to.

    outcome           GIVEN · UNAVAILABLE · NOT_ALLOWED · NO_HOST
    source            the string form asked for (with the memory's revision)
    host              the host asked ("" when none was)
    lines             GIVEN: the lines, in the host's order
    truncated_after   GIVEN: the last line sent when later ones were left out
    cut_here          GIVEN: Loci cut the text at max_chars
    reason            NOT_ALLOWED: one of GONE_WORDS, a registry state, REDIRECT, TOO_BIG,
                      MALFORMED, UNKNOWN_WORD, REQUEST_REFUSED, ORDER_UNKNOWN, TLS_FAILED
                      or http_<code>; "" when the host gave none
    why               UNAVAILABLE: what went wrong (UNREACHABLE, TIMEOUT, HOST_SAYS, …, or
                      http_<code> for one of TEMPORARY_STATUSES)
    holds             [(identity string, withdrawn | deleted)]: what the host said is gone
                      that the caller has to hold (core/_source_change.hold)
    """
    outcome: str
    source: str = ""
    host: str = ""
    lines: tuple = ()
    truncated_after: Optional[str] = None
    cut_here: bool = False
    reason: str = ""
    why: str = ""
    holds: tuple = ()

    @property
    def partial(self) -> bool:
        """GIVEN, but not all of it: a line missing, cut, or the lines after one left out."""
        return self.outcome == GIVEN and (
            self.cut_here or self.truncated_after is not None
            or any(ln.missing is not None or ln.cut for ln in self.lines))


def host_for(hosts, identity) -> Optional[object]:
    """The host to ask for this source: the one declared as serving it, with a
    `fetch_url` (core/scope.Hosts.provider_for). None = none is declared."""
    sid = identity if isinstance(identity, _src.SourceId) else _src.record_id(identity)
    provider_for = getattr(hosts, "provider_for", None)
    return provider_for(sid) if callable(provider_for) else None


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
    return Answer(NOT_ALLOWED, reason=MALFORMED)


def _optional_text(value) -> bool:
    return value is None or isinstance(value, str)


def speaker_of(value) -> Optional[str]:
    """A line's `speaker` as the host sent it, or None when absent or not a display name
    (not text, empty, longer than SPEAKER_MAX, a control character)."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > SPEAKER_MAX or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None
    return name


def at_of(value) -> Optional[str]:
    """A line's `at` as local ISO 8601 with its offset (core/_when.parse_instant), or None
    when absent or not a moment with an offset."""
    moment = _w.parse_instant(value)
    return moment.isoformat(timespec="seconds") if moment is not None else None


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
        if reason not in GONE_WORDS:
            return Answer(NOT_ALLOWED, reason=UNKNOWN_WORD)
        holds = ((sid.to_string(), reason),) if reason in HOLD_WORDS else ()
        return Answer(NOT_ALLOWED, reason=reason, holds=holds)
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
        if missing is not None and missing not in (*GONE_WORDS, *MISSING_NOW):
            return Answer(NOT_ALLOWED, reason=UNKNOWN_WORD)
        speaker, at = speaker_of(row.get("speaker")), at_of(row.get("at"))
        if (speaker is None and row.get("speaker") is not None) or (
                at is None and row.get("at") is not None):
            logger.info("[originals] a line's speaker or at does not read; taken as absent")
        lines.append(Line(id=lid, revision=row.get("revision"), text=text, cut=cut,
                          missing=missing, speaker=speaker, at=at))
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
    holds = tuple((sid.piece(ln.id).to_string() if sid.through else sid.to_string(),
                   ln.missing) for ln in lines if ln.missing in HOLD_WORDS)
    gone = next((ln.missing for ln in lines if ln.missing in GONE_WORDS), None)
    if gone:
        return Answer(NOT_ALLOWED, reason=gone, holds=holds)
    if all(ln.text is None for ln in lines):
        return Answer(UNAVAILABLE, why=(NOT_FOUND if all(ln.missing == NOT_FOUND for ln in lines)
                                        else ALL_MISSING))
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


def _by_status(status: int) -> Answer:
    """An HTTP status other than 200. 502 / 503 / 504: the host's side is down or slow for
    now (UNAVAILABLE). 401 / 403: the host refused Loci. 410: the endpoint says it is gone —
    nothing about any one source, so nothing is held. Anything else, a redirect included,
    is an answer Loci cannot read. Only the temporary ones let the memory's body stand in."""
    if status in TEMPORARY_STATUSES:
        return Answer(UNAVAILABLE, why=f"http_{status}")
    if status == 410:
        return Answer(NOT_ALLOWED, reason=GONE_HTTP)
    if 300 <= status < 400:
        return Answer(NOT_ALLOWED, reason=REDIRECT)
    return Answer(NOT_ALLOWED, reason=f"http_{status}")


# How OpenSSL's errors read once a library has turned them into text ("[SSL: …] …").
_TLS_WORDS = ("[ssl", "certificate_verify_failed", "certificate verify failed",
              "wrong_version_number", "wrong version number")


def _tls_failure(exc: BaseException) -> bool:
    """Did the TLS handshake or the host's certificate fail somewhere down this error's
    chain (`__cause__` / `__context__`)? A refused connection, a name that does not
    resolve or a timeout is not one."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        text = str(cur).lower()
        if any(word in text for word in _TLS_WORDS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class _TooBig(Exception):
    pass


async def _post(host, body: dict, settings: Settings, transport) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {host.fetch_token}", "Accept": "application/json"}
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


def _blocked_by_registry(registry, sid) -> str:
    """The registry's reason not to show this source now: withdrawn, deleted, held, or
    order_unknown for a run whose lines are not registered; "" when there is none."""
    if registry is None:
        return ""
    if not registry.order_known(sid):
        return ORDER_UNKNOWN
    state = registry.state_of(sid)
    return state if state in (_src.WITHDRAWN, _src.DELETED, _src.HELD) else ""


async def fetch(record: dict, *, hosts, request=None, settings: Settings = Settings(),
                registry=None, transport: Optional[httpx.AsyncBaseTransport] = None) -> Answer:
    """Ask the host serving this source record for its original. `request` is the call's
    resolved RequestScope (None outside a request), `registry` the source registry,
    `transport` an httpx transport for tests. Never raises for anything the host does, and
    writes nothing: what the host said is gone comes back in `holds` for the caller."""
    sid = _src.record_id(record)
    label = _src.record_string(record)
    if request is not None and getattr(request, "refused", False):
        return Answer(NOT_ALLOWED, source=label, reason=REQUEST_REFUSED)
    blocked = _blocked_by_registry(registry, sid)
    if blocked:
        return Answer(NOT_ALLOWED, source=label, reason=blocked)
    if sid.system == IMPORT_SYSTEM:
        # An imported conversation is Loci's own material: it is read from the import's
        # store, never asked of a host (core/import_memory.py).
        from .import_memory import original_of      # lazy: it imports this module
        answer = original_of(registry.base_dir if registry is not None else "", sid,
                             settings)
        return replace(answer, source=label, host=LOCI)
    host = host_for(hosts, sid)
    if host is None:
        return Answer(NO_HOST, source=label)
    if not host.fetch_token:
        return Answer(UNAVAILABLE, source=label, host=host.name, why=NO_TOKEN)
    body = build_request(record, request, settings)
    status, raw = 0, b""
    try:
        status, raw = await asyncio.wait_for(_post(host, body, settings, transport),
                                             timeout=settings.timeout_seconds)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        answer = Answer(UNAVAILABLE, why=TIMEOUT)
    except _TooBig:
        answer = Answer(NOT_ALLOWED, reason=TOO_BIG)
    except ssl.SSLError:
        answer = Answer(NOT_ALLOWED, reason=TLS_FAILED)
    except httpx.HTTPError as e:
        answer = (Answer(NOT_ALLOWED, reason=TLS_FAILED) if _tls_failure(e)
                  else Answer(UNAVAILABLE, why=UNREACHABLE))
    else:
        answer = parse_answer(raw, sid, settings) if status == 200 else _by_status(status)
    if answer.reason in (GONE_HTTP, TLS_FAILED):
        # The endpoint's fault, not a word about this source: nothing is held or recorded.
        logger.warning("[originals] the fetch endpoint of host %s failed (%s) asking for %s; "
                       "nothing is held", host.name, answer.reason, label)
    # A withdrawal that landed while the host was answering wins over what it gave.
    blocked = _blocked_by_registry(registry, sid)
    if blocked and answer.outcome != NOT_ALLOWED:
        answer = Answer(NOT_ALLOWED, reason=blocked)
    answer = replace(answer, source=label, host=host.name)
    # Identity, outcome and sizes only: the text itself is never logged.
    logger.info("[originals] %s from %s: %s%s (%d bytes)", label, host.name, answer.outcome,
                f" {answer.reason or answer.why}" if (answer.reason or answer.why) else "",
                len(raw))
    return answer


# ============================================================
# The deployment's side: what a memory asks for, whom it asks, and what an answer holds
# (shared by recall's view="original" and the dream's quote share)
# ============================================================

def deployment_hosts():
    """The deployment's hosts, read per call the way the request layer reads them
    (web/panel_auth.hosts): `hosts:` in config, the legacy host's key falling back to the
    hook key."""
    cfg = rt.config or {}
    legacy = (str(os.environ.get(_scope.LEGACY_TOKEN_ENV) or "").strip()
              or str(cfg.get("hook_token") or "").strip())
    return _scope.load_hosts(cfg, os.environ, legacy_token=legacy)


def source_records_of(meta: dict) -> list[dict]:
    """The sources to ask for: the entry's records, then the sources its wasQuotedFrom
    lines name by string form; each identity-and-revision once. A record that no longer
    reads as one (a hand edit), or a quoted target that is not a source string form, is
    skipped."""
    out: list[dict] = []
    seen: set[str] = set()
    for rec in meta.get(_src.SOURCES_FIELD) or []:
        try:
            key = _src.record_string(rec)
        except (KeyError, TypeError, AttributeError):
            continue
        if key not in seen:
            seen.add(key)
            out.append(dict(rec))
    for line in read_prov(meta):
        if line["rel"] != WAS_QUOTED_FROM:
            continue
        try:
            sid, revision = _src.SourceId.parse(line["target"])
        except _src.SourceRecordError:
            continue
        key = sid.to_string(revision)
        if key in seen:
            continue
        seen.add(key)
        rec = {"system": sid.system, "instance": sid.instance, "container": sid.container,
               "id": sid.id, "revision": revision}
        if sid.through:
            rec["through"] = sid.through
        out.append(rec)
    return out


async def hold_what_hosts_said(answers, store) -> None:
    """Hold every source a host said is withdrawn or deleted (`Answer.holds`) that the
    registry does not record so: nothing resting on it is used until the ordered change
    settles it (core/_source_change.hold)."""
    from . import _source_change as _SC
    for answer in answers:
        for identity, said in answer.holds:
            try:
                await _SC.hold(store, identity, said, answer.host)
            except (ValueError, _src.SourceRecordError) as e:
                rt.logger.warning(f"[originals] could not hold {identity}: {e}")
