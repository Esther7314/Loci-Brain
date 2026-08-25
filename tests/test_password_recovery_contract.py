# -*- coding: utf-8 -*-
"""CONTRACT: web/_shared.py — the password family and forgotten-password recovery.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    One password guards the panel and the remote MCP authorization page. The
    helpers here decide what verifies, what a recovery answer may overwrite, and
    when a brute-force source gets locked out. Every one of them fails silently:
    a wrong answer accepted, a stale rotation that clobbers a newer password, a
    lockout that never engages — none of these raise.

    _save_security_qa / _verify_security_answer currently have no caller (the
    recovery flow's front door was cut in the strip-down), but the decision on
    2026-08-25 is that forgotten-password recovery is coming back. This file
    freezes their behavior contract *before* that rewiring, so the repair has a
    definition of "working" to build against instead of re-deriving one.

    All state lives under tmp_path via a synthetic sh.config; secrets are fakes;
    no environment leaks past monkeypatch. Rate-limit tests stay inside the
    15-minute window on the real clock, so nothing sleeps.
"""
import hashlib
import types

import pytest

from web import _shared as sh


@pytest.fixture
def auth_store(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)}, raising=False)
    monkeypatch.delenv("LOCI_DASHBOARD_PASSWORD", raising=False)
    return tmp_path


def _legacy_hash(secret: str, salt: str = "ab12cd34") -> str:
    return f"{salt}:{hashlib.sha256(f'{salt}:{secret}'.encode()).hexdigest()}"


# ───────────────────────── the KDF and its verifier ─────────────────────────

def test_a_wrong_password_never_verifies():
    stored = sh._hash_secret("right-password")
    assert sh._verify_secret("right-password", stored)
    assert not sh._verify_secret("wrong-password", stored)
    assert not sh._verify_secret("", stored)


def test_each_hash_gets_its_own_salt():
    # Two users with the same password must not share a hash — equal hashes turn
    # one cracked entry into every entry, and reveal password reuse for free.
    first, second = sh._hash_secret("same"), sh._hash_secret("same")
    assert first != second
    assert sh._verify_secret("same", first) and sh._verify_secret("same", second)


def test_the_legacy_format_still_verifies_but_only_exactly():
    stored = _legacy_hash("old-pw")
    assert sh._verify_secret("old-pw", stored)
    assert not sh._verify_secret("old-pw-x", stored)


@pytest.mark.parametrize("stored", [
    "",
    "justastring",
    "pbkdf2_sha256$not-an-int$aa$bb",
    "pbkdf2_sha256$1000$not-hex!$bb",
])
def test_a_corrupt_stored_hash_refuses_instead_of_crashing(stored):
    # Criterion: a mangled auth file must read as "wrong password", never as an
    # exception a route's broad except turns into "let them in" or a 500 loop.
    assert sh._verify_secret("anything", stored) is False


def test_needs_rehash_flags_legacy_and_weakened_entries_only():
    assert sh._needs_rehash(_legacy_hash("x")) is True
    assert sh._needs_rehash("pbkdf2_sha256$1000$aa$bb") is True  # below current cost
    assert sh._needs_rehash(sh._hash_secret("x")) is False


def test_a_legacy_hash_is_silently_upgraded_on_successful_login(auth_store):
    # The upgrade may only ride on a *successful* verification (that is the one
    # moment the plaintext is legitimately in hand) and must not change what
    # verifies.
    sh._atomic_write_private_json(
        sh._get_auth_file(), {"password_hash": _legacy_hash("old-pw")}
    )
    assert sh._verify_any_password("old-pw") is True
    upgraded = sh._load_password_hash()
    assert upgraded.startswith(sh._PBKDF2_ALGO + "$")
    assert sh._verify_any_password("old-pw") is True
    assert sh._verify_any_password("wrong") is False


# ───────────────────────── saving: compare-and-swap, not last-writer-wins ─────────────────────────

def test_password_save_and_verify_roundtrip(auth_store):
    assert sh._is_setup_needed() is True
    assert sh._save_password_hash("first-pw") is True
    assert sh._is_setup_needed() is False
    assert sh._verify_any_password("first-pw") is True
    assert sh._verify_any_password("not-it") is False


def test_a_save_expecting_the_wrong_current_hash_is_refused(auth_store):
    assert sh._save_password_hash("first-pw")
    assert sh._save_password_hash("evil-pw", expected_hash="stale-hash") is False
    # The refused write changed nothing.
    assert sh._verify_any_password("first-pw") is True
    assert sh._verify_any_password("evil-pw") is False


def test_a_save_bound_to_an_old_generation_is_refused(auth_store):
    assert sh._save_password_hash("first-pw")
    stale_generation = sh._credential_generation_snapshot()
    assert sh._save_password_hash("second-pw")  # someone else rotates first
    assert (
        sh._save_password_hash("late-pw", expected_generation=stale_generation)
        is False
    )
    assert sh._verify_any_password("second-pw") is True


def test_the_environment_password_replaces_the_stored_one_entirely(auth_store, monkeypatch):
    # LOCI_DASHBOARD_PASSWORD is an override, not a second valid password. If the
    # file hash kept verifying alongside it, "I rotated via the env var" would
    # leave the old password silently alive.
    assert sh._save_password_hash("file-pw")
    monkeypatch.setenv("LOCI_DASHBOARD_PASSWORD", "env-pw")
    assert sh._verify_any_password("env-pw") is True
    assert sh._verify_any_password("file-pw") is False
    assert sh._is_setup_needed() is False


# ───────────────────────── forgotten-password recovery ─────────────────────────

def test_the_security_answer_is_case_and_whitespace_insensitive(auth_store):
    # Criterion: "What was the cat called" answered years later as "fluffy",
    # " Fluffy " or "FLUFFY" is the same human. Refusing those turns recovery
    # into a second way to be locked out.
    assert sh._save_security_qa("first cat?", "  Fluffy ") is True
    assert sh._verify_security_answer("fluffy") is True
    assert sh._verify_security_answer("FLUFFY  ") is True
    assert sh._verify_security_answer("fluffy the cat") is False


def test_no_question_set_means_no_answer_verifies(auth_store):
    assert sh._verify_security_answer("") is False
    assert sh._verify_security_answer("anything") is False


def test_changing_the_password_keeps_the_recovery_question(auth_store):
    # keep_qa is the default on purpose: a password change must not quietly
    # delete the only way back in.
    assert sh._save_password_hash("first-pw")
    assert sh._save_security_qa("first cat?", "fluffy")
    assert sh._save_password_hash("second-pw")
    assert sh._load_auth_data().get("security_question") == "first cat?"
    assert sh._verify_security_answer("fluffy") is True


def test_recovery_is_a_compare_and_swap_against_concurrent_password_changes(auth_store):
    # The exact race /auth/recover guards: the answer was verified, then someone
    # (owner or attacker) changed the password before the reset committed. The
    # stale proof must not overwrite the newer credential.
    assert sh._save_password_hash("first-pw")
    assert sh._save_security_qa("first cat?", "fluffy")
    proof = sh._verify_security_answer_for_rotation("  FLUFFY ")
    assert proof is not None
    assert sh._save_password_hash("changed-meanwhile")  # the race
    assert (
        sh._save_password_hash("recovered-pw", expected_generation=proof.generation)
        is False
    )
    assert sh._verify_any_password("changed-meanwhile") is True
    assert sh._verify_any_password("recovered-pw") is False
    # And the unraced flow still works: fresh proof, immediate commit.
    fresh = sh._verify_security_answer_for_rotation("fluffy")
    assert fresh is not None
    assert sh._save_password_hash("recovered-pw", expected_generation=fresh.generation)
    assert sh._verify_any_password("recovered-pw") is True


def test_setting_the_question_invalidates_in_flight_proofs(auth_store):
    # Updating the QA advances the credential generation, so a proof obtained
    # against the old answer cannot authorize anything afterwards.
    assert sh._save_password_hash("first-pw")
    before = sh._credential_generation_snapshot()
    assert sh._save_security_qa("first cat?", "fluffy")
    assert sh._credential_generation_snapshot() > before


# ───────────────────────── online brute force: the lockout ─────────────────────────

class fake_request:
    """Only what the throttle reads: the socket peer and headers."""

    def __init__(self, host):
        self.client = types.SimpleNamespace(host=host)
        self.headers = {}


@pytest.fixture
def throttle_state():
    yield
    with sh._login_state_lock:
        for store in (sh._login_failures, sh._login_locked_until, sh._login_source_lru):
            for key in [k for k in store if str(k).startswith("198.51.100.")]:
                store.pop(key, None)


def test_repeated_failures_lock_the_source_out(throttle_state):
    attacker = fake_request("198.51.100.10")
    for _ in range(sh._LOGIN_MAX_FAILURES - 1):
        sh._record_login_failure(attacker)
    assert sh._login_retry_after(attacker) == 0  # one short of the line: still allowed
    sh._record_login_failure(attacker)
    assert sh._login_retry_after(attacker) > 0  # at the line: locked, with a wait time


def test_the_lockout_is_per_source_not_global(throttle_state):
    # One flooding client must not lock the owner out of their own panel.
    attacker, owner = fake_request("198.51.100.11"), fake_request("198.51.100.12")
    for _ in range(sh._LOGIN_MAX_FAILURES):
        sh._record_login_failure(attacker)
    assert sh._login_retry_after(attacker) > 0
    assert sh._login_retry_after(owner) == 0


def test_a_successful_login_clears_the_slate(throttle_state):
    fumbler = fake_request("198.51.100.13")
    for _ in range(sh._LOGIN_MAX_FAILURES):
        sh._record_login_failure(fumbler)
    assert sh._login_retry_after(fumbler) > 0
    sh._record_login_success(fumbler)
    assert sh._login_retry_after(fumbler) == 0


def test_rotating_ipv6_interface_ids_shares_one_bucket():
    # A /64 is one delegation; letting each interface id be its own bucket gives
    # an IPv6 attacker ~2^64 fresh identities against a per-source limit.
    assert sh._normalize_login_source("2001:db8::1") == sh._normalize_login_source(
        "2001:db8::dead:beef"
    )
    assert sh._normalize_login_source("2001:db8::1") != sh._normalize_login_source(
        "2001:db8:0:1::1"
    )
    assert sh._normalize_login_source("::ffff:192.0.2.1") == "192.0.2.1"
