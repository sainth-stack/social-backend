"""Social Media API — accounts, OAuth, and posts CRUD."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.workspaces.models import Workspace
from app.users.models import User
from app.workspaces.deps import require_workspace_access
from app.social.dependencies import get_social_account_or_404, get_social_post_or_404
from app.social.models import SocialAccount, SocialPlatform, SocialPost
from app.social.schemas import (
    AnalyticsOverviewOut,
    ApplyTemplateRequest,
    ApplyTemplateResponse,
    AudienceGrowthOut,
    BrandVoiceOut,
    BrandVoiceTestResponse,
    BrandVoiceUpdateRequest,
    BulkRetryRequest,
    BulkRetryResponse,
    CalendarResponse,
    ContentPlanGenerateRequest,
    ContentPlanGenerateResponse,
    ContentPlanJobStartResponse,
    ContentPlanJobStatusResponse,
    ContentPlanJobProgress,
    CreateSocialAccountRequest,
    CreateSocialPostRequest,
    GenerateImageRequest,
    GenerateImageResponse,
    GeneratePostRequest,
    GeneratePostResponse,
    GenerateVideoRequest,
    GenerateVideoResponse,
    MediaAssetListParams,
    MediaAssetListResponse,
    MediaAssetOut,
    OAuthCallbackRequest,
    OAuthUrlResponse,
    PlatformAnalyticsOut,
    PostPerformanceOut,
    RetryResponse,
    RegeneratePostContentRequest,
    SchedulePostRequest,
    SocialAccountListResponse,
    SocialAccountOut,
    SocialPostListParams,
    SocialPostListResponse,
    SocialPostOut,
    UpdateSocialAccountRequest,
    UpdateSocialPostRequest,
    UploadVideoResponse,
)
from app.social.service import SocialMediaService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces/{workspace_id}/social",
    tags=["social-media"],
)

# Public OAuth browser callback (Meta redirects here)
oauth_callback_router = APIRouter(tags=["social-media"])


def _parse_platform(platform: str) -> SocialPlatform:
    try:
        return SocialPlatform(platform.lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform: {platform}",
        ) from exc


# ── Accounts ──────────────────────────────────────────────────────────────────


@router.get("/accounts", response_model=SocialAccountListResponse)
def list_accounts(
    platform: Optional[str] = Query(default=None),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialAccountListResponse:
    platform_enum = _parse_platform(platform) if platform else None
    items = SocialMediaService(db).list_accounts(workspace, platform_enum)
    return SocialAccountListResponse(items=items)


@router.post("/accounts", response_model=SocialAccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: CreateSocialAccountRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialAccountOut:
    return SocialMediaService(db).create_account(workspace, payload)


@router.patch("/accounts/{account_id}", response_model=SocialAccountOut)
def update_account(
    payload: UpdateSocialAccountRequest,
    account: SocialAccount = Depends(get_social_account_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialAccountOut:
    return SocialMediaService(db).update_account(account, payload)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account: SocialAccount = Depends(get_social_account_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    SocialMediaService(db).delete_account(account)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/accounts/{account_id}/sync", response_model=SocialAccountOut)
def sync_account(
    account: SocialAccount = Depends(get_social_account_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialAccountOut:
    return SocialMediaService(db).sync_account(account)


@router.post("/accounts/{account_id}/reconnect", response_model=OAuthUrlResponse)
def reconnect_account(
    account: SocialAccount = Depends(get_social_account_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OAuthUrlResponse:
    url = SocialMediaService(db).get_oauth_url(
        workspace,
        account.platform,
        reconnect_account_id=account.id,
    )
    return OAuthUrlResponse(url=url)


# ── OAuth ─────────────────────────────────────────────────────────────────────


@router.get("/oauth/{platform}/url", response_model=OAuthUrlResponse)
def get_oauth_url(
    platform: str,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> OAuthUrlResponse:
    platform_enum = _parse_platform(platform)
    url = SocialMediaService(db).get_oauth_url(workspace, platform_enum)
    return OAuthUrlResponse(url=url)


@router.post("/oauth/{platform}/callback", response_model=SocialAccountListResponse)
def oauth_callback_api(
    platform: str,
    payload: OAuthCallbackRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialAccountListResponse:
    """Exchange code when the frontend receives it (non-popup fallback)."""
    platform_enum = _parse_platform(platform)
    items = SocialMediaService(db).handle_oauth_callback(
        platform_enum, payload.code, payload.state
    )
    # Ensure accounts belong to the requesting org (state already binds org).
    for item in items:
        if item.workspaceId != str(workspace.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return SocialAccountListResponse(items=items)


def _oauth_popup_html(*, success: bool, message: str = "") -> str:
    status_value = "success" if success else "error"
    safe_message = message.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
    return f"""<!DOCTYPE html>
<html><head><title>Connecting…</title></head>
<body>
<script>
  try {{
    if (window.opener) {{
      window.opener.postMessage({{
        source: 'opsbrain-social-oauth',
        status: '{status_value}',
        message: '{safe_message}'
      }}, '*');
    }}
  }} catch (e) {{}}
  window.close();
</script>
<p>{'Connected successfully. You can close this window.' if success else safe_message or 'Connection failed.'}</p>
</body></html>"""


@oauth_callback_router.get("/api/v1/social/oauth/{platform}/callback")
def oauth_callback_browser(
    platform: str,
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Meta redirects here after user consent; closes popup via postMessage."""
    if error:
        msg = error_description or error
        return HTMLResponse(_oauth_popup_html(success=False, message=msg), status_code=400)

    if not code or not state:
        return HTMLResponse(
            _oauth_popup_html(success=False, message="Missing OAuth code or state"),
            status_code=400,
        )

    try:
        platform_enum = _parse_platform(platform)
        SocialMediaService(db).handle_oauth_callback(platform_enum, code, state)
        return HTMLResponse(_oauth_popup_html(success=True))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "OAuth failed"
        logger.warning("Social OAuth callback failed: %s", detail)
        return HTMLResponse(_oauth_popup_html(success=False, message=detail), status_code=400)
    except Exception as exc:
        logger.exception("Social OAuth callback error: %s", exc)
        return HTMLResponse(
            _oauth_popup_html(success=False, message="OAuth connection failed"),
            status_code=500,
        )


# ── Posts ─────────────────────────────────────────────────────────────────────


@router.get("/posts", response_model=SocialPostListResponse)
def list_posts(
    params: SocialPostListParams = Depends(),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialPostListResponse:
    return SocialMediaService(db).list_posts(workspace, params)


@router.get("/posts/{post_id}", response_model=SocialPostOut)
def get_post(
    post: SocialPost = Depends(get_social_post_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).get_post(post)


@router.post("/posts", response_model=SocialPostOut, status_code=status.HTTP_201_CREATED)
def create_post(
    payload: CreateSocialPostRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).create_post(workspace, current_user, payload)


@router.patch("/posts/{post_id}", response_model=SocialPostOut)
def update_post(
    payload: UpdateSocialPostRequest,
    post: SocialPost = Depends(get_social_post_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).update_post(post, workspace, payload)


@router.post("/posts/{post_id}/regenerate-content", response_model=SocialPostOut)
def regenerate_post_content(
    payload: RegeneratePostContentRequest,
    post: SocialPost = Depends(get_social_post_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).regenerate_post_content(
        post,
        workspace,
        current_user,
        prompt=payload.prompt,
        regenerate_image=payload.regenerateImage,
    )


@router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post: SocialPost = Depends(get_social_post_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    SocialMediaService(db).delete_post(post)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/posts/{post_id}/duplicate", response_model=SocialPostOut, status_code=status.HTTP_201_CREATED)
def duplicate_post(
    post: SocialPost = Depends(get_social_post_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).duplicate_post(post, current_user, workspace)


@router.post("/posts/{post_id}/schedule", response_model=SocialPostOut)
def schedule_post(
    payload: SchedulePostRequest,
    post: SocialPost = Depends(get_social_post_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).schedule_post(post, payload)


@router.post("/posts/{post_id}/publish-now", response_model=SocialPostOut)
def publish_now(
    post: SocialPost = Depends(get_social_post_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).publish_now(post)


@router.post("/posts/{post_id}/cancel-schedule", response_model=SocialPostOut)
def cancel_schedule(
    post: SocialPost = Depends(get_social_post_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).cancel_schedule(post)


@router.post("/posts/{post_id}/archive", response_model=SocialPostOut)
def archive_post(
    post: SocialPost = Depends(get_social_post_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> SocialPostOut:
    return SocialMediaService(db).archive_post(post)


@router.post("/posts/{post_id}/retry", response_model=RetryResponse)
def retry_post(
    post: SocialPost = Depends(get_social_post_or_404),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> RetryResponse:
    return SocialMediaService(db).retry_post(post)


@router.post("/posts/bulk-retry", response_model=BulkRetryResponse)
def bulk_retry(
    payload: BulkRetryRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> BulkRetryResponse:
    return SocialMediaService(db).bulk_retry(workspace, payload.postIds)


@router.get("/calendar", response_model=CalendarResponse)
def calendar(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> CalendarResponse:
    return SocialMediaService(db).calendar(workspace, month)


@router.post(
    "/content-plan/generate",
    response_model=ContentPlanJobStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate_content_plan(
    payload: ContentPlanGenerateRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentPlanJobStartResponse:
    """Enqueue async day-wise content plan generation."""
    from app.social.content_plan import ContentPlanService
    from app.social.tasks.content_plan import generate_content_plan_task

    # Validate accounts and permissions before enqueueing
    ContentPlanService(db).validate_plan_request(workspace, current_user, payload)

    try:
        task = generate_content_plan_task.apply_async(
            args=[str(workspace.id), str(current_user.id), payload.model_dump(mode="json")],
            queue="social_maintenance",
        )
        return ContentPlanJobStartResponse(jobId=task.id)
    except Exception as exc:
        logger.warning("Celery enqueue failed, running content plan synchronously: %s", exc)
        result = ContentPlanService(db).generate(workspace, current_user, payload)
        # Store completed result in Celery backend via a pseudo task id pattern
        sync_id = f"sync-{uuid.uuid4()}"
        from workers.celery_app import celery_app

        celery_app.backend.store_result(
            sync_id,
            result.model_dump(mode="json"),
            state="SUCCESS",
        )
        return ContentPlanJobStartResponse(jobId=sync_id)


@router.get("/content-plan/jobs/{job_id}", response_model=ContentPlanJobStatusResponse)
def get_content_plan_job(
    job_id: str,
    workspace: Workspace = Depends(require_workspace_access),
    _: User = Depends(get_current_user),
) -> ContentPlanJobStatusResponse:
    """Poll async content plan job status and result."""
    from celery.result import AsyncResult

    from workers.celery_app import celery_app

    result = AsyncResult(job_id, app=celery_app)
    state = result.state or "PENDING"

    if state == "PENDING":
        return ContentPlanJobStatusResponse(jobId=job_id, status="pending")
    if state == "PROGRESS":
        meta = result.info if isinstance(result.info, dict) else {}
        return ContentPlanJobStatusResponse(
            jobId=job_id,
            status="running",
            progress=ContentPlanJobProgress(
                current=int(meta.get("current", 0)),
                total=int(meta.get("total", 0)),
                message=str(meta.get("message", "")),
            ),
        )
    if state == "SUCCESS":
        payload = result.result
        if isinstance(payload, dict) and payload.get("error"):
            return ContentPlanJobStatusResponse(
                jobId=job_id,
                status="failed",
                error=str(payload.get("error")),
            )
        return ContentPlanJobStatusResponse(
            jobId=job_id,
            status="completed",
            result=ContentPlanGenerateResponse.model_validate(payload),
        )
    if state == "FAILURE":
        err = result.result
        return ContentPlanJobStatusResponse(
            jobId=job_id,
            status="failed",
            error=str(err) if err else "Content plan job failed",
        )
    return ContentPlanJobStatusResponse(jobId=job_id, status=state.lower())


# ── Analytics ─────────────────────────────────────────────────────────────────


@router.get("/analytics/overview", response_model=AnalyticsOverviewOut)
def analytics_overview(
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> AnalyticsOverviewOut:
    return SocialMediaService(db).analytics_overview(workspace, from_date, to_date)


@router.get("/analytics/platform/{platform}", response_model=PlatformAnalyticsOut)
def analytics_platform(
    platform: str,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> PlatformAnalyticsOut:
    return SocialMediaService(db).analytics_platform(
        workspace, _parse_platform(platform), from_date, to_date
    )


@router.get("/analytics/posts", response_model=PostPerformanceOut)
def analytics_posts(
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    sort: str = Query(default="engagementRate"),
    order: str = Query(default="desc"),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> PostPerformanceOut:
    return SocialMediaService(db).analytics_posts(
        workspace, from_date, to_date, sort=sort, order=order
    )


@router.get("/analytics/audience", response_model=AudienceGrowthOut)
def analytics_audience(
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> AudienceGrowthOut:
    return SocialMediaService(db).analytics_audience(workspace, from_date, to_date)


# ── Brand voice & AI generation ───────────────────────────────────────────────


@router.get("/brand-voice", response_model=BrandVoiceOut)
def get_brand_voice(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> BrandVoiceOut:
    return SocialMediaService(db).get_brand_voice(workspace)


@router.put("/brand-voice", response_model=BrandVoiceOut)
def put_brand_voice(
    payload: BrandVoiceUpdateRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> BrandVoiceOut:
    return SocialMediaService(db).upsert_brand_voice(workspace, payload)


@router.post("/brand-voice/test", response_model=BrandVoiceTestResponse)
def test_brand_voice(
    payload: Optional[BrandVoiceUpdateRequest] = Body(default=None),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> BrandVoiceTestResponse:
    return SocialMediaService(db).test_brand_voice(workspace, payload)


@router.post("/generate", response_model=GeneratePostResponse)
def generate_post(
    payload: GeneratePostRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GeneratePostResponse:
    return SocialMediaService(db).generate_post(workspace, payload, user)


@router.post("/generate-image", response_model=GenerateImageResponse)
def generate_image(
    payload: GenerateImageRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GenerateImageResponse:
    return SocialMediaService(db).generate_image(
        workspace,
        user,
        payload.topic,
        payload.style,
        payload.size,
        mode=payload.mode,  # type: ignore[arg-type]
        source_image_url=payload.sourceImageUrl,
    )


@router.post("/upload-image", response_model=GenerateImageResponse)
async def upload_image(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    file: UploadFile = File(...),
) -> GenerateImageResponse:
    """Upload a social post image to Azure Blob; returns a public HTTPS URL."""
    data = await file.read()
    return SocialMediaService(db).upload_image(
        workspace,
        user,
        data=data,
        content_type=file.content_type or "image/jpeg",
        filename=file.filename,
    )


@router.post("/generate-video", response_model=GenerateVideoResponse)
async def generate_video(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    prompt: str = Form(...),
    size: str = Form("1280x720"),
    seconds: str = Form("4"),
    reference_image: Optional[UploadFile] = File(None),
    reference_image_url: Optional[str] = Form(None),
    mode: str = Form("create"),
    remix_video_id: Optional[str] = Form(None),
) -> GenerateVideoResponse:
    """Generate a social post video using Azure OpenAI Sora 2 (gated preview).

    Optional ``reference_image`` file or ``reference_image_url`` guides generation
    (logo, brand asset, or first frame). Image is resized to match ``size``.

    Returns HTTP 503 with ``{"error": "sora_unavailable", ...}`` when Sora 2
    access is not provisioned on the Azure subscription/region.
    """
    if seconds not in ("4", "8", "12"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Video duration must be 4, 8, or 12 seconds on Azure Sora 2.",
        )
    if len(prompt) > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Video prompt must be at most 1000 characters (got {len(prompt)}).",
        )

    from app.social.ai.video_generator import VideoGenerationUnavailableError

    ref_bytes: Optional[bytes] = None
    ref_mime: Optional[str] = None

    if reference_image and reference_image.filename:
        ref_bytes = await reference_image.read()
        ref_mime = reference_image.content_type or "image/jpeg"
    elif reference_image_url and reference_image_url.strip():
        try:
            import httpx as _httpx

            with _httpx.Client(timeout=60.0, follow_redirects=True) as client:
                resp = client.get(reference_image_url.strip())
                resp.raise_for_status()
                ref_bytes = resp.content
                ref_mime = resp.headers.get("content-type") or "image/jpeg"
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not download reference image URL",
            ) from exc

    try:
        return SocialMediaService(db).generate_video(
            workspace,
            user,
            prompt=prompt,
            size=size,  # type: ignore[arg-type]
            seconds=seconds,  # type: ignore[arg-type]
            reference_image_bytes=ref_bytes,
            reference_content_type=ref_mime,
            mode=mode,  # type: ignore[arg-type]
            remix_video_id=remix_video_id,
        )
    except VideoGenerationUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "sora_unavailable", "message": exc.detail},
        ) from exc


@router.post("/upload-video", response_model=UploadVideoResponse)
async def upload_video(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    file: UploadFile = File(...),
) -> UploadVideoResponse:
    """Upload a social post video to Azure Blob; returns a public HTTPS URL."""
    content_type = file.content_type or "video/mp4"
    if not content_type.startswith("video/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only video files are accepted by this endpoint",
        )
    data = await file.read()
    return SocialMediaService(db).upload_video(
        workspace,
        user,
        data=data,
        content_type=content_type,
        filename=file.filename,
    )


@router.get("/media-assets", response_model=MediaAssetListResponse)
def list_media_assets(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    params: MediaAssetListParams = Depends(),
) -> MediaAssetListResponse:
    return SocialMediaService(db).list_media_assets(workspace, params)


@router.get("/media-assets/{asset_id}", response_model=MediaAssetOut)
def get_media_asset(
    asset_id: uuid.UUID,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> MediaAssetOut:
    return SocialMediaService(db).get_media_asset(workspace, asset_id)


@router.delete("/media-assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_media_asset(
    asset_id: uuid.UUID,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Response:
    SocialMediaService(db).delete_media_asset(workspace, asset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Settings, templates, dashboard, approval ──────────────────────────────────


@router.get("/settings")
def get_settings(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).get_settings(workspace)


@router.put("/settings")
def put_settings(
    payload: dict,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).update_settings(workspace, current_user, payload)


@router.get("/team-permissions")
def list_team_permissions(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).list_team_permissions(workspace)


@router.put("/team-permissions/{user_id}")
def update_team_permission(
    user_id: uuid.UUID,
    payload: dict,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    from app.social.models import SocialPermission
    from app.social.polish import SocialPolishService

    permission = SocialPermission(payload.get("permission", "editor"))
    return SocialPolishService(db).update_team_permission(
        workspace, current_user, user_id, permission
    )


@router.get("/templates")
def list_templates(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[dict]:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).list_templates(workspace, current_user)


@router.post("/templates/{template_id}/apply", response_model=ApplyTemplateResponse)
def apply_template(
    template_id: uuid.UUID,
    payload: ApplyTemplateRequest,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ApplyTemplateResponse:
    from app.social.polish import SocialPolishService

    result = SocialPolishService(db).apply_template(
        workspace,
        current_user,
        template_id,
        payload.model_dump(),
    )
    return ApplyTemplateResponse(**result)


@router.post("/templates", status_code=status.HTTP_201_CREATED)
def create_template(
    payload: dict,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).create_template(workspace, current_user, payload)


@router.put("/templates/{template_id}")
def update_template(
    template_id: uuid.UUID,
    payload: dict,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).update_template(
        workspace, current_user, template_id, payload
    )


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: uuid.UUID,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    from app.social.polish import SocialPolishService

    SocialPolishService(db).delete_template(workspace, current_user, template_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/dashboard/stats")
def dashboard_stats(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).dashboard_stats(workspace)


@router.get("/activity")
def activity_feed(
    limit: int = Query(default=10, ge=1, le=50),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).activity(workspace, limit=limit)


@router.get("/recommendations")
def recommendations(
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    from app.social.polish import SocialPolishService

    return SocialPolishService(db).recommendations(workspace)


@router.post("/posts/{post_id}/submit-approval", response_model=SocialPostOut)
def submit_approval(
    post: SocialPost = Depends(get_social_post_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SocialPostOut:
    from app.social.polish import SocialPolishService

    updated = SocialPolishService(db).submit_approval(workspace, current_user, post)
    return SocialMediaService(db).get_post(updated)


@router.post("/posts/{post_id}/approve", response_model=SocialPostOut)
def approve_post(
    post: SocialPost = Depends(get_social_post_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SocialPostOut:
    from app.social.polish import SocialPolishService

    updated = SocialPolishService(db).approve_post(workspace, current_user, post)
    return SocialMediaService(db).get_post(updated)


@router.post("/posts/{post_id}/reject", response_model=SocialPostOut)
def reject_post(
    payload: Optional[dict] = Body(default=None),
    post: SocialPost = Depends(get_social_post_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SocialPostOut:
    from app.social.polish import SocialPolishService

    reason = (payload or {}).get("reason")
    updated = SocialPolishService(db).reject_post(workspace, current_user, post, reason)
    return SocialMediaService(db).get_post(updated)


@router.post("/posts/{post_id}/request-changes", response_model=SocialPostOut)
def request_changes(
    payload: Optional[dict] = Body(default=None),
    post: SocialPost = Depends(get_social_post_or_404),
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SocialPostOut:
    from app.social.polish import SocialPolishService

    reason = (payload or {}).get("reason")
    updated = SocialPolishService(db).request_changes(
        workspace, current_user, post, reason
    )
    return SocialMediaService(db).get_post(updated)
