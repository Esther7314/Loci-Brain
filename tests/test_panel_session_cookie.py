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
import asyncio
import builtins
import hashlib
import json
import os
import time
import types

import pytest

from web import panel_auth as PA
from web import _shared as sh

HASH_A = "pbkdf2_sha256$240000$aa11$feed"
HASH_B = "pbkdf2_sha256$240000$bb22$beef"

# What `_key()` used to return whenever the hash was unavailable: sha256 of the prefix
# with an empty string appended. It is a constant, so anybody could compute it and mint
# a cookie the panel would accept.
EMPTY_DERIVED_KEY = hashlib.sha256(b"loci-panel-v1:").digest()


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


# ─────────────── a broken auth store: locked, not opened (fail-closed) ───────────────
#
# The bug this section freezes: `_load_password_hash()` answered "the file is corrupt"
# and "no password was ever set" with the same None, and gate_needed() read that None as
# rule 1 — do not lock a door that has no key. So corrupting or chmod-ing one file
# **unlocked the panel**, at the one moment a lock matters most. `hook_ok` in the same
# module had the opposite stance all along ("locked and misconfigured -> refuse"); these
# assertions pull the rest of the module onto that side and pin it there.
#
# The other direction is asserted just as hard, immediately below: an auth store that
# reads perfectly well and simply holds no password must still NOT lock. That is rule 1
# of the module header, and it has locked its owner out once already.


@pytest.fixture
def real_store(tmp_path, monkeypatch):
    """A real auth file under tmp_path — no monkeypatched hash loader, because what is
    being tested is exactly how the file's contents are read."""
    monkeypatch.setattr(
        sh, "config", {"buckets_dir": str(tmp_path), "panel_auth": True}, raising=False
    )
    monkeypatch.delenv("LOCI_DASHBOARD_PASSWORD", raising=False)
    return tmp_path


def _write_store(text: str) -> None:
    with open(sh._get_auth_file(), "w", encoding="utf-8") as f:
        f.write(text)


def _break_store(shape: str, monkeypatch) -> None:
    """One of the shapes a real auth file goes wrong in."""
    path = sh._get_auth_file()
    if shape == "bad json":
        _write_store('{"password_hash": "pbkdf2_sha256$240000$aa11$feed"')  # no brace
    elif shape == "half a file":
        _write_store("")            # an interrupted write leaves zero bytes
    elif shape == "not an object":
        _write_store(json.dumps(["password_hash", HASH_A]))
    elif shape == "a directory in its place":
        if os.path.isfile(path):
            os.remove(path)
        os.makedirs(path, exist_ok=True)
    elif shape == "cannot be read":
        _write_store(json.dumps({"password_hash": HASH_A}))
        real_open = builtins.open

        def denied(file, *a, **kw):
            if str(file) == path:
                raise PermissionError(13, "Permission denied")
            return real_open(file, *a, **kw)

        monkeypatch.setattr(builtins, "open", denied)
    else:                                                    # pragma: no cover
        raise AssertionError(f"unknown shape {shape}")


BROKEN = ["bad json", "half a file", "not an object",
          "a directory in its place", "cannot be read"]


@pytest.mark.parametrize("shape", BROKEN)
def test_the_read_layer_tells_broken_apart_from_never_set(real_store, monkeypatch, shape):
    # Criterion: the two states must be distinguishable **at the read layer**. If
    # anything upstream collapses them again — a bare `except: return {}` — every
    # assertion below it becomes untestable and the hole reopens.
    _break_store(shape, monkeypatch)
    with pytest.raises(sh.AuthPersistenceError):
        sh._load_password_hash()


@pytest.mark.parametrize("shape", BROKEN)
def test_a_broken_store_locks_the_gate(real_store, monkeypatch, shape):
    _break_store(shape, monkeypatch)
    assert PA.gate_needed() is True, shape


@pytest.mark.parametrize("shape", BROKEN)
def test_a_broken_store_refuses_every_session(real_store, monkeypatch, shape):
    # A cookie that was valid a second ago must stop verifying: with the hash
    # unreadable there is nothing to prove it against, and "cannot check" is not
    # "checked out fine".
    _write_store(json.dumps({"password_hash": HASH_A}))
    cookie = PA._make_cookie()
    assert PA.has_session(fake_request(cookie))
    _break_store(shape, monkeypatch)
    assert not PA.has_session(fake_request(cookie)), shape


@pytest.mark.parametrize("shape", BROKEN)
def test_a_broken_store_never_signs_with_the_empty_string(real_store, monkeypatch, shape):
    # The old fallback was `h = ""`, i.e. one fixed key for every broken install on
    # earth — forging a session cookie needs no secret at all, only this constant.
    _break_store(shape, monkeypatch)
    assert PA._key() != EMPTY_DERIVED_KEY, shape


def test_no_password_at_all_still_never_signs_with_the_empty_string(real_store):
    # Same constant, reached the other way: no file, so no hash to derive from. The
    # gate is open here, but the key must still be unguessable — the day some route
    # checks has_session() without asking gate_needed() first, this is what stands
    # between a forged cookie and the panel.
    assert PA._key() != EMPTY_DERIVED_KEY


@pytest.mark.parametrize("shape", BROKEN)
def test_a_broken_store_is_not_a_fresh_install(real_store, monkeypatch, shape):
    # _is_setup_needed() gates first-run password setup, which needs **no** current
    # password. Answering True here would turn "corrupt the auth file" into "claim the
    # panel": /api/loci/auth/set-password and /oauth/authorize would both treat the
    # instance as never configured.
    _break_store(shape, monkeypatch)
    assert sh._is_setup_needed() is False, shape


@pytest.mark.parametrize("shape", BROKEN)
def test_a_broken_store_says_so_where_someone_can_read_it(real_store, monkeypatch, caplog, shape):
    # Rule from hook_ok: when it breaks it has to say how to fix it. A panel that
    # silently locks everyone out, with nothing in the log naming the file, is the
    # failure mode this module has already produced three times.
    _break_store(shape, monkeypatch)
    monkeypatch.setattr(PA, "_storage_broken_logged_at", 0.0, raising=False)
    with caplog.at_level("ERROR", logger="loci_brain.web.panel_auth"):
        PA.gate_needed()
    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "存储损坏" in said and ".dashboard_auth.json" in said, shape


# ─────────── and the other half of the rule: readable-but-empty still does not lock ───────────

def test_a_store_that_is_merely_absent_does_not_lock(real_store):
    # Rule 1, unchanged and non-negotiable: a fresh install has no auth file, and it
    # must open. If the fail-closed change above ever spreads to this case, the first
    # thing a new user meets is a password prompt with no password.
    assert not os.path.exists(sh._get_auth_file())
    assert sh._load_password_hash() is None
    assert sh._is_setup_needed() is True
    assert PA.gate_needed() is False


def test_a_store_whose_password_field_is_cleanly_absent_does_not_lock(real_store):
    # The file is valid JSON and readable; it just carries no password (only a
    # recovery question was ever saved). That is "no password set", not "broken".
    _write_store(json.dumps({"security_question": "first cat?"}))
    assert sh._load_password_hash() is None
    assert PA.gate_needed() is False


# ─────────────── /auth/login on a broken store: refuse, and say what broke ───────────────

class _RouteCapture:
    """Just enough of `mcp` to collect the handlers `register()` hands over."""

    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods=None, **kw):
        def deco(fn):
            self.routes[path] = fn
            return fn
        return deco


class post_request:
    """Only what /auth/login reads: a JSON body, the peer address, headers."""

    def __init__(self, body, host="203.0.113.7"):
        self._body = body
        self.cookies = {}
        self.headers = {}
        self.client = types.SimpleNamespace(host=host)

    async def json(self):
        return self._body


@pytest.fixture
def login_route():
    capture = _RouteCapture()
    PA.register(capture)
    yield capture.routes["/auth/login"]
    # The route records failures in process-wide throttle state; leaving them behind
    # would lock a later test out of a source it never used.
    with sh._login_state_lock:
        for store in (sh._login_failures, sh._login_locked_until, sh._login_source_lru):
            for key in [k for k in store if str(k).startswith("203.0.113.")]:
                store.pop(key, None)


def test_login_on_a_broken_store_refuses_and_names_the_file(real_store, monkeypatch, login_route):
    # It already refused — with the hash unreadable nothing verifies — but it refused
    # as "密码不对", which sends the owner off to retype a password that cannot work.
    # The refusal has to point at the file that actually broke.
    _break_store("bad json", monkeypatch)
    resp = asyncio.run(login_route(post_request({"password": "whatever"})))
    assert resp.status_code == 503
    assert ".dashboard_auth.json" in json.loads(resp.body)["error"]


def test_login_on_a_healthy_store_still_answers_401_for_a_wrong_password(real_store, login_route):
    # The counterweight: "broken" must be a real detection, not a permanent 503 that
    # would swallow every ordinary typo and hide the rate limiter behind it.
    _write_store(json.dumps({"password_hash": sh._hash_secret("right-pw")}))
    resp = asyncio.run(login_route(post_request({"password": "wrong-pw"})))
    assert resp.status_code == 401


def test_the_switch_still_wins_over_a_broken_store(real_store, monkeypatch):
    # panel_auth=false is somebody's explicit decision about their own trusted LAN.
    # Fail-closed applies to "we cannot tell whether a password exists", not to
    # "the owner said no lock" — overriding that would lock people out of installs
    # that never wanted a lock.
    _break_store("bad json", monkeypatch)
    sh.config["panel_auth"] = False
    assert PA.gate_needed() is False
