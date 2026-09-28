"""AI image generation via AWS Bedrock (Nova Canvas)."""

from __future__ import annotations

import logging
from typing import Literal, Optional

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.providers.llm.bedrock_image import (
    generate_image_variation,
    generate_text_to_image,
    image_generation_configured,
)

logger = logging.getLogger(__name__)

ImageGenerationMode = Literal["create", "edit"]


def _build_create_prompt(topic: str, style: Optional[str]) -> str:
    style_line = style or (
        "premium modern brand photography, clean product-marketing aesthetic, "
        "soft cinematic lighting, high contrast, polished color grade"
    )
    return (
        "Professional social media marketing image, scroll-stopping, ad-ready for "
        "Instagram and LinkedIn. Not clip-art, not cartoon unless the brief requires it. "
        f"Subject: {topic}. Visual style: {style_line}. "
        "Strong focal point, square-friendly composition, negative space for optional text. "
        "No watermarks, no logos unless described, no readable text overlays, no garbled letters."
    )


def _build_edit_prompt(topic: str, style: Optional[str]) -> str:
    return (
        "Refine this social marketing image with a premium commercial finish. "
        f"Change: {topic}. "
        f"Style: {style or 'modern, minimal, professional, high-end lighting'}. "
        "Keep composition brand-safe. No watermarks, no garbled text."
    )


def _download_source_image(url: str) -> bytes:
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url.strip())
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not download source image for editing",
        ) from exc


def generate_post_image(
    *,
    topic: str,
    style: Optional[str] = None,
    size: str = "1024x1024",
    mode: ImageGenerationMode = "create",
    source_image_bytes: Optional[bytes] = None,
) -> dict[str, str | bytes]:
    if not image_generation_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "AI image generation is not configured. Set BEDROCK_IMAGE_MODEL_ID and "
                "Bedrock credentials, or upload an image instead."
            ),
        )

    if mode == "edit" and not source_image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source image is required to refine an existing image",
        )

    model_id = settings.bedrock_image_model_id or "amazon.nova-canvas-v1:0"
    quality = settings.bedrock_image_quality or "premium"

    try:
        if mode == "edit":
            prompt = _build_edit_prompt(topic, style)
            image_bytes = generate_image_variation(
                prompt=prompt,
                source_image_bytes=source_image_bytes,
                size=size,
                quality=quality,
            )
        else:
            prompt = _build_create_prompt(topic, style)
            image_bytes = generate_text_to_image(
                prompt=prompt,
                size=size,
                quality=quality,
            )
        return {"imageB64": image_bytes, "source": "ai_generated"}

    except HTTPException:
        raise
    except RuntimeError as exc:
        logger.warning("Bedrock image generation failed model=%s: %s", model_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.warning("Image generation failed: %s", exc)
        action = "refinement" if mode == "edit" else "generation"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Image {action} failed (model={model_id}). Enable the model in Bedrock "
                f"({settings.resolved_bedrock_region}) or upload an image instead."
            ),
        ) from exc


def generate_post_image_from_url(
    *,
    topic: str,
    style: Optional[str] = None,
    size: str = "1024x1024",
    mode: ImageGenerationMode = "create",
    source_image_url: Optional[str] = None,
) -> dict[str, str | bytes]:
    source_bytes: Optional[bytes] = None
    if mode == "edit" and source_image_url:
        source_bytes = _download_source_image(source_image_url)
    return generate_post_image(
        topic=topic,
        style=style,
        size=size,
        mode=mode,
        source_image_bytes=source_bytes,
    )
