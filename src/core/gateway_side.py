"""
========================================
core/gateway_side.py — the side model through the gateway (`dehydration.api_format: gateway`)
========================================

"Same as the gateway": the side model's calls (tags, slices, summaries, dreams) go to the
gateway's side door, `/loci/upstream/v1/...` (gateway/present/upstream.js), and the gateway
sends them on to the upstream in force with the key in force. Loci holds no address and no
key of the provider for it; only the model name is Loci's own (`dehydration.model`).

Which gateway: the host whose `hosts.<name>.present_url` names it (core/scope.py), the same
one the panel's present page is forwarded to (web/loci_present.py), with the same credential
(`fetch_token_env`, as `Authorization: Bearer`). Exactly one host may have a present_url;
none, or several, is said in words and nothing is sent. The hosts are read per call, so a
config change is seen at once.

Every failure raises GatewaySideError with words for the person, never the URL (a gateway
on a non-loopback bind keeps its passphrase in the path) and never a key:
    no gateway host, several, no credential toward it   — said before anything is sent
    the gateway cannot be reached or times out           — `transient`: retried like a
                                                            provider's connection error
    the gateway refuses Loci's credential (401 / 403)
    its side door is closed (LOCI_GATEWAY_TOKEN unset there: 404 with connected: false)
    the gateway holds no key yet, or no upstream (409)   — the gateway's own words
    the upstream answered an error                       — its status and message; 429 and
                                                            5xx keep `status_code`, so the
                                                            caller's retry treats them as
                                                            it treats any provider's
A failure is an exception like any provider's: the callers (core/_slicer, core/_dream,
tools/grow/_backfill) already let a side-model failure stop before anything is written.

Proxy settings from the environment are not used, so the request goes where present_url
says and nowhere else; redirects are not followed.

Exports: SIDE_PATH · GatewaySideError · find_gateway · chat · list_models
========================================
"""

import json
import os
from collections.abc import Mapping

import httpx

from . import scope as _scope

SIDE_PATH = "/loci/upstream/v1"
MODELS_TIMEOUT_SECONDS = 10.0
_MESSAGE_KEEP = 300

# An httpx transport the tests put in place of the network; None = the real one.
_transport = None


class GatewaySideError(RuntimeError):
    """A side-model call through the gateway that did not come back with an answer.
    `status_code`: the HTTP status the gateway answered, or None. `transient`: worth
    another try (Dehydrator._is_transient_error reads it)."""

    def __init__(self, message: str, *, status_code=None, transient: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


def find_gateway(config: Mapping, environ: Mapping[str, str] | None = None):
    """The gateway host: the one host with a present_url and Loci's credential toward it.
    Raises GatewaySideError in words when there is none, several, or no credential."""
    environ = os.environ if environ is None else environ
    hs = _scope.load_hosts(config or {}, environ,
                           legacy_token=str(environ.get("LOCI_HOOK_TOKEN") or ""))
    found = [h for h in hs.hosts.values() if h.present_url]
    if not found:
        raise GatewaySideError(
            "副模型选了「跟网关一样」，可是还没接上网关：config 的 hosts 里要有一个宿主写了 "
            "present_url 和 fetch_token_env（跟面板 present 页用的是同一个）")
    if len(found) > 1:
        names = "、".join(sorted(h.name for h in found))
        raise GatewaySideError(f"好几个宿主配了 present_url（{names}），副模型不知道该走哪一个网关")
    host = found[0]
    if not host.fetch_token:
        raise GatewaySideError(
            f"Loci 对宿主 {host.name} 没有钥匙：hosts.{host.name}.fetch_token_env 要写上一个"
            "环境变量的名字，值跟网关的 LOCI_GATEWAY_TOKEN 一样")
    return host


def _message_of(raw: bytes) -> str:
    """What an error body says: an OpenAI-style error.message, else its text, cut short."""
    text = raw.decode("utf-8", "replace")
    try:
        err = json.loads(text).get("error")
    except (ValueError, AttributeError):
        err = None
    if isinstance(err, dict) and str(err.get("message") or "").strip():
        return str(err["message"]).strip()[:_MESSAGE_KEEP]
    if isinstance(err, str) and err.strip():
        return err.strip()[:_MESSAGE_KEEP]
    return text.strip()[:_MESSAGE_KEEP]


def _closed(raw: bytes) -> bool:
    try:
        return json.loads(raw.decode("utf-8")).get("connected") is False
    except (ValueError, UnicodeDecodeError, AttributeError):
        return False


async def _ask(config: Mapping, method: str, sub: str, payload, timeout: float) -> bytes:
    host = find_gateway(config)
    headers = {"Authorization": f"Bearer {host.fetch_token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False,
                                     trust_env=False, transport=_transport) as client:
            r = await client.request(method, host.present_url + SIDE_PATH + sub,
                                     headers=headers, json=payload)
    except httpx.TimeoutException as e:
        raise GatewaySideError(f"网关 {timeout:g} 秒内没回话", transient=True) from e
    except httpx.HTTPError as e:
        # The class name only: an httpx message can carry the URL, and the URL the passphrase.
        raise GatewaySideError(f"连不上网关（{type(e).__name__}），看看它起了没有、"
                               f"hosts.{host.name}.present_url 对不对", transient=True) from e
    status, raw = r.status_code, r.content
    if 200 <= status < 300:
        return raw
    if 300 <= status < 400:
        raise GatewaySideError(f"网关要跳转（HTTP {status}），Loci 不跟", status_code=status)
    if status in (401, 403) and r.headers.get("www-authenticate"):
        raise GatewaySideError(
            f"网关不认 Loci 的钥匙（HTTP {status}）：hosts.{host.name}.fetch_token_env 那个环境变量的值"
            "要跟网关的 LOCI_GATEWAY_TOKEN 一样", status_code=status)
    if status == 404 and _closed(raw):
        raise GatewaySideError("网关的门关着：网关那边没设 LOCI_GATEWAY_TOKEN", status_code=status)
    if status == 409:
        # the gateway's own words: no key yet, or no upstream
        raise GatewaySideError(_message_of(raw), status_code=status)
    raise GatewaySideError(f"上游回了 HTTP {status}：{_message_of(raw)}", status_code=status)


async def chat(config: Mapping, payload: dict, *, timeout: float) -> str:
    """One chat completion through the gateway; the answer's text ("" when it has none)."""
    raw = await _ask(config, "POST", "/chat/completions", payload, timeout)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise GatewaySideError("网关转回来的不是 JSON") from e
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    return str((message or {}).get("content") or "")


async def list_models(config: Mapping, *, timeout: float = MODELS_TIMEOUT_SECONDS) -> list[str]:
    """The model names the gateway's upstream offers, sorted."""
    raw = await _ask(config, "GET", "/models", None, timeout)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise GatewaySideError("网关转回来的模型列表不是 JSON") from e
    items = data.get("data") if isinstance(data, dict) else None
    return sorted({str(m.get("id") or "") for m in (items or []) if isinstance(m, dict)} - {""})
