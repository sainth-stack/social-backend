"""AI image generation via Azure AI Foundry / OpenAI gpt-image-2.

Uses the same client shape as Foundry samples::

    client = OpenAI(base_url=\"...services.ai.azure.com/openai/v1\", api_key=...)
    img = client.images.generate(model=\"gpt-image-2\", prompt=..., n=1, size=\"1024x1024\")
    image_bytes = base64.b64decode(img.data[0].b64_json)

``get_image_client()`` already selects the Foundry OpenAI client when
``AZURE_OPENAI_ENDPOINT`` is a ``services.ai.azure.com`` URL.
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
    """Elevate any brief into a scroll-stopping, commercial-quality social image."""
    style_line = style or (
        "premium modern brand photography / clean product-marketing aesthetic, "
        "soft cinematic lighting, shallow depth of field when natural, "
        "high contrast, polished color grade"
    )
    return (
        "Create a single, scroll-stopping social media marketing image. "
        "It must look expensive, professional, and ready for Instagram/LinkedIn ads — "
        "not clip-art, not generic stock, not cartoonish unless the brief demands it.\n"
        f"Subject / brief (interpret creatively and elevate): {topic}\n"
        f"Visual style: {style_line}\n"
        "Composition: strong focal point, rule of thirds or bold centered hero, "
        "negative space for optional future text, square-friendly framing.\n"
        "Mood: trustworthy, aspirational, conversion-ready — makes the viewer want the product/service.\n"
        "Strict: no watermarks, no logos unless described, no unreadable fake UI text, "
        "no typography overlays, no garbled letters, no collage clutter."
    )


def _build_edit_prompt(topic: str, style: Optional[str]) -> str:
    return (
        "Edit this social media marketing image with a premium commercial finish.\n"
        f"Requested change: {topic}\n"
        "Keep composition coherent and brand-safe. "
        f"Style notes: {style or 'modern, minimal, professional, high-end lighting'}.\n"
        "No watermarks, no garbled text overlays."
    )


def _decode_image_result(result) -> dict[str, str | bytes]:
    if not result.data:
        raise RuntimeError("No image data returned from gpt-image-2")

    item = result.data[0]
    b64 = getattr(item, "b64_json", None)
    if b64:
        return {"imageB64": base64.b64decode(b64), "source": "ai_generated"}
    url = getattr(item, "url", None)
    if url:
        return {"imageUrl": url, "source": "ai_generated"}
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


def _generate_create(client, *, model: str, prompt: str, size: str):
    """Foundry-compatible create call (matches Azure sample)."""
    # Primary: Foundry / OpenAI v1 style (returns b64_json by default).
    try:
        return client.images.generate(
            model=model,
            prompt=prompt,
            n=1,
            size=size,
        )
    except Exception as first_exc:
        # Fallback for classic Azure OpenAI that still wants response_format.
        try:
            return client.images.generate(
                model=model,
                prompt=prompt,
                n=1,
                size=size,
                response_format="b64_json",
            )
        except Exception:
            raise first_exc from None


def generate_post_image(
    *,
    topic: str,
    style: Optional[str] = None,
    size: str = "1024x1024",
    mode: ImageGenerationMode = "create",
    source_image_bytes: Optional[bytes] = None,
) -> dict[str, str | bytes]:
    """Generate or edit an image for a social post via gpt-image-2."""
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

    model = settings.azure_openai_image_deployment  # gpt-image-2

    try:
        client = get_image_client()

        if mode == "edit":
            prompt = _build_edit_prompt(topic, style)
            image_file = io.BytesIO(source_image_bytes)
            image_file.name = "source.png"
            try:
                result = client.images.edit(
                    model=model,
                    image=image_file,
                    prompt=prompt,
                    size=size,
                    n=1,
                )
            except TypeError:
                # Older SDK keyword set
                image_file.seek(0)
                result = client.images.edit(
                    model=model,
                    image=image_file,
                    prompt=prompt,
                    size=size,
                    n=1,
                    response_format="b64_json",
                )
        else:
            prompt = _build_create_prompt(topic, style)
            result = _generate_create(client, model=model, prompt=prompt, size=size)

        return _decode_image_result(result)

    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Image generation failed: %s", exc)
        action = "refinement" if mode == "edit" else "generation"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Image {action} failed. Ensure AZURE_OPENAI_ENDPOINT points to "
                "your Foundry resource (...services.ai.azure.com/openai/v1) and "
                f"AZURE_OPENAI_IMAGE_DEPLOYMENT={model} exists. "
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
