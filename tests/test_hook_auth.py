# -*- coding: utf-8 -*-
"""Unit tests for the bridge-token gate on the four hook routes.

BACKGROUND — why these four routes are special
    The panel is protected by a password. Four routes were not, and could not be,
    because the caller is a separate process (the bridge) that has no browser cookie:

        GET  /api/loci/poke
        GET  /api/muse/pending
        GET  /api/dream/current
        POST /api/loci/dream/wake

    The original answer was to put them on the public list. The reason was sound —
    the bridge really has no cookie — but the remedy took the door off its hinges
    instead of cutting the bridge a key. An external review demonstrated the
    consequence on a running instance: with the panel password set and every other
    route returning 401, these four still served dream text and still mutated state
    (`dream/wake` advances the lifecycle and bumps the recall counter).

    On a machine bound to loopback that is close to harmless. Behind a tunnel, a
    reverse proxy, a LAN binding, or one bad config line, it is an open read/write
    surface on someone's private memory.

THE RULE THIS FILE GUARDS
    Once the door is locked, there are no exceptions.

        door unlocked (no panel password)  -> everything through, as before
        door locked                        -> these four need the key, in a header

    Fail closed: door locked and no key configured means refuse. A lock that only
    exists when the config happens to be right is not a lock — and "the config was
    wrong so it defaulted to open" is the exact shape of the original bug.

WHY UNIT TESTS AND NOT ONLY END-TO-END
    Proving the locked half end-to-end means setting a panel password on a running
    instance. These are pure functions over a fake request, so both halves get
    tested without touching anybody's configuration. The end-to-end suite covers
    the unlocked half against a real server; the two halves together are the whole
    claim.
"""
import pytest

from web import panel_auth as PA


class fake_request:
    """Only what `hook_ok` reads: headers and cookies."""

    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


@pytest.fixture
def door_locked(monkeypatch):
    monkeypatch.setattr(PA, "gate_needed", lambda: True)
    monkeypatch.setattr(PA, "has_session", lambda r: False)


@pytest.fixture
def door_unlocked(monkeypatch):
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(PA, "has_session", lambda r: False)


def with_key(monkeypatch, value):
    monkeypatch.setattr(PA, "hook_token", lambda: value)


# ───────────────────────── which routes are which ─────────────────────────

def test_the_four_hook_routes_are_no_longer_on_the_public_list():
    # The whole defect was these sitting in PUBLIC_PATHS. If anyone moves one back
    # to make a client "just work" again, that is the bug returning, and it returns
    # silently — nothing errors, the route simply stops asking.
    for path in ("/api/loci/poke", "/api/muse/pending",
                 "/api/dream/current", "/api/loci/dream/wake"):
        assert PA.is_hook(path), f"{path} should be a hook route"
        assert path not in PA.PUBLIC_PATHS, f"{path} is public again — that is the bug"


def test_login_routes_stay_public():
    # Guards the other direction: over-tightening locks people out of their own
    # panel with no way back in, which is worse than the hole we are closing.
    for path in ("/auth/login", "/api/loci/auth/state", "/loci"):
        assert PA.is_public(path)
        assert not PA.is_hook(path)


def test_an_ordinary_api_route_is_neither():
    # Ordinary routes must keep going through the cookie gate. If one of them
    # drifted into the hook set it would start accepting the bridge token instead
    # of a login — a downgrade dressed up as a fix.
    assert not PA.is_hook("/api/loci/graph")
    assert not PA.is_public("/api/loci/graph")
    assert not PA.is_host_read("/api/loci/graph")
    # recall is a host read (decision Q1): a host's credential reads it under its scope,
    # but without one it is still the panel's, behind the login — not a hook route.
    assert PA.is_host_read("/api/loci/recall") and not PA.is_hook("/api/loci/recall")


# ───────────────────────── the door is not locked ─────────────────────────

def test_door_unlocked_lets_everything_through(door_unlocked, monkeypatch):
    # Someone who never set a panel password must not be blocked by a lock they
    # never asked for. This is the half the end-to-end suite also covers.
    with_key(monkeypatch, "")
    ok, _ = PA.hook_ok(fake_request())
    assert ok


def test_door_unlocked_ignores_a_wrong_key(door_unlocked, monkeypatch):
    # With no lock, the key is not part of the decision at all. Were it otherwise,
    # a stale token in someone's environment would break a setup that has no
    # security to enforce in the first place.
    with_key(monkeypatch, "right")
    ok, _ = PA.hook_ok(fake_request({PA.HOOK_HEADER: "wrong"}))
    assert ok


# ───────────────────────── the door is locked ─────────────────────────

def test_locked_and_no_key_configured_refuses_and_says_how_to_fix(door_locked, monkeypatch):
    # Fail closed. The interesting half of this assertion is the second one:
    # the refusal has to be actionable. A silent or cryptic failure here means the
    # bridge stops working and nobody knows why — and this whole area of the
    # codebase has already produced three "it just quietly stopped" bugs.
    with_key(monkeypatch, "")
    ok, said = PA.hook_ok(fake_request())
    assert not ok
    assert PA.HOOK_HEADER in said and "LOCI_HOOK_TOKEN" in said


def test_locked_with_the_right_key_passes(door_locked, monkeypatch):
    with_key(monkeypatch, "s3cret")
    ok, _ = PA.hook_ok(fake_request({PA.HOOK_HEADER: "s3cret"}))
    assert ok


def test_locked_with_a_wrong_key_refuses(door_locked, monkeypatch):
    with_key(monkeypatch, "s3cret")
    ok, said = PA.hook_ok(fake_request({PA.HOOK_HEADER: "nope"}))
    assert not ok
    assert PA.HOOK_HEADER in said


def test_locked_with_no_header_at_all_refuses(door_locked, monkeypatch):
    # The plain attack: reach the port, ask for the dream, send nothing.
    with_key(monkeypatch, "s3cret")
    ok, _ = PA.hook_ok(fake_request())
    assert not ok


def test_locked_with_an_empty_header_refuses(door_locked, monkeypatch):
    # An empty header must never compare equal to a configured key. Getting this
    # wrong turns the lock into decoration.
    with_key(monkeypatch, "s3cret")
    assert not PA.hook_ok(fake_request({PA.HOOK_HEADER: ""}))[0]


def test_locked_but_a_logged_in_browser_still_gets_through(door_locked, monkeypatch):
    # The panel itself reads these routes. If a logged-in session were refused,
    # the fix would break the page it is meant to protect.
    monkeypatch.setattr(PA, "has_session", lambda r: True)
    with_key(monkeypatch, "s3cret")
    ok, _ = PA.hook_ok(fake_request())
    assert ok


def test_the_key_travels_in_a_header_not_the_url():
    # Query strings end up in access logs, Referer headers and browser history.
    # A secret that rides in the URL leaks by being used.
    assert PA.HOOK_HEADER.startswith("x-") and "?" not in PA.HOOK_HEADER


def test_key_comparison_is_not_a_prefix_match(door_locked, monkeypatch):
    # A truncated key must fail. `startswith`-style comparisons look correct in
    # every hand test and hand the attacker a byte-at-a-time oracle.
    with_key(monkeypatch, "s3cretlonger")
    assert not PA.hook_ok(fake_request({PA.HOOK_HEADER: "s3cret"}))[0]
    assert not PA.hook_ok(fake_request({PA.HOOK_HEADER: "s3cretlonger_and_more"}))[0]


def test_env_beats_config_for_the_key(monkeypatch):
    # Deployments override config with the environment; if config won, a stale
    # value in a file would silently outrank what the operator just set.
    monkeypatch.setenv("LOCI_HOOK_TOKEN", "from-env")
    assert PA.hook_token() == "from-env"


def test_a_blank_env_key_falls_back_instead_of_locking_everyone_out(monkeypatch):
    # `LOCI_HOOK_TOKEN=` (set but empty) is a common way to end up with an empty
    # string. It must read as "not configured", not as "the key is the empty
    # string" — the latter would make every request with no header succeed.
    monkeypatch.setenv("LOCI_HOOK_TOKEN", "   ")
    monkeypatch.setattr(PA.sh, "config", {}, raising=False)
    assert PA.hook_token() == ""


def test_whitespace_around_a_real_key_is_trimmed(monkeypatch):
    # Copy-pasting a token out of a terminal drags a newline along more often than
    # not. An untrimmed key compares unequal to the one the bridge sends and the
    # failure looks like "wrong key" rather than "stray whitespace".
    monkeypatch.setenv("LOCI_HOOK_TOKEN", "  s3cret\n")
    assert PA.hook_token() == "s3cret"
