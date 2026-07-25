"""
LLM provider — Azure OpenAI / Azure AI Foundry for chat, image, and video.

Supports both endpoint styles:

1) Azure AI Foundry (recommended for gpt-5.4-nano):
   AZURE_OPENAI_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1
   → uses OpenAI(base_url=..., api_key=...)

2) Classic Azure OpenAI:
   AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
   → uses AzureOpenAI(azure_endpoint=..., api_version=..., api_key=...)

Required env vars (chat):
  AZURE_OPENAI_API_KEY
  AZURE_OPENAI_ENDPOINT
  AZURE_OPENAI_DEPLOYMENT      e.g. gpt-5.4-nano
  AZURE_OPENAI_API_VERSION     used only for classic AzureOpenAI clients
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Union

from openai import AzureOpenAI, OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

LlmClient = Union[OpenAI, AzureOpenAI]


def _is_foundry_endpoint(endpoint: str) -> bool:
    e = endpoint.lower()
    return "services.ai.azure.com" in e or e.rstrip("/").endswith("/openai/v1")


def _foundry_base_url(endpoint: str) -> str:
    """Ensure Foundry base_url ends with /openai/v1 (no trailing slash beyond that)."""
    e = endpoint.strip().rstrip("/")
    if e.endswith("/openai/v1"):
        return e
    if e.endswith("/openai"):
        return f"{e}/v1"
    return f"{e}/openai/v1"


def _classic_azure_endpoint(endpoint: str) -> str:
    """Strip /openai/v1 suffix for AzureOpenAI azure_endpoint."""
    e = endpoint.strip().rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if e.endswith(suffix):
            return e[: -len(suffix)]
    return e


def _make_client(
    *,
    endpoint: str,
    api_key: str,
    api_version: str,
) -> LlmClient:
    if _is_foundry_endpoint(endpoint):
        base_url = _foundry_base_url(endpoint)
        logger.info("Using Azure AI Foundry OpenAI client base_url=%s", base_url)
        return OpenAI(base_url=base_url, api_key=api_key)
    azure_endpoint = _classic_azure_endpoint(endpoint)
    logger.info(
        "Using classic AzureOpenAI client endpoint=%s api_version=%s",
        azure_endpoint,
        api_version,
    )
    return AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=azure_endpoint,
    )


@lru_cache(maxsize=1)
def get_llm_client() -> LlmClient:
    """Return a process-wide singleton client for chat completions."""
    if not settings.azure_openai_api_key or not settings.azure_openai_endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT are not configured."
        )
    return _make_client(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


@lru_cache(maxsize=1)
def get_image_client() -> LlmClient:
    """Client for gpt-image-2 — Foundry OpenAI base_url when configured.

    Matches::
        OpenAI(base_url=\"...services.ai.azure.com/openai/v1\", api_key=...)
        client.images.generate(model=\"gpt-image-2\", ...)
    """
    if not settings.azure_openai_api_key:
        raise RuntimeError("AZURE_OPENAI_API_KEY is not configured.")
    if not settings.azure_openai_endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is not configured.")
    return _make_client(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        # Foundry ignores api_version; classic Azure still uses image preview version.
        api_version=settings.azure_openai_image_api_version,
    )


def get_video_client() -> LlmClient:
    """Client for Sora 2 video generation."""
    if not settings.azure_openai_api_key:
        raise RuntimeError("AZURE_OPENAI_API_KEY is not configured.")
    endpoint = settings.azure_openai_video_endpoint or settings.azure_openai_endpoint
    if not endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_VIDEO_ENDPOINT) is not configured."
        )
    return _make_client(
        endpoint=endpoint,
        api_key=settings.azure_openai_video_api_key or settings.azure_openai_api_key,
        api_version=settings.azure_openai_video_api_version,
    )


def get_llm_model() -> str:
    """Return the Azure OpenAI / Foundry chat deployment name."""
    return settings.azure_openai_deployment


def model_supports_custom_temperature(model: str | None = None) -> bool:
    """GPT-5 / o-series reasoning deployments only accept the default temperature."""
    name = (model or get_llm_model()).lower()
    unsupported = ("gpt-5", "o1", "o3", "o4")
    return not any(name.startswith(prefix) for prefix in unsupported)


def with_chat_temperature(kwargs: dict, *, temperature: float = 0.55) -> dict:
    """Attach temperature only when the active deployment supports custom values."""
    out = dict(kwargs)
    if model_supports_custom_temperature(out.get("model")):
        out.setdefault("temperature", temperature)
    else:
        out.pop("temperature", None)
    return out


def apply_low_latency_llm_options(kwargs: dict[str, Any], *, temperature: float = 0.55) -> dict[str, Any]:
    out = dict(kwargs)
    name = (out.get("model") or get_llm_model()).lower()
    if name.startswith(("gpt-5", "o1", "o3", "o4")):
        out["reasoning_effort"] = "none"
        out.pop("temperature", None)
    else:
        out = with_chat_temperature(out, temperature=temperature)
    return out


def create_chat_completion_stream(client: Any, kwargs: dict[str, Any]) -> Any:
    """Create a streaming completion; retry without reasoning_effort if rejected."""
    attempts: list[dict[str, Any]] = [kwargs]
    if kwargs.get("reasoning_effort") is not None:
        stripped = {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
        attempts.append(stripped)
    last_exc: Exception | None = None
    for attempt in attempts:
        try:
            return client.chat.completions.create(**attempt)
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if attempt is attempts[-1] or "reasoning_effort" not in msg:
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("create_chat_completion_stream: no attempts")
