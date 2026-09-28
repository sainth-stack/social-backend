"""AWS Bedrock image generation — Stability SD3.5 (recommended) or Amazon Nova Canvas."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.providers.llm.bedrock_client import get_bedrock_runtime_client

logger = logging.getLogger(__name__)

# Stability — what most accounts see under Model catalog → Output: Image (not Legacy).
DEFAULT_IMAGE_MODEL_ID = "stability.sd3-5-large-v1:0"
NOVA_CANVAS_MODEL_ID = "amazon.nova-canvas-v1:0"


def _is_stability_model(model_id: str) -> bool:
    return model_id.lower().startswith("stability.")


def _is_nova_canvas_model(model_id: str) -> bool:
    return "nova-canvas" in model_id.lower()


def image_generation_configured() -> bool:
    model = (settings.bedrock_image_model_id or DEFAULT_IMAGE_MODEL_ID).strip()
    return bool(
        model
        and settings.resolved_bedrock_access_key_id
        and settings.resolved_bedrock_secret_access_key
    )


def _resolved_image_model_id() -> str:
    model = (settings.bedrock_image_model_id or DEFAULT_IMAGE_MODEL_ID).strip()
    if "titan-image-generator" in model.lower():
        logger.warning("BEDROCK_IMAGE_MODEL_ID uses EOL Titan Image: %s", model)
    return model


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


def _aspect_ratio(width: int, height: int) -> str:
    if height > width * 1.2:
        return "9:16"
    if width > height * 1.2:
        return "16:9"
    return "1:1"


def _friendly_invoke_error(exc: Exception, *, model: str, region: str) -> RuntimeError:
    msg = str(exc).lower()
    if "titan-image-generator" in model.lower() or "end of its life" in msg:
        return RuntimeError(
            "Titan Image Generator is retired. Use stability.sd3-5-large-v1:0 "
            "(Bedrock → Model access → Stability AI) or amazon.nova-canvas-v1:0."
        )
    if "resourcenotfound" in msg or "model not found" in msg:
        hint = (
            "Stability SD3.5: enable in us-west-2 (Oregon). "
            "Nova Canvas (Legacy): enable in us-east-1 (N. Virginia)."
        )
        if _is_stability_model(model):
            hint = (
                f"Enable {model} under Bedrock → Model access in {region}. "
                "Stability image models are often in US West (Oregon) us-west-2."
            )
        return RuntimeError(
            f"Bedrock image model not available in {region}: {model}. {hint}"
        )
    if "accessdenied" in msg or "not authorized" in msg:
        return RuntimeError(
            f"IAM denied bedrock:InvokeModel for {model} in {region}."
        )
    return RuntimeError(f"Bedrock image generation failed ({region}, {model}): {exc}")


def _invoke_raw(body: dict[str, Any], *, model_id: str) -> list[str]:
    region = settings.resolved_bedrock_image_region
    runtime = get_bedrock_runtime_client(region_name=region)
    try:
        response = runtime.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning("Bedrock image invoke failed model=%s region=%s: %s", model_id, region, exc)
        raise _friendly_invoke_error(exc, model=model_id, region=region) from exc

    payload = json.loads(response["body"].read())
    images = payload.get("images") or []
    if not images:
        err = payload.get("error") or payload.get("message") or "no images returned"
        raise RuntimeError(f"Bedrock image model returned no images: {err}")
    return [str(img) for img in images]


def _stability_text_to_image_body(*, prompt: str, width: int, height: int) -> dict[str, Any]:
    return {
        "prompt": prompt[:10000],
        "mode": "text-to-image",
        "aspect_ratio": _aspect_ratio(width, height),
        "output_format": "png",
        "negative_prompt": "blurry, watermark, text overlay, logo, low quality",
    }


def _stability_image_to_image_body(
    *, prompt: str, source_b64: str, width: int, height: int
) -> dict[str, Any]:
    return {
        "prompt": prompt[:10000],
        "mode": "image-to-image",
        "image": source_b64,
        "strength": 0.65,
        "aspect_ratio": _aspect_ratio(width, height),
        "output_format": "png",
    }


def _nova_text_to_image_body(
    *,
    prompt: str,
    width: int,
    height: int,
    quality: str,
    style: str | None = "PHOTOREALISM",
) -> dict[str, Any]:
    text_params: dict[str, Any] = {"text": prompt[:1024]}
    if style:
        text_params["style"] = style
    return {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": text_params,
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "quality": quality,
            "width": width,
            "height": height,
            "cfgScale": 8.0,
        },
    }


def generate_text_to_image(
    *,
    prompt: str,
    size: str = "1024x1024",
    quality: str | None = None,
) -> bytes:
    width, height = _parse_size(size)
    model_id = _resolved_image_model_id()

    if _is_stability_model(model_id):
        body = _stability_text_to_image_body(prompt=prompt, width=width, height=height)
        b64_images = _invoke_raw(body, model_id=model_id)
        return base64.b64decode(b64_images[0])

    q = (quality or settings.bedrock_image_quality or "premium").strip().lower()
    if q not in ("standard", "premium"):
        q = "premium"

    body = _nova_text_to_image_body(prompt=prompt, width=width, height=height, quality=q)
    try:
        b64_images = _invoke_raw(body, model_id=model_id)
    except RuntimeError as first_exc:
        if q == "premium" and _is_nova_canvas_model(model_id):
            fallback = _nova_text_to_image_body(
                prompt=prompt, width=width, height=height, quality="standard", style=None
            )
            try:
                b64_images = _invoke_raw(fallback, model_id=model_id)
            except RuntimeError:
                raise first_exc from None
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
    model_id = _resolved_image_model_id()
    source_b64 = base64.b64encode(source_image_bytes).decode("ascii")

    if _is_stability_model(model_id):
        body = _stability_image_to_image_body(
            prompt=prompt, source_b64=source_b64, width=width, height=height
        )
        b64_images = _invoke_raw(body, model_id=model_id)
        return base64.b64decode(b64_images[0])

    q = (quality or settings.bedrock_image_quality or "premium").strip().lower()
    if q not in ("standard", "premium"):
        q = "premium"

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
    b64_images = _invoke_raw(body, model_id=model_id)
    return base64.b64decode(b64_images[0])
