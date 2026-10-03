"""
========================================
embedding_engine.py — the vector engine behind semantic recall in breath/search
========================================

Vectorisation is split into two layers, a facade and a backend:
- A backend (a BaseEmbeddingEngine subclass) does nothing but turn text into a vector;
  it touches no IO and no SQLite. The backend is an OpenAI-compatible API (Gemini by
  default).
- The facade (EmbeddingEngine) holds one backend instance and owns SQLite storage,
  cosine search, deletion, orphan reconciliation, and model/dimension metadata checks.
  Its public interface never changes, so bucket_manager needs no adjustment.

Key behaviours:
- generate_and_store(bucket_id, content): write or overwrite one bucket's vector
- search_similar(query, top_k): returns [(bucket_id, score)] ordered by similarity, descending
- search(query, top_k): the newer interface, returning bucket ids only, per spec
- delete_embedding(bucket_id): called in step with BucketManager.delete
- list_all_ids(): used by tools/clean_orphan_embeddings to find orphaned vectors
- With enabled=False every method is a no-op, which makes offline work and tests easy
- If the model/dimension recorded in the db disagrees with the current backend at
  startup -> log an OB-W005 warning, without blocking startup

What it deliberately does not do:
- It neither reads nor writes bucket files
- It does no keyword retrieval (that is BucketManager's job)
- It makes no dedup or merge judgements

Exports:
- BaseEmbeddingEngine (the abstract base, so this can be extended later)
- APIEmbeddingEngine (an OpenAI-compatible API, Gemini by default)
- EmbeddingEngine (the facade: the backwards-compatible public class)
========================================
"""

from __future__ import annotations

import abc
import asyncio
import heapq
import hashlib
import json
import logging
import os
import sqlite3
from collections import OrderedDict
from typing import Any, Collection, Optional

import httpx
import numpy as np
from openai import AsyncOpenAI

try:
    from utils import parse_bool, positive_float
except ImportError:  # pragma: no cover
    from .utils import parse_bool, positive_float  # type: ignore

from locibrain.integrations.provider_detect import (
    is_known_cloud_embedding_endpoint,
    normalize_model_for_endpoint,
    strip_native_resource_prefix,
)

logger = logging.getLogger("loci_brain.embedding")


# ============================================================
# Constants
# ============================================================

_GEMINI_DEFAULT_DIM = 3072
_API_TIMEOUT_SECONDS = 30.0

# Input truncation length
_MAX_INPUT_CHARS = 2000

# The same piece of text gets requested by several paths within a short window: inside one
# breath(query=...), bucket_mgr.search() and surface_search() each ask for it, and inside
# one hold(), merge_or_create / check_duplicate_for / check_plan_resolution each embed the
# same content. A given (text, model) maps to the same vector every time, so caching the
# last N results catches those duplicates inside the process and spares the real vector API.
_QUERY_CACHE_MAXSIZE = 32

# SQLite stores vectors as JSON text.  Reading the whole table and converting
# every row to Python floats before the matrix operation can briefly hold the
# JSON strings, Python float objects and a second NumPy matrix at the same time.
# Keep that peak independent of vault size (important on 512 MiB hosts).
_SEARCH_BATCH_ROWS = 32


def _norm_model(name: str) -> str:
    """Normalise a model name so that identity comparisons work.

    The Gemini OpenAI-compat endpoint requires a "models/" prefix, while OpenAI-compatible
    proxies use the bare name — the same model, differing only in prefix. Stripping the
    prefix, trimming whitespace and lowercasing lets the model_name reconciliation look at
    real identity rather than a spelling convention (which is what produced false
    OB-W005s).
    Ollama's ":latest" is the default tag: bge-m3 and bge-m3:latest are the same model, so
    a trailing ":latest" is stripped before comparing. Only that one tag — a quantisation
    tag such as :q4_0 denotes a genuinely different build and must not be stripped.
    """
    return strip_native_resource_prefix(name).lower().removesuffix(":latest")


def _humanize_api_error(
    e: Exception,
    *,
    api_format: str = "openai_compat",
    base_url: str = "",
) -> str:
    """Translate the common exceptions of an OpenAI-compatible backend into a readable
    hint, appended to the end of the OB-E001 detail.

    The point is that the error panel should say plainly what to do about a 401/400/404 or
    a timeout, especially when the wrong cross-border provider has been picked (a US VPS
    timing out against a domestic domain, an international endpoint that does not carry a
    given model, a key that does not belong to the provider).
    An empty string means there is no extra hint worth adding.
    """
    name = type(e).__name__
    code = getattr(e, "status_code", None)
    s = str(e).lower()
    is_local = (api_format or "").strip().lower() in ("ollama", "local")
    if is_local:
        if code in (400, 404) or "badrequest" in name.lower() or "notfound" in name.lower():
            return "→ 本地 Ollama 未找到或不支持该模型：确认 Ollama 已运行并已拉取 bge-m3。"
        if "timeout" in name.lower() or "connect" in name.lower() or "timeout" in s:
            return "→ 本地 Ollama 连接失败：确认服务已启动，且 Base URL 指向 Ollama 而不是云端 API。"
        if code == 401 or "authentication" in name.lower() or "401" in s:
            return "→ 本地 Ollama 返回 401：当前地址可能是需要鉴权的代理，而不是标准 Ollama 服务。"
        return ""
    if code == 401 or "authentication" in name.lower() or "401" in s:
        return "→ 401：API key 无效或无权限，确认 key 正确且属于当前 base_url 的 provider。"
    if code == 404 or "notfound" in name.lower() or "404" in s:
        return "→ 404/model 不存在：确认模型名与 base_url 属同一 provider（如 SiliconFlow 国际站可能没有 BAAI/bge-m3）。"
    if code == 400 or "badrequest" in name.lower():
        return "→ 400：请求被拒，多为模型名不存在或参数不被支持，核对 model 名。"
    if "timeout" in name.lower() or "connect" in name.lower() or "timeout" in s:
        return (
            "→ 超时/连接失败：检查网络与 base_url 可达性。美国 VPS 直连国内域名"
            "（api.siliconflow.cn）极易超时，建议改用就近 provider 或本地 ollama。"
        )
    return ""


# ============================================================
# Backend Abstract Base
# ============================================================

class BaseEmbeddingEngine(abc.ABC):
    """The contract every embedding backend obeys.

    Design principles:
    - `generate` is a synchronous interface (the protocol demands one), but the production
      path should go through the natively asynchronous `generate_async`.
    - model_name / vector_dim must return stably once construction is done; a backend that
      is built but still cannot state its dimension is not acceptable.
    - A backend opens no SQLite connection and touches no bucket file; storage and querying
      belong to the EmbeddingEngine facade.
    """

    @abc.abstractmethod
    def generate(self, text: str) -> list[float]:
        """Compute one vector synchronously. On failure return an empty list, never raise."""

    @abc.abstractmethod
    def model_name(self) -> str:
        """The current model name (written into metadata and shown in the front end)."""

    @abc.abstractmethod
    def vector_dim(self) -> int:
        """The vector dimension (checked against db metadata so two kinds never mix)."""

    @abc.abstractmethod
    async def generate_async(self, text: str) -> list[float]:
        """Compute one vector asynchronously (the production path). On failure return an
        empty list, never raise."""


# ============================================================
# API backend: OpenAI-compatible (Gemini by default)
# ============================================================

class APIEmbeddingEngine(BaseEmbeddingEngine):
    """A remote OpenAI-compatible embedding API (Gemini by default).

    An api_key is required; an empty one raises OB-F001 up in the facade. This class does
    nothing but send the request and take the vector.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int = _GEMINI_DEFAULT_DIM,
        timeout_seconds: float = _API_TIMEOUT_SECONDS,
        api_format: str = "openai_compat",
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.api_format = (api_format or "openai_compat").strip().lower()
        self.backend_name = (
            "ollama" if self.api_format in ("ollama", "local") else "api"
        )
        self.timeout_seconds = positive_float(timeout_seconds, _API_TIMEOUT_SECONDS)
        # Google's OpenAI-compatible endpoint wants OpenAI-style bare model IDs.
        # Native REST uses the "models/" resource prefix, so normalize pasted
        # native IDs here before calling embeddings.create().
        self.model = normalize_model_for_endpoint(model, base_url, self.api_format)
        self._dim = dim
        # A local or containerised ollama must bypass the system proxy. httpx defaults to
        # trust_env=True, which reads the environment variables **and the Windows
        # registry / WinINET system proxy**, so the moment a desktop proxy client is
        # running, even 127.0.0.1:11434 gets handed to the proxy -> an empty 502 and local
        # vectorisation dies outright. (A Docker deployment has no proxy, so this never
        # showed up there, but it is extremely common for bare-metal users.)
        # Deciding "local": base_url points at localhost / 127.0.0.1 / the ollama container
        # name -> trust_env=False.
        # Cloud backends keep trust_env=True, since in many regions a proxy is exactly what
        # is needed to reach them.
        _host = base_url or ""
        _is_local_host = any(h in _host for h in ("127.0.0.1", "localhost", "ollama", "[::1]"))
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=not _is_local_host),
        )

    def model_name(self) -> str:
        return self.model

    def vector_dim(self) -> int:
        return self._dim

    def generate(self, text: str) -> list[float]:
        """The synchronous interface the base protocol requires. Production goes through
        generate_async."""
        try:
            return asyncio.run(self.generate_async(text))
        except RuntimeError:
            logger.warning("[embedding] sync generate() called inside event loop; use generate_async")
            return []

    async def generate_async(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        try:
            response = await self._client.embeddings.create(
                model=self.model,
                input=text[:_MAX_INPUT_CHARS],
            )
            if response.data and len(response.data) > 0:
                vec = response.data[0].embedding
                # The first vector that comes back confirms the real dimension
                if vec and len(vec) != self._dim:
                    self._dim = len(vec)
                if vec:
                    return list(vec)
            # A 2xx response arrived but carries no usable vector. Returning [] silently is
            # not acceptable: "the call succeeded and produced nothing" would then happen
            # without a sound. Record OB-E001 so the error panel can see it.
            self._record_e001(
                f"backend={self.backend_name} model={self.model} 返回空向量"
                f"（base_url={self.base_url}，检查 model 名 / base_url / key 是否匹配该 provider）"
            )
            return []
        except Exception as e:
            _hint = _humanize_api_error(
                e,
                api_format=self.api_format,
                base_url=self.base_url,
            )
            self._record_e001(
                f"backend={self.backend_name} model={self.model} base_url={self.base_url} "
                f"err={type(e).__name__}: {e}" + (f" {_hint}" if _hint else "")
            )
            return []

    @staticmethod
    def _record_e001(detail: str) -> None:
        try:
            from errors import record_error  # type: ignore
        except ImportError:
            from .errors import record_error  # type: ignore
        try:
            record_error("OB-E001", detail)
        except Exception:
            logger.warning(f"[embedding] OB-E001 (record failed): {detail}")


# ============================================================
# API backend: native Gemini REST
# ============================================================

class GeminiNativeEmbeddingEngine(BaseEmbeddingEngine):
    """Native Gemini REST embedding: no OpenAI-compat wrapper, embedContent called directly.

    Endpoint: POST .../v1beta/models/{model}:embedContent?key={api_key}
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        dim: int = _GEMINI_DEFAULT_DIM,
        timeout_seconds: float = _API_TIMEOUT_SECONDS,
    ):
        self.api_key = api_key
        self.model = model
        self._dim = dim
        self.timeout_seconds = positive_float(timeout_seconds, _API_TIMEOUT_SECONDS)

    def model_name(self) -> str:
        return self.model

    def vector_dim(self) -> int:
        return self._dim

    def generate(self, text: str) -> list[float]:
        try:
            return asyncio.run(self.generate_async(text))
        except RuntimeError:
            logger.warning("[embedding] sync generate() called inside event loop; use generate_async")
            return []

    async def generate_async(self, text: str) -> list[float]:
        if not text or not text.strip():
            return []
        import httpx
        model_id = strip_native_resource_prefix(self.model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:embedContent"
        payload = {"content": {"parts": [{"text": text[:_MAX_INPUT_CHARS]}]}}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as c:
                r = await c.post(
                    url,
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                )
                r.raise_for_status()
            values = r.json().get("embedding", {}).get("values", [])
            if values and len(values) != self._dim:
                self._dim = len(values)
            if values:
                return list(values)
            self._record_e001(
                f"backend=gemini_native model={self.model} 返回空向量（检查模型名是否支持 embedContent）"
            )
            return []
        except Exception as e:
            self._record_e001(
                f"backend=gemini_native model={self.model} err={type(e).__name__}: {e}"
            )
            return []

    @staticmethod
    def _record_e001(detail: str) -> None:
        try:
            from errors import record_error  # type: ignore
        except ImportError:
            from .errors import record_error  # type: ignore
        try:
            record_error("OB-E001", detail)
        except Exception:
            logger.warning(f"[embedding] OB-E001 (record failed): {detail}")


# ============================================================
# The facade: EmbeddingEngine — the public interface stays as it was
# ============================================================

class EmbeddingEngine:
    """SQLite storage, search and metadata checks, holding one BaseEmbeddingEngine."""

    def __init__(self, config: dict):
        self.v3_runtime = None
        # A small in-process LRU of text -> embedding, to collapse repeated vector requests
        # that arrive close together.
        self._query_cache: "OrderedDict[str, list[float]]" = OrderedDict()
        embed_cfg = config.get("embedding", {}) or {}
        timeout_seconds = positive_float(embed_cfg.get("timeout_seconds"), _API_TIMEOUT_SECONDS)

        # Resolve the backend: env > config > the "api" default
        self.backend = "api"

        # 2) Resolve `enabled`. OB-F001: enabled=true with an empty api_key on an api
        #    backend means refusing to start.
        enabled_cfg = parse_bool(embed_cfg.get("enabled", True), default=True)

        # 3) Resolve the SQLite path (a test fixture may override it via db_path)
        custom_db = (embed_cfg.get("db_path") or "").strip()
        if custom_db:
            self.db_path = custom_db
        else:
            self.db_path = os.path.join(config["buckets_dir"], "embeddings.db")

        # 4) Instantiate the backend
        self._backend: BaseEmbeddingEngine | None = None
        self.enabled = False
        # `model` is a mirror attribute, kept because the hot reload in server.py setattrs
        # it directly
        self.model: str = ""

        if not enabled_cfg:
            # Explicitly off: no-op mode, but still initialise the db so list_all_ids works
            self._init_db()
            return

        # Resolve api_format ahead of the key check: a local ollama backend needs no real
        # key, and must not be knocked into standby just because the key is empty.
        api_format = (
            (embed_cfg.get("api_format") or "").strip()
            or os.environ.get("LOCI_EMBED_FORMAT", "openai_compat")
        ).lower()
        self.api_format = api_format
        is_local = api_format in ("ollama", "local")

        api_key = (embed_cfg.get("api_key") or "").strip()
        if not api_key:
            api_key = os.environ.get("LOCI_EMBED_API_KEY", "").strip()
        # A local model has no notion of a key, but the OpenAI client library insists
        # api_key be non-empty, so a placeholder is supplied. It goes to ollama as a Bearer
        # token, which ollama does not check and accepts as given.
        if is_local:
            # Never forward a retained Gemini/SiliconFlow secret to a local or
            # user-supplied Ollama URL. The real cloud key stays in config for
            # switching back, but the local runtime uses a non-secret token.
            api_key = "ollama"

        if not api_key:
            # No key (only a cloud backend reaches here) -> standby: enabled=False, the DB
            # is still initialised, and a hot key update activates it
            logger.warning("[embedding] enabled=true but no api_key — starting in standby (disabled); set LOCI_EMBED_API_KEY to activate")
            self._init_db()
            return

        if is_local:
            # Local Ollama: the OpenAI-compatible /v1/embeddings.
            # The default address branches on the host: inside Docker, the ollama container
            # on the same network; on bare metal, 127.0.0.1 — otherwise a native user
            # switching to local would try to reach a container name that does not exist.
            _local_default = (
                "http://ollama:11434/v1"
                if os.path.exists("/.dockerenv")
                else "http://127.0.0.1:11434/v1"
            )
            configured_base = (embed_cfg.get("base_url") or "").strip()
            if configured_base and is_known_cloud_embedding_endpoint(configured_base):
                logger.warning(
                    "[embedding] local mode ignored stale cloud base_url=%s",
                    configured_base,
                )
                configured_base = ""
            base_url = (
                os.environ.get("LOCI_OLLAMA_URL", "").strip()
                or configured_base
                or _local_default
            )
            model = embed_cfg.get("model") or "bge-m3"
            # bge-m3 is 1024-dimensional; APIEmbeddingEngine self-corrects once it has its
            # first vector, but the default given here should still be right
            try:
                dim = int(embed_cfg.get("dim") or 1024)
            except (TypeError, ValueError):
                dim = 1024
            self._backend = APIEmbeddingEngine(
                api_key=api_key,
                base_url=base_url,
                model=model,
                dim=dim,
                timeout_seconds=timeout_seconds,
                api_format=api_format,
            )
        elif api_format == "gemini":
            model = embed_cfg.get("model") or "gemini-embedding-001"
            self._backend = GeminiNativeEmbeddingEngine(
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        else:
            model = embed_cfg.get("model") or "gemini-embedding-001"
            base_url = (
                (embed_cfg.get("base_url") or "").strip()
                or "https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            # Read `dim` and pass it through. Without that, an OpenAI-compatible model with
            # a non-default dimension gets pinned to APIEmbeddingEngine's Gemini default,
            # so at startup the db dimension and the current dimension disagree, OB-W005
            # fires, and the user is pushed into a migration — even though config.yaml
            # already says embedding.dim: 1024.
            # The fallback is _GEMINI_DEFAULT_DIM rather than 1024 because this branch's
            # default endpoint and model ARE Gemini: with no explicit dim configured it has
            # to keep Gemini's official default, or the default Gemini path gets broken
            # instead.
            try:
                dim = int(embed_cfg.get("dim") or _GEMINI_DEFAULT_DIM)
            except (TypeError, ValueError):
                dim = _GEMINI_DEFAULT_DIM
            self._backend = APIEmbeddingEngine(
                api_key=api_key,
                base_url=base_url,
                model=model,
                dim=dim,
                timeout_seconds=timeout_seconds,
                api_format=api_format,
            )

        self.model = self._backend.model_name()
        self.enabled = True

        # 5) Initialise SQLite and verify the metadata
        self._init_db()
        self._check_meta_consistency()

    def attach_v3_runtime(self, runtime) -> None:
        self.v3_runtime = runtime

    # -------------------- SQLite initialisation --------------------

    def _init_db(self) -> None:
        """Create the tables: the main `embeddings` table plus the `embeddings_meta`
        metadata table."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    bucket_id TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
            }
            if "content_hash" not in columns:
                conn.execute(
                    "ALTER TABLE embeddings ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"
                )
            if "meaning_embedding" not in columns:
                # The meaning vector gets a column of its own and is never generated mixed
                # in with the content embedding, because a long content would dilute the
                # semantic signal of a one-sentence meaning. NULL means the bucket has no
                # meaning written, or its meaning has not been vectorised successfully yet
                # (no dedicated migration of existing buckets is needed).
                conn.execute(
                    "ALTER TABLE embeddings ADD COLUMN meaning_embedding TEXT"
                )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _read_meta(self) -> dict[str, str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT key, value FROM embeddings_meta").fetchall()
            return {k: v for k, v in rows}
        finally:
            conn.close()

    def _write_meta(self, key: str, value: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def _check_meta_consistency(self) -> None:
        """Reconcile the recorded model_name / vector_dim against the current backend.

        - Main table empty: this is the first write, so overwriting the meta is harmless
        - Meta disagrees with the current backend: log an OB-W005 warning telling the user
          to run the migration
        """
        if not self._backend:
            return
        meta = self._read_meta()
        conn = sqlite3.connect(self.db_path)
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        finally:
            conn.close()

        cur_name = self._backend.model_name()
        cur_dim = str(self._backend.vector_dim())

        if cnt == 0:
            self._write_meta("model_name", cur_name)
            self._write_meta("vector_dim", cur_dim)
            return

        old_name = meta.get("model_name", "")
        old_dim = meta.get("vector_dim", "")
        if not old_name and not old_dim:
            # An older store has no rows in the meta table: write them once, without warning
            self._write_meta("model_name", cur_name)
            self._write_meta("vector_dim", cur_dim)
            return

        # Normalise the model names before comparing: the Gemini OpenAI-compat endpoint
        # requires a "models/" prefix while OpenAI-compatible proxies use the bare name, so
        # one and the same model (models/gemini-embedding-001 vs gemini-embedding-001) gets
        # misread as a mismatch and fires a false OB-W005. A prefix is only an endpoint
        # spelling convention and says nothing about model identity, so it is always
        # stripped before comparison.
        if _norm_model(old_name) == _norm_model(cur_name) and old_name != cur_name:
            # Materially identical, differing only in prefix: upgrade the meta to the
            # current spelling while we are here, so startup does not re-reconcile forever
            self._write_meta("model_name", cur_name)
            old_name = cur_name

        # Same model name but a different dimension: almost certainly the backend's `_dim`
        # is still its initial default (it cannot self-correct until the first vector has
        # been generated — the openai_compat branch defaults bge-m3 to 768 when the truth is
        # 1024), while old_dim in the db is that model's real output dimension. A given
        # model's dimension is constant, so trust the db, correct the backend from it, and
        # do not fire OB-W005. This is exactly the root cause of "W005 keeps firing again
        # after a rebuild or redeploy": reconciliation always happens before
        # self-correction. Only a genuinely different model name (an actual model change)
        # falls through to the W005 below and prompts a migration.
        if (
            _norm_model(old_name) == _norm_model(cur_name)
            and old_dim and old_dim != cur_dim
        ):
            try:
                self._backend._dim = int(old_dim)
                cur_dim = old_dim
                logger.info(
                    f"[embedding] 按 db 已存维度校正后端 vector_dim → {old_dim}"
                    f"（模型 {cur_name} 一致，避免假 OB-W005）"
                )
            except (TypeError, ValueError):
                pass

        if _norm_model(old_name) != _norm_model(cur_name) or old_dim != cur_dim:
            try:
                from errors import record_error  # type: ignore
            except ImportError:
                from .errors import record_error  # type: ignore
            record_error(
                "OB-W005",
                (
                    f"embeddings.db meta mismatch: "
                    f"db(model={old_name},dim={old_dim}) vs current(model={cur_name},dim={cur_dim}). "
                    f"Run /api/embedding/migrate to re-index."
                ),
            )

    # -------------------- Generate and store --------------------

    async def _generate_async(self, text: str) -> list[float]:
        """One vector for `text`, asked of the backend at most once at a time.

        A write asks for its body's vector twice at the same moment: the look-back searches
        with it (core/_reconsolidation) while the vector queue stores it. The cache alone
        does not join them — neither has an answer yet — so a call already in flight for
        the same text on the same loop is awaited instead of made again, and its answer is
        cached whoever started it (also when that caller gave up waiting: the look-back's
        time budget does not cancel the call the queue is waiting on)."""
        if not self._backend:
            return []
        cached = self._query_cache.get(text)
        if cached is not None:
            self._query_cache.move_to_end(text)
            return list(cached)
        loop = asyncio.get_running_loop()
        in_flight = self.__dict__.setdefault("_in_flight", {})
        held = in_flight.get(text)
        if held is None or held[0] is not loop:
            task = loop.create_task(self._backend.generate_async(text))
            held = (loop, task)
            in_flight[text] = held
            task.add_done_callback(lambda done, text=text: self._settle(text, done))
        embedding = await asyncio.shield(held[1])
        return list(embedding or [])

    def _settle(self, text: str, task: "asyncio.Task") -> None:
        """A backend call finished: stop sharing it, and cache what it brought back."""
        in_flight = self.__dict__.get("_in_flight") or {}
        if in_flight.get(text, (None, None))[1] is task:
            in_flight.pop(text, None)
        if task.cancelled() or task.exception() is not None:
            return
        embedding = task.result()
        if embedding:
            self._query_cache[text] = list(embedding)
            self._query_cache.move_to_end(text)
            if len(self._query_cache) > _QUERY_CACHE_MAXSIZE:
                self._query_cache.popitem(last=False)

    async def probe(self, timeout_seconds: float = 4.0) -> tuple[bool, str]:
        """Actually ask the backend for one vector. Returns `(works, why not)`.

        🔴 WHY THIS EXISTS, and why it embeds rather than asking "is the model installed":
            Being configured and being usable are different things, and until this existed
            nothing checked the second one. On a fresh machine the default compose file
            starts an Ollama container with **no model pulled**, so:

                everything comes up · every service reports healthy · the setup screen
                shows a green tick for embedding · and the first real write fails

            The failure is late, and it arrives attached to whatever the person was doing
            at the time rather than to the thing that was actually wrong. Configuration is
            the one moment they were prepared to hear about configuration.

        ⚠️ It asks for a real embedding instead of querying a model list, because a model
           list is a proxy for the question and this is the question. It is also the only
           form that works across backends — Ollama, an OpenAI-compatible API, anything
           else — without this function needing to know which one it is talking to.

        The `why not` string is the same humanized hint the error panel uses, so a 404
        already reads as "that model does not exist on this provider" rather than as a
        status code.
        """
        if not self.enabled or not self._backend:
            return False, "embedding is switched off"
        try:
            vector = await asyncio.wait_for(
                self._backend.generate_async("probe"), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return False, (f"no answer within {timeout_seconds:g}s — is the backend up, "
                           f"and is {self.model} already pulled?")
        except Exception as exc:                      # noqa: BLE001 - report, never raise
            hint = _humanize_api_error(
                exc, api_format=getattr(self._backend, "api_format", ""),
                base_url=getattr(self._backend, "base_url", ""))
            return False, f"{exc}{(' ' + hint) if hint else ''}"
        if not vector:
            return False, "the backend answered, but with an empty vector"
        return True, ""

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """Generate an embedding for the content and store it in SQLite. True on success."""
        if not self.enabled or not content or not content.strip():
            return False
        try:
            embedding = await self._generate_async(content)
            if not embedding:
                return False
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            self._store_embedding(bucket_id, embedding, digest)
            return True
        except Exception as e:
            logger.warning(f"Embedding generation failed for {bucket_id}: {e}")
            return False

    def _store_embedding(
        self, bucket_id: str, embedding: list[float], content_hash: str = ""
    ) -> None:
        try:
            from utils import now_iso  # type: ignore
        except ImportError:
            from .utils import now_iso  # type: ignore
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO embeddings (bucket_id, embedding, updated_at, content_hash)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(bucket_id) DO UPDATE SET
                     embedding=excluded.embedding,
                     updated_at=excluded.updated_at,
                     content_hash=excluded.content_hash""",
                (bucket_id, json.dumps(embedding), now_iso(), content_hash),
            )
            conn.commit()
        finally:
            conn.close()

    async def generate_and_store_meaning(self, bucket_id: str, meaning_text: str) -> bool:
        """Generate and store a separate embedding for the meaning (the most recent one).

        It lives in its own column beside the content vector and the two never overwrite
        each other. The bucket may not have a content vector row yet (the outbox is still
        queued), so this upserts rather than requiring the row to exist.
        """
        if not self.enabled or not meaning_text or not meaning_text.strip():
            return False
        try:
            embedding = await self._generate_async(meaning_text)
            if not embedding:
                return False
            self._store_meaning_embedding(bucket_id, embedding)
            return True
        except Exception as e:
            logger.warning(f"Meaning embedding generation failed for {bucket_id}: {e}")
            return False

    def _store_meaning_embedding(self, bucket_id: str, embedding: list[float]) -> None:
        try:
            from utils import now_iso  # type: ignore
        except ImportError:
            from .utils import now_iso  # type: ignore
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO embeddings (bucket_id, embedding, updated_at, content_hash, meaning_embedding)
                   VALUES (?, '', ?, '', ?)
                   ON CONFLICT(bucket_id) DO UPDATE SET
                     meaning_embedding=excluded.meaning_embedding""",
                (bucket_id, now_iso(), json.dumps(embedding)),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_embedding(self, bucket_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
            conn.commit()
        finally:
            conn.close()

    def list_all_ids(self) -> list[str]:
        """For orphan reconciliation: every bucket_id in the embeddings table."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT bucket_id FROM embeddings").fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    def list_content_ids(self) -> list[str]:
        """Return IDs that have a real content vector, not only meaning data.

        Rows created by ``generate_and_store_meaning`` deliberately contain an
        empty ``embedding`` value until the durable content outbox catches up.
        Legacy content rows, on the other hand, may have an empty
        ``content_hash`` after the schema migration while still holding a valid
        vector. Inspecting the vector column keeps those two states distinct.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT bucket_id FROM embeddings WHERE TRIM(embedding) <> ''"
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            conn.close()

    def list_content_hashes(self) -> dict[str, str]:
        """Return hashes recorded by new writes; legacy rows contain ``""``."""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT bucket_id, content_hash FROM embeddings"
            ).fetchall()
            return {str(bucket_id): str(digest or "") for bucket_id, digest in rows}
        finally:
            conn.close()

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT embedding FROM embeddings WHERE bucket_id = ?", (bucket_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    # -------------------- Search --------------------

    async def search_similar_strict(
        self, query: str, top_k: int = 10, among: Optional[Collection[str]] = None
    ) -> list[tuple[str, float]]:
        """Return ranked neighbors, surfacing provider failures to the caller.

        `among` ranks only those bucket ids (the slicer's "memories written that day");
        None ranks every stored vector. An empty `among` asks the provider nothing."""
        if not self.enabled:
            raise RuntimeError("embedding is disabled")
        if among is not None:
            among = frozenset(str(i) for i in among)
            if not among:
                return []

        # Preserve the old behaviour of not calling the provider for an empty
        # index, without retaining any embedding payload across the await.
        conn = sqlite3.connect(self.db_path)
        try:
            has_rows = conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone()
        finally:
            conn.close()
        if not has_rows:
            return []

        query_embedding = await self._generate_async(query)
        if not query_embedding:
            raise RuntimeError("embedding provider returned an empty query vector")

        if top_k <= 0:
            return []

        # Heap entries use (score, -SQLite row index, bucket_id).  Larger is
        # better, so the min-heap root is always the worst retained candidate;
        # for equal scores, later rows are worse.  This preserves the old
        # stable-sort tie order while bounding ranking memory to O(top_k).
        top_results: list[tuple[float, int, str]] = []
        row_index = 0
        query_dim = len(query_embedding)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT bucket_id, embedding, meaning_embedding FROM embeddings"
            )
            while True:
                rows = cursor.fetchmany(_SEARCH_BATCH_ROWS)
                if not rows:
                    break

                bucket_ids: list[str] = []
                best_scores: list[float | None] = []
                candidate_vectors: list[list[float]] = []
                candidate_owners: list[int] = []
                for bucket_id, emb_json, meaning_emb_json in rows:
                    if among is not None and bucket_id not in among:
                        continue
                    # A bucket may carry both a content vector and a meaning vector; take
                    # whichever scores higher. Every large object here lives only until the
                    # end of the current small batch.
                    owner = len(bucket_ids)
                    bucket_ids.append(bucket_id)
                    best_scores.append(None)
                    for label, raw_embedding in (
                        ("embedding", emb_json),
                        ("meaning embedding", meaning_emb_json),
                    ):
                        if not raw_embedding:
                            continue
                        try:
                            stored_embedding = json.loads(raw_embedding)
                            if not isinstance(stored_embedding, list):
                                raise TypeError(
                                    f"embedding is {type(stored_embedding).__name__}, not list"
                                )
                            if not stored_embedding:
                                continue
                            stored_embedding = [float(value) for value in stored_embedding]
                        except (json.JSONDecodeError, ValueError, TypeError) as _emb_exc:
                            logger.warning(
                                f"[embedding] Skipping malformed {label} for {bucket_id!r}: "
                                f"{type(_emb_exc).__name__}: {_emb_exc}"
                            )
                            continue
                        if len(stored_embedding) != query_dim:
                            # Preserve the pairwise helper's existing contract:
                            # a dimension mismatch contributes a 0.0 score.
                            logger.warning(
                                f"[embedding] {label} dimension mismatch for {bucket_id!r}: "
                                f"stored={len(stored_embedding)}, query={query_dim}"
                            )
                            current = best_scores[owner]
                            best_scores[owner] = (
                                0.0 if current is None else max(current, 0.0)
                            )
                            continue
                        candidate_vectors.append(stored_embedding)
                        candidate_owners.append(owner)

                if candidate_vectors:
                    similarities = self._cosine_similarity_batch(
                        query_embedding, candidate_vectors
                    )
                    for owner, similarity in zip(candidate_owners, similarities):
                        score = float(similarity)
                        current = best_scores[owner]
                        best_scores[owner] = (
                            score if current is None else max(current, score)
                        )

                for bucket_id, score in zip(bucket_ids, best_scores):
                    current_index = row_index
                    row_index += 1
                    if score is None:
                        continue
                    entry = (score, -current_index, bucket_id)
                    if len(top_results) < top_k:
                        heapq.heappush(top_results, entry)
                    elif entry > top_results[0]:
                        heapq.heapreplace(top_results, entry)
        finally:
            conn.close()

        top_results.sort(reverse=True)
        return [(bucket_id, score) for score, _negative_index, bucket_id in top_results]

    async def search_similar(self, query: str, top_k: int = 10,
                             among: Optional[Collection[str]] = None) -> list[tuple[str, float]]:
        """Returns [(bucket_id, similarity)]; on failure it returns an empty list, for
        compatibility with older callers. `among` as in search_similar_strict."""
        try:
            return await self.search_similar_strict(query, top_k=top_k, among=among)
        except Exception as e:
            logger.warning(f"Query embedding failed: {e}")
            return []

    async def search(self, query: str, top_k: int = 10) -> list[str]:
        """The newer interface per spec: a list of bucket ids only."""
        pairs = await self.search_similar(query, top_k=top_k)
        return [bid for bid, _ in pairs]

    @staticmethod
    def _cosine_similarity_batch(
        query: list[float], vectors: list[list[float]]
    ) -> "np.ndarray":
        """Return cosine similarity for equally-sized vectors in one matrix pass."""
        query_array = np.asarray(query, dtype=np.float64)
        matrix = np.asarray(vectors, dtype=np.float64)
        dots = matrix @ query_array
        denominator = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_array)
        return np.divide(
            dots,
            denominator,
            out=np.zeros_like(dots),
            where=denominator != 0,
        )

    # -------------------- Status, readable by the front end --------------------

    def status(self) -> dict[str, Any]:
        """What the front end's /api/embedding/status reads."""
        if not self._backend:
            return {
                "enabled": False,
                "backend": self.backend,
                "model": "",
                "vector_dim": 0,
                "db_path": self.db_path,
                "embedding_count": 0,
            }
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            cnt = -1
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "model": self._backend.model_name(),
            "vector_dim": self._backend.vector_dim(),
            "db_path": self.db_path,
            "embedding_count": cnt,
        }


__all__ = [
    "BaseEmbeddingEngine",
    "APIEmbeddingEngine",
    "GeminiNativeEmbeddingEngine",
    "EmbeddingEngine",
]
