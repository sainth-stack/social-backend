"""AWS Bedrock text-to-image (Nova Canvas / Titan Image Generator)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.providers.llm.bedrock_client import get_bedrock_runtime_client

logger = logging.getLogger(__name__)

# Nova Canvas — highest-quality Bedrock option for marketing/social visuals.
DEFAULT_IMAGE_MODEL_ID = "amazon.nova-canvas-v1:0"
TITAN_IMAGE_MODEL_ID = "amazon.titan-image-generator-v2:0"


def image_generation_configured() -> bool:
    model = (settings.bedrock_image_model_id or DEFAULT_IMAGE_MODEL_ID).strip()
    return bool(
        model
        and settings.resolved_bedrock_access_key_id
        and settings.resolved_bedrock_secret_access_key
    )


def _resolved_image_model_id() -> str:
    return (settings.bedrock_image_model_id or DEFAULT_IMAGE_MODEL_ID).strip()


def _parse_size(size: str) -> tuple[int, int]:
    raw = (size or "1024x1024").lower().replace(" ", "")
    if "x" in raw:
        w, h = raw.split("x", 1)
        try:
            width = max(512, min(4096, int(w)))
            height = max(512, min(4096, int(h)))
            return width, height
        except ValueError:
            pass
    return 1024, 1024


def _invoke_image_model(body: dict[str, Any], *, model_id: str | None = None) -> list[str]:
    model = model_id or _resolved_image_model_id()
    runtime = get_bedrock_runtime_client()
    try:
        response = runtime.invoke_model(
            modelId=model,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Bedrock image invoke failed model=%s: %s", model, exc)
        raise RuntimeError(f"Bedrock image generation failed: {exc}") from exc

    payload = json.loads(response["body"].read())
    images = payload.get("images") or []
    if not images:
        err = payload.get("error") or payload.get("message") or "no images returned"
        raise RuntimeError(f"Bedrock image model returned no images: {err}")
    return [str(img) for img in images]


def generate_text_to_image(
    *,
    prompt: str,
    size: str = "1024x1024",
    quality: str | None = None,
) -> bytes:
    width, height = _parse_size(size)
    q = (quality or settings.bedrock_image_quality or "premium").strip().lower()
    if q not in ("standard", "premium"):
        q = "premium"

    body: dict[str, Any] = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {"text": prompt[:1024]},
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "quality": q,
            "width": width,
            "height": height,
            "cfgScale": 8.0,
        },
    }

    model_id = _resolved_image_model_id()
    try:
        b64_images = _invoke_image_model(body, model_id=model_id)
    except RuntimeError:
        if model_id != TITAN_IMAGE_MODEL_ID:
            logger.info("Retrying image generation with %s", TITAN_IMAGE_MODEL_ID)
            titan_body = dict(body)
            titan_cfg = dict(titan_body["imageGenerationConfig"])
            titan_cfg.pop("quality", None)
            titan_body["imageGenerationConfig"] = titan_cfg
            b64_images = _invoke_image_model(titan_body, model_id=TITAN_IMAGE_MODEL_ID)
        else:
            raise

    return base64.b64decode(b64_images[0])


def generate_image_variation(
    *,
    prompt: str,
    source_image_bytes: bytes,
    size: str = "1024x1024",
    quality: str | None = None,
) -> bytes:
    width, height = _parse_size(size)
    q = (quality or settings.bedrock_image_quality or "premium").strip().lower()
    if q not in ("standard", "premium"):
        q = "premium"

    source_b64 = base64.b64encode(source_image_bytes).decode("ascii")
    body: dict[str, Any] = {
        "taskType": "IMAGE_VARIATION",
        "imageVariationParams": {
            "text": prompt[:1024],
            "images": [source_b64],
            "similarityStrength": 0.6,
        },
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "quality": q,
            "width": width,
            "height": height,
            "cfgScale": 8.0,
        },
    }

    model_id = _resolved_image_model_id()
    try:
        b64_images = _invoke_image_model(body, model_id=model_id)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "image_variation" in msg or "tasktype" in msg or model_id != TITAN_IMAGE_MODEL_ID:
            raise RuntimeError(
                "Image refinement is not supported for this Bedrock model. "
                "Upload a new image or use create mode."
            ) from exc
        raise

    return base64.b64decode(b64_images[0])
