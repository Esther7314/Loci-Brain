"""
========================================
core/scope.py — who is calling, and what this request may read
========================================

A request carries three things from its host, never from the model:

    the host's credential   which host is calling (`hosts:` in config): its ceiling of
                            sources (`max_grant`), whether it may restore a withdrawn
                            source (`may_restore`), and whether it may read the whole
                            library without saying what for (`scope_mode: open`)
    Loci-Scope              this turn's read scope: where the call comes from (`entry`),
                            the occasion (`venue`), who sees the reply (`audience`), and
                            the sources it may read (`grant`)
    Loci-Turn               this turn's id and the write call's ordinal in it,
                            `<turn>#<ordinal>`: the write key (core/_sources.run_once)

Over HTTP every request carries its own; under stdio `LOCI_SCOPE` (and the optional
`LOCI_HOST_TOKEN`) are read once when the process starts. The request layer
(src/server.py `_with_notice`, web/panel_auth for the hook routes) resolves them once per
call into a `RequestScope` and sets it for that call; tools read it and hand core a
`ScopeView`, which the gate (core/visibility.visible_for) asks. Nothing in core reads the
contextvar.

------------------------------------------------------------
Hosts
------------------------------------------------------------
    hosts:
      legacy:                              # the life line: Lento, the panel's own clients
        token_env: LOCI_HOOK_TOKEN
        scope_mode: open
      entry-side-a:
        token_env: LOCI_HOST_TOKEN_ENTRY_A # the value lives in the environment
        max_grant:
          - {system: telegram, instance: bot-a}
        may_restore: false                 # scope_mode defaults to restricted
        authority:                         # the sources whose changes this host sends
          - {system: telegram, instance: bot-a}
        provides:                          # the sources whose originals this host serves
          - {system: telegram, instance: bot-a}
        registers:                         # the runs whose line lists it submits on the
          - {system: telegram, instance: bot-a}   # authority's behalf
        fetch_url: http://127.0.0.1:3010/api/loci/source   # where Loci asks for them
        fetch_token_env: LOCI_FETCH_TOKEN_ENTRY_A           # Loci's own credential there

`max_grant` says what a host may touch; it never says whose a source is. Who is the change
authority for a source (`authority`, core/_source_change.py), who serves its original
(`provides` with `fetch_url` and `fetch_token_env`, core/_originals.py) and who besides the
authority may register which lines a run holds (`registers`,
core/_source_change.registration_refusal) are declared, place by place: the deepest
declared place covering a source names its host, and two hosts declaring covering places
of the same depth name neither (`Hosts.authority_for`, `Hosts.provider_for`,
`Hosts.registrar_for`). Such a collision refuses only what it covers — every other source
of both hosts stands — and is reported: `load_hosts` names both hosts, the key and the
place in `Hosts.errors` (and `Hosts.collisions`), logs it once, and the setup screen shows
it red (web/loci.build_setup, `hosts_collision`). A source no host declares the authority
of has its changes refused; one no host provides cannot have its original fetched; a run's
lines are registered only by the authority of each line or the host that registers it. An
authority or registers place has to lie within the host's `max_grant`. The fetch
credential is Loci's toward the host, never the host's own toward Loci: it names another
environment variable, and a value equal to the host's inbound credential is not used.

An imported conversation (`system: import`, core/import_memory.py) is Loci's own
material: Loci is its change authority (`LOCI_HOST`, whatever the table says) and serves
its original itself (core/_originals.py). No host may be named `loci`.

No `hosts:` table means exactly one host, `legacy` above (its key falls back to config
`hook_token`, as the hook routes always read it), which is then the change authority for
every source but the imported ones and serves no originals; it is also who a caller
presenting no host credential is (an MCP client, a hook caller on an unlocked panel).
With a table, a caller is a host only by that host's credential: an MCP client the
deployment's MCP auth let in is the `legacy` row (if any), and nobody else who presents
nothing is anyone — MCP with auth off and the hook routes refuse them, and the panel's own
routes need the panel locked. A table holding a host with a ceiling (`Hosts.ceilinged`)
also needs the panel locked and MCP auth on; until both hold, those hosts' credentials
are refused (`Hosts.unsafe`, filled in by web/panel_auth). Only an `open` host reads
without a Loci-Scope; every other host gets nothing without one, and a scope that is
malformed or reaches past its `max_grant` is refused whole: no host ever falls back to
open.

------------------------------------------------------------
A memory under a scope (the three conditions)
------------------------------------------------------------
Every source record standing behind it must pass all three:
  1. its `use` (the host's latest use_changed for the source, else the record's own)
     allows this venue and every person in this audience — and for a run, so does each
     line's use_changed inside it (`SourceRegistry.uses_of`). An absent side does not
     narrow; an empty list allows nothing; a side with a rule needs the turn to say it (a
     turn with no audience never passes an audience rule — the empty set is not taken as
     a subset); `use: null` is no rule of its own (the material's authorisation stands,
     i.e. the grant); a use the gate cannot read lets nothing through;
  2. the grant covers it (`SourceRegistry.granted`): a piece by a place over it, a run
     only when Loci knows which lines it holds (the host registered its order) and every
     one of them is granted — by a place at its container or wider, or by places naming
     each line. A run whose order Loci does not know is refused under any grant: a line
     withdrawn in its middle could not be told;
  3. its state (`SourceRegistry.read_state`, a run judged by every line it holds) is
     active, or unreadable (the text is out of reach for now; what was understood from it
     stays readable). Held (a host said, while serving the original, that it is withdrawn
     or deleted, and the ordered change has not settled it) is not readable.
"Standing behind it" is the entry's own `sources`, every source its quoted lines name by
string form (`wasQuotedFrom`; such a line alone does not make an entry readable), and, for
anything derived, every root:
the walk follows `read_from_ids` (derived-from and primary source, the same edges
recall's root lines follow; a revision's previous version is not a source) to the entries
with nothing further, through every state — a root that sank or was deleted still says
where the material came from. An entry with neither sources nor anything it stands on (a
memory from before sources existed) is readable only without a scope, and so is anything
standing on such an entry, or on an id the library does not have.

The view never says how many entries it withheld: a count is itself a leak.

The whole library is read through a view too (`view_of`; `whole_library`): it narrows
nothing, but it still reads the registry, so an entry with a source behind it (the same
walk) that the registry records withdrawn, deleted or held is kept off as the gate's
`source_gone` from the moment the change is recorded (`ScopeView.source_blocked`), before
the change has written its records on what stood on the source.

Exports: SCOPE_HEADER · TURN_HEADER · HOST_HEADER · SCOPE_ENV · HOST_TOKEN_ENV · OPEN ·
         RESTRICTED · LEGACY · LOCI · LOCI_HOST · IMPORT_SYSTEM · ScopeError · Host ·
         Hosts (authority_for · provider_for · registrar_for · claimants · collisions ·
         ceilinged · unsafe) ·
         load_hosts · Scope ·
         parse_scope · parse_turn · RequestScope · ScopeView · view_of · narrows · unsupported_line ·
         current_request · request_scope
========================================
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

from utils import parse_bool, read_from_ids

from . import _sources as _src

logger = logging.getLogger("loci_brain.scope")

SCOPE_HEADER = "Loci-Scope"
TURN_HEADER = "Loci-Turn"
HOST_HEADER = "x-loci-hook-token"
SCOPE_ENV = "LOCI_SCOPE"
HOST_TOKEN_ENV = "LOCI_HOST_TOKEN"
OPEN = "open"
RESTRICTED = "restricted"
LEGACY = "legacy"
LEGACY_TOKEN_ENV = "LOCI_HOOK_TOKEN"
# Loci itself, as the change authority for the material it holds of its own: an imported
# conversation's lines.
LOCI = "loci"
IMPORT_SYSTEM = "import"

SCOPE_MAX = 16 * 1024            # bytes of one Loci-Scope
SCOPE_VERSION = 1
_SCOPE_KEYS = ("v", "entry", "venue", "audience", "grant")
_ENTRY_KEYS = ("system", "instance", "container")
_NAME_MAX = 128                  # a venue, a person
_LIST_MAX = 256                  # people in an audience, places in a grant
_HOST_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_TURN = re.compile(r"^([^\s#]{1,200})#([1-9][0-9]{0,8})$")


class ScopeError(ValueError):
    """A Loci-Scope or Loci-Turn the request layer will not act on. `zh` says what is
    wrong the way a reply says it, for the host's developer."""

    def __init__(self, message: str, zh: str):
        super().__init__(message)
        self.zh = zh


# ============================================================
# Hosts
# ============================================================

@dataclass(frozen=True)
class Host:
    name: str
    scope_mode: str = RESTRICTED
    max_grant: Optional[tuple] = None    # places; None = no ceiling (an open host only)
    may_restore: bool = False
    token: str = field(default="", repr=False, compare=False)
    # Where Loci asks this host for a source's original (core/_originals.py); "" = never
    # asked. It serves the sources its `provides` declares.
    fetch_url: str = ""
    # Loci's credential toward `fetch_url`; "" = none, and then nothing is fetched.
    fetch_token: str = field(default="", repr=False, compare=False)
    provides: tuple = ()                 # places whose originals it serves
    authority: tuple = ()                # places whose changes it sends
    registers: tuple = ()                # places whose runs' line lists it submits

    @property
    def open(self) -> bool:
        return self.scope_mode == OPEN


LOCI_HOST = Host(LOCI, scope_mode=OPEN)

_HOST_KEYS = {"token_env", "max_grant", "may_restore", "scope_mode", "fetch_url",
              "fetch_token_env", "provides", "authority", "registers"}
# The keys declaring places whose host is resolved one per source (`_declared`).
_DECLARING = ("authority", "provides", "registers")


def _places(name: str, raw, key: str) -> tuple:
    items = raw.get(key) or []
    if not isinstance(items, list):
        raise ValueError(f"host {name}: {key} is a list of places")
    return tuple(_src.Place.from_mapping(p, where=f" {key}[{i}]") for i, p in enumerate(items))


def _host_from(name: str, raw, environ: Mapping[str, str], fallback_token: str) -> Host:
    if not _HOST_NAME.match(name):
        raise ValueError(f"host name {name!r} must be 1-64 of A-Z a-z 0-9 _ . -")
    if not isinstance(raw, dict):
        raise ValueError(f"host {name}: expected a mapping")
    extra = sorted(set(map(str, raw)) - _HOST_KEYS)
    if extra:
        raise ValueError(f"host {name}: unknown keys {extra}")
    mode = str(raw.get("scope_mode") or RESTRICTED).strip().lower()
    if mode not in (OPEN, RESTRICTED):
        raise ValueError(f"host {name}: scope_mode is open or restricted, got {mode!r}")
    token_env = str(raw.get("token_env") or "").strip()
    if not token_env:
        raise ValueError(f"host {name}: token_env names the environment variable holding "
                         "its credential")
    token = str(environ.get(token_env) or "").strip() or fallback_token
    max_grant = None
    if "max_grant" in raw or mode == RESTRICTED:
        items = raw.get("max_grant") or []
        if not isinstance(items, list):
            raise ValueError(f"host {name}: max_grant is a list of places")
        max_grant = tuple(_src.Place.from_mapping(p, where=f" max_grant[{i}]")
                          for i, p in enumerate(items))
    fetch_url = str(raw.get("fetch_url") or "").strip()
    if fetch_url:
        parts = urlsplit(fetch_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.fragment):
            raise ValueError(f"host {name}: fetch_url must be an http(s) URL with a host and "
                             "no credentials or fragment in it")
    fetch_env = str(raw.get("fetch_token_env") or "").strip()
    if fetch_env and fetch_env == token_env:
        raise ValueError(f"host {name}: fetch_token_env names Loci's own credential toward the "
                         "host and must be another variable than token_env")
    fetch_token = str(environ.get(fetch_env) or "").strip() if fetch_env else ""
    if fetch_token and token and hmac.compare_digest(fetch_token.encode("utf-8"),
                                                     token.encode("utf-8")):
        _say_once(f"host {name}: the value of {fetch_env} equals the host's own credential; "
                  "Loci fetches nothing from it until the two differ")
        fetch_token = ""
    authority = _places(name, raw, "authority")
    registers = _places(name, raw, "registers")
    if max_grant is not None:
        for key, places in (("authority", authority), ("registers", registers)):
            for place in places:
                if not any(place.within(top) for top in max_grant):
                    raise ValueError(f"host {name}: {key} {place.label()} is past its "
                                     "max_grant")
    return Host(name=name, scope_mode=mode, max_grant=max_grant,
                may_restore=parse_bool(raw.get("may_restore"), default=False), token=token,
                fetch_url=fetch_url, fetch_token=fetch_token,
                provides=_places(name, raw, "provides"), authority=authority,
                registers=registers)


def _depth(place) -> int:
    return sum(1 for level in _src.PLACE_KEYS if getattr(place, level) is not None)


def _claimants(hosts: Iterable[Host], attr: str, sid) -> list[Host]:
    """The hosts whose `attr` places declare this source at the deepest depth any does:
    one host, several (a collision), or none."""
    best: list[Host] = []
    best_depth = -1
    for host in hosts:
        depth = max((_depth(p) for p in getattr(host, attr) if p.covers(sid)), default=-1)
        if depth < 0:
            continue
        if depth > best_depth:
            best, best_depth = [host], depth
        elif depth == best_depth:
            best.append(host)
    return best


def _declared(hosts: Iterable[Host], attr: str, sid) -> Optional[Host]:
    """The host whose `attr` places declare this source: the deepest covering place wins;
    two hosts declaring covering places of the same depth name neither."""
    best = _claimants(hosts, attr, sid)
    return best[0] if len(best) == 1 else None


def _collisions(hosts: Iterable[Host]) -> list[str]:
    """Every place two or more hosts declare under the same key (the same label is the
    same depth): there neither is taken. Each named once, with its hosts and key."""
    out: list[str] = []
    for key in _DECLARING:
        by_label: dict[str, list[str]] = {}
        for host in hosts:
            for label in dict.fromkeys(p.label() for p in getattr(host, key)):
                by_label.setdefault(label, []).append(host.name)
        for label, names in by_label.items():
            if len(names) > 1:
                out.append(f"hosts: {' and '.join(names)} both declare {key} {label}; at the "
                           f"same depth neither is taken there ({_COLLISION_EFFECT[key]}), "
                           "everything else of theirs stands")
    return out


_COLLISION_EFFECT = {
    "authority": "its changes are refused as no_change_authority",
    "provides": "its original cannot be fetched",
    "registers": "neither may register its runs' lines on that ground",
}


class Hosts:
    """The hosts of one deployment. `implicit` = no `hosts:` table was written. `errors`
    are the table's problems (a host left out, a collision); `collisions` the places two
    hosts declare at the same depth, each already among `errors` too."""

    def __init__(self, hosts: Iterable[Host], implicit: bool, errors: Iterable[str] = (),
                 collisions: Iterable[str] = ()):
        self.hosts = {h.name: h for h in hosts}
        self.implicit = implicit
        self.errors = tuple(errors)
        self.collisions = tuple(collisions)
        # Why the hosts with a ceiling are refused in this deployment, or "": the request
        # layer fills it in (web/panel_auth.lock_problem — the panel unlocked, MCP auth off).
        self.unsafe = ""

    @property
    def ceilinged(self) -> bool:
        """A table holding a host with a ceiling (`max_grant`; every restricted host has
        one): what it keeps out has to be kept out on every door."""
        return not self.implicit and any(h.max_grant is not None for h in self.hosts.values())

    def by_token(self, token: str) -> Optional[Host]:
        """The host whose credential this is (compared in constant time per host). Two
        hosts sharing one credential are a deployment error: neither is taken."""
        got = str(token or "").strip()
        if not got:
            return None
        found = [h for h in self.hosts.values()
                 if h.token and hmac.compare_digest(got.encode("utf-8"), h.token.encode("utf-8"))]
        return found[0] if len(found) == 1 else None

    @property
    def default(self) -> Optional[Host]:
        """Who a caller presenting no host credential is once the deployment let it in
        some other way (MCP auth; without a table, an unlocked panel): the `legacy` host,
        if any."""
        return self.hosts.get(LEGACY)

    def get(self, name: str) -> Optional[Host]:
        return self.hosts.get(str(name or ""))

    def authority_for(self, identity) -> Optional[Host]:
        """The host declared as the change authority for this source (a SourceId, a
        record or a string form); None when none is, or two are at the same depth. An
        imported conversation's authority is Loci itself (`LOCI_HOST`). With no `hosts:`
        table the one legacy host is the authority for every other source."""
        sid = _src._identity(identity)
        if sid.system == IMPORT_SYSTEM:
            return LOCI_HOST
        if self.implicit:
            return self.default
        return _declared(self.hosts.values(), "authority", sid)

    def provider_for(self, identity) -> Optional[Host]:
        """The host declared as serving this source's original (`provides`), when it
        has a `fetch_url`; None otherwise. Nothing else decides it: a host that may touch
        a source is not thereby asked for it."""
        host = _declared(self.hosts.values(), "provides", _src._identity(identity))
        return host if host is not None and host.fetch_url else None

    def registrar_for(self, identity) -> Optional[Host]:
        """The host entrusted with registering the line lists of runs over this source
        (`registers`) on its authority's behalf; None when none is, or two are at the same
        depth. Nobody registers for an imported conversation: its lines are Loci's own."""
        sid = _src._identity(identity)
        if sid.system == IMPORT_SYSTEM:
            return None
        return _declared(self.hosts.values(), "registers", sid)

    def claimants(self, key: str, identity) -> tuple:
        """The hosts declaring this source under `key` (authority · provides · registers)
        at the deepest depth any does: one is the declared host, two or more a collision,
        none nobody."""
        if key not in _DECLARING:
            raise ValueError(f"claimants: key is one of {_DECLARING}")
        return tuple(_claimants(self.hosts.values(), key, _src._identity(identity)))


def load_hosts(config: Mapping, environ: Mapping[str, str], *,
               legacy_token: str = "") -> Hosts:
    """`hosts:` from config. Absent: the one legacy host (credential `legacy_token`, the
    hook key as panel_auth reads it). A host entry that is malformed is left out and
    logged — its credential then matches nothing, which refuses rather than opens. Two
    hosts declaring the same place under one key both stay; the collision is named in
    `errors` and `collisions` and logged once."""
    raw = (config or {}).get("hosts")
    if raw is None:
        return Hosts([Host(LEGACY, scope_mode=OPEN, token=str(legacy_token or "").strip())],
                     implicit=True)
    hosts: list[Host] = []
    errors: list[str] = []
    if not isinstance(raw, dict):
        errors.append("hosts: expected a mapping of host name -> settings")
        raw = {}
    for name, body in raw.items():
        if str(name) == LOCI:
            errors.append(f"hosts: '{LOCI}' is Loci's own name (the authority for imported "
                          "conversations); name the host otherwise")
            continue
        fallback = str(legacy_token or "").strip() if str(name) == LEGACY else ""
        try:
            hosts.append(_host_from(str(name), body, environ, fallback))
        except (ValueError, _src.SourceRecordError) as e:
            errors.append(str(e))
    for e in errors:
        _say_once(f"{e} — that host is left out and its credential refused")
    collisions = _collisions(hosts)
    for c in collisions:
        _say_once(c)
    return Hosts(hosts, implicit=False, errors=errors + collisions, collisions=collisions)


_LOGGED: set[str] = set()


def _say_once(message: str) -> None:
    """Hosts are read per request: log each configuration problem once."""
    if message not in _LOGGED:
        _LOGGED.add(message)
        logger.error("[hosts] %s", message)


# ============================================================
# Loci-Scope and Loci-Turn
# ============================================================

@dataclass(frozen=True)
class Scope:
    entry: tuple            # (system, instance, container), each or None past system
    venue: str
    audience: frozenset
    grant: tuple            # Place

    def entry_label(self) -> str:
        return "/".join(x for x in self.entry if x)


def _name(value, what: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ScopeError(f"{what} must be a non-empty name", f"{what} 要是一个不空的名字")
    text = str(value).strip()
    if len(text) > _NAME_MAX or "\n" in text or "\r" in text:
        raise ScopeError(f"{what} is over {_NAME_MAX} characters or more than one line",
                         f"{what} 超过 {_NAME_MAX} 字或不止一行")
    return text


def _header_text(raw) -> str:
    """HTTP hands headers over as latin-1; a host that put UTF-8 bytes in gets them back."""
    text = str(raw or "")
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def parse_scope(raw) -> Scope:
    """The Loci-Scope header (or LOCI_SCOPE) -> Scope. Raises ScopeError on anything
    that is not exactly the agreed shape: an unknown key is refused, not ignored, so a host
    never believes a field it sent was applied."""
    text = _header_text(raw).strip()
    if len(text.encode("utf-8")) > SCOPE_MAX:
        raise ScopeError(f"longer than {SCOPE_MAX} bytes", f"超过 {SCOPE_MAX} 字节")
    try:
        body = json.loads(text)
    except ValueError as e:
        raise ScopeError(f"not JSON: {e}", "不是合法的 JSON") from None
    if not isinstance(body, dict):
        raise ScopeError("not a JSON object", "要是一个 JSON 对象")
    extra = sorted(set(map(str, body)) - set(_SCOPE_KEYS))
    if extra:
        raise ScopeError(f"unknown keys {extra}", f"不认识的字段 {', '.join(extra)}；"
                         f"只认 {' / '.join(_SCOPE_KEYS)}")
    missing = [k for k in _SCOPE_KEYS if k not in body]
    if missing:
        raise ScopeError(f"missing {missing}", f"缺 {', '.join(missing)}")
    if body["v"] != SCOPE_VERSION or isinstance(body["v"], bool):
        raise ScopeError(f"v must be {SCOPE_VERSION}", f"v 要是 {SCOPE_VERSION}")
    entry = body["entry"]
    if not isinstance(entry, dict) or set(map(str, entry)) - set(_ENTRY_KEYS):
        raise ScopeError("entry must be {system, instance?, container?}",
                         "entry 要写成 {system, instance?, container?}")
    try:
        place = _src.Place.from_mapping(entry, where=" entry")
    except _src.SourceRecordError as e:
        raise ScopeError(str(e), e.zh) from None
    venue = _name(body["venue"], "venue")
    audience = body["audience"]
    if not isinstance(audience, list) or len(audience) > _LIST_MAX:
        raise ScopeError(f"audience must be a list of at most {_LIST_MAX} names",
                         f"audience 要是一列名字（最多 {_LIST_MAX} 个）")
    people = frozenset(_name(p, f"audience[{i}]") for i, p in enumerate(audience))
    grant = body["grant"]
    if not isinstance(grant, list) or len(grant) > _LIST_MAX:
        raise ScopeError(f"grant must be a list of at most {_LIST_MAX} places",
                         f"grant 要是一列来源位置（最多 {_LIST_MAX} 处）")
    places = []
    for i, p in enumerate(grant):
        try:
            places.append(_src.Place.from_mapping(p, where=f" grant[{i}]"))
        except _src.SourceRecordError as e:
            raise ScopeError(str(e), e.zh) from None
    return Scope(entry=(place.system, place.instance, place.container), venue=venue,
                 audience=people, grant=tuple(places))


def parse_turn(raw) -> tuple[str, int]:
    """Loci-Turn `<turn>#<ordinal>` -> (turn, ordinal). The turn is the host's id for this
    turn (no whitespace, no `#`, at most 200 characters); the ordinal is which write call of
    the turn this is, counted by the host from 1. A retry or a restart sends both again."""
    text = _header_text(raw).strip()
    m = _TURN.match(text)
    if not m:
        raise ScopeError(f"Loci-Turn must be <turn>#<ordinal>, got {text[:60]!r}",
                         "Loci-Turn 要写成 <这一轮的编号>#<这一轮里第几次写>（比如 t-0925-01#2；"
                         "编号不带空白和 #，第几次从 1 数）")
    return m.group(1), int(m.group(2))


# ============================================================
# What one request may read
# ============================================================

NO_SCOPE = "no_scope"
EXCEEDS = "exceeds"
MALFORMED = "malformed"
NO_HOST = "no_host"

OPEN_LINE = "〔范围：全库（open）〕"


def unsupported_line(what: str) -> str:
    """The first line of a road that cannot be filtered by scope in this version."""
    return f"〔范围：受限 · {what}这一版还不能按范围筛，这次没放进来〕"


@dataclass(frozen=True)
class RequestScope:
    """One request, resolved: its host, its mode, its scope, or why nothing is read.

    open + no scope        the whole library, as before
    a scope                the three conditions decide (both modes)
    refusal                nothing is read; `first_line` says why"""
    host: Optional[Host]
    mode: str
    scope: Optional[Scope] = None
    refusal: str = ""
    detail: str = ""

    @classmethod
    def resolve(cls, host: Optional[Host], header, *,
                no_host: str = "没带宿主凭据，部署里也没有 legacy 宿主") -> "RequestScope":
        """Decide once: the host and the raw Loci-Scope (None or blank when absent).
        `no_host` says why there is no host, when there is none."""
        if host is None:
            return cls(None, RESTRICTED, refusal=NO_HOST, detail=no_host)
        mode = OPEN if host.open else RESTRICTED
        if header is None or not str(header).strip():
            return cls(host, mode) if host.open else cls(host, mode, refusal=NO_SCOPE)
        try:
            scope = parse_scope(header)
        except ScopeError as e:
            return cls(host, mode, refusal=MALFORMED, detail=e.zh)
        if host.max_grant is not None:
            for place in scope.grant:
                if not any(place.within(top) for top in host.max_grant):
                    return cls(host, mode, refusal=EXCEEDS, detail=place.label())
        return cls(host, mode, scope=scope)

    @property
    def whole_library(self) -> bool:
        """Open and unscoped: nothing is filtered."""
        return not self.refusal and self.scope is None and self.mode == OPEN

    @property
    def refused(self) -> bool:
        return bool(self.refusal)

    @property
    def grant(self) -> Optional[tuple]:
        """What a write may draw on: the scope's grant; None = not enforced (open and
        unscoped); () when refused."""
        if self.refusal:
            return ()
        return self.scope.grant if self.scope is not None else None

    def first_line(self) -> str:
        if self.refusal == NO_SCOPE:
            return f"〔范围：受限 · 没收到范围，什么都不给。宿主要在请求上带 {SCOPE_HEADER}〕"
        if self.refusal == EXCEEDS:
            return f"〔范围：受限 · 拒绝：范围超出这个宿主凭据（{self.detail}），这次什么都没读〕"
        if self.refusal == MALFORMED:
            return f"〔范围：受限 · 拒绝：{SCOPE_HEADER} 读不懂（{self.detail}），这次什么都没读〕"
        if self.refusal == NO_HOST:
            return f"〔范围：受限 · 拒绝：认不出是哪个宿主（{self.detail}），这次什么都没读〕"
        if self.scope is None:
            return OPEN_LINE
        s = self.scope
        return (f"〔范围：受限 · 入口 {s.entry_label()} · 场合 {s.venue} · "
                f"许读 {len(s.grant)} 处〕")


def _meta_of(row):
    """A store bucket ({"metadata": …}) or a bare meta -> the meta."""
    if isinstance(row, dict) and isinstance(row.get("metadata"), dict):
        return row["metadata"]
    return row


class ScopeView:
    """One request's reading of one library, for the gate: `permits(meta)` (may this
    request read it at all) and `source_blocked(meta)` (does the registry say now that a
    source behind it is withdrawn, deleted or held).

    `request` None is no request at all (a direct call, a background job), which reads the
    whole library as an open request without a scope does (`whole_library`). `metas` maps
    ids to metadata for the walk to the roots: every id in the library, archive included,
    under a scope; with `partial` (the whole library, `view_of`) only what was loaded, and
    the walk reads an id missing there from the registry's library by id. `registry` is
    the source registry (states and use changes). Decisions are kept per id for the life
    of the view (one request)."""

    def __init__(self, request: Optional[RequestScope], metas: Mapping[str, dict],
                 registry=None, *, partial: bool = False):
        self.request = request
        self.metas = metas
        self.registry = registry
        self.partial = partial
        self._memo: dict[str, Optional[bool]] = {}
        self._records: dict[str, bool] = {}
        self._blocking: Optional[frozenset] = None
        self._blocked: dict[str, bool] = {}
        self._record_blocked: dict[str, bool] = {}
        self._read: dict[str, Optional[dict]] = {}

    @property
    def whole_library(self) -> bool:
        """Nothing is narrowed: no request, or an open one that sent no scope."""
        return self.request is None or self.request.whole_library

    @property
    def first_line(self) -> str:
        return OPEN_LINE if self.request is None else self.request.first_line()

    def _record_ok(self, rec) -> bool:
        if not isinstance(rec, dict):
            return False
        try:
            sid = _src.record_id(rec)
        except (KeyError, TypeError):
            return False
        key = f"{sid.to_string()}|{json.dumps(rec.get('use'), sort_keys=True, ensure_ascii=False)}"
        if key in self._records:
            return self._records[key]
        ok = self._judge_record(rec, sid)
        self._records[key] = ok
        return ok

    def _judge_record(self, rec: dict, sid) -> bool:
        scope = self.request.scope
        reg = self.registry
        granted = (reg.granted(scope.grant, sid) if reg is not None
                   else _src.places_cover(scope.grant, sid))
        if not granted:
            return False
        state = reg.read_state(rec) if reg is not None else _src.ACTIVE
        if state not in (_src.ACTIVE, _src.UNREADABLE):
            return False
        uses = reg.uses_of(rec) if reg is not None else [u for u in [rec.get("use")]
                                                          if u is not None]
        for use in uses:
            rule = _src.parse_use(use)
            if rule is None:
                return False
            if rule["venues"] is not None and (not scope.venue
                                               or scope.venue not in rule["venues"]):
                return False
            if rule["audience"] is not None and (not scope.audience
                                                 or not scope.audience <= rule["audience"]):
                return False
        return True

    def _walk(self, bid: str, meta: dict, path: frozenset) -> Optional[bool]:
        """True: every record behind it passes and there is at least one. False: one
        fails, or a root has nothing to stand on. None: only a cycle was found."""
        if bid and bid in self._memo:
            return self._memo[bid]
        sources = meta.get(_src.SOURCES_FIELD) or []
        # A source named only by a quoted line is one more thing it rests on; it is never
        # enough on its own to make an entry readable.
        own = list(sources) + _src.quoted_records(meta)
        parents = [p for p in read_from_ids(meta) if p]
        result: Optional[bool]
        if not sources and not parents:
            result = False
        else:
            result = True if own else None
            for rec in own:
                if not self._record_ok(rec):
                    result = False
                    break
            if result is not False:
                for p in parents:
                    if p in path or p == bid:
                        continue
                    pm = self.metas.get(p)
                    if pm is None:
                        result = False
                        break
                    got = self._walk(p, pm, path | {bid})
                    if got is False:
                        result = False
                        break
                    if got is True:
                        result = True
        if bid:
            self._memo[bid] = result
        return result

    def permits(self, meta) -> bool:
        """May this request read this entry at all (before any road's own rules)?"""
        req = self.request
        if req is None:
            return True
        if req.refused:
            return False
        if req.scope is None:
            return req.mode == OPEN
        m = _meta_of(meta)
        if not isinstance(m, dict):
            return False
        bid = str(m.get("id") or "")
        return self._walk(bid, m, frozenset()) is True

    def permits_id(self, bid: str) -> bool:
        if self.whole_library:
            return True
        meta = self.metas.get(str(bid or ""))
        return meta is not None and self.permits(meta)

    def permits_handle(self, handle: str) -> bool:
        """A short handle (an id's first characters, as tags and lines print them) names
        something this request may read."""
        h = str(handle or "")
        if self.whole_library:
            return bool(h)
        return bool(h) and any(bid.startswith(h) and self.permits_id(bid) for bid in self.metas)

    # ---------- what the registry says now ----------

    def source_blocked(self, meta, *, roots: bool = True) -> bool:
        """Does the registry say, now, that a source standing behind this entry is
        withdrawn, deleted or held: one of its own `sources`, one its quoted lines name,
        or (`roots`) one behind anything it is derived from (the walk `permits` takes)?
        Asked on every read, the whole library's included: a host's change is recorded in
        the registry before the memories resting on the source carry their `source_gone`
        records (core/_source_change.py), and a read in between must not count the
        source. Nothing is asked of a registry with no such source (`blocking_containers`)."""
        reg = self.registry
        if reg is None:
            return False
        if self._blocking is None:
            containers = getattr(reg, "blocking_containers", None)
            self._blocking = containers() if callable(containers) else frozenset()
        if not self._blocking:
            return False
        m = _meta_of(meta)
        if not isinstance(m, dict):
            return False
        if not roots:
            return any(self._record_gone(rec) for rec in _src.basis_records(m))
        return self._blocked_walk(str(m.get("id") or ""), m, frozenset())

    def _blocked_walk(self, bid: str, meta: dict, path: frozenset) -> bool:
        if bid and bid in self._blocked:
            return self._blocked[bid]
        hit = any(self._record_gone(rec) for rec in _src.basis_records(meta))
        if not hit:
            for p in read_from_ids(meta):
                if not p or p == bid or p in path:
                    continue
                pm = self._meta_for(p)
                if pm is not None and self._blocked_walk(p, pm, path | {bid}):
                    hit = True
                    break
        if bid:
            self._blocked[bid] = hit
        return hit

    def _record_gone(self, rec) -> bool:
        try:
            sid = _src.record_id(rec)
        except (KeyError, TypeError, AttributeError):
            return False
        if (sid.system, sid.instance, sid.container) not in self._blocking:
            return False
        key = sid.to_string()
        if key not in self._record_blocked:
            try:
                state = self.registry.state_of(sid)
            except (_src.SourceRecordError, ValueError):
                state = _src.ACTIVE
            self._record_blocked[key] = state in (_src.WITHDRAWN, _src.DELETED, _src.HELD)
        return self._record_blocked[key]

    def _meta_for(self, bid: str) -> Optional[dict]:
        """An entry's metadata for the walk: from `metas`, else (a partial view) read from
        the registry's library by id."""
        meta = self.metas.get(bid)
        if meta is not None or not self.partial:
            return meta
        if bid not in self._read:
            read = getattr(getattr(self.registry, "store", None), "meta_of", None)
            try:
                self._read[bid] = read(bid) if callable(read) else None
            except Exception:                    # noqa: BLE001 - an unreadable file stands on nothing known
                self._read[bid] = None
        return self._read[bid]

    def covers_container(self, source: Mapping) -> bool:
        """Does the grant cover a whole container of the host's material (a batch of raw
        lines waiting to be sliced)? Only a place naming no single piece does."""
        if self.request is None:
            return True
        if self.request.refused:
            return False
        if self.request.scope is None:
            return self.request.mode == OPEN
        try:
            sid = _src.SourceId(str(source["system"]), str(source["instance"]),
                                str(source["container"]), "\0")
        except (KeyError, TypeError):
            return False
        return any(p.id is None and p.covers(sid) for p in self.request.scope.grant)


def narrows(view) -> bool:
    """Does this view keep anything out of the request's reach (a read scope, or a
    refusal)? None and a whole-library view narrow nothing."""
    return view is not None and not getattr(view, "whole_library", False)


def _metas(buckets) -> dict:
    out: dict = {}
    for b in buckets:
        meta = b.get("metadata") or {}
        bid = str(meta.get("id") or b.get("id") or "")
        if bid:
            out[bid] = meta
    return out


async def view_of(store, req: Optional[RequestScope]) -> ScopeView:
    """The view one request reads `store`'s library through. Under a scope the walk to
    the roots needs the whole library, archive included, loaded once (nothing for a
    refused request). The whole library loads the live listing only, and only when the
    registry holds a withdrawn, deleted or held source at all: the walk reads anything
    else it needs by id (`ScopeView.source_blocked`)."""
    registry = getattr(store, "sources", None)
    if req is None or req.whole_library:
        containers = getattr(registry, "blocking_containers", None)
        blocking = containers() if callable(containers) else frozenset()
        metas = _metas(await store.list_all(include_archive=False)) if blocking else {}
        return ScopeView(req, metas, registry, partial=True)
    metas = {} if req.refused else _metas(await store.list_all(include_archive=True))
    return ScopeView(req, metas, registry)


# ============================================================
# The request in progress
# ============================================================

_REQUEST: contextvars.ContextVar[Optional[RequestScope]] = contextvars.ContextVar(
    "loci_request_scope", default=None)


def current_request() -> Optional[RequestScope]:
    """This call's resolved request, or None outside a request (a direct call, a
    background job): the whole library, as before."""
    return _REQUEST.get()


@contextmanager
def request_scope(req: Optional[RequestScope]):
    """Run a block as one request: its read scope, and the grant writes are checked
    against."""
    token = _REQUEST.set(req)
    try:
        with _src.grant_scope(req.grant if req is not None else None):
            yield req
    finally:
        _REQUEST.reset(token)
