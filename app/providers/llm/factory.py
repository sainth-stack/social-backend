"""
LLM provider — Azure OpenAI for chat, image, and video generation.

This standalone social media backend only talks to Azure OpenAI (no AWS
Bedrock hybrid client like the parent OpsBrain-Backend project). Every call
site (`app/social/ai/*`) uses the OpenAI-compatible
``client.chat.completions.create(...)`` shape, which ``AzureOpenAI`` already
implements natively.

Required env vars (chat):
  AZURE_OPENAI_API_KEY
  AZURE_OPENAI_ENDPOINT        https://<resource>.openai.azure.com
  AZURE_OPENAI_API_VERSION     (default: 2024-08-01-preview)
  AZURE_OPENAI_DEPLOYMENT      chat deployment name (default: gpt-4o-mini)

Image generation env vars:
  AZURE_OPENAI_IMAGE_DEPLOYMENT    gpt-image-2 deployment name (default: gpt-image-2)
  AZURE_OPENAI_IMAGE_API_VERSION   must be "preview" for gpt-image-2 (default: preview)

Video generation env vars (Sora 2 — gated preview):
  AZURE_OPENAI_VIDEO_DEPLOYMENT    sora-2 deployment name (default: sora-2)
  AZURE_OPENAI_VIDEO_API_VERSION   must be "preview" (default: preview)
  AZURE_OPENAI_VIDEO_ENDPOINT      optional; falls back to AZURE_OPENAI_ENDPOINT
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from openai import AzureOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm_client() -> AzureOpenAI:
    """Return a process-wide singleton Azure OpenAI client for chat completions."""
    if not settings.azure_openai_api_key or not settings.azure_openai_endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT are not configured."
        )
    return AzureOpenAI(
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        azure_endpoint=settings.azure_openai_endpoint,
    )


def get_image_client() -> AzureOpenAI:
    """Return an AzureOpenAI client configured for gpt-image-2 image generation.

    Uses api_version='preview' which is required for gpt-image-2.
    """
    if not settings.azure_openai_api_key:
        raise RuntimeError("AZURE_OPENAI_API_KEY is not configured.")
    if not settings.azure_openai_endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT is not configured. Set it to https://<your-resource>.openai.azure.com"
        )

    return AzureOpenAI(
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_image_api_version,
        azure_endpoint=settings.azure_openai_endpoint,
    )


def get_video_client() -> AzureOpenAI:
    """Return an AzureOpenAI client configured for Sora 2 video generation.

    Uses api_version='preview' and optionally a separate endpoint when Sora 2
    is deployed on a different Azure OpenAI resource than the chat model.
    """
    if not settings.azure_openai_api_key:
        raise RuntimeError("AZURE_OPENAI_API_KEY is not configured.")
    endpoint = settings.azure_openai_video_endpoint or settings.azure_openai_endpoint
    if not endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_VIDEO_ENDPOINT) is not configured."
        )

    return AzureOpenAI(
        api_key=settings.azure_openai_video_api_key or settings.azure_openai_api_key,
        api_version=settings.azure_openai_video_api_version,
        azure_endpoint=endpoint,
    )


def get_llm_model() -> str:
    """Return the Azure OpenAI chat deployment name."""
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
