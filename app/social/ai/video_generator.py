"""AI video generation for social posts using Azure OpenAI Sora 2.

Sora 2 is in public preview on Azure AI Foundry (gated). Supports:
  - Text-to-video
  - Image-to-video via ``input_reference`` (logo, brand asset, first frame)
  - Async job polling and download
  - Graceful degradation when access is not provisioned

API (preview):
  POST {endpoint}/openai/v1/videos?api-version=preview
  GET  {endpoint}/openai/v1/videos/{id}?api-version=preview
  GET  {endpoint}/openai/v1/videos/{id}/content?api-version=preview

Reference image rules (Sora 2):
  - MIME: image/jpeg, image/png, image/webp
  - Resolution must exactly match video size (1280x720 or 720x1280)
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import Literal, Optional

import httpx
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

VideoSize = Literal["1280x720", "720x1280"]
VideoSeconds = Literal["4", "8", "12"]

_POLL_INTERVAL = 5
_POLL_TIMEOUT = 600
_MAX_PROMPT_CHARS = 1000
_ALLOWED_REF_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


class VideoGenerationUnavailableError(Exception):
    """Raised when Sora 2 is not accessible on this Azure subscription/region."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _video_endpoint() -> str:
    endpoint = (settings.azure_openai_video_endpoint or settings.azure_openai_endpoint or "").rstrip("/")
    if not endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_VIDEO_ENDPOINT) is not configured."
        )
    # Foundry "project endpoint" (services.ai.azure.com/.../projects/...) is NOT the Sora API base.
    if ".services.ai.azure.com" in endpoint:
        match = re.match(r"https://([^/]+)\.services\.ai\.azure\.com", endpoint)
        if match:
            normalized = f"https://{match.group(1)}.openai.azure.com"
            logger.warning(
                "AZURE_OPENAI_VIDEO_ENDPOINT is a Foundry project URL; using OpenAI endpoint %s",
                normalized,
            )
            return normalized.rstrip("/")
    return endpoint


def _video_api_key() -> str:
    return settings.azure_openai_video_api_key or settings.azure_openai_api_key or ""


def _api_version() -> str:
    return settings.azure_openai_video_api_version


def _auth_headers(*, json_request: bool = True) -> dict[str, str]:
    headers = {"api-key": _video_api_key()}
    if json_request:
        headers["Content-Type"] = "application/json"
    return headers


def _parse_size(size: VideoSize) -> tuple[int, int]:
    w, h = size.split("x")
    return int(w), int(h)


def prepare_reference_image(
    data: bytes,
    content_type: str,
    target_size: VideoSize,
) -> tuple[bytes, str]:
    """Resize a reference image to exactly match the target video resolution."""
    mime = (content_type or "image/jpeg").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reference must be an image file (JPEG, PNG, or WebP)",
        )

    target_w, target_h = _parse_size(target_size)

    try:
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Image processing is not available on the server (Pillow missing)",
        ) from exc

    try:
        with Image.open(io.BytesIO(data)) as img:
            # Cover-crop to target aspect, then resize exactly (Sora requires exact match)
            src_w, src_h = img.size
            target_ratio = target_w / target_h
            src_ratio = src_w / src_h

            if src_ratio > target_ratio:
                new_w = int(src_h * target_ratio)
                left = (src_w - new_w) // 2
                img = img.crop((left, 0, left + new_w, src_h))
            elif src_ratio < target_ratio:
                new_h = int(src_w / target_ratio)
                top = (src_h - new_h) // 2
                img = img.crop((0, top, src_w, top + new_h))

            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if mime == "image/png" else "RGB")
            elif img.mode == "RGBA" and mime != "image/png":
                img = img.convert("RGB")

            img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            out = io.BytesIO()
            if mime == "image/png":
                img.save(out, format="PNG")
                return out.getvalue(), "image/png"
            if mime == "image/webp":
                img.save(out, format="WEBP", quality=92)
                return out.getvalue(), "image/webp"
            if img.mode == "RGBA":
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=92)
            return out.getvalue(), "image/jpeg"
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Failed to prepare reference image: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not process reference image. Use JPEG, PNG, or WebP.",
        ) from exc


def _sora_error_detail(resp: httpx.Response) -> str:
    """Extract a human-readable message from a Sora API error response."""
    try:
        body = resp.json()
        err = body.get("error") or {}
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if body.get("message"):
            return str(body["message"])
    except Exception:
        pass
    return resp.text[:300] if resp.text else f"HTTP {resp.status_code}"


def _raise_for_sora_status(resp: httpx.Response) -> None:
    """Map Sora HTTP errors to actionable exceptions."""
    if resp.status_code == 401:
        raise VideoGenerationUnavailableError(
            "Sora 2 authentication failed (401). Use the Azure OpenAI endpoint "
            "(https://<resource>.openai.azure.com), not the Foundry project URL. "
            "If Sora is on a different resource than your chat model, set "
            "AZURE_OPENAI_VIDEO_ENDPOINT and AZURE_OPENAI_VIDEO_API_KEY from that "
            "resource's Keys and Endpoint page in Foundry."
        )
    if resp.status_code == 403:
        raise VideoGenerationUnavailableError(
            "Access to Sora 2 video generation is not enabled for your Azure subscription "
            "or region. Apply for access at https://aka.ms/oai/sora2access, or upload a "
            "video manually."
        )
    if resp.status_code == 404:
        raise VideoGenerationUnavailableError(
            "Sora 2 deployment not found. Ensure you have deployed 'sora-2' in your Azure "
            "AI Foundry resource and set AZURE_OPENAI_VIDEO_DEPLOYMENT correctly."
        )
    if resp.status_code == 429:
        raise VideoGenerationUnavailableError(
            "Sora 2 rate limit reached. Please wait a moment and try again."
        )
    if resp.status_code == 400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sora 2 rejected the request: {_sora_error_detail(resp)}",
        )
    resp.raise_for_status()


def _validate_video_request(prompt: str, seconds: VideoSeconds) -> None:
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Video prompt must be at most {_MAX_PROMPT_CHARS} characters (got {len(prompt)}).",
        )
    if seconds not in ("4", "8", "12"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video duration must be 4, 8, or 12 seconds on Azure Sora 2.",
        )


def _submit_job(
    client: httpx.Client,
    prompt: str,
    size: VideoSize,
    seconds: VideoSeconds,
    reference_image: Optional[tuple[bytes, str]] = None,
) -> str:
    """Submit a Sora 2 video job; return video job ID."""
    url = f"{_video_endpoint()}/openai/v1/videos"
    params = {"api-version": _api_version()}
    model = settings.azure_openai_video_deployment

    if reference_image:
        img_bytes, img_mime = reference_image
        ext = "jpg" if "jpeg" in img_mime else "png" if "png" in img_mime else "webp"
        data = {
            "prompt": prompt,
            "model": model,
            "size": size,
            "seconds": seconds,
        }
        files = {"input_reference": (f"reference.{ext}", img_bytes, img_mime)}
        resp = client.post(
            url,
            params=params,
            data=data,
            files=files,
            headers=_auth_headers(json_request=False),
        )
    else:
        payload = {
            "prompt": prompt,
            "model": model,
            "size": size,
            "seconds": seconds,
        }
        resp = client.post(url, json=payload, params=params, headers=_auth_headers())

    _raise_for_sora_status(resp)
    data = resp.json()
    job_id: str = data.get("id") or data.get("job_id") or ""
    if not job_id:
        raise RuntimeError(f"Sora 2 job submission returned no job ID: {data}")
    return job_id


def _remix_job(client: httpx.Client, video_id: str, prompt: str) -> str:
    """Remix an existing Sora 2 video with a targeted edit prompt; return new job ID."""
    url = f"{_video_endpoint()}/openai/v1/videos/{video_id}/remix"
    params = {"api-version": _api_version()}
    payload = {"prompt": prompt}
    resp = client.post(url, json=payload, params=params, headers=_auth_headers())

    if resp.status_code == 404:
        raise VideoGenerationUnavailableError(
            "Source video not found or expired. Sora jobs are available for 24 hours — "
            "generate a new video instead."
        )
    _raise_for_sora_status(resp)
    data = resp.json()
    job_id: str = data.get("id") or data.get("job_id") or ""
    if not job_id:
        raise RuntimeError(f"Sora 2 remix returned no job ID: {data}")
    return job_id


def _run_job_to_bytes(client: httpx.Client, job_id: str) -> tuple[bytes, str, str]:
    """Poll a job until complete and download video bytes. Returns (bytes, content_type, job_id)."""
    _poll_job(client, job_id)
    video_bytes, content_type = _download_video_content(client, job_id)
    return video_bytes, content_type, job_id


def _poll_job(client: httpx.Client, job_id: str) -> None:
    """Poll until the Sora 2 job completes."""
    url = f"{_video_endpoint()}/openai/v1/videos/{job_id}"
    deadline = time.monotonic() + _POLL_TIMEOUT

    while time.monotonic() < deadline:
        resp = client.get(url, params={"api-version": _api_version()}, headers=_auth_headers())
        resp.raise_for_status()
        data = resp.json()
        job_status: str = (data.get("status") or "").lower()

        if job_status in ("succeeded", "completed"):
            return

        if job_status in ("failed", "canceled", "cancelled"):
            error_msg = data.get("error", {}).get("message", "Unknown error")
            raise RuntimeError(f"Sora 2 job {job_status}: {error_msg}")

        logger.debug("Sora 2 job %s status=%s, polling again in %ss", job_id, job_status, _POLL_INTERVAL)
        time.sleep(_POLL_INTERVAL)

    raise TimeoutError(f"Sora 2 video generation timed out after {_POLL_TIMEOUT}s (job_id={job_id})")


def _download_video_content(client: httpx.Client, job_id: str) -> tuple[bytes, str]:
    """Download completed video bytes from the Sora 2 content endpoint."""
    url = f"{_video_endpoint()}/openai/v1/videos/{job_id}/content"
    resp = client.get(
        url,
        params={"api-version": _api_version()},
        headers=_auth_headers(json_request=False),
        follow_redirects=True,
        timeout=120.0,
    )
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "video/mp4").split(";")[0].strip()
    return resp.content, content_type


def generate_post_video(
    *,
    prompt: str,
    size: VideoSize = "1280x720",
    seconds: VideoSeconds = "4",
    reference_image_bytes: Optional[bytes] = None,
    reference_content_type: Optional[str] = None,
) -> dict:
    """Generate a social post video using Azure OpenAI Sora 2."""
    if not settings.video_generation_enabled:
        raise VideoGenerationUnavailableError(
            "Video generation is currently disabled. Set VIDEO_GENERATION_ENABLED=true to enable."
        )

    if not settings.azure_openai_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AZURE_OPENAI_API_KEY is not configured.",
        )

    if not (settings.azure_openai_video_endpoint or settings.azure_openai_endpoint):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_VIDEO_ENDPOINT) is not configured.",
        )

    _validate_video_request(prompt, seconds)

    reference: Optional[tuple[bytes, str]] = None
    if reference_image_bytes:
        reference = prepare_reference_image(reference_image_bytes, reference_content_type or "image/jpeg", size)

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0),
        ) as client:
            logger.info(
                "Submitting Sora 2 job: prompt_len=%d size=%s seconds=%s has_reference=%s",
                len(prompt),
                size,
                seconds,
                bool(reference),
            )
            job_id = _submit_job(client, prompt, size, seconds, reference_image=reference)
            logger.info("Sora 2 job submitted: job_id=%s", job_id)

            video_bytes, content_type, job_id = _run_job_to_bytes(client, job_id)
            logger.info("Downloaded video: %d bytes content_type=%s job_id=%s", len(video_bytes), content_type, job_id)

    except VideoGenerationUnavailableError:
        raise
    except HTTPException:
        raise
    except TimeoutError as exc:
        logger.warning("Sora 2 timed out: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Video generation timed out. Sora 2 videos can take several minutes — please try again.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.warning("Sora 2 HTTP error: %s %s", exc.response.status_code, exc)
        detail = _sora_error_detail(exc.response)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Video generation request failed: {detail}",
        ) from exc
    except Exception as exc:
        logger.exception("Sora 2 video generation error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Video generation failed. Please try again or upload a video manually.",
        ) from exc

    return {
        "videoBytes": video_bytes,
        "contentType": content_type,
        "source": "ai_generated",
        "soraVideoId": job_id,
    }


def remix_post_video(
    *,
    remix_video_id: str,
    prompt: str,
) -> dict:
    """Refine an existing Sora 2 video with a targeted edit prompt."""
    if not settings.video_generation_enabled:
        raise VideoGenerationUnavailableError(
            "Video generation is currently disabled. Set VIDEO_GENERATION_ENABLED=true to enable."
        )

    if not _video_api_key():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AZURE_OPENAI_API_KEY is not configured.",
        )

    if not (settings.azure_openai_video_endpoint or settings.azure_openai_endpoint):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AZURE_OPENAI_ENDPOINT (or AZURE_OPENAI_VIDEO_ENDPOINT) is not configured.",
        )

    if len(prompt) > _MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Video prompt must be at most {_MAX_PROMPT_CHARS} characters (got {len(prompt)}).",
        )

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0),
        ) as client:
            logger.info("Submitting Sora 2 remix: source_id=%s prompt_len=%d", remix_video_id, len(prompt))
            job_id = _remix_job(client, remix_video_id, prompt)
            logger.info("Sora 2 remix job submitted: job_id=%s", job_id)

            video_bytes, content_type, job_id = _run_job_to_bytes(client, job_id)
            logger.info("Downloaded remixed video: %d bytes job_id=%s", len(video_bytes), job_id)

    except VideoGenerationUnavailableError:
        raise
    except HTTPException:
        raise
    except TimeoutError as exc:
        logger.warning("Sora 2 remix timed out: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Video refinement timed out. Please try again.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.warning("Sora 2 remix HTTP error: %s %s", exc.response.status_code, exc)
        detail = _sora_error_detail(exc.response)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Video refinement request failed: {detail}",
        ) from exc
    except Exception as exc:
        logger.exception("Sora 2 remix error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Video refinement failed. Try generating a new video instead.",
        ) from exc

    return {
        "videoBytes": video_bytes,
        "contentType": content_type,
        "source": "ai_generated",
        "soraVideoId": job_id,
    }
