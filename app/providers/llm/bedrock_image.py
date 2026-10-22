from __future__ import annotations

import base64
import json
import logging
import math
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.providers.llm.bedrock_client import get_bedrock_runtime_client
from app.providers.llm.model_selection import SUPPORTED_IMAGE_MODEL_IDS, current_models, image_region

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_MODEL_ID = "stability.sd3-5-large-v1:0"


def image_generation_configured() -> bool:
    model = (current_models().image or settings.bedrock_image_model_id or DEFAULT_IMAGE_MODEL_ID).strip()
    return bool(
        model in SUPPORTED_IMAGE_MODEL_IDS
        and settings.resolved_bedrock_access_key_id
        and settings.resolved_bedrock_secret_access_key
    )


def _resolved_image_model_id() -> str:
    model = (current_models().image or settings.bedrock_image_model_id or DEFAULT_IMAGE_MODEL_ID).strip()
    if "nova-canvas" in model.lower() or "titan-image-generator" in model.lower():
        raise RuntimeError(f"Bedrock image model {model} is retired. Use {DEFAULT_IMAGE_MODEL_ID} or Stable Image Ultra/Core v1:1.")
    if model not in SUPPORTED_IMAGE_MODEL_IDS:
        raise RuntimeError(f"Unsupported Bedrock image model: {model}. Select a supported model from /ai-models.")
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


_STABILITY_RATIOS = ("1:1", "4:5", "5:4", "2:3", "3:2", "9:16", "16:9", "9:21", "21:9")


def _aspect_ratio(width: int, height: int) -> str:
    target = math.log(width / height)
    return min(
        _STABILITY_RATIOS,
        key=lambda r: abs(math.log(int(r.split(":")[0]) / int(r.split(":")[1])) - target),
    )


def _friendly_invoke_error(exc: Exception, *, model: str, region: str) -> RuntimeError:
    msg = str(exc).lower()
    if "end of its life" in msg or "end of life" in msg:
        return RuntimeError(
            f"Bedrock image model {model} is retired. Select an active supported model from /ai-models."
        )
    if "resourcenotfound" in msg or "model not found" in msg:
        hint = (
            f"Enable {model} under Bedrock → Model access in {region}. "
            "Supported Stability image models use US West (Oregon) us-west-2."
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
    region = image_region(model_id)
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
    reasons = [reason for reason in payload.get("finish_reasons", []) if reason]
    if reasons or not images or not all(isinstance(img, str) and img for img in images):
        err = payload.get("error") or payload.get("message") or "; ".join(map(str, reasons)) or "no images returned"
        raise RuntimeError(f"Bedrock image model returned no images: {err}")
    return images


def _stability_text_to_image_body(*, prompt: str, width: int, height: int) -> dict[str, Any]:
    return {
        "prompt": prompt[:10000],
        "mode": "text-to-image",
        "aspect_ratio": _aspect_ratio(width, height),
        "output_format": "png",
        "negative_prompt": (
            "blurry, watermark, text, letters, words, typography, caption, logo, signature, "
            "low quality, distorted, deformed hands, extra fingers, oversaturated"
        ),
    }


def _stability_image_to_image_body(
    *, prompt: str, source_b64: str, model_id: str
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "prompt": prompt[:10000],
        "image": source_b64,
        "strength": 0.65,
        "output_format": "png",
    }
    if model_id == DEFAULT_IMAGE_MODEL_ID:
        body["mode"] = "image-to-image"
    return body


def generate_text_to_image(
    *,
    prompt: str,
    size: str = "1024x1024",
    quality: str | None = None,
) -> bytes:
    width, height = _parse_size(size)
    model_id = _resolved_image_model_id()
    body = _stability_text_to_image_body(prompt=prompt, width=width, height=height)
    if model_id == "stability.stable-image-core-v1:1":
        body.pop("mode")
    b64_images = _invoke_raw(body, model_id=model_id)
    return base64.b64decode(b64_images[0])


def generate_image_variation(
    *,
    prompt: str,
    source_image_bytes: bytes,
    size: str = "1024x1024",
    quality: str | None = None,
) -> bytes:
    model_id = _resolved_image_model_id()
    if model_id == "stability.stable-image-core-v1:1":
        raise RuntimeError("Stable Image Core supports text-to-image only. Use Stable Diffusion 3.5 Large or Stable Image Ultra for image variations.")
    source_b64 = base64.b64encode(source_image_bytes).decode("ascii")
    body = _stability_image_to_image_body(prompt=prompt, source_b64=source_b64, model_id=model_id)
    b64_images = _invoke_raw(body, model_id=model_id)
    return base64.b64decode(b64_images[0])
