"""
========================================
core/ollama_local.py — where a local Ollama answers, and what it holds
========================================

The side model's local mode (core/dehydrator.endpoint) and the embedding's (core/
embedding_engine) talk to one Ollama, and the panel's setting page asks whether it is
there (`GET /api/loci/ollama`, web/config_api). Its address, in this order:

    LOCI_OLLAMA_URL                                   (the operator's word wins)
    embedding.base_url while the embedding runs locally   (what the panel last saved)
    the default for where Loci runs                   (the `ollama` container in Docker,
                                                       127.0.0.1 on bare metal)

Addresses here are Ollama's root, without `/v1`: Ollama's own API (`/api/tags`) hangs off
the root, and its OpenAI-compatible endpoint is the root plus `/v1` (`openai_base`).

`detect()` asks the root's `/api/tags` once, with a short timeout and around any system
proxy (a proxy happily routes 127.0.0.1 through itself and answers 502 for a running
Ollama). It never pulls a model and never starts Ollama (bridge/ollama_child keeps an
installed one running on bare metal).

Public surface: LOCAL_FORMATS · default_root() · root(config) · openai_base(config) ·
                detect(base, timeout)
========================================
"""

from __future__ import annotations

import os

from .provider_detect import is_known_cloud_embedding_endpoint

# The embedding `api_format` values that mean "runs on the local Ollama".
LOCAL_FORMATS = ("ollama", "local")

DETECT_TIMEOUT_SECONDS = 2.0


def _strip_v1(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    return url[:-3].rstrip("/") if url.endswith("/v1") else url


def default_root() -> str:
    """Inside Docker the `ollama` container on the compose network; on bare metal the
    machine itself (a container name does not resolve there)."""
    return "http://ollama:11434" if os.path.exists("/.dockerenv") else "http://127.0.0.1:11434"


def root(config: dict | None) -> str:
    """The Ollama root Loci talks to (see the module header for the order)."""
    env = _strip_v1(os.environ.get("LOCI_OLLAMA_URL", ""))
    if env:
        return env
    emb = (config or {}).get("embedding") if isinstance(config, dict) else None
    if isinstance(emb, dict):
        fmt = str(emb.get("api_format") or "").strip().lower()
        base = str(emb.get("base_url") or "").strip()
        if fmt in LOCAL_FORMATS and base and not is_known_cloud_embedding_endpoint(base):
            return _strip_v1(base)
    return default_root()


def openai_base(config: dict | None) -> str:
    """Ollama's OpenAI-compatible endpoint, what the side model's client is pointed at."""
    return root(config) + "/v1"


def _model_name(name: str) -> str:
    """`latest` is Ollama's default tag (bge-m3 and bge-m3:latest are one model); the
    short name is what config.yaml and the panel's drop-down carry."""
    return name[:-7] if name.endswith(":latest") else name


async def detect(base: str, timeout: float = DETECT_TIMEOUT_SECONDS) -> dict:
    """{"running", "base_url", "models"}: whether Ollama answers at `base` and the models
    it holds (sorted, short names). Not answering adds `error`, the reason in a few words."""
    import httpx

    out: dict = {"running": False, "base_url": base, "models": []}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await client.get(f"{base.rstrip('/')}/api/tags")
        r.raise_for_status()
        data = r.json()
    except Exception as e:      # noqa: BLE001 - every failure reads "not found here"
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    names = []
    for m in (data.get("models") if isinstance(data, dict) else None) or []:
        if isinstance(m, dict):
            name = str(m.get("name") or m.get("model") or "").strip()
            if name:
                names.append(_model_name(name))
    out["running"] = True
    out["models"] = sorted(set(names))
    return out
