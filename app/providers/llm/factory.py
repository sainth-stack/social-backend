"""LLM provider — standard OpenAI API."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm_client() -> OpenAI:
    """Return a process-wide singleton client for chat completions."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    return OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)


@lru_cache(maxsize=1)
def get_image_client() -> OpenAI:
    """Client for image generation (dall-e-3)."""
    return get_llm_client()


def get_video_client() -> OpenAI:
    """Client for video generation (falls back to chat client)."""
    return get_llm_client()


def get_llm_model() -> str:
    """Return the configured chat model name."""
    return settings.openai_deployment


def model_supports_custom_temperature(model: str | None = None) -> bool:
    """GPT-5 / o-series reasoning models only accept the default temperature."""
    name = (model or get_llm_model()).lower()
    unsupported = ("gpt-5", "o1", "o3", "o4")
    return not any(name.startswith(prefix) for prefix in unsupported)


def with_chat_temperature(kwargs: dict, *, temperature: float = 0.55) -> dict:
    """Attach temperature only when the active model supports custom values."""
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
