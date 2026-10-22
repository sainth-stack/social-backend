"""LLM provider — AWS Bedrock (Converse API)."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.providers.llm.bedrock_client import BedrockChatClient, get_bedrock_runtime_client
from app.providers.llm.model_selection import current_models

logger = logging.getLogger(__name__)


def llm_configured() -> bool:
    return bool(
        get_llm_model()
        and settings.resolved_bedrock_access_key_id
        and settings.resolved_bedrock_secret_access_key
    )


def get_llm_client() -> BedrockChatClient:
    """Return a process-wide singleton client for chat completions."""
    model = get_llm_model()
    if not model:
        raise RuntimeError("BEDROCK_MODEL_ID is not configured.")
    return _get_llm_client(model, settings.resolved_bedrock_region)


@lru_cache(maxsize=16)
def _get_llm_client(model: str, region: str) -> BedrockChatClient:
    runtime = get_bedrock_runtime_client(region_name=region)
    return BedrockChatClient(runtime, model)


def get_image_client() -> BedrockChatClient:
    """Image generation uses Bedrock invoke_model, not the chat client."""
    raise RuntimeError("Use app.providers.llm.bedrock_image for image generation.")


def get_video_client() -> BedrockChatClient:
    return get_llm_client()


def get_llm_model() -> str:
    """Return the configured Bedrock model ID."""
    return current_models().text or settings.bedrock_model_id


def model_supports_custom_temperature(model: str | None = None) -> bool:
    name = (model or get_llm_model()).lower()
    unsupported = ("gpt-5", "o1", "o3", "o4")
    return not any(name.startswith(prefix) for prefix in unsupported)


def with_chat_temperature(kwargs: dict, *, temperature: float = 0.55) -> dict:
    out = dict(kwargs)
    if model_supports_custom_temperature(out.get("model")):
        out.setdefault("temperature", temperature)
    else:
        out.pop("temperature", None)
    return out


def apply_low_latency_llm_options(kwargs: dict[str, Any], *, temperature: float = 0.55) -> dict[str, Any]:
    out = dict(kwargs)
    out = with_chat_temperature(out, temperature=temperature)
    out.pop("reasoning_effort", None)
    return out


def create_chat_completion_stream(client: Any, kwargs: dict[str, Any]) -> Any:
    """Streaming not used for social copy; delegate to non-streaming create."""
    return client.chat.completions.create(**kwargs)
