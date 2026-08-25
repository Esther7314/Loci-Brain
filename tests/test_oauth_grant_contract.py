# -*- coding: utf-8 -*-
"""CONTRACT: bridge/oauth.py — who gets a Bearer token, and when a token stops working.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    The OAuth surface is the only lock between the public internet and /mcp. Every
    judgment in it shares the shape of this repo's historical bugs: nothing raises,
    the request simply passes when it should have been refused — and here a wrong
    pass does not corrupt data, it hands a stranger a working token to someone's
    private memory.

    So this file asserts the refusal shapes first: expired grants, replayed codes,
    rotated refresh tokens, tokens bound to another resource, PKCE downgrades,
    browser-executable redirect URIs, blank-vs-blank static token comparisons.
    The live issuance path is the atomic pair commit
    (_commit_authorization_code_exchange / _commit_refresh_token_rotation); the
    non-persisting single-shot issuers they replaced were deleted in eae28e9, and
    the "no disk write, no token" property is asserted directly below.

    Everything runs on synthetic state with buckets_dir pointed at tmp_path.
    No server, no network port, no real credentials. Expiry is exercised by
    seeding timestamps (or injecting a fake clock), never by sleeping.
"""
import base64
import hashlib
import json
import time
import types

import pytest

import bridge.oauth as oauth
from web import _shared as sh
from web import config_api as CA


# ───────────────────────── synthetic state plumbing ─────────────────────────

_STATE_DICTS = (
    oauth._oauth_clients,
    oauth._oauth_codes,
    oauth._mcp_tokens,
    oauth._mcp_token_resources,
    oauth._mcp_refresh_tokens,
)

RESOURCE = "https://example.com/mcp"
CALLBACK = "https://client.example/callback"
VERIFIER = "a" * 43  # minimal RFC 7636 verifier


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture
def grant_state(tmp_path, monkeypatch):
    """Empty in-memory grant state + a private tmp dir for the persistence files."""
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)}, raising=False)
    saved = [dict(d) for d in _STATE_DICTS]
    for d in _STATE_DICTS:
        d.clear()
    yield tmp_path
    for d, snapshot in zip(_STATE_DICTS, saved):
        d.clear()
        d.update(snapshot)


def _store_fresh_code(code="code-under-test", resource=RESOURCE):
    """Store one authorization code the way the authorize route does, and return
    (code, the exact stored dict) for the exchange step."""
    generation = sh._credential_generation_snapshot()
    code_data = {
        "client_id": "client-1",
        "redirect_uri": CALLBACK,
        "code_challenge": s256(VERIFIER),
        "resource": resource,
        "scope": "mcp",
        "expires": time.time() + 300,
    }
    assert oauth._store_authorization_code(code, code_data, generation)
    return code, dict(oauth._oauth_codes[code])


def _tokens_file(tmp_path):
    return json.loads(
        (tmp_path / ".dashboard_mcp_tokens.json").read_text(encoding="utf-8")
    )


# ───────────────────────── PKCE: the proof of possession ─────────────────────────

def test_pkce_matching_verifier_passes():
    assert oauth._verify_pkce(VERIFIER, s256(VERIFIER))


def test_pkce_wrong_verifier_is_refused():
    # Criterion: PKCE is the only thing binding the token request to the browser
    # that authorized. Accepting a wrong verifier means a stolen code is enough.
    assert not oauth._verify_pkce("b" * 43, s256(VERIFIER))


def test_pkce_plain_method_is_refused():
    # A challenge equal to the verifier is the "plain" method. Only S256 is
    # advertised; accepting plain silently would downgrade every client that a
    # network attacker can strip the hash from.
    assert not oauth._verify_pkce(VERIFIER, VERIFIER)


def test_pkce_undersized_verifier_is_refused_even_when_the_digest_matches():
    # Criterion: RFC 7636 sets 43 chars as the entropy floor. A 20-char verifier
    # with a correct digest is a brute-forceable secret, not a proof.
    short = "a" * 20
    assert not oauth._verify_pkce(short, s256(short))


def test_pkce_value_shape_bounds():
    assert oauth._valid_pkce_value("a" * 43)
    assert oauth._valid_pkce_value("a" * 128)
    assert not oauth._valid_pkce_value("a" * 42)
    assert not oauth._valid_pkce_value("a" * 129)
    assert not oauth._valid_pkce_value("a" * 42 + "!")  # charset
    assert not oauth._valid_pkce_value(None)


# ───────────────────────── redirect URIs: where a code may be sent ─────────────────────────

@pytest.mark.parametrize("uri", [
    "javascript:alert(1)",
    "data:text/html;base64,PGI+",
    "file:///etc/passwd",
    "vbscript:msgbox",
    "about:blank",
    "blob:https://example.com/x",
])
def test_browser_executable_redirect_schemes_are_refused(uri):
    # Criterion: the authorization code rides on this URI in a 302. A javascript:
    # or data: target turns the consent page into an XSS trampoline.
    assert not oauth._valid_redirect_uri(uri)


def test_http_redirects_are_loopback_only():
    # RFC 8252: plaintext http is acceptable only for native-app loopback
    # callbacks. An http URI to a routable host would ship codes in cleartext.
    assert oauth._valid_redirect_uri("http://localhost/cb")
    assert oauth._valid_redirect_uri("http://127.0.0.1:8080/cb")
    assert not oauth._valid_redirect_uri("http://192.168.1.5/cb")
    assert not oauth._valid_redirect_uri("http://client.example/cb")


def test_redirects_with_credentials_or_fragments_are_refused():
    assert not oauth._valid_redirect_uri("https://user:pw@client.example/cb")
    assert not oauth._valid_redirect_uri("https://client.example/cb#frag")


def test_private_use_native_scheme_is_allowed():
    assert oauth._valid_redirect_uri("com.example.app:/oauth2redirect")


def test_redirect_uri_junk_shapes_are_refused():
    assert not oauth._valid_redirect_uri("")
    assert not oauth._valid_redirect_uri(42)
    assert not oauth._valid_redirect_uri("https://x.example/" + "a" * 2050)


def test_one_bad_redirect_uri_poisons_the_whole_registration():
    # Criterion: a registration is judged as a set. Accepting the good URIs and
    # keeping the javascript: one "for later" is how a vetted client ends up with
    # an unvetted callback.
    registration, _ = oauth._normalize_client_registration({
        "redirect_uris": [CALLBACK, "javascript:alert(1)"],
    })
    assert registration is None


def test_registration_shape_gates():
    assert oauth._normalize_client_registration("not-a-dict")[0] is None
    assert oauth._normalize_client_registration({})[0] is None
    assert oauth._normalize_client_registration({"redirect_uris": []})[0] is None
    too_many = {"redirect_uris": [f"https://c{i}.example/cb" for i in range(11)]}
    assert oauth._normalize_client_registration(too_many)[0] is None
    assert oauth._normalize_client_registration(
        {"redirect_uris": [CALLBACK], "client_name": 7}
    )[0] is None


def test_registration_caps_the_name_and_dedupes_uris():
    registration, _ = oauth._normalize_client_registration({
        "redirect_uris": [CALLBACK, CALLBACK],
        "client_name": "x" * 500,
    })
    assert registration is not None
    assert registration["redirect_uris"] == [CALLBACK]
    assert len(registration["client_name"]) == 200


def test_scope_must_be_exactly_mcp():
    # Criterion: any extra word is a privilege the server never defined. Echoing
    # an unknown scope back in a token teaches clients it was granted.
    assert oauth._valid_scope("mcp")
    assert not oauth._valid_scope("mcp admin")
    assert not oauth._valid_scope("")
    assert not oauth._valid_scope(None)


# ───────────────────────── access token validation ─────────────────────────

def test_an_unknown_token_is_refused(grant_state):
    assert not oauth._is_valid_mcp_token("never-issued")


def test_an_expired_token_is_refused_and_swept(grant_state):
    # Criterion: expiry must be checked at use, not only at load. And the dead
    # entry must leave the store — an unbounded graveyard of expired tokens is
    # attacker-fillable memory.
    oauth._mcp_tokens["stale"] = time.time() - 5
    oauth._mcp_token_resources["stale"] = RESOURCE
    assert not oauth._is_valid_mcp_token("stale")
    assert "stale" not in oauth._mcp_tokens
    assert "stale" not in oauth._mcp_token_resources


def test_a_live_token_passes(grant_state):
    oauth._mcp_tokens["live"] = time.time() + 60
    assert oauth._is_valid_mcp_token("live")


def test_a_token_bound_to_another_resource_is_refused(grant_state):
    # Criterion: RFC 8707 audience binding. Without it, a token minted for one
    # public URL keeps working after the deployment moves — exactly the replay
    # the binding exists to stop.
    oauth._mcp_tokens["bound"] = time.time() + 60
    oauth._mcp_token_resources["bound"] = RESOURCE
    assert not oauth._is_valid_mcp_token("bound", "https://other.example/mcp")


def test_resource_comparison_is_canonical_not_textual(grant_state):
    # HTTPS://EXAMPLE.COM:443/mcp names the same resource as the stored value.
    # A byte-wise comparison would refuse legitimate clients on a default port
    # and push deployments toward turning the binding off.
    oauth._mcp_tokens["bound"] = time.time() + 60
    oauth._mcp_token_resources["bound"] = RESOURCE
    assert oauth._is_valid_mcp_token("bound", "HTTPS://EXAMPLE.COM:443/mcp")


# ───────────────────────── static token mode ─────────────────────────

def test_blank_static_token_never_matches_blank_config(grant_state, monkeypatch):
    # Criterion: the classic "empty equals empty" hole. With no secret configured
    # the mode must be unusable, not open to whoever sends an empty header.
    monkeypatch.delenv("LOCI_MCP_TOKEN", raising=False)
    assert not oauth._is_valid_static_mcp_token("")
    assert not oauth._is_valid_static_mcp_token("anything")
    sh.config["mcp_token"] = "   "
    assert not oauth._is_valid_static_mcp_token("   ")


def test_static_token_env_beats_config(grant_state, monkeypatch):
    monkeypatch.setenv("LOCI_MCP_TOKEN", "env-secret")
    sh.config["mcp_token"] = "yaml-secret"
    assert oauth._is_valid_static_mcp_token("env-secret")
    # The config value must stop working the moment the env var exists —
    # otherwise a leaked old yaml secret stays valid forever.
    assert not oauth._is_valid_static_mcp_token("yaml-secret")


def test_static_token_config_fallback_requires_exact_match(grant_state, monkeypatch):
    monkeypatch.delenv("LOCI_MCP_TOKEN", raising=False)
    sh.config["mcp_token"] = "yaml-secret"
    assert oauth._is_valid_static_mcp_token("yaml-secret")
    assert not oauth._is_valid_static_mcp_token("yaml-secret-and-more")
    assert not oauth._is_valid_static_mcp_token("yaml-secre")


def test_oauth_routes_are_on_by_default_and_off_in_token_mode(grant_state):
    # {} means a fresh install: auth required, oauth mode. The ship-locked
    # default is a settled position (module docstring).
    assert oauth._oauth_required_from_config() is True
    sh.config["mcp_auth_mode"] = "token"
    assert oauth._oauth_required_from_config() is False
    sh.config["mcp_auth_mode"] = "oauth"
    sh.config["mcp_require_auth"] = "false"
    assert oauth._oauth_required_from_config() is False


# ───────────────────────── the authorize step ─────────────────────────

def test_authorize_refuses_unknown_clients_and_unregistered_callbacks(grant_state):
    oauth._oauth_clients["client-1"] = {
        "redirect_uris": [CALLBACK],
        "client_name": "T",
        "activated": True,
        "created_at": time.time(),
        "expires": time.time() + 3600,
    }
    ok, _ = oauth._validate_authorize_redirect("client-1", CALLBACK)
    assert ok
    assert not oauth._validate_authorize_redirect("nobody", CALLBACK)[0]
    # Exact-match only: a lookalike path on the registered host is where open
    # redirects come from.
    assert not oauth._validate_authorize_redirect(
        "client-1", CALLBACK + "/../evil"
    )[0]
    assert not oauth._validate_authorize_redirect("", CALLBACK)[0]
    assert not oauth._validate_authorize_redirect("client-1", "")[0]


def test_a_code_is_refused_when_the_password_rotated_during_its_kdf(grant_state):
    # Criterion: _store_authorization_code is a compare-and-swap on the
    # credential generation. A password verified before the rotation must not
    # publish a code after it.
    stale_generation = sh._credential_generation_snapshot() - 1
    stored = oauth._store_authorization_code(
        "raced", {"expires": time.time() + 300}, stale_generation
    )
    assert stored is False
    assert "raced" not in oauth._oauth_codes


def test_the_code_store_is_bounded_but_sweeps_expired_entries_first(grant_state):
    # Fill the store with expired junk: a public attacker must not be able to
    # wedge authorization shut by parking dead codes at the cap.
    now = time.time()
    for i in range(oauth._MAX_OAUTH_CODES):
        oauth._oauth_codes[f"dead-{i}"] = {"expires": now - 10}
    generation = sh._credential_generation_snapshot()
    assert oauth._store_authorization_code(
        "fresh", {"expires": now + 300}, generation
    )
    # And with the store full of *live* codes, refuse instead of evicting one.
    for i in range(oauth._MAX_OAUTH_CODES):
        oauth._oauth_codes[f"live-{i}"] = {"expires": now + 300}
    assert not oauth._store_authorization_code(
        "one-too-many", {"expires": now + 300}, generation
    )


# ───────────────────────── the exchange: atomic pair issuance ─────────────────────────

def test_exchange_issues_a_working_pair_and_persists_both_together(grant_state):
    code, stored = _store_fresh_code()
    pair = oauth._commit_authorization_code_exchange(code, stored, RESOURCE)
    assert pair is not None
    access, refresh = pair
    # In memory: the access token authenticates, bound to the resource.
    assert oauth._is_valid_mcp_token(access, RESOURCE)
    assert refresh in oauth._mcp_refresh_tokens
    # On disk: both halves in one file, so a restart cannot resurrect one
    # without the other (the deleted single-shot issuers used to lose exactly
    # this property).
    persisted = _tokens_file(grant_state)
    assert access in persisted["access_tokens"]
    assert refresh in persisted["refresh_tokens"]


def test_a_code_is_single_use(grant_state):
    # Criterion: replaying a sniffed code must yield nothing. The second answer
    # has to be a refusal, not a second valid token.
    code, stored = _store_fresh_code()
    assert oauth._commit_authorization_code_exchange(code, stored, RESOURCE)
    assert oauth._commit_authorization_code_exchange(code, stored, RESOURCE) is None


def test_an_expired_code_is_refused_at_the_exchange(grant_state):
    code, stored = _store_fresh_code()
    expired = dict(stored, expires=time.time() - 1)
    oauth._oauth_codes[code] = dict(expired)
    assert oauth._commit_authorization_code_exchange(code, expired, RESOURCE) is None
    assert not oauth._mcp_tokens


def test_a_password_rotation_kills_codes_already_in_flight(grant_state):
    code, stored = _store_fresh_code()
    with sh._credential_state_guard():
        sh._advance_credential_generation_locked()
    assert oauth._commit_authorization_code_exchange(code, stored, RESOURCE) is None
    # The stale code is consumed, not left around for a third try.
    assert code not in oauth._oauth_codes


def test_no_disk_write_means_no_token(grant_state, monkeypatch):
    # Criterion: issuance is persist-then-publish. If the disk write fails, no
    # token may become valid in memory — a token that exists only until the next
    # restart looks fine in every manual test and dies silently in production.
    code, stored = _store_fresh_code()

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(sh, "_atomic_write_private_json", boom)
    with pytest.raises(oauth.OAuthPersistenceError):
        oauth._commit_authorization_code_exchange(code, stored, RESOURCE)
    assert not oauth._mcp_tokens
    assert not oauth._mcp_refresh_tokens
    # The code survives for a retry: the client saw 503, not "code consumed".
    assert code in oauth._oauth_codes


# ───────────────────────── refresh rotation ─────────────────────────

def _issue_pair(grant_state):
    code, stored = _store_fresh_code()
    pair = oauth._commit_authorization_code_exchange(code, stored, RESOURCE)
    assert pair is not None
    return pair


def test_refresh_rotation_replaces_the_refresh_token(grant_state):
    _access, refresh = _issue_pair(grant_state)
    expected = dict(oauth._mcp_refresh_tokens[refresh])
    rotated = oauth._commit_refresh_token_rotation(refresh, expected, RESOURCE)
    assert rotated is not None
    new_access, new_refresh = rotated
    assert new_refresh != refresh
    assert refresh not in oauth._mcp_refresh_tokens
    assert oauth._is_valid_mcp_token(new_access, RESOURCE)
    # Replay of the rotated-out token: refuse. A refresh token that stays valid
    # after rotation makes theft undetectable forever.
    assert oauth._commit_refresh_token_rotation(refresh, expected, RESOURCE) is None


def test_an_expired_refresh_token_is_refused(grant_state):
    _access, refresh = _issue_pair(grant_state)
    expired = dict(oauth._mcp_refresh_tokens[refresh], expires=time.time() - 1)
    oauth._mcp_refresh_tokens[refresh] = dict(expired)
    assert oauth._commit_refresh_token_rotation(refresh, expired, RESOURCE) is None


def test_a_failed_rotation_keeps_the_old_refresh_token_alive(grant_state, monkeypatch):
    # The mirror of the issuance atomicity: if the rotated state cannot reach
    # disk, the old token must not have been destroyed — otherwise one full disk
    # silently logs the client out with no way back but a full reauthorization.
    _access, refresh = _issue_pair(grant_state)
    expected = dict(oauth._mcp_refresh_tokens[refresh])

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(sh, "_atomic_write_private_json", boom)
    with pytest.raises(oauth.OAuthPersistenceError):
        oauth._commit_refresh_token_rotation(refresh, expected, RESOURCE)
    assert refresh in oauth._mcp_refresh_tokens


# ───────────────────────── revocation ─────────────────────────

def test_revoke_all_grants_kills_memory_disk_and_in_flight_codes(grant_state):
    # revoke_all_mcp_grants currently has no route (the door goes back in the
    # panel-rework batch); this freezes its contract so wiring it up later
    # cannot quietly ship a half-revocation.
    access, refresh = _issue_pair(grant_state)
    _store_fresh_code("pending-code")
    oauth.revoke_all_mcp_grants()
    assert not oauth._is_valid_mcp_token(access, RESOURCE)
    assert refresh not in oauth._mcp_refresh_tokens
    assert not oauth._oauth_codes
    persisted = _tokens_file(grant_state)
    assert persisted == {"access_tokens": {}, "refresh_tokens": {}}


# ───────────────────────── persistence round trips ─────────────────────────

def test_restart_keeps_live_grants_and_drops_expired_ones(grant_state):
    access, refresh = _issue_pair(grant_state)
    now = time.time()
    # Age one access token into the past on disk.
    persisted = _tokens_file(grant_state)
    persisted["access_tokens"]["long-dead"] = {"expires": now - 100, "resource": ""}
    persisted["refresh_tokens"]["dead-refresh"] = {
        "expires": now - 100, "client_id": "c", "resource": "",
    }
    (grant_state / ".dashboard_mcp_tokens.json").write_text(
        json.dumps(persisted), encoding="utf-8"
    )
    for d in _STATE_DICTS:
        d.clear()
    oauth._load_mcp_tokens()
    assert oauth._is_valid_mcp_token(access, RESOURCE)  # survives restart, still bound
    assert refresh in oauth._mcp_refresh_tokens
    assert "long-dead" not in oauth._mcp_tokens
    assert "dead-refresh" not in oauth._mcp_refresh_tokens


def test_loading_clients_does_not_grant_consent_to_never_authorized_registrations(grant_state):
    # A registration that was never authorized is an attacker-writable slot; the
    # pending TTL must hold across a restart even when the file claims a year of
    # validity (hand-edited or written by an older build).
    now = time.time()
    registry = {
        "stale-pending": {
            "redirect_uris": [CALLBACK], "client_name": "P",
            "created_at": now - 7200, "expires": now + 9999,
        },
        "old-authorized": {
            "redirect_uris": [CALLBACK], "client_name": "A",
            "created_at": now - 7200, "expires": now + 9999, "activated": True,
        },
    }
    (grant_state / ".oauth_clients.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    oauth._load_oauth_clients()
    assert "stale-pending" not in oauth._oauth_clients
    assert "old-authorized" in oauth._oauth_clients


def test_eviction_frees_the_oldest_pending_slot_but_never_an_authorized_client(grant_state):
    # Criterion: registration flooding may evict other junk registrations, never
    # a client a human actually approved.
    registry = {
        "approved": {"activated": True, "created_at": 100.0},
        "pending-old": {"activated": False, "created_at": 200.0},
        "pending-new": {"activated": False, "created_at": 300.0},
    }
    assert oauth._evict_oldest_pending_client_locked(registry) is True
    assert "pending-old" not in registry
    assert "approved" in registry and "pending-new" in registry
    only_approved = {"approved": {"activated": True, "created_at": 100.0}}
    assert oauth._evict_oldest_pending_client_locked(only_approved) is False
    assert "approved" in only_approved


def test_activating_an_unknown_client_reports_failure(grant_state):
    assert oauth._activate_oauth_client("never-registered") is False


# ───────────────────────── public registration rate limit ─────────────────────────

class fake_request:
    """Only what _client_key reads: a socket peer and headers."""

    def __init__(self, host="203.0.113.7"):
        self.client = types.SimpleNamespace(host=host)
        self.headers = {}


@pytest.fixture
def registration_clock(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(
        oauth, "_time_mod", types.SimpleNamespace(time=lambda: clock["now"])
    )
    oauth._oauth_registration_source_attempts.clear()
    oauth._oauth_registration_global_attempts.clear()
    yield clock
    oauth._oauth_registration_source_attempts.clear()
    oauth._oauth_registration_global_attempts.clear()


def test_registration_is_throttled_per_source_and_recovers_after_the_window(registration_clock):
    # /oauth/register is unauthenticated by design (RFC-compatible DCR), so this
    # window is the only thing between the port and unlimited registry writes.
    for _ in range(oauth._OAUTH_REGISTRATION_SOURCE_MAX):
        assert oauth._reserve_oauth_registration(fake_request()) == 0
    retry = oauth._reserve_oauth_registration(fake_request())
    assert retry > 0
    # A different source is not punished for the first one's flood.
    assert oauth._reserve_oauth_registration(fake_request(host="203.0.113.9")) == 0
    # And the flooded source is admitted again once its window has passed.
    registration_clock["now"] += oauth._OAUTH_REGISTRATION_WINDOW_SECONDS + 1
    assert oauth._reserve_oauth_registration(fake_request()) == 0


# ───────────────────────── what the panel may say about the static token ─────────────────────────

def test_the_token_mask_never_contains_the_token():
    token = "secret-token-0123456789abcdef"
    mask = CA._mask_mcp_token(token)
    assert token not in mask
    assert mask == "secr...cdef"
    # Short tokens: first4+last4 would reconstruct most of the secret, so the
    # mask degrades to fully opaque instead.
    assert CA._mask_mcp_token("12345678") == "***"
    assert CA._mask_mcp_token("") is None


def test_current_mcp_token_env_beats_config(monkeypatch):
    monkeypatch.setattr(sh, "config", {"mcp_token": "yaml-secret"}, raising=False)
    monkeypatch.setenv("LOCI_MCP_TOKEN", "  env-secret  ")
    assert CA._current_mcp_token() == "env-secret"
    monkeypatch.delenv("LOCI_MCP_TOKEN")
    assert CA._current_mcp_token() == "yaml-secret"


def test_mcp_auth_mode_never_answers_something_the_middleware_does_not_know():
    # Only two modes exist. Junk normalizing to anything else would make the
    # panel display a mode no code path implements.
    assert CA._mcp_auth_mode({"mcp_auth_mode": "token"}) == "token"
    assert CA._mcp_auth_mode({"mcp_auth_mode": "  OAuth "}) == "oauth"
    assert CA._mcp_auth_mode({"mcp_auth_mode": "off"}) == "oauth"
    assert CA._mcp_auth_mode({}) == "oauth"
    assert CA._mcp_auth_mode("not-a-mapping") == "oauth"
