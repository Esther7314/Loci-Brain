"""
========================================
web/_shared.py — runtime dependencies and password tooling shared across the panel/MCP boundary
========================================

The counterpart of core/runtime.py: modules under web/ and bridge/ take their runtime
dependency (config) and their cross-cutting helpers (password hashing, login rate
limiting, security-question recovery) from here.

This is **not "dashboard authentication"**: there are no cookie sessions, and the panel's
/api/* routes are not authenticated at this layer.
**The password and rate-limit primitives are here** because the remote MCP OAuth
authorization page in bridge/oauth.py needs the same password, and the same brute-force
resistance around it. That is "one password, two doorways", not "authentication lives
here".

Why it is a separate file at all:
- Routes live in many modules; server.py holds none of them.
- Password verification is bridge/oauth.py's single cross-cutting dependency, so it needs
  exactly one source. Without that, splitting the routes duplicates it everywhere.

Key behaviour:
- init(config): server.py injects the config at startup; functions then read
  config["buckets_dir"] as needed.
- Passwords: PBKDF2-HMAC-SHA256, stored in <buckets_dir>/.dashboard_auth.json. The
  LOCI_DASHBOARD_PASSWORD environment variable overrides it. The security question exists
  for forgotten-password recovery.
- Credential generation (_credential_state_guard and friends): rotating the password
  invalidates in-flight OAuth code/token derivations, and MCP OAuth persistence takes this
  same lock.

What this does NOT do:
- It defines no routes. Routes live in web/<module>.py and bridge/<module>.py and are
  registered with register(mcp).
- It holds no business engines. bucket_mgr and the rest still live in server.py /
  core/runtime, and would be injected the same way if ever needed here.
- It does not deal with cookie sessions; there are none.

Public surface: init, plus the password and login rate-limit helpers.
========================================
"""

import os
import time
import asyncio
import json as _json_lib
import hashlib
import hmac
import ipaddress
import secrets
import logging
import threading
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("loci_brain")

# --- Runtime environment probe: Docker vs bare metal ---
# Local vectorization has to branch on the host type: inside Docker, Ollama is a separate
# container and is reached by service name; on bare metal it is reached on 127.0.0.1.
# The result is cached once so this is not I/O on every call.
_in_docker_cache: "bool | None" = None


def in_docker() -> bool:
    """Whether we are running inside a Docker container. Checks /.dockerenv and
    /proc/1/cgroup. Cached."""
    global _in_docker_cache
    if _in_docker_cache is not None:
        return _in_docker_cache
    found = False
    try:
        if os.path.exists("/.dockerenv"):
            found = True
        else:
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            found = ("docker" in txt) or ("containerd" in txt) or ("kubepods" in txt)
    except Exception:
        found = False
    _in_docker_cache = found
    return found


def _path_is_on_non_root_mount(path: str) -> bool:
    """Return true when path is a mount point or lives below one.

    Render users commonly mount a disk at ``/var/data`` and place buckets in
    ``/var/data/buckets``.  ``os.path.ismount`` only recognizes the former.
    Never count the filesystem root: on Render that is precisely the ephemeral
    layer we are trying to distinguish from a persistent disk.
    """
    if not path:
        return False
    try:
        current = os.path.realpath(os.path.abspath(path))
        while True:
            parent = os.path.dirname(current)
            if parent == current:
                return False
            if os.path.ismount(current):
                return True
            current = parent
    except Exception:
        return False


def data_dir_persistence(buckets_dir: str) -> dict:
    """Work out whether the memory data directory is really on persistent storage. The
    worst thing that can happen to a memory system is believing something was saved when
    it was not.

    - Bare metal: the directory is on the user's own disk -> locally persistent.
    - Docker, directory is not a mount point: it is sitting in the container's ephemeral
      layer, and everything is lost the moment the container is rebuilt or removed ->
      dangerous, warn loudly.
    - Docker, mounted: survives restarts and ordinary rebuilds at least; an explicit host
      or named volume is sturdier still.

    This only detects and reports. It must never block startup — blocking would make
    deployment miserable. Returns {persistent, mode, note}.
    """
    # Render's native Python runtime is not a Docker container from inside the
    # process, but its root filesystem is ephemeral.  Only an attached disk
    # mount survives a restart/redeploy, so treating every non-Docker host as
    # local/persistent gives the most dangerous possible false positive.
    is_render = os.environ.get("RENDER", "").strip().lower() == "true"
    if is_render:
        try:
            is_mount = _path_is_on_non_root_mount(buckets_dir)
        except Exception:
            is_mount = False
        if is_mount:
            return {
                "persistent": True,
                "mode": "render_disk",
                "note": "Render：记忆目录已挂载 Persistent Disk，实例重启或重新部署后仍会保留。",
            }
        return {
            "persistent": False,
            "mode": "render_ephemeral",
            "note": (
                "Render：记忆目录不在 Persistent Disk 挂载点上；根文件系统会在实例重启或"
                "重新部署时还原，请挂载磁盘并把 LOCI_BUCKETS_DIR 指向该挂载点。"
            ),
        }
    if not in_docker():
        return {"persistent": True, "mode": "local",
                "note": "本地部署：记忆就存在你磁盘上的这个目录里。"}
    is_mount = False
    try:
        is_mount = os.path.ismount(buckets_dir) if buckets_dir else False
    except Exception:
        is_mount = False
    if not is_mount:
        return {
            "persistent": False,
            "mode": "ephemeral",
            "note": ("记忆目录没有挂到持久卷，正躺在容器的临时层——容器一旦重建或删除，"
                     "记忆会全部丢失。请在 docker-compose 里把它挂到命名卷或宿主机目录。"),
        }
    if os.environ.get("LOCI_HOST_VAULT_DIR", "").strip():
        return {"persistent": True, "mode": "host_mount",
                "note": "记忆目录已挂到宿主机/命名卷，重建容器也不会丢。"}
    return {
        "persistent": True,
        "mode": "volume",
        "note": ("记忆目录在 Docker 卷上，重启和常规重建都不会丢。若你用的是匿名卷，"
                 "建议改成命名卷或宿主机目录，避免 `docker compose down -v` 等操作误删。"),
    }


# --- Injected runtime config; server.py calls init() at startup ---
config: dict = {}

# --- Injected engines and runtime info (the core/runtime pattern; server.py calls
#     init_runtime() at startup) ---
# Route modules read these as sh.<name>, so that server.py and the routes cannot drift
# apart holding two copies.
# embedding_engine is replaced by hot reload — whoever replaces it MUST assign to
# sh.embedding_engine as an attribute, so that every module's next read of
# sh.embedding_engine picks up the new instance.
version: str = ""
repo_root: str = ""   # repository root, injected by server.py; used to locate frontend/ and
                      # such, so that modules do not each compute it from __file__
bucket_mgr = None
dehydrator = None
decay_engine = None
embedding_engine = None
embedding_outbox = None
import_engine = None
migrate_engine = None


def init(cfg: dict) -> None:
    """Called by server.py at startup to inject the global config."""
    global config
    config = cfg


def init_runtime(**kwargs) -> None:
    """Inject engines, version, and other runtime objects at startup.

    Usage: init_runtime(version=..., bucket_mgr=..., decay_engine=..., ...)
    Only the keys passed in are updated; anything omitted is left alone.
    """
    globals().update(kwargs)


async def _read_json_object(request: Request) -> dict:
    """Parse a JSON request body and reject non-object top-level values.

    Mutation routes use named fields. Accepting arrays, strings, or scalars makes
    later ``body.get(...)`` calls fail as HTTP 500s and can turn malformed input
    into an unintended default action. Callers keep control of their response
    shape by catching ``ValueError`` alongside JSON parse errors.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def replace_embedding_engine(engine) -> None:
    """Atomically publish a hot-reloaded embedding engine to all holders."""
    global embedding_engine
    embedding_engine = engine

    for holder_name, attribute in (
        ("bucket_mgr", "embedding_engine"),
        ("import_engine", "embedding_engine"),
        ("migrate_engine", "_embedding_engine"),
    ):
        holder = globals().get(holder_name)
        if holder is not None:
            try:
                setattr(holder, attribute, engine)
            except Exception:
                logger.warning(
                    "Failed to refresh %s.%s", holder_name, attribute,
                    exc_info=True,
                )

    # MCP tools (and the engine pieces under core/) keep a separate runtime
    # container. Without updating it, reads keep using the old model while
    # Dashboard writes use the new one.
    try:
        from core import runtime as core_runtime  # type: ignore
    except ImportError:  # pragma: no cover
        core_runtime = None
    if core_runtime is not None:
        core_runtime.embedding_engine = engine
    outbox = globals().get("embedding_outbox")
    if outbox is not None:
        try:
            outbox.set_embedding_engine(engine)
        except Exception:
            logger.warning("Failed to refresh embedding outbox engine", exc_info=True)


# --- Dashboard auth constants ---
# There is no panel cookie login, and /api/* is not authenticated at this layer.
# **The password and login rate-limit family is here** because bridge/oauth.py's
# /oauth/authorize page relies on it to resist brute force. That is the same password,
# not a second thing.
_PASSWORD_SALT_BYTES = 16            # secrets.token_hex(this) -> 32-char hex salt
_auth_mutation_lock = threading.RLock()
_credential_generation = 0
_credential_proof_key = secrets.token_bytes(32)


class AuthPersistenceError(RuntimeError):
    """A security-state mutation could not be committed durably."""


@dataclass(frozen=True)
class CredentialProof:
    """Opaque proof that one exact credential was verified at one generation.

    Password/security-answer KDF work happens without holding the credential
    lock.  Routes must carry this proof into the later session, OAuth-code, or
    password-rotation commit so a verification that raced a credential change
    cannot authorize a mutation with stale input.
    """

    source: str
    value: str
    generation: int


class CredentialChangedError(RuntimeError):
    """The credential used to authorize a mutation is no longer current."""


@contextmanager
def _credential_state_guard() -> Iterator[None]:
    """Serialize credential changes and credential-derived grant commits.

    Cross-module lock order is always this guard first, followed by the
    session or OAuth registry lock.  Keeping that order prevents both stale
    grants and auth/OAuth deadlocks during a password rotation.
    """
    with _auth_mutation_lock:
        yield


def _credential_generation_snapshot() -> int:
    with _auth_mutation_lock:
        return _credential_generation


def _advance_credential_generation_locked() -> int:
    """Invalidate every in-flight credential proof while the guard is held."""
    global _credential_generation
    _credential_generation += 1
    return _credential_generation


def _environment_password_proof(password: str) -> str:
    return hmac.new(
        _credential_proof_key,
        password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# --- Failed-login throttling with exponential back-off lockout (against online password
#     brute force) ---
# A pure in-memory sliding window with no external dependency; a process restart clears it.
# That is acceptable, because the restart itself interrupts the attacker's run of attempts.
# Buckets are keyed per client (first segment of X-Forwarded-For, falling back to
# request.client.host) so that one bad client cannot lock everybody out. A successful login
# clears the bucket immediately.
_LOGIN_WINDOW_SECONDS = 900          # failures are counted within a 15-minute sliding window
_LOGIN_MAX_FAILURES = 5              # failures allowed in the window before lockout begins
_LOGIN_BASE_LOCK_SECONDS = 60        # first lockout duration; grows exponentially with excess failures
_LOGIN_MAX_LOCK_SECONDS = 3600       # lockout ceiling (1 hour)

# Bound the amount of attacker-controlled state retained by this single-user
# service. The global window also caps how many expensive password KDF jobs can
# be admitted when an attacker rotates source addresses.
_LOGIN_MAX_TRACKED_SOURCES = 2048
_LOGIN_SOURCE_TTL_SECONDS = max(_LOGIN_WINDOW_SECONDS, _LOGIN_MAX_LOCK_SECONDS)
_LOGIN_FAILURE_HISTORY_LIMIT = 16
_LOGIN_GLOBAL_WINDOW_SECONDS = 60
_LOGIN_GLOBAL_MAX_ATTEMPTS = 60

_login_failures: dict[str, list[float]] = {}      # {client_key: [failure timestamps...]}
_login_locked_until: dict[str, float] = {}        # {client_key: unlock timestamp}
_login_source_lru: OrderedDict[str, float] = OrderedDict()
_login_global_attempts: deque[float] = deque()
_login_state_lock = threading.RLock()


def _normalize_login_source(value: str) -> str:
    """Normalize one network source for fair, bounded login throttling.

    IPv4 remains per-address. IPv6 is grouped by /64 so rotating interface
    identifiers cannot manufacture an effectively unlimited set of buckets.
    IPv4-mapped IPv6 addresses retain their underlying IPv4 identity.
    """
    raw = str(value or "").strip()
    # A socket peer can carry an IPv6 zone id (for example ``%eth0``); it is
    # local routing metadata, not part of the remote security identity.
    address = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.ip_network((address, 64), strict=False)
        return f"{network.network_address}/64"
    return str(address)


def _client_key(request: Request) -> str:
    """Return a spoof-resistant login rate-limit key.

    Forwarding headers are accepted only from an explicitly trusted proxy.
    Direct clients cannot evade lockout by rotating a forged X-Forwarded-For.
    The built-in Cloudflare child connects over loopback, which is trusted by
    default. Additional proxy CIDRs can be listed in
    ``LOCI_TRUSTED_PROXY_CIDRS``.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    peer = str(host or "").strip()
    if _is_trusted_proxy(peer):
        try:
            forwarded = (
                request.headers.get("x-forwarded-for") or ""
            ).split(",", 1)[0].strip()
            if forwarded:
                return _normalize_login_source(forwarded)
        except (AttributeError, ValueError):
            pass
    try:
        return _normalize_login_source(peer)
    except ValueError:
        return peer.lower()[:128] or "unknown"


def _prune_login_source_state(now: float) -> None:
    """Expire inactive source buckets and their associated failure state."""
    cutoff = now - max(1, int(_LOGIN_SOURCE_TTL_SECONDS))
    for key, last_seen in list(_login_source_lru.items()):
        if last_seen > cutoff:
            continue
        if _login_locked_until.get(key, 0.0) > now:
            continue
        _login_source_lru.pop(key, None)
        _login_failures.pop(key, None)
        _login_locked_until.pop(key, None)


def _touch_login_source(key: str, now: float) -> None:
    """Refresh one LRU entry and evict complete source records at the cap."""
    _login_source_lru[key] = now
    _login_source_lru.move_to_end(key)
    limit = max(1, int(_LOGIN_MAX_TRACKED_SOURCES))
    while len(_login_source_lru) > limit:
        evicted, _last_seen = _login_source_lru.popitem(last=False)
        _login_failures.pop(evicted, None)
        _login_locked_until.pop(evicted, None)


def _reserve_global_login_attempt() -> int:
    """Reserve one process-wide expensive auth attempt.

    The operation contains no await point, so callers on the server event loop
    cannot overbook the window. A positive result tells the route to shed the
    request before scheduling PBKDF2 work.
    """
    with _login_state_lock:
        now = time.time()
        window = max(1, int(_LOGIN_GLOBAL_WINDOW_SECONDS))
        cutoff = now - window
        while _login_global_attempts and _login_global_attempts[0] <= cutoff:
            _login_global_attempts.popleft()
        limit = max(1, int(_LOGIN_GLOBAL_MAX_ATTEMPTS))
        if len(_login_global_attempts) >= limit:
            return max(1, int(_login_global_attempts[0] + window - now) + 1)
        _login_global_attempts.append(now)
        return 0


def _trusted_proxy_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    raw = os.environ.get(
        "LOCI_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128"
    )
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("[auth] ignoring invalid trusted proxy CIDR: %s", item)
    return tuple(networks)


def _is_trusted_proxy(peer: str) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in _trusted_proxy_networks())


def _trusted_forwarded_value(request: Request, header: str) -> str:
    """Read one forwarded header only when the immediate peer is trusted."""
    client = getattr(request, "client", None)
    peer = str(getattr(client, "host", "") or "") if client else ""
    if not _is_trusted_proxy(peer):
        return ""
    try:
        return (request.headers.get(header) or "").split(",", 1)[0].strip()
    except Exception:
        return ""


def _login_retry_after(request: Request) -> int:
    """>0 means currently locked out, and is the number of seconds to wait; 0 means an
    attempt is allowed."""
    key = _client_key(request)
    with _login_state_lock:
        now = time.time()
        _prune_login_source_state(now)
        if key in _login_failures or key in _login_locked_until:
            _touch_login_source(key, now)
        until = _login_locked_until.get(key, 0.0)
        if until > now:
            return int(until - now) + 1
        if until:
            _login_locked_until.pop(key, None)
        return 0


def _record_login_failure(request: Request) -> None:
    """Record one failure. Past the threshold within the window, lock this client out with
    exponential back-off."""
    key = _client_key(request)
    with _login_state_lock:
        now = time.time()
        _prune_login_source_state(now)
        _touch_login_source(key, now)
        fails = [
            timestamp
            for timestamp in _login_failures.get(key, [])
            if now - timestamp < _LOGIN_WINDOW_SECONDS
        ]
        fails.append(now)
        fails = fails[-_LOGIN_FAILURE_HISTORY_LIMIT:]
        _login_failures[key] = fails
        if len(fails) >= _LOGIN_MAX_FAILURES:
            over = len(fails) - _LOGIN_MAX_FAILURES
            lock = min(
                _LOGIN_BASE_LOCK_SECONDS * (2 ** over),
                _LOGIN_MAX_LOCK_SECONDS,
            )
            _login_locked_until[key] = now + lock
            logger.warning(
                "[auth] login rate-limit: client %s locked for %ss after %s failures",
                key,
                int(lock),
                len(fails),
            )


def _record_login_success(request: Request) -> None:
    """Successful login: clear this client's failure count and lockout."""
    key = _client_key(request)
    with _login_state_lock:
        _login_failures.pop(key, None)
        _login_locked_until.pop(key, None)
        _login_source_lru.pop(key, None)


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _atomic_write_private_json(path: str, data: object) -> None:
    """Atomically persist authentication material with owner-only permissions."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{secrets.token_hex(6)}.tmp"
    fd = -1
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            _json_lib.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _read_auth_data_locked(*, strict: bool = False) -> dict:
    """Read the dashboard auth file. **Two states must never collapse into one**:

      1. **No password was ever set** — the file does not exist -> `{}`.
      2. **The storage is broken** — unreadable (permissions, a directory in its
         place), not JSON, truncated, or JSON that is not an object.

    `strict=True` raises `AuthPersistenceError` for the second state; `strict=False`
    flattens it into `{}`, which reads exactly like the first one.
    **Anyone deciding "is this locked / may this request in" must pass strict=True.**
    Flattening those two together is what once let a corrupt auth file open the panel:
    "the file is broken" was answered as "no password has been set yet".
    """
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                loaded = _json_lib.load(f)
            if isinstance(loaded, dict):
                return loaded
            if strict:
                raise AuthPersistenceError("dashboard auth state is not an object")
    except AuthPersistenceError:
        raise
    except Exception as e:
        if strict:
            raise AuthPersistenceError("failed to read dashboard auth state") from e
    return {}


def _load_auth_data() -> dict:
    """The lenient read: broken storage reads as empty.

    **Only for things that are not a security decision** (what the recovery question
    says, what to draw on a status screen). Anything answering "locked or not / let
    them in or not" goes through `_load_password_hash` below instead.
    """
    with _auth_mutation_lock:
        return _read_auth_data_locked()


def _load_password_hash() -> str | None:
    """The stored password hash. **`None` means one thing only: no password was ever
    set** (no file, or the field cleanly absent) — the state in which the panel is
    deliberately not locked.

    Broken storage raises `AuthPersistenceError` instead of returning `None`. It used
    to return `None` too, so `gate_needed()` read "the file is corrupt" as "fresh
    install, nothing to protect" and opened the door. Every caller must now decide
    what a broken store means for it — and, except when merely displaying something,
    that answer is "locked".
    """
    with _auth_mutation_lock:
        return _read_auth_data_locked(strict=True).get("password_hash")


# --- Key derivation for passwords and security-question answers ---
# The historical format was a single-round `salt:sha256hex`, which makes offline cracking
# almost free once the auth file leaks. It is PBKDF2-HMAC-SHA256 now — a deliberately slow
# KDF. Storage format: pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>.
# The old format still verifies, for backward compatibility, and is silently upgraded to
# the new one on the next successful verification (see _verify_any_password).
_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 240_000


def _hash_secret(secret: str) -> str:
    """Derive a plaintext password or answer into a pbkdf2_sha256$iter$salt$hash string."""
    salt = secrets.token_hex(_PASSWORD_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def _verify_secret(secret: str, stored: str) -> bool:
    """Check plaintext against a stored string. Accepts both the current PBKDF2 format and
    the legacy `salt:sha256hex` one."""
    if not stored:
        return False
    if stored.startswith(_PBKDF2_ALGO + "$"):
        try:
            _algo, iter_s, salt, expected = stored.split("$", 3)
            iterations = int(iter_s)
            dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), iterations)
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(dk.hex(), expected)
    # Legacy format: salt:sha256(salt:secret)
    if ":" in stored:
        salt, h = stored.split(":", 1)
        return hmac.compare_digest(h, hashlib.sha256(f"{salt}:{secret}".encode()).hexdigest())
    return False


def _needs_rehash(stored: str) -> bool:
    """Legacy format, or an iteration count below the current standard -> silently upgrade
    on the next successful verification."""
    if not stored or not stored.startswith(_PBKDF2_ALGO + "$"):
        return True
    try:
        return int(stored.split("$", 3)[1]) < _PBKDF2_ITERATIONS
    except (ValueError, IndexError):
        return True


def _credential_proof_matches_locked(
    proof: CredentialProof,
    *,
    strict: bool = True,
) -> bool:
    """Compare one proof while ``_credential_state_guard`` is held."""
    if not isinstance(proof, CredentialProof):
        return False
    if proof.generation != _credential_generation:
        return False
    if proof.source == "environment_password":
        current = os.environ.get("LOCI_DASHBOARD_PASSWORD", "")
        return bool(current) and hmac.compare_digest(
            _environment_password_proof(current), proof.value
        )
    if proof.source not in {"password_hash", "security_answer_hash"}:
        return False
    current = _read_auth_data_locked(strict=strict).get(proof.source, "")
    return bool(current) and hmac.compare_digest(str(current), proof.value)


def _credential_proof_matches(
    proof: CredentialProof,
    *,
    strict: bool = True,
) -> bool:
    with _auth_mutation_lock:
        return _credential_proof_matches_locked(proof, strict=strict)


# ------------------------------------------------------------
# Concurrency and rate-limit primitives for public password verification
# ------------------------------------------------------------
# bridge/oauth.py's /oauth/authorize page, where the user types the dashboard password to
# authorize a remote MCP client, verifies the *same* password and has to pass the *same*
# login throttling. One password, not two separate things.
class _CrossLoopSemaphore:
    """Small async context manager backed by a process-wide thread semaphore.

    FastMCP can serve requests from more than one event loop.  An
    ``asyncio.Semaphore`` binds contended waiters to one loop and therefore is
    not a process-wide KDF ceiling.  Non-blocking polling keeps acquisition off
    the executor, so queued login attempts cannot occupy every worker thread
    while the PBKDF2 jobs holding the slots wait behind them.
    """

    def __init__(self, value: int):
        self._semaphore = threading.BoundedSemaphore(value)

    async def __aenter__(self):
        while not self._semaphore.acquire(blocking=False):
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self._semaphore.release()


_PASSWORD_WORK_MAX_CONCURRENCY = 2
_password_work_semaphore = _CrossLoopSemaphore(_PASSWORD_WORK_MAX_CONCURRENCY)


async def _await_password_worker(func, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # A task may receive more than one cancellation while the executor
        # thread is still running. Keep shielding until it truly finishes;
        # otherwise each repeated disconnect could release another slot.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        try:
            worker.result()
        except BaseException:
            pass
        raise


async def _run_public_password_verification(request, verifier, secret):
    """Recheck per-source lockout after waiting for a global KDF slot."""
    async with _password_work_semaphore:
        retry = _login_retry_after(request)
        if retry:
            return False, retry
        verified = await _await_password_worker(verifier, secret)
        return verified, 0


def _verify_password_for_rotation(password: str) -> CredentialProof | None:
    """Verify a password and return the exact credential/generation checked."""
    with _auth_mutation_lock:
        generation = _credential_generation
        env_password = os.environ.get("LOCI_DASHBOARD_PASSWORD", "")
        if env_password:
            stored = ""
            proof = CredentialProof(
                "environment_password",
                _environment_password_proof(env_password),
                generation,
            )
        else:
            stored = str(_read_auth_data_locked().get("password_hash", ""))
            proof = CredentialProof("password_hash", stored, generation)

    if env_password:
        verified = hmac.compare_digest(password, env_password)
    else:
        verified = bool(stored) and _verify_secret(password, stored)
    if not verified:
        return None
    with _auth_mutation_lock:
        return proof if _credential_proof_matches_locked(proof) else None


def _verify_security_answer_for_rotation(
    answer: str,
) -> CredentialProof | None:
    """Verify the recovery answer and bind it to the current auth generation."""
    with _auth_mutation_lock:
        generation = _credential_generation
        stored = str(
            _read_auth_data_locked().get("security_answer_hash", "")
        )
        proof = CredentialProof("security_answer_hash", stored, generation)
    if not stored or not _verify_secret(answer.strip().lower(), stored):
        return None
    with _auth_mutation_lock:
        return proof if _credential_proof_matches_locked(proof) else None


def _save_prehashed_password(
    password_hash: str,
    *,
    keep_qa: bool = True,
    expected_hash: str | None = None,
    expected_generation: int | None = None,
    advance_generation: bool = True,
) -> bool:
    """Persist an already-derived password hash with optional CAS checks."""
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with _auth_mutation_lock:
        existing = _read_auth_data_locked(strict=True)
        if (
            expected_hash is not None
            and existing.get("password_hash") != expected_hash
        ):
            return False
        if (
            expected_generation is not None
            and _credential_generation != expected_generation
        ):
            return False
        data: dict = {"password_hash": password_hash}
        if keep_qa:
            if existing.get("security_question"):
                data["security_question"] = existing["security_question"]
            if existing.get("security_answer_hash"):
                data["security_answer_hash"] = existing["security_answer_hash"]
        try:
            _atomic_write_private_json(auth_file, data)
        except Exception as e:
            raise AuthPersistenceError(
                "failed to persist dashboard password"
            ) from e
        if advance_generation:
            _advance_credential_generation_locked()
        return True


def _save_password_hash(
    password: str,
    *,
    keep_qa: bool = True,
    expected_hash: str | None = None,
    expected_generation: int | None = None,
    advance_generation: bool = True,
) -> bool:
    """Replace the password without losing a concurrent security-QA update.

    ``expected_hash`` turns legacy rehash into compare-and-swap: a login that
    verified an old hash must never overwrite a password changed meanwhile.
    """
    return _save_prehashed_password(
        _hash_secret(password),
        keep_qa=keep_qa,
        expected_hash=expected_hash,
        expected_generation=expected_generation,
        advance_generation=advance_generation,
    )


def _save_security_qa(
    question: str,
    answer: str,
    *,
    expected_generation: int | None = None,
) -> bool:
    answer_hash = _hash_secret(answer.strip().lower())
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with _auth_mutation_lock:
        data = _read_auth_data_locked(strict=True)
        if (
            expected_generation is not None
            and _credential_generation != expected_generation
        ):
            return False
        data["security_question"] = question.strip()
        data["security_answer_hash"] = answer_hash
        try:
            _atomic_write_private_json(auth_file, data)
        except Exception as e:
            raise AuthPersistenceError(
                "failed to persist dashboard security question"
            ) from e
        _advance_credential_generation_locked()
        return True


def _verify_security_answer(answer: str) -> bool:
    proof = _verify_security_answer_for_rotation(answer)
    return proof is not None and _credential_proof_matches(proof)


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file).

    **Broken storage is not a fresh install** -> False. Answering True there would
    hand `/api/loci/auth/set-password` and `/oauth/authorize` a first-run path:
    overwrite the password without showing the old one. So a store that cannot be
    read counts as "a password exists", and whoever wants in has to prove it (which,
    with the hash unreadable, nobody can — that is the fail-closed half).
    """
    if os.environ.get("LOCI_DASHBOARD_PASSWORD", ""):
        return False
    try:
        return _load_password_hash() is None
    except AuthPersistenceError as e:
        logger.error(
            "[auth] dashboard auth 存储损坏（%s）：不当成首次安装，口令一律按「已设置」处理。"
            "修好 %s 或者删掉它再重设一次口令。",
            e,
            _get_auth_file(),
        )
        return False


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    proof = _verify_password_for_rotation(password)
    if proof is None:
        return False
    if proof.source == "environment_password":
        return _credential_proof_matches(proof)
    # Verified. If what was stored is the legacy format or a low iteration count, use the
    # plaintext we happen to be holding to silently upgrade it to the current standard.
    if _needs_rehash(proof.value):
        try:
            upgraded = _save_password_hash(
                password,
                expected_hash=proof.value,
                expected_generation=proof.generation,
                advance_generation=False,
            )
            if upgraded:
                return True
        except Exception as e:
            logger.warning(f"[auth] password hash upgrade failed: {e}")
    return _credential_proof_matches(proof)


# The cookie-session family (_create_session / _is_authenticated / _require_auth /
# _set_session_cookie / _is_https_request / _authenticated_credential_generation) is
# entirely gone as of here: /api/* is not authenticated at this layer any more, so nobody
# needs to ask "does this request carry a valid session cookie?".
# The password itself (_verify_password_for_rotation and friends) and credential generation
# (_credential_state_guard and friends) were NOT deleted: the /oauth/authorize page still
# relies on them to resist brute force. Same password, not a second thing.
