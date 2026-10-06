"""
========================================
core/provider_detect.py — provider detection and model-name normalization
========================================

dehydrator.py (the LLM used for compression) and embedding_engine.py (vectorization)
each grew their own answer to "is this a Gemini endpoint?" and "should this model name
carry the ``models/`` prefix?". The two answers differed slightly in wording and in
coverage, and that divergence was one of the roots of the embedding model-name bug —
bare name confused with ``models/``-prefixed name. The logic is collapsed into one copy
here, shared by both callers, so that endpoint detection only ever has to change in one
place.

What this does NOT do:
- It does not decide the automatic ``api_format`` switch for ``AQ.*`` keys. Only
  dehydrator.py uses that, and it is tightly coupled to the "is this Gemini?" question,
  so hoisting it here would be abstraction for its own sake.
========================================
"""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit


_SILICONFLOW_HOSTS = frozenset({"api.siliconflow.cn", "api.siliconflow.com"})
_KNOWN_CLOUD_EMBEDDING_HOSTS = _SILICONFLOW_HOSTS | frozenset(
    {"generativelanguage.googleapis.com"}
)


def _split_endpoint(base_url: str) -> SplitResult:
    """Parse an endpoint even when a user omitted the URL scheme."""
    value = (base_url or "").strip()
    if value and "://" not in value:
        value = f"//{value}"
    try:
        return urlsplit(value)
    except ValueError:
        return urlsplit("")


def endpoint_hostname(base_url: str) -> str:
    """Return a normalized exact hostname for provider classification."""
    try:
        return (_split_endpoint(base_url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def is_siliconflow_endpoint(base_url: str) -> bool:
    """Whether an endpoint is an official SiliconFlow API hostname."""
    return endpoint_hostname(base_url) in _SILICONFLOW_HOSTS


def is_known_cloud_embedding_endpoint(base_url: str) -> bool:
    """Detect cloud presets that must never be reused by local Ollama mode."""
    return endpoint_hostname(base_url) in _KNOWN_CLOUD_EMBEDDING_HOSTS


def is_gemini_native_host(base_url: str) -> bool:
    """Whether base_url points at Google's generativelanguage.googleapis.com host.

    Host only: it does not care whether the path is native REST or the OpenAI-compat
    subpath. This answers the coarser question "is this key / this base_url talking to
    Google at all?" — for example dehydrator.py's automatic switch on ``AQ.*`` keys.
    """
    return endpoint_hostname(base_url) == "generativelanguage.googleapis.com"


def is_gemini_openai_compat_endpoint(base_url: str) -> bool:
    """Whether base_url is Gemini's OpenAI-compatible endpoint (.../v1beta/openai/).

    As opposed to the native REST endpoint (.../v1beta/models/...:generateContent).
    The OpenAI-compat endpoint wants a bare model name ("gemini-embedding-001"); native
    REST wants the "models/" resource prefix ("models/gemini-embedding-001"). Confusing
    the two is the root cause of OB-E001 ("unexpected model name format").
    """
    parsed = _split_endpoint(base_url)
    return is_gemini_native_host(base_url) and "/openai" in parsed.path.rstrip("/")


def normalize_model_for_endpoint(
    model: str,
    base_url: str,
    api_format: str = "",
) -> str:
    """Decide from the endpoint type whether the model name carries the "models/" prefix.

    - Gemini OpenAI-compat endpoint: strip the "models/" prefix (bare name).
    - Every other endpoint (native Gemini REST, third-party OpenAI-compatible proxies):
      leave the name exactly as given. The native-REST call site strips the prefix itself
      while building its URL, because its resource-path format differs from
      OpenAI-compat's — the two cannot be normalized to one rule here.
    """
    model = (model or "").strip()
    api_format = (api_format or "").strip().lower()
    model_key = model.lower()

    # Local Ollama and SiliconFlow use different public IDs for the same BGE
    # family. Normalize only the known aliases; arbitrary custom model names
    # must remain untouched.
    if api_format in ("ollama", "local"):
        if model_key in ("baai/bge-m3", "baai/bge-m3:latest"):
            return "bge-m3" if not model_key.endswith(":latest") else "bge-m3:latest"
        return model

    if is_siliconflow_endpoint(base_url) and model_key in (
        "bge-m3",
        "bge-m3:latest",
        "baai/bge-m3",
        "baai/bge-m3:latest",
    ):
        return "BAAI/bge-m3"
    if is_gemini_openai_compat_endpoint(base_url):
        return model.removeprefix("models/").strip()
    return model


def strip_native_resource_prefix(model: str) -> str:
    """Strip the native-REST resource-path prefix to get the bare model id.

    The native REST endpoint (.../v1beta/models/{model}:generateContent or
    :embedContent) already contains a "models/" segment in its own URL, so the model
    passed in must not repeat the prefix — otherwise the URL comes out as
    "models/models/xxx".
    """
    return (model or "").strip().removeprefix("models/").strip()
