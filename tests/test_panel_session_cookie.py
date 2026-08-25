# -*- coding: utf-8 -*-
"""CONTRACT: web/panel_auth.py — the signed cookie and the lock-only-with-a-key rule.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    The hook-route half of this module already has its own file
    (test_hook_auth.py). This one covers the other half: the session cookie that
    stands in for the password for two weeks, and gate_needed(), the single
    judgment deciding whether the panel is locked at all.

    Both judgments fail silently by construction — a forged cookie that passes
    does not raise, it just serves the panel; a gate that reads its switch wrong
    does not raise, it just never asks. Only assertions can see either.

    The signing key is derived from the password hash on purpose (rule 2 in the
    module header): changing the password must kill every session with no
    separate revocation machinery. That derivation is asserted here, because it
    is the kind of property a refactor to "store the key in its own file"
    would remove without turning anything red.
"""
import time

import pytest

from web import panel_auth as PA
from web import _shared as sh

HASH_A = "pbkdf2_sha256$240000$aa11$feed"
HASH_B = "pbkdf2_sha256$240000$bb22$beef"


class fake_request:
    """Only what has_session reads: cookies."""

    def __init__(self, cookie=None):
        self.cookies = {} if cookie is None else {PA._COOKIE: cookie}
        self.headers = {}


@pytest.fixture
def with_password(monkeypatch):
    monkeypatch.setattr(sh, "config", {"panel_auth": True}, raising=False)
    monkeypatch.setattr(sh, "_load_password_hash", lambda: HASH_A, raising=False)


# ───────────────────────── the cookie ─────────────────────────

def test_a_fresh_cookie_opens_the_door(with_password):
    assert PA.has_session(fake_request(PA._make_cookie()))


def test_an_expired_cookie_is_refused_even_with_a_valid_signature(with_password):
    # Criterion: the signature proves who minted it, the timestamp says until
    # when. Checking only the signature makes every cookie eternal.
    exp = int(time.time()) - 10
    assert not PA.has_session(fake_request(f"{exp}.{PA._sign(exp)}"))


def test_extending_the_expiry_by_hand_breaks_the_signature(with_password):
    # The expiry is attacker-writable text in the cookie. Signing must cover it,
    # or "edit the number" is a lifetime extension.
    exp = int(time.time()) + 100
    sig = PA._sign(exp)
    assert not PA.has_session(fake_request(f"{exp + 999999}.{sig}"))


def test_forged_and_garbage_cookies_are_refused(with_password):
    exp = int(time.time()) + 100
    assert not PA.has_session(fake_request(f"{exp}.{'0' * 32}"))
    assert not PA.has_session(fake_request("no-dot-in-here"))
    assert not PA.has_session(fake_request(f"not-a-number.{PA._sign(exp)}"))
    assert not PA.has_session(fake_request(""))
    assert not PA.has_session(fake_request())  # no cookie at all


def test_changing_the_password_kills_every_existing_session(with_password, monkeypatch):
    # Criterion: rule 2 of the module. The old cookie is signed with a key
    # derived from the old hash; after a password change it must read as forged,
    # with no revocation list involved.
    old_cookie = PA._make_cookie()
    assert PA.has_session(fake_request(old_cookie))
    monkeypatch.setattr(sh, "_load_password_hash", lambda: HASH_B, raising=False)
    assert not PA.has_session(fake_request(old_cookie))
    # And a cookie minted under the new password works.
    assert PA.has_session(fake_request(PA._make_cookie()))


# ───────────────────────── gate_needed: locked only when it can be ─────────────────────────

def test_the_gate_locks_only_with_both_the_switch_and_a_password(monkeypatch):
    monkeypatch.setattr(sh, "_load_password_hash", lambda: HASH_A, raising=False)
    monkeypatch.setattr(sh, "config", {"panel_auth": True}, raising=False)
    assert PA.gate_needed() is True
    # Switch off: explicitly trusted LAN, stays open regardless of the password.
    sh.config["panel_auth"] = False
    assert PA.gate_needed() is False


def test_no_password_means_no_lock(monkeypatch):
    # Criterion: rule 1 of the module — locking a door before a key exists locks
    # the owner out of a fresh install (which has actually happened once).
    monkeypatch.setattr(sh, "config", {"panel_auth": True}, raising=False)
    monkeypatch.setattr(sh, "_load_password_hash", lambda: None, raising=False)
    assert PA.gate_needed() is False


def test_the_switch_reads_quoted_false_as_off(monkeypatch):
    # config.yaml hands over strings; bool("false") is True. Getting this wrong
    # flips the meaning of every explicit opt-out.
    monkeypatch.setattr(sh, "_load_password_hash", lambda: HASH_A, raising=False)
    for spelling in ("false", "0", "off", "no", "None", ""):
        monkeypatch.setattr(sh, "config", {"panel_auth": spelling}, raising=False)
        assert PA.gate_needed() is False, spelling


def test_the_gate_defaults_to_locked_when_the_switch_is_absent(monkeypatch):
    # Ship-locked default: an installation that set a password but never touched
    # the switch gets the lock, not the exception.
    monkeypatch.setattr(sh, "_load_password_hash", lambda: HASH_A, raising=False)
    monkeypatch.setattr(sh, "config", {}, raising=False)
    assert PA.gate_needed() is True
