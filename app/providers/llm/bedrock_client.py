"""AWS Bedrock chat client with an OpenAI-style completions surface for existing callers."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.refusal = None


class _Choice:
    def __init__(self, message: _Message, finish_reason: str) -> None:
        self.message = message
        self.finish_reason = finish_reason


class _ChatCompletion:
    def __init__(self, choices: list[_Choice]) -> None:
        self.choices = choices


class _ChatCompletions:
    def __init__(self, runtime: Any, model_id: str) -> None:
        self._runtime = runtime
        self._model_id = model_id

    def create(self, **kwargs: Any) -> _ChatCompletion:
        messages: list[dict[str, str]] = kwargs.get("messages") or []
        model_id = str(kwargs.get("model") or self._model_id)
        max_tokens = int(kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or 4096)
        temperature = kwargs.get("temperature")

        system_blocks: list[dict[str, str]] = []
        converse_messages: list[dict[str, Any]] = []

        prefer_json = False
        rf = kwargs.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            prefer_json = True

        for msg in messages:
            role = msg.get("role", "user")
            content = str(msg.get("content") or "")
            if role == "system":
                system_blocks.append({"text": content})
            elif role in ("user", "assistant"):
                converse_messages.append(
                    {"role": role, "content": [{"text": content}]}
                )
            else:
                converse_messages.append(
                    {"role": "user", "content": [{"text": content}]}
                )

        if prefer_json and system_blocks:
            system_blocks.append(
                {
                    "text": "Respond with valid JSON only. No markdown fences or commentary.",
                }
            )
        elif prefer_json:
            system_blocks = [
                {
                    "text": (
                        "Respond with valid JSON only. No markdown fences or commentary."
                    ),
                }
            ]

        if not converse_messages:
            raise ValueError("Bedrock chat requires at least one user message")

        request: dict[str, Any] = {
            "modelId": model_id,
            "messages": converse_messages,
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system_blocks:
            request["system"] = system_blocks
        if temperature is not None:
            request["inferenceConfig"]["temperature"] = float(temperature)

        try:
            response = self._runtime.converse(**request)
        except (ClientError, BotoCoreError) as exc:
            logger.warning("Bedrock converse failed model=%s: %s", model_id, exc)
            raise RuntimeError(f"Bedrock request failed: {exc}") from exc

        output = response.get("output") or {}
        message = output.get("message") or {}
        parts: list[str] = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
        text = "".join(parts).strip()

        stop_reason = str(response.get("stopReason") or "end_turn")
        finish_reason = "length" if stop_reason == "max_tokens" else "stop"

        return _ChatCompletion(
            choices=[_Choice(_Message(text), finish_reason=finish_reason)]
        )


class _Chat:
    def __init__(self, runtime: Any, model_id: str) -> None:
        self.completions = _ChatCompletions(runtime, model_id)


class BedrockChatClient:
    """Minimal OpenAI-compatible client backed by Bedrock Converse API."""

    def __init__(self, runtime: Any, model_id: str) -> None:
        self.chat = _Chat(runtime, model_id)


@lru_cache(maxsize=4)
def get_bedrock_runtime_client(*, region_name: str | None = None) -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is not installed. Run: pip install boto3") from exc

    access_key = settings.resolved_bedrock_access_key_id
    secret_key = settings.resolved_bedrock_secret_access_key
    if not access_key or not secret_key:
        raise RuntimeError(
            "AWS credentials are not configured for Bedrock. "
            "Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or BEDROCK_AWS_*."
        )

    region = (region_name or settings.resolved_bedrock_region).strip()
    return boto3.client(
        "bedrock-runtime",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )
