"""
========================================
utils.py — small helpers shared across the whole project
========================================

Config loading, logger setup, path-safety checks, ID generation, token estimation, time
formatting: every small function that several modules need and that belongs to no
particular piece of business logic lives here.

Key behaviour:
- load_config(): read config.yaml, apply environment overrides (LOCI_VAULT_DIR and the
  rest), mkdir the directories that must exist.
- setup_logger(): one log format, console plus an optional file.
- safe_path(): refuse path traversal (OWASP).
- generate_bucket_id(): 12 hex characters; collision probability is negligible.
- count_tokens_approx(): rough token estimate from character counts, usable offline.
- now_iso() / parse_iso(): one time-string format.

What this does NOT do (the boundary):
- It depends on no business module. Everything depends on it, so it must never import
  back the other way.
- No LLM calls, no network calls.
- No memory-bucket business logic.

Public surface: all of the above.
========================================
"""

import errno
import os
import re
import sys
import uuid
import json
import yaml
import logging
import math
import tempfile
import threading
import time
from pathlib import Path
from datetime import date, datetime, timezone
from typing import Callable, Optional


# ============================================================
# Named constants
# ------------------------------------------------------------
# No bare magic numbers. These values are gathered here rather than spread through function
# bodies, so that (1) the whole "tuning panel" is visible at a glance and
# (2) changing one place changes every use.
# ============================================================

# Rough coefficients used by count_tokens_approx().
# Empirical, and not meant to be precise — they only have to answer "does this need
# compressing?".
_TOKEN_RATIO_PER_CN_CHAR = 1.5   # one Chinese character ~ 1.5 tokens
_TOKEN_RATIO_PER_EN_WORD = 1.3   # one English word ~ 1.3 tokens
_TOKEN_RATIO_PER_CHAR = 0.05     # catch-all contribution from punctuation, spaces, etc.

# File-log rotation settings for setup_logging().
_LOG_FILE_MAX_BYTES = 1_000_000  # rotate a log file once it passes 1 MB
_LOG_FILE_BACKUP_COUNT = 3       # keep three historical files
_LOG_FALLBACK_DIR = os.path.join(tempfile.gettempdir(), "loci_logs")

# Maximum bucket-name length for sanitize_name(); an over-long filename makes the OS error.
_BUCKET_NAME_MAX_LEN = 80

_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})

# The set of configurable environment-variable names that the real OS or hosting platform
# injected at the moment this process started (non-empty values only).
# Snapshotted before any dashboard save can mutate os.environ — that snapshot is the only
# reliable way to tell "platform-level env" apart from "a value the dashboard wrote into
# os.environ at runtime".
# It is used so the dashboard can warn: "these fields come from platform environment
# variables and a restart will overwrite whatever you save here." That fixes the trap where
# config.yaml holds one provider while the platform's LOCI_COMPRESS_BASE_URL points at
# another and quietly wins on every restart.
BOOT_ENV_CONFIG: frozenset[str] = frozenset(
    k for k, v in os.environ.items()
    if (k.startswith("LOCI_") or k == "AI_NAME") and str(v).strip()
)
def _project_root() -> str:
    """Return absolute path to the project root (parent of src/ where utils.py lives)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_config_yaml_lock = threading.RLock()


def _migrate_legacy_render_config(legacy_path: str, persistent_path: str) -> None:
    """Copy an old Render cwd config into the data disk without overwriting it.

    Render services created before ``LOCI_CONFIG_PATH`` was added already have
    ``LOCI_BUCKETS_DIR`` pointing at the persistent disk, but Dashboard hot
    updates do not apply new ``render.yaml`` environment variables.  Those
    instances therefore kept writing ``<cwd>/config.yaml`` on Render's
    ephemeral code filesystem.  Validate the legacy YAML, then publish a copy
    through a same-directory temporary file so an interrupted migration cannot
    leave a partial persistent config.  The legacy file is intentionally kept
    as a rollback copy.
    """
    legacy_abs = os.path.abspath(legacy_path)
    persistent_abs = os.path.abspath(persistent_path)
    if os.path.normcase(legacy_abs) == os.path.normcase(persistent_abs):
        return

    tmp = ""
    with _config_yaml_lock:
        if os.path.exists(persistent_abs) or not os.path.isfile(legacy_abs):
            return
        try:
            with open(legacy_abs, "r", encoding="utf-8") as source:
                legacy_config = yaml.safe_load(source) or {}
            if not isinstance(legacy_config, dict):
                raise ValueError("legacy config.yaml top level is not a mapping")

            parent = os.path.dirname(persistent_abs)
            os.makedirs(parent, exist_ok=True)
            descriptor, tmp = tempfile.mkstemp(
                prefix=f".{os.path.basename(persistent_abs)}.migrate.",
                dir=parent,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                yaml.safe_dump(
                    legacy_config,
                    target,
                    allow_unicode=True,
                    default_flow_style=False,
                )
                target.flush()
                os.fsync(target.fileno())

            # Publish with true no-clobber semantics.  ``exists`` followed by
            # ``replace`` has a TOCTOU window and can overwrite a config another
            # worker creates between those calls.  The temp file lives in the
            # same directory/filesystem, so link(2) atomically either creates
            # the target name or raises FileExistsError without touching it.
            try:
                os.link(tmp, persistent_abs)
            except FileExistsError:
                pass
        except Exception as exc:
            logging.warning(
                "Failed to migrate Render config.yaml from %s to %s: %s",
                legacy_abs,
                persistent_abs,
                exc,
            )
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def config_file_path() -> str:
    """Absolute path of config.yaml — the single source of truth shared by readers,
    writers and the entrypoint.

    Order:
      1. $LOCI_CONFIG_PATH — taken as given when set, **even if the file does not exist
         yet**. The entrypoint creates it from this before the service starts, and the
         dashboard writes here too.
      2. If an older hosted instance has no LOCI_CONFIG_PATH, follow the existing
         LOCI_BUCKETS_DIR / LOCI_VAULT_DIR onto persistent storage, safely copying the old
         cwd config across.
      3. <cwd>/config.yaml — used only if it exists.
      4. <project_root>/config.yaml — the fallback default.

    Why this is its own function: load_config reads it, the dashboard routes (config_api,
    buckets, embedding) write it, and the entrypoint initializes it. If each
    hardcoded its own path, reads and writes could fork to different files, and any key
    the dashboard saved would be lost on restart. (The config lives in the data directory
    because a single-file Docker bind mount is created as a directory on Windows and
    crash-loops the container.) Centralizing it here means LOCI_CONFIG_PATH takes effect in one place and reads and
    writes always hit the same file."""
    env_cfg = os.environ.get("LOCI_CONFIG_PATH", "").strip()
    if env_cfg:
        return env_cfg

    is_render = str(os.environ.get("RENDER", "")).strip().lower() in _BOOL_TRUE
    render_data_dir = (
        os.environ.get("LOCI_BUCKETS_DIR", "").strip()
        or os.environ.get("LOCI_VAULT_DIR", "").strip()
    )
    if is_render and render_data_dir:
        persistent_cfg = os.path.join(
            os.path.abspath(os.path.expanduser(render_data_dir)),
            "config.yaml",
        )
        _migrate_legacy_render_config(
            os.path.join(os.getcwd(), "config.yaml"),
            persistent_cfg,
        )
        return persistent_cfg

    cwd_cfg = os.path.join(os.getcwd(), "config.yaml")
    if os.path.exists(cwd_cfg):
        return cwd_cfg
    return os.path.join(_project_root(), "config.yaml")


# Every dashboard endpoint that writes to config.yaml (today web/config_api.py)
# shares this one lock and this one atomic write. Nobody may bypass it and
# open(path, "w") over the whole file themselves.
# Why: an open(w) over the whole file that fails partway leaves a blank or torn config;
# if the endpoint then logs a warning and still returns 200, the user sees "saved
# successfully" and finds the settings gone after the next restart.


_MOUNTINFO_ESCAPES = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(
        r"\\(040|011|012|134)",
        lambda match: _MOUNTINFO_ESCAPES[match.group(1)],
        value,
    )


def _is_exact_linux_mount_point(path: str) -> bool:
    """Return whether ``path`` is a regular-file mount point on Linux."""
    if not sys.platform.startswith("linux") or not os.path.isfile(path):
        return False
    target = os.path.realpath(os.path.abspath(path))
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as mountinfo:
            for line in mountinfo:
                fields = line.split()
                if len(fields) > 4:
                    mounted_at = _decode_mountinfo_path(fields[4])
                    if os.path.realpath(mounted_at) == target:
                        return True
    except OSError:
        return False
    return False


def _write_bytes_and_sync(path: str, payload: bytes) -> None:
    with open(path, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


def _overwrite_mounted_config(tmp: str, config_path: str) -> bytes:
    """Overwrite an unreplaceable file mount and return bytes for rollback."""
    with open(config_path, "rb") as current:
        previous_payload = current.read()
    with open(tmp, "rb") as source:
        next_payload = source.read()
    try:
        _write_bytes_and_sync(config_path, next_payload)
    except Exception as write_error:
        try:
            _write_bytes_and_sync(config_path, previous_payload)
        except Exception as restore_error:
            raise OSError(
                "config.yaml bind-mount write failed and restoring the previous "
                f"file also failed: {restore_error}"
            ) from write_error
        raise
    return previous_payload


def read_config_yaml() -> dict:
    """Read the persisted config under the same lock used by atomic writers.

    A normal ``os.replace`` reader is already protected from partial files, but
    Docker's single-file bind-mount fallback must overwrite the mounted inode
    in place.  Sharing ``_config_yaml_lock`` prevents Dashboard readers from
    observing that short write window and gives every route one desired-config
    snapshot instead of stale per-route caches.
    """
    config_path = config_file_path()
    with _config_yaml_lock:
        if not os.path.exists(config_path):
            return {}
        with open(config_path, "r", encoding="utf-8") as handle:
            persisted = yaml.safe_load(handle) or {}
        if not isinstance(persisted, dict):
            raise ValueError("config.yaml top level must be a mapping")
        return persisted


def atomic_update_config_yaml(mutate: Callable[[dict], None]) -> dict:
    """Thread-safe read-modify-write of config.yaml: take the lock, read what is there,
    and hand it to ``mutate`` to patch in place.

    An ordinary file is written atomically via a temp file plus ``os.replace``. An old-style
    single-file Docker bind mount is a mount point that cannot be replaced, and Linux
    answers ``os.replace`` with ``EBUSY``; in that case this falls back to overwriting in
    place under the lock, with flush + fsync, and still runs the same read-back check. The
    fallback path is not crash-atomic, but it works on mount points that cannot be renamed,
    and the global lock still prevents concurrent in-process read-modify-writes from
    clobbering each other.

    Any failure raises. The caller must turn that exception into an honest error response.
    It must not swallow it and answer "saved successfully" — the user would then believe
    the config is stored when it only exists in memory, and the next restart (crash, hot
    update, or the manual restart button) overwrites it with the stale contents that never
    made it to disk.

    Returns the complete config dict as written, equivalent to re-reading the file."""
    config_path = config_file_path()
    tmp = ""
    with _config_yaml_lock:
        save_config: dict = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                save_config = yaml.safe_load(f) or {}
        if not isinstance(save_config, dict):
            save_config = {}
        mutate(save_config)
        try:
            parent = os.path.dirname(os.path.abspath(config_path))
            os.makedirs(parent, exist_ok=True)
            descriptor, tmp = tempfile.mkstemp(
                prefix=f".{os.path.basename(config_path)}.tmp.",
                dir=parent,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
                yaml.dump(save_config, f, allow_unicode=True, default_flow_style=False)
                f.flush()
                os.fsync(f.fileno())
            fallback_backup: bytes | None = None
            try:
                os.replace(tmp, config_path)
            except OSError as e:
                if (
                    e.errno != errno.EBUSY
                    or not _is_exact_linux_mount_point(config_path)
                ):
                    raise
                # A bind-mounted file is itself a mount point and cannot be
                # be replaced with rename(2).  It remains writable, so perform
                # a lock-protected overwrite while keeping the old bytes for
                # rollback if the write or verification fails.
                fallback_backup = _overwrite_mounted_config(tmp, config_path)
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    persisted = yaml.safe_load(f) or {}
                if persisted != save_config:
                    raise OSError("config.yaml verification failed after write")
            except Exception as verify_error:
                if fallback_backup is not None:
                    try:
                        _write_bytes_and_sync(config_path, fallback_backup)
                    except Exception as restore_error:
                        raise OSError(
                            "config.yaml verification failed and restoring the "
                            f"previous bind-mounted file also failed: {restore_error}"
                        ) from verify_error
                raise
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass
        return save_config


def parse_bool(value, *, default=...) -> bool:
    """Parse an explicit boolean without Python's ``bool('false')`` trap.

    JSON/YAML callers may supply booleans, 0/1, or common textual forms. Other
    values are rejected unless a default is supplied. This keeps public API
    boundaries predictable while still accepting environment-style strings.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
    # Ellipsis is a process-wide singleton, so this check remains valid even
    # if a hot-update/test reloads utils while callers retain the old function.
    if default is not ...:
        return bool(default)
    raise ValueError(f"expected boolean value, got {value!r}")


def parse_iso_datetime(value) -> datetime:
    """Parse ISO/date metadata into a naive UTC datetime.

    Stored stamps are naive UTC (see ``now_iso``), while imported/frontmatter
    data may carry ``Z`` or an explicit offset. Aware values are converted to UTC
    and stripped, so every result compares against ``utc_now()``.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("empty datetime")
        if raw[-1:].lower() == "z":
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def load_config(config_path: Optional[str] = None) -> dict:
    """
    Load configuration file.

    Priority: environment variables > config.yaml > built-in defaults.
    """
    project_root = _project_root()
    # --- Built-in defaults (fallback so it runs even without config.yaml) ---
    defaults = {
        "transport": "stdio",
        "log_level": "INFO",
        "mcp_require_auth": True,
        # Only meaningful when mcp_require_auth=true: "oauth" (default) or "token".
        # Pick one; they are mutually exclusive.
        "mcp_auth_mode": "oauth",
        "mcp_token": "",
        "buckets_dir": os.path.join(project_root, "buckets"),
        "merge_threshold": 75,
        "dehydration": {
            "model": "gemini-2.0-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "api_key": "",
            "max_tokens": 4096,
            "temperature": 0.1,
            "timeout_seconds": 60,
        },
        "decay": {
            "lambda": 0.05,
            "threshold": 0.3,
            "check_interval_hours": 24,
            "emotion_weights": {
                "base": 1.0,
                "arousal_boost": 0.8,
            },
        },
        "matching": {
            "fuzzy_threshold": 50,
            "max_results": 5,
        },
        "storage": {
            "external_change_poll_seconds": 1.0,
        },
        "embedding": {
            "enabled": True,
            "background_indexing": True,
            "retry_base_seconds": 5,
            "retry_max_seconds": 300,
            "circuit_failure_threshold": 3,
            "circuit_base_seconds": 30,
            "circuit_max_seconds": 600,
        },
    }

    # --- Load user config from YAML file ---
    if config_path is None:
        # Readers and writers share one resolution path (config_file_path):
        # $LOCI_CONFIG_PATH > cwd > project_root.
        config_path = config_file_path()

    config = defaults.copy()
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
            if isinstance(file_config, dict):
                config = _deep_merge(defaults, file_config)
            else:
                logging.warning(
                    f"Config file is not a valid YAML dict, using defaults / "
                    f"配置文件不是有效的 YAML 字典，使用默认配置: {config_path}"
                )
        except yaml.YAMLError as e:
            logging.warning(
                f"Failed to parse config file, using defaults / "
                f"配置文件解析失败，使用默认配置: {e}"
            )

    # Normalize YAML booleans before environment overrides. Quoted values such
    # as mcp_require_auth: "false" must not become truthy via bool("false").
    config["mcp_require_auth"] = parse_bool(
        config.get("mcp_require_auth", True), default=True
    )

    # --- Environment variable overrides (highest priority) ---
    # Each one does the same thing:
    #   "if the env var is non-empty -> write it to some nested key in config"
    # They all go through _apply_env_override(), so adding one is adding one table row.

    # v1.x compatibility: a refactor must not silently break the old variable names. An
    # explicitly set new variable always wins.
    legacy_api_key = os.environ.get("LOCI_API_KEY", "").strip()
    legacy_base_url = os.environ.get("LOCI_BASE_URL", "").strip()
    if legacy_api_key and not os.environ.get("LOCI_COMPRESS_API_KEY", "").strip():
        config.setdefault("dehydration", {})["api_key"] = legacy_api_key
        logging.warning(
            "LOCI_API_KEY 是兼容变量；请迁移到 LOCI_COMPRESS_API_KEY，旧名仍会继续生效。"
        )
    if legacy_base_url and not os.environ.get("LOCI_COMPRESS_BASE_URL", "").strip():
        config.setdefault("dehydration", {})["base_url"] = legacy_base_url
        logging.warning(
            "LOCI_BASE_URL 是兼容变量；请迁移到 LOCI_COMPRESS_BASE_URL，旧名仍会继续生效。"
        )

    # An older deployment template used a generic PASSWORD variable; map it across only
    # when the proper variable is absent.
    legacy_password = os.environ.get("PASSWORD", "").strip()
    if legacy_password and not os.environ.get("LOCI_DASHBOARD_PASSWORD", "").strip():
        os.environ["LOCI_DASHBOARD_PASSWORD"] = legacy_password
        logging.warning(
            "PASSWORD 是兼容变量；请迁移到 LOCI_DASHBOARD_PASSWORD，旧名仍会继续生效。"
        )

    # The compression group (dehydration, tagging, merging) -> config["dehydration"][*]
    _apply_env_override(config, "LOCI_COMPRESS_API_KEY", "dehydration", "api_key")
    _apply_env_override(config, "LOCI_COMPRESS_BASE_URL", "dehydration", "base_url")
    _apply_env_override(config, "LOCI_COMPRESS_MODEL", "dehydration", "model")
    # Accept both names: LOCI_COMPRESS_FORMAT (dashboard) and LOCI_COMPRESS_API_FORMAT (legacy)
    _apply_env_override(config, "LOCI_COMPRESS_FORMAT", "dehydration", "api_format")
    _apply_env_override(config, "LOCI_COMPRESS_API_FORMAT", "dehydration", "api_format")
    _apply_env_float_override(config, "LOCI_COMPRESS_TIMEOUT_SECONDS", "dehydration", "timeout_seconds")

    # The vectorization group (embedding) -> config["embedding"][*]
    _apply_env_override(config, "LOCI_EMBED_API_KEY", "embedding", "api_key")
    _apply_env_override(config, "LOCI_EMBED_BASE_URL", "embedding", "base_url")
    _apply_env_override(config, "LOCI_EMBED_MODEL", "embedding", "model")
    _apply_env_override(config, "LOCI_EMBED_FORMAT", "embedding", "api_format")
    _apply_env_float_override(config, "LOCI_EMBED_TIMEOUT_SECONDS", "embedding", "timeout_seconds")

    # Obsidian / Git / manual Markdown edits cache poll interval.
    _apply_env_float_override(
        config,
        "LOCI_EXTERNAL_CHANGE_POLL_SECONDS",
        "storage",
        "external_change_poll_seconds",
    )

    # Top-level runtime settings
    _apply_env_override(config, "LOCI_TRANSPORT", "transport")
    # Normalize the transport name — one source of truth, so that server.py and the
    # diagnostic endpoints all see the canonical value.
    # Background: remote clients should be configured with "streamable-http", but people
    # reasonably guess "http", "streamable_http", "streamablehttp" and other variants.
    # server.py's entry point matches exactly, with
    # `transport in ("sse","streamable-http")`, so a near-miss silently fell back to stdio
    # — meaning the HTTP server never started at all and the client could never connect.
    # Collapsing every equivalent spelling into the canonical "streamable-http" here saves
    # hours of debugging over one hyphen or underscore.
    # Only known aliases are collapsed. An unrecognized value is passed through untouched,
    # so server.py's mcp.run() can raise a clear error about it.
    _raw_transport = str(config.get("transport", "stdio")).strip().lower()
    _transport_aliases = {
        "http": "streamable-http",
        "streamable": "streamable-http",
        "streamable_http": "streamable-http",
        "streamablehttp": "streamable-http",
        "streamable-http": "streamable-http",
        "http-stream": "streamable-http",
        "streaming": "streamable-http",
        "sse": "sse",
        "stdio": "stdio",
    }
    config["transport"] = _transport_aliases.get(_raw_transport, _raw_transport)
    _apply_env_override(config, "LOCI_BUCKETS_DIR", "buckets_dir")
    env_buckets_dir = os.environ.get("LOCI_BUCKETS_DIR", "")

    # The MCP OAuth switch (boolean, handled on its own) — LOCI_MCP_REQUIRE_AUTH
    # It cannot go through _apply_env_override, which only writes strings: the auth
    # middleware and the diagnostic endpoints both require a real bool in the config.
    # Otherwise the string "false" is still truthy under an ordinary truth test and reads
    # as "enabled".
    # Purpose: when wiring this into a self-hosted front-end or a client that does not
    # speak OAuth, setting LOCI_MCP_REQUIRE_AUTH=false (or mcp_require_auth: false in
    # config.yaml) allows unauthenticated direct connections to /mcp.
    # It only overrides when explicitly set to a recognized value. Unset, or set to
    # something unparseable, keeps the default — which is on, because the safe default is
    # protected.
    _env_mcp_auth = os.environ.get("LOCI_MCP_REQUIRE_AUTH", "").strip()
    if _env_mcp_auth:
        config["mcp_require_auth"] = parse_bool(
            _env_mcp_auth, default=config["mcp_require_auth"]
        )

    # The MCP auth mode (an enum, only in effect when mcp_require_auth=true) —
    # mcp_auth_mode / LOCI_MCP_AUTH_MODE
    # "oauth" (the default) uses the OAuth 2.1 + PKCE flow above; "token" switches to a
    # static secret (mcp_token / LOCI_MCP_TOKEN).
    # The two are mutually exclusive: in token mode the OAuth discovery, register,
    # authorize and token routes all return 404 (see bridge/oauth.py).
    # It cannot go through _apply_env_override, because this needs enum validation: an
    # invalid value always falls back to the default "oauth".
    _raw_auth_mode = str(config.get("mcp_auth_mode", "oauth")).strip().lower()
    config["mcp_auth_mode"] = _raw_auth_mode if _raw_auth_mode in ("oauth", "token") else "oauth"
    _env_mcp_auth_mode = os.environ.get("LOCI_MCP_AUTH_MODE", "").strip().lower()
    if _env_mcp_auth_mode in ("oauth", "token"):
        config["mcp_auth_mode"] = _env_mcp_auth_mode

    _apply_env_override(config, "LOCI_MCP_TOKEN", "mcp_token")

    # Safety fallback: token mode selected but no secret configured. Better to fall back to
    # the stronger OAuth than to let the user believe protection is on while /mcp is in
    # fact either accidentally locked shut or wide open, depending on how the validator
    # handles a missing secret.
    if config["mcp_auth_mode"] == "token" and not str(config.get("mcp_token") or "").strip():
        logging.warning(
            "mcp_auth_mode=token 但未配置 mcp_token / LOCI_MCP_TOKEN，已自动回退为 oauth 模式 / "
            "mcp_auth_mode=token but no mcp_token/LOCI_MCP_TOKEN configured — falling back to oauth"
        )
        config["mcp_auth_mode"] = "oauth"

    # LOCI_VAULT_DIR is the recommended name; the older LOCI_BUCKETS_DIR still works.
    # Priority: LOCI_BUCKETS_DIR (legacy explicit) > LOCI_VAULT_DIR > config.yaml.buckets_dir
    # We keep BUCKETS_DIR with higher priority than VAULT_DIR for two reasons:
    #   1) Existing tests use monkeypatch.setenv("LOCI_BUCKETS_DIR", ...) extensively;
    #      flipping priority would break them when conftest also sets VAULT_DIR globally.
    #   2) Anyone who already had BUCKETS_DIR working should keep working unchanged.
    # New users / new docs should prefer LOCI_VAULT_DIR; both names map to the same path.
    env_vault_dir = os.environ.get("LOCI_VAULT_DIR", "")
    if env_vault_dir and not env_buckets_dir:
        config["buckets_dir"] = env_vault_dir
    elif env_buckets_dir and not env_vault_dir:
        # Only legacy var set — emit one INFO hint so users know about the new name.
        try:
            import logging as _logging
            _logging.getLogger(__name__).info(
                "LOCI_BUCKETS_DIR is the legacy name; LOCI_VAULT_DIR is preferred "
                "/ 旧变量 LOCI_BUCKETS_DIR 仍可用，但建议改用 LOCI_VAULT_DIR"
            )
        except Exception:
            pass

    # Media has to land on the same persistent volume as the memories; the default is a
    # separate _media directory inside the data directory.
    # Override with LOCI_MEDIA_DIR only when a second persistent disk really is mounted.
    media_dir = os.environ.get("LOCI_MEDIA_DIR", "").strip()
    config["media_dir"] = media_dir or os.path.join(str(config["buckets_dir"]), "_media")
    try:
        config["media_max_bytes"] = max(
            1,
            int(os.environ.get("LOCI_MEDIA_MAX_BYTES", 25 * 1024 * 1024)),
        )
    except (TypeError, ValueError, OverflowError):
        config["media_max_bytes"] = 25 * 1024 * 1024

    # --- Ensure bucket storage directories exist ---
    buckets_dir: str = str(config["buckets_dir"])
    for subdir in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(buckets_dir, subdir), exist_ok=True)
    os.makedirs(str(config["media_dir"]), exist_ok=True)

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two dicts; override values take precedence.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_override(config: dict, env_name: str, *path: str) -> None:
    """Write one environment variable into a nested dict along `path`, if it is non-empty.

    Why this exists: without it, load_config() would hold one near-identical block per
    override —
        env = os.environ.get("XXX", "")
        if env:
            config["a"]["b"] = env
    which is bulky and has to be copied again for every new entry. Hoisted into one place:
      * adding an override is one line,
        `_apply_env_override(config, "LOCI_FOO", "a", "b")`
      * behaviour is uniform: an empty string counts as "unset" and never overwrites a
        default
      * intermediate dicts are setdefault'ed automatically, so there is no KeyError

    Arguments:
        config   : the config dict, modified in place
        env_name : the environment variable's name
        *path    : the nested key path. One key for one level, two for two, and so on.
                   ("dehydration", "api_key") writes to
                   config["dehydration"]["api_key"].

    Edges (defensive programming):
      * env var empty or unset -> return immediately, leaving config alone
      * empty path -> return immediately; a caller that got the path wrong must not
        silently overwrite the entire config
    """
    value = os.environ.get(env_name, "").strip()
    if not value or not path:
        return
    # Walk to the second-to-last level, setdefault'ing each nested dict on the way
    cursor = config
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = value


def positive_float(value, default: float) -> float:
    """Parse a positive numeric config value, falling back to default."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(parsed) or parsed <= 0:
        return float(default)
    return parsed


def _apply_env_float_override(config: dict, env_name: str, *path: str) -> None:
    value = os.environ.get(env_name, "").strip()
    if not value or not path:
        return
    try:
        parsed = float(value)
    except ValueError:
        return
    if not math.isfinite(parsed) or parsed <= 0:
        return
    cursor = config
    for key in path[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[path[-1]] = int(parsed) if parsed.is_integer() else parsed


def clean_llm_json(raw: str) -> str:
    """Return the first complete JSON value from an LLM response.

    Models sometimes wrap JSON in Markdown fences or add a short sentence before
    or after it. Keep strict JSON validation in callers, but make the extraction
    step tolerant enough to recover the balanced array/object.
    """
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch not in "[{":
            continue
        try:
            _value, end = decoder.raw_decode(cleaned[idx:])
        except json.JSONDecodeError:
            continue
        return cleaned[idx:idx + end].strip()
    return cleaned


def _resolve_log_dir(explicit: str | None) -> str:
    """Decide which directory server.log lands in.

    Priority:
        the explicit argument > $LOCI_LOG_DIR > <buckets_dir>/.logs > /tmp as a last resort

    Why it is separate: four if-fallback blocks inlined in setup_logging() crowd together
    and are hard to read. On its own it can be unit-tested directly,
    and changing the priority order does not mean touching the body of setup_logging.
    """
    if explicit:
        return explicit
    env_dir = os.environ.get("LOCI_LOG_DIR", "").strip()
    if env_dir:
        return env_dir
    bd = os.environ.get("LOCI_BUCKETS_DIR", "").strip()
    if bd:
        return os.path.join(bd, ".logs")
    return _LOG_FALLBACK_DIR


def setup_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    """
    Initialize logging system.

    Note: In MCP stdio mode, stdout is occupied by the protocol;
    logs must go to stderr.

    Besides stderr, a copy is written to ``server.log`` via a RotatingFileHandler. The
    panel's log tab reads that file through ``/api/logs``, so ERROR and WARNING lines can
    be read in the browser. Path priority:
        the log_dir argument > $LOCI_LOG_DIR > <buckets_dir>/.logs > /tmp/loci_logs
    """
    log_level = getattr(logging, level.upper(), None)
    if not isinstance(log_level, int):
        log_level = logging.INFO

    handlers: list[logging.Handler] = [logging.StreamHandler()]  # stderr by default

    # ---- File logging: enabled on demand, and degrades silently to stderr-only on
    # ---- failure.
    chosen_dir = _resolve_log_dir(log_dir)

    try:
        from logging.handlers import RotatingFileHandler
        os.makedirs(chosen_dir, exist_ok=True)
        log_path = os.path.join(chosen_dir, "server.log")
        fh = RotatingFileHandler(
            log_path,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(log_level)
        handlers.append(fh)
        # Expose the path to server.py, so /api/logs can read it
        os.environ["LOCI_LOG_FILE"] = log_path
    except Exception as e:
        # A failure to open the log file must not block the service from starting
        sys.stderr.write(f"[setup_logging] file handler disabled: {e}\n")

    logging.basicConfig(
        level=log_level,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    # Attach the shared error system's in-memory log buffer, so E-class errors carry a tail
    try:
        try:
            from core.errors import attach_log_buffer_handler  # type: ignore
        except ImportError:
            from .core.errors import attach_log_buffer_handler  # type: ignore
        attach_log_buffer_handler(level=log_level)
    except Exception as _e:
        sys.stderr.write(f"[setup_logging] buffer handler attach failed: {_e}\n")


def generate_bucket_id() -> str:
    """
    Generate a unique bucket ID (12-char short UUID for readability).
    """
    return uuid.uuid4().hex[:12]


def strip_wikilinks(text: str) -> str:
    """
    Remove Obsidian wikilink brackets: [[word]] -> word
    """
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text) if text else text


# ===============================================================
# Wikilink parsing
# ---------------------------------------------------------------
# Obsidian writes bidirectional links as `[[target bucket name]]`, optionally with an alias
# and a section:
#   [[Memory]]                 -> target = "Memory"
#   [[Memory#section]]         -> target = "Memory"     (after # is a heading anchor)
#   [[Memory|display text]]    -> target = "Memory"     (after | is a display alias)
# The regex captures only the first segment, the target name, and stops at # or |.
# Notes on the regex:
#   * re.compile precompiles it, which beats re.findall recompiling on every call
#   * the character class `[^\]\|#]+` means "a run of characters that are not ], | or #"
#   * (?:...) is a non-capturing group: it only groups alternatives, and does not consume a
#     group number
# ===============================================================
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")


def extract_wikilinks(text: str) -> list[str]:
    """Extract Obsidian-style [[wikilinks]] target names from text.

    Pull every `[[xxx]]` target name out of the text, deduplicated but order-preserving,
    with `|alias` and `#section` stripped off.
    Returns list[str] rather than a set, because callers want the order of appearance.

    Example:
        >>> extract_wikilinks("see [[A]] and [[B|alias]] also [[A]]")
        ['A', 'B']
    """
    # Defensive: None or an empty string returns an empty list, so a caller's for-loop
    # cannot blow up on it.
    if not text:
        return []
    # A list plus a manual membership check, not a set(), so that first-appearance order
    # survives. (dict has preserved insertion order since Python 3.7, so dict.fromkeys
    # would work too; this is just more obvious.)
    seen: list[str] = []
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(1).strip()  # group(1) = what the first ([^\]\|#]+) captured
        if target and target not in seen:
            seen.append(target)
    return seen


def get_version() -> str:
    """The version that runs: src/VERSION, else the repository root's VERSION.

    This is the one reader: the startup log, `server.__version__` and the setting page
    (`GET /api/loci/version`, which compares it with the latest GitHub release) all go
    through it. When neither file can be read, this returns "0.0.0+unknown", which is at
    least diagnosable.

    Why src/ is read first: src/VERSION travels with the code that runs. Code copied over
    an older install replaces src/ and brings its VERSION along, while a root VERSION can
    be left from the first install; read root-first, the panel would announce an older
    version than the one running. When cutting a release, bump both files —
    tests/test_version_is_consistent.py fails while they differ.

    Notes:
      * `with open(...) as f:` is a context manager: the file closes on leaving the block,
        including when an exception is raised on the way out — cleaner than try/finally.
      * `OSError` covers missing files, permission problems, disk errors — every IO
        exception. Safer than a bare `except:`, and broader than `except FileNotFoundError`.
    """
    candidates = [
        # First: the copy next to src/, which travels with the code that runs.
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"),
        # Fallback: the repository-root VERSION (in Docker the Dockerfile COPYs it to
        # /app/VERSION).
        os.path.join(_project_root(), "VERSION"),
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            # If this candidate cannot be read, try the next. No logging: there is no
            # logger yet this early in startup.
            continue
    return "0.0.0+unknown"


_NAME_CACHE = {"mtime": -1.0, "data": {}}


def _persisted_names() -> dict:
    """The two names stored in config.yaml, cached on mtime — they change once every few
    months.

    Why the names have to be storable in config.yaml at all: coming **only** from the
    container's environment variables would force that field in the panel to be read-only
    — and for someone who has just installed this, "go edit docker-compose and restart" is
    a wall they hit on step one.
    The order is **config first, environment as fallback**: what is typed into the panel has
    to count, or that input box is lying to the user. Saving something that does not take
    effect is the worst version of this.
    """
    path = os.environ.get("LOCI_CONFIG_PATH", "").strip()
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _NAME_CACHE["mtime"] == mtime:
        return _NAME_CACHE["data"]
    data = {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if isinstance(raw, dict):
            for k in ("ai_name", "owner_name"):
                v = str(raw.get(k) or "").strip()
                if v:
                    data[k] = v
    except Exception:      # noqa: BLE001 - an unreadable config must not take the service down over something as small as a display name
        data = {}
    _NAME_CACHE["mtime"], _NAME_CACHE["data"] = mtime, data
    return data


def get_ai_name() -> str:
    """Display name for the AI side.

    Order: `ai_name` in config.yaml -> the `AI_NAME` environment variable -> "AI".
    Used in user-facing text (prompts, UI, error messages).
    """
    return (_persisted_names().get("ai_name")
            or os.environ.get("AI_NAME", "").strip()
            or "AI")


def get_owner_name() -> str:
    """Display name of this instance's memory owner.

    When several people share one deployment, each runs a separate instance with its own
    data directory and port, and the instance declares whose memories these are through the
    `LOCI_OWNER_NAME` environment variable, for the ownership badge at the top of the panel.
    Unset falls back to an empty string; the front-end decides whether to show the badge
    based on owner_count.
    Order: `owner_name` in config.yaml -> the `LOCI_OWNER_NAME` environment variable -> "".
    WARNING: never write this into a shared .env — instances running from the same code
       would cross-contaminate each other's names. config.yaml is the copy **inside each
       instance's own data directory**, which is why it is safe.
    """
    return (_persisted_names().get("owner_name")
            or os.environ.get("LOCI_OWNER_NAME", "").strip())


def get_owner_count() -> int:
    """Total number of people sharing this deployment.

    The launcher injects `LOCI_OWNER_COUNT` from the configured headcount; a manual
    deployment sets it itself. The front-end uses it to decide whether to show the
    ownership badge: only at `>= 2`, so a single user is never bothered by it. Invalid or
    unset falls back to 1.
    Read from the `LOCI_OWNER_COUNT` env var; falls back to 1 when unset/invalid.
    """
    raw = os.environ.get("LOCI_OWNER_COUNT", "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def sanitize_name(name: str) -> str:
    """
    Sanitize bucket name, keeping only safe characters.
    Prevents path traversal attacks (e.g. ../../etc/passwd).
    """
    if not isinstance(name, str):
        return "unnamed"
    cleaned = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", name, flags=re.UNICODE)
    cleaned = cleaned.strip()[:_BUCKET_NAME_MAX_LEN]
    return cleaned if cleaned else "unnamed"


def safe_path(base_dir: str, filename: str) -> Path:
    """
    Construct a safe file path, ensuring it stays within base_dir.
    Prevents directory traversal.
    """
    base = Path(base_dir).resolve()
    target = (base / filename).resolve()
    # is_relative_to rather than startswith, to avoid prefix confusion: with
    # base=/data/buckets and target=/data/buckets_evil/f.md, a string prefix check calls it
    # safe. is_relative_to does not.
    if not target.is_relative_to(base):
        raise ValueError(
            f"Path safety check failed / 路径安全检查失败: "
            f"{target} is not inside / 不在 {base} 内"
        )
    return target


def _win_long_path(path: Path) -> str:
    """Prefix an absolute path with ``\\\\?\\`` on Windows to bypass the 260-char
    MAX_PATH limit. Domain names can sanitize down to 80 chars each, and nested
    under a deep install/data dir the combined bucket path can exceed it. No-op
    on other platforms."""
    if os.name != "nt":
        return str(path)
    resolved = os.path.abspath(str(path))
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved[2:]
    return "\\\\?\\" + resolved


# How long a write or a read waits out a file another handle has open. On Windows a file
# someone is reading cannot be replaced, and a file being replaced cannot be opened
# (WinError 5 access denied / 32 sharing violation); both last milliseconds. Elsewhere
# PermissionError is a real permission problem and the waits simply run out.
_BUSY_TRIES = 12
_BUSY_FIRST_WAIT = 0.01
_BUSY_MAX_WAIT = 0.25


def busy_retry(do):
    """Run `do()`, waiting out a PermissionError (a file busy for a moment) with a short
    backoff; the last one is raised. About two seconds in all."""
    wait = _BUSY_FIRST_WAIT
    for attempt in range(_BUSY_TRIES):
        try:
            return do()
        except PermissionError:
            if attempt == _BUSY_TRIES - 1:
                raise
            time.sleep(wait)
            wait = min(wait * 2, _BUSY_MAX_WAIT)


def replace_file(source: str | Path, target: str | Path) -> None:
    """os.replace that waits out a reader holding the target open (busy_retry)."""
    busy_retry(lambda: os.replace(source, target))


def atomic_write_text(path: str | Path, text: str) -> None:
    """Atomically replace a UTF-8 text file after flushing it to disk. A reader holding
    the file open for a moment is waited out (replace_file)."""
    target = Path(path)
    os.makedirs(_win_long_path(target.parent), exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    temporary_long = _win_long_path(temporary)
    try:
        with open(temporary_long, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary_long, _win_long_path(target))
    except Exception:
        try:
            os.remove(temporary_long)
        except OSError:
            pass
        raise


def count_tokens_approx(text: str) -> int:
    """
    Rough token count estimate.

    Chinese ~ 1 char = 1.5 tokens, English ~ 1 word = 1.3 tokens.
    Used to decide whether dehydration is needed; precision not required.
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    english_words = len(re.findall(r"[a-zA-Z]+", text))
    return int(
        chinese_chars * _TOKEN_RATIO_PER_CN_CHAR
        + english_words * _TOKEN_RATIO_PER_EN_WORD
        + len(text) * _TOKEN_RATIO_PER_CHAR
    )


def utc_now() -> datetime:
    """Now as a naive UTC datetime: the one form stored stamps are compared against."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_iso() -> str:
    """Now as the stored stamp: naive UTC, on every machine.

    Naive stamps are read as UTC (core/_when.py). The container's clock is UTC, so
    plain ``datetime.now()`` happened to match there; on a host running at +08 it
    wrote local time and every stamp read back eight hours off.
    """
    return utc_now().isoformat(timespec="seconds")


# ============================================================
# `prov` — where a memory came from, one typed line per source
# ------------------------------------------------------------
# On disk: `prov: [{rel, target}, ...]`. `rel` is one of four W3C PROV relations, and the
# write path picks it from the route taken, never from the caller:
#   wasDerivedFrom    grew out of another memory (grow / fold `from`)
#   wasRevisionOf     the previous version of this same memory (regrow)
#   wasQuotedFrom     a line of the host's conversation (an `m_…` id), not a memory
#   hadPrimarySource  defined so the reader knows it; nothing writes it yet
# The tools still take the argument as `from`, which is the word a caller thinks in.
#
# A library before schema 5 stored a comma string under `from`; the migration
# (core/schema.py, step 4 -> 5) turns each memory id in it into a wasDerivedFrom line and
# anything else into a wasQuotedFrom line. The readers below read it the same way, so a
# library that was not migrated keeps its chains.
# ============================================================
PROV_FIELD = "prov"
LEGACY_FROM_FIELD = "from"

WAS_DERIVED_FROM = "wasDerivedFrom"
WAS_REVISION_OF = "wasRevisionOf"
WAS_QUOTED_FROM = "wasQuotedFrom"
HAD_PRIMARY_SOURCE = "hadPrimarySource"
PROV_RELS = (WAS_DERIVED_FROM, WAS_REVISION_OF, WAS_QUOTED_FROM, HAD_PRIMARY_SOURCE)
# The memories in this library a memory stands on. A revision line names the previous
# version of the same memory, not a source (the chain is read from `supersedes`), and a
# quoted line points outside the library.
SOURCE_RELS = frozenset({WAS_DERIVED_FROM, HAD_PRIMARY_SOURCE})

PROV_MAX_LINES = 64      # per memory; more is refused at the door, never cut
PROV_TARGET_MAX = 128    # one target, in characters; a longer one is refused

# A memory's own id: 12 hex characters, or the readable `feel_…` ids feel once wrote.
_BUCKET_ID_RE = re.compile(r"[0-9a-f]{12}|feel_\S+")


def is_bucket_id(value) -> bool:
    """Is this shaped like the id of a memory in this library (as opposed to the host's
    own line id)? Shape only; whether it exists is the caller's question."""
    return bool(_BUCKET_ID_RE.fullmatch(str(value or "").strip()))


def read_prov(meta: Optional[dict]) -> list[dict]:
    """Every provenance line, typed and in stored order: [{"rel", "target"}].

    Read from `prov`; an entry that carries only the older `from` string reads the way
    the migration converts it — a memory id as wasDerivedFrom, anything else as
    wasQuotedFrom. A line with an unknown rel or no target is skipped, so a hand-edited
    file cannot stop a sweep over the library."""
    m = meta if isinstance(meta, dict) else {}
    raw = m.get(PROV_FIELD)
    if raw:
        out: list[dict] = []
        for line in raw if isinstance(raw, list) else []:
            if not isinstance(line, dict):
                continue
            rel = str(line.get("rel") or "").strip()
            target = str(line.get("target") or "").strip()
            if rel in PROV_RELS and target:
                out.append({"rel": rel, "target": target})
        return out
    legacy = m.get(LEGACY_FROM_FIELD)
    ids = legacy if isinstance(legacy, (list, tuple)) else str(legacy or "").split(",")
    targets = [str(s).strip() for s in ids if str(s).strip()]
    return [{"rel": WAS_DERIVED_FROM if is_bucket_id(t) else WAS_QUOTED_FROM, "target": t}
            for t in targets]


def prov_targets(lines: list[dict], rels=SOURCE_RELS) -> list[str]:
    """The targets of the lines whose rel is in `rels` (the sources, by default), in
    order, each once."""
    return list(dict.fromkeys(ln["target"] for ln in lines if ln["rel"] in rels))


def read_from_ids(meta: Optional[dict]) -> list[str]:
    """The memories in this library this one stands on — derived-from and
    primary-source — in order. Its own previous version (wasRevisionOf) and quoted lines
    are not sources in the library and are left out."""
    return prov_targets(read_prov(meta))


def is_closed(meta: Optional[dict]) -> bool:
    """
    Whether a memory has been explicitly closed — let go of, or given up on.

    Closure is decided by `status` alone. The `resolved` boolean and `status` governed the
    same thing and could therefore contradict each other: one real bucket carried
    `resolved: true` alongside `status: want`, and breath kept nagging about it for ten
    days.
    Every write path goes through `status` now. The old `resolved` boolean is still read for
    compatibility, and the field-cleanup step that would have removed it was dropped
    entirely.
    """
    m = meta or {}
    if str(m.get("status") or "").strip().lower() in ("resolved", "abandoned"):
        return True
    return bool(m.get("resolved", False))


def is_telic(meta: Optional[dict]) -> bool:
    """Is this something wanted — a promise, a plan, a wish (direction_of_fit telic)?

    Wanted and closed are separate questions: an open want is `is_telic and not
    is_closed`. Nothing closes itself; resolved / abandoned are written by hand."""
    return str((meta or {}).get("direction_of_fit") or "").strip() == "telic"
