"""AI image generation for social posts using Azure OpenAI gpt-image-2.

Note: dall-e-3 was retired on March 4, 2026 and is no longer functional.
gpt-image-2 is the GA replacement — it returns base64-encoded image data
(b64_json) rather than URLs, so we decode and upload to Azure Blob directly.
API requires api-version=preview (handled by get_image_client()).
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Literal, Optional

import httpx
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

ImageGenerationMode = Literal["create", "edit"]


def _build_create_prompt(topic: str, style: Optional[str]) -> str:
    return (
        f"Create a clean, professional social media image about: {topic}. "
        f"Style: {style or 'modern, minimal, brand-safe, no text overlays'}."
    )


def _build_edit_prompt(topic: str, style: Optional[str]) -> str:
    return (
        f"Edit this social media image with the following change: {topic}. "
        f"Keep the overall composition and brand-safe look. "
        f"Style notes: {style or 'modern, minimal, professional'}."
    )


def _decode_image_result(result) -> dict[str, str | bytes]:
    if not result.data:
        raise RuntimeError("No image data returned from gpt-image-2")

    item = result.data[0]
    if item.b64_json:
        return {"imageB64": base64.b64decode(item.b64_json), "source": "ai_generated"}
    if item.url:
        return {"imageUrl": item.url, "source": "ai_generated"}
    raise RuntimeError("gpt-image-2 returned neither b64_json nor url")


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
    """Generate or edit an image for a social post via Azure OpenAI gpt-image-2."""
    if not settings.azure_openai_api_key or not settings.azure_openai_endpoint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Image generation is not configured. Set AZURE_OPENAI_API_KEY and "
                "AZURE_OPENAI_ENDPOINT, or upload an image instead."
            ),
        )

    if mode == "edit" and not source_image_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source image is required to refine an existing image",
        )

    try:
        from app.providers.llm.factory import get_image_client
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Image generation is not available",
        ) from exc

    try:
        client = get_image_client()

        if mode == "edit":
            prompt = _build_edit_prompt(topic, style)
            image_file = io.BytesIO(source_image_bytes)
            image_file.name = "source.png"
            result = client.images.edit(
                model=settings.azure_openai_image_deployment,
                image=image_file,
                prompt=prompt,
                size=size,
                n=1,
                response_format="b64_json",
            )
        else:
            prompt = _build_create_prompt(topic, style)
            result = client.images.generate(
                model=settings.azure_openai_image_deployment,
                prompt=prompt,
                size=size,
                quality="medium",
                n=1,
                response_format="b64_json",
            )

        return _decode_image_result(result)

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Image generation failed: %s", exc)
        action = "refinement" if mode == "edit" else "generation"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Image {action} failed. Ensure your Azure OpenAI resource has a "
                "gpt-image-2 deployment and the api-version is set to 'preview'. "
                "You can also upload an image instead."
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
