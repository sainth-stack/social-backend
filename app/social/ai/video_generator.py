"""AI video generation — currently disabled (VIDEO_GENERATION_ENABLED=false).

To enable, set VIDEO_GENERATION_ENABLED=true and configure a video generation provider.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import HTTPException, status

from app.core.config import settings

VideoSize = Literal["1280x720", "720x1280"]
VideoSeconds = Literal["4", "8", "12"]


class VideoGenerationUnavailableError(Exception):
    """Raised when video generation is not configured."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _check_enabled() -> None:
    if not settings.video_generation_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Video generation is not enabled. "
                "Set VIDEO_GENERATION_ENABLED=true and configure a video generation provider."
            ),
        )


def generate_post_video(
    *,
    prompt: str,
    size: VideoSize = "1280x720",
    seconds: VideoSeconds = "4",
    reference_image_bytes: Optional[bytes] = None,
    reference_content_type: Optional[str] = None,
) -> dict:
    """Generate a video for a social post."""
    _check_enabled()
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Video generation provider not configured.",
    )


def remix_post_video(*, remix_video_id: str, prompt: str) -> dict:
    """Remix/refine an existing video."""
    _check_enabled()
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Video generation provider not configured.",
    )
