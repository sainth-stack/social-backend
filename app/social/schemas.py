"""Pydantic request/response schemas for Social Media (Sprint 1)."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import Query
from pydantic import BaseModel, Field

from app.social.models import (
    EmojiUsage,
    SentenceLength,
    SocialAccountType,
    SocialApprovalStatus,
    SocialImageSource,
    SocialPlatform,
    SocialPlatformPostStatus,
    SocialPostStatus,
)


# ── Accounts ──────────────────────────────────────────────────────────────────


class SocialAccountOut(BaseModel):
    id: str
    workspaceId: str
    platform: SocialPlatform
    accountType: SocialAccountType
    platformAccountId: str
    accountName: str
    accountPictureUrl: Optional[str] = None
    followerCount: int
    tokenExpiresAt: Optional[str] = None
    tokenStatus: Literal["active", "expires_soon", "expired", "disconnected"]
    isDefault: bool
    isActive: bool
    lastSyncedAt: Optional[str] = None
    createdAt: str
    updatedAt: str


class SocialAccountListResponse(BaseModel):
    items: List[SocialAccountOut]


class CreateSocialAccountRequest(BaseModel):
    platform: SocialPlatform
    accountType: SocialAccountType = SocialAccountType.PAGE
    platformAccountId: str = Field(min_length=1, max_length=128)
    accountName: str = Field(min_length=1, max_length=255)
    accountPictureUrl: Optional[str] = None
    followerCount: int = 0
    accessToken: str = Field(min_length=1)
    refreshToken: Optional[str] = None
    tokenExpiresAt: Optional[str] = None
    isDefault: bool = False


class UpdateSocialAccountRequest(BaseModel):
    accountName: Optional[str] = Field(default=None, min_length=1, max_length=255)
    isDefault: Optional[bool] = None
    isActive: Optional[bool] = None


class OAuthUrlResponse(BaseModel):
    url: str


class OAuthCallbackRequest(BaseModel):
    code: str = Field(min_length=1)
    state: str = Field(min_length=1)


# ── Posts ─────────────────────────────────────────────────────────────────────


class SocialPostPlatformIn(BaseModel):
    platform: SocialPlatform
    socialAccountId: Optional[str] = None
    caption: str = ""
    hashtags: List[str] = Field(default_factory=list)
    firstComment: Optional[str] = None


class SocialPostPlatformOut(BaseModel):
    id: str
    platform: SocialPlatform
    socialAccountId: Optional[str] = None
    caption: str
    hashtags: List[Any] = Field(default_factory=list)
    firstComment: Optional[str] = None
    characterCount: int
    status: SocialPlatformPostStatus
    platformPostId: Optional[str] = None
    publishedAt: Optional[str] = None
    errorCode: Optional[str] = None
    errorMessage: Optional[str] = None
    retryCount: int
    nextRetryAt: Optional[str] = None
    reach: int = 0
    impressions: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    clicks: int = 0
    engagementRate: float = 0.0


class SocialPostOut(BaseModel):
    id: str
    workspaceId: str
    createdBy: str
    title: str
    status: SocialPostStatus
    scheduledAt: Optional[str] = None
    publishedAt: Optional[str] = None
    approvalStatus: SocialApprovalStatus
    approvedBy: Optional[str] = None
    templateId: Optional[str] = None
    aiPrompt: Optional[str] = None
    imageUrl: Optional[str] = None
    imageSource: SocialImageSource
    platforms: List[SocialPostPlatformOut] = Field(default_factory=list)
    createdAt: str
    updatedAt: str


class SocialPostListResponse(BaseModel):
    items: List[SocialPostOut]
    total: int
    page: int
    pageSize: int
    totalPages: int


class CreateSocialPostRequest(BaseModel):
    title: str = Field(default="", max_length=255)
    status: SocialPostStatus = SocialPostStatus.DRAFT
    scheduledAt: Optional[str] = None
    imageUrl: Optional[str] = None
    imageSource: SocialImageSource = SocialImageSource.NONE
    aiPrompt: Optional[str] = None
    templateId: Optional[str] = None
    platforms: List[SocialPostPlatformIn] = Field(default_factory=list)


class UpdateSocialPostRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=255)
    status: Optional[SocialPostStatus] = None
    scheduledAt: Optional[str] = None
    imageUrl: Optional[str] = None
    imageSource: Optional[SocialImageSource] = None
    aiPrompt: Optional[str] = None
    templateId: Optional[str] = None
    platforms: Optional[List[SocialPostPlatformIn]] = None


class SocialPostListParams:
    def __init__(
        self,
        status: Optional[SocialPostStatus] = Query(default=None),
        search: Optional[str] = Query(default=None, max_length=200),
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=20, ge=1, le=100, alias="pageSize"),
    ) -> None:
        self.status = status
        self.search = search
        self.page = page
        self.pageSize = pageSize


class MediaAssetListParams:
    def __init__(
        self,
        mediaType: Optional[str] = Query(default=None, alias="mediaType"),
        source: Optional[str] = Query(default=None),
        search: Optional[str] = Query(default=None, max_length=200),
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=24, ge=1, le=100, alias="pageSize"),
    ) -> None:
        self.mediaType = mediaType
        self.source = source
        self.search = search
        self.page = page
        self.pageSize = pageSize


# ── Brand voice & AI generation ───────────────────────────────────────────────


class BrandVoiceOut(BaseModel):
    id: Optional[str] = None
    workspaceId: str
    brandName: str = ""
    industry: str = ""
    tagline: str = ""
    targetAudience: str = ""
    tones: List[str] = Field(default_factory=list)
    wordsToUse: List[str] = Field(default_factory=list)
    wordsToAvoid: List[str] = Field(default_factory=list)
    ctaPhrases: List[str] = Field(default_factory=list)
    sentenceLength: SentenceLength = SentenceLength.MEDIUM
    emojiUsage: EmojiUsage = EmojiUsage.SOMETIMES
    primaryLanguage: str = "en"
    systemPromptOverride: Optional[str] = None
    updatedAt: Optional[str] = None


class BrandVoiceUpdateRequest(BaseModel):
    brandName: str = Field(default="", max_length=160)
    industry: str = Field(default="", max_length=160)
    tagline: str = Field(default="", max_length=255)
    targetAudience: str = ""
    tones: List[str] = Field(default_factory=list)
    wordsToUse: List[str] = Field(default_factory=list)
    wordsToAvoid: List[str] = Field(default_factory=list)
    ctaPhrases: List[str] = Field(default_factory=list)
    sentenceLength: SentenceLength = SentenceLength.MEDIUM
    emojiUsage: EmojiUsage = EmojiUsage.SOMETIMES
    primaryLanguage: str = Field(default="en", max_length=16)
    systemPromptOverride: Optional[str] = None


class GeneratePostRequest(BaseModel):
    topic: str = Field(min_length=5, max_length=2000)
    tone: str = Field(default="Professional", max_length=64)
    platforms: List[SocialPlatform] = Field(min_length=1)
    audience: Optional[str] = None
    cta: Optional[str] = None
    includeHashtags: bool = True
    includeComment: bool = False
    format: Optional[str] = Field(default="single", max_length=32)


class GeneratedPlatformContent(BaseModel):
    caption: str
    hashtags: List[str] = Field(default_factory=list)
    firstComment: str = ""
    characterCount: int = 0


class GeneratedSlide(BaseModel):
    headline: str
    body: str
    imagePrompt: str = ""
    imageUrl: Optional[str] = None


class GeneratedTweet(BaseModel):
    text: str
    characterCount: int = 0


class GeneratePostResponse(BaseModel):
    format: str = "single"
    platforms: Dict[str, GeneratedPlatformContent] = Field(default_factory=dict)
    prompt: str
    # Carousel
    slides: Optional[List[GeneratedSlide]] = None
    # Thread
    tweets: Optional[List[GeneratedTweet]] = None
    # Poll
    pollQuestion: Optional[str] = None
    pollOptions: Optional[List[str]] = None
    # Shared for non-single formats
    caption: Optional[str] = None
    hashtags: Optional[List[str]] = None


class GenerateImageRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    style: Optional[str] = None
    size: Optional[str] = Field(default="1024x1024", pattern=r"^\d+x\d+$")
    mode: str = Field(default="create", pattern=r"^(create|edit)$")
    sourceImageUrl: Optional[str] = None


class GenerateImageResponse(BaseModel):
    imageUrl: str
    source: str
    assetId: Optional[str] = None


class GenerateVideoRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=1000)
    size: str = Field(default="1280x720", pattern=r"^\d+x\d+$")
    seconds: str = Field(default="4", pattern=r"^(4|8|12)$")
    mode: str = Field(default="create", pattern=r"^(create|remix)$")
    remixVideoId: Optional[str] = None


class GenerateVideoResponse(BaseModel):
    videoUrl: str
    source: str
    soraVideoId: Optional[str] = None
    assetId: Optional[str] = None


class UploadVideoResponse(BaseModel):
    videoUrl: str
    source: str
    assetId: Optional[str] = None


class MediaAssetOut(BaseModel):
    id: str
    mediaType: str
    source: str
    url: str
    mimeType: str
    fileSizeBytes: int
    prompt: Optional[str] = None
    soraVideoId: Optional[str] = None
    durationSeconds: Optional[int] = None
    postId: Optional[str] = None
    createdAt: str


class MediaAssetListResponse(BaseModel):
    items: List[MediaAssetOut]
    total: int
    page: int
    pageSize: int
    totalPages: int


class ApplyTemplateRequest(BaseModel):
    values: Dict[str, str] = Field(default_factory=dict)


class ApplyTemplateResponse(BaseModel):
    templateId: str
    name: str
    category: str
    goal: str
    platforms: List[SocialPlatform]
    topic: str
    captionTemplate: str
    hashtags: List[str] = Field(default_factory=list)
    firstComment: str = ""
    suggestedTone: str = "Professional"
    suggestedCta: str = ""
    generateImage: bool = False
    imagePrompt: str = ""
    placeholderValues: Dict[str, str] = Field(default_factory=dict)


class BrandVoiceTestResponse(BaseModel):
    platform: str
    caption: str
    hashtags: List[str] = Field(default_factory=list)
    firstComment: str = ""


class SchedulePostRequest(BaseModel):
    scheduledAt: str = Field(min_length=1)


class CalendarPostOut(BaseModel):
    id: str
    title: str
    status: SocialPostStatus
    scheduledAt: Optional[str] = None
    publishedAt: Optional[str] = None
    platforms: List[SocialPlatform] = Field(default_factory=list)
    captionPreview: str = ""
    imageUrl: Optional[str] = None


class CalendarResponse(BaseModel):
    month: str
    items: List[CalendarPostOut]


class BulkRetryRequest(BaseModel):
    postIds: List[str] = Field(min_length=1)


class RetryResponse(BaseModel):
    post: SocialPostOut
    retriedPlatforms: int = 0
    skippedPlatforms: int = 0


class BulkRetryResponse(BaseModel):
    items: List[RetryResponse]
    totalRetried: int = 0


# ── Analytics ─────────────────────────────────────────────────────────────────


class AnalyticsMetricsOut(BaseModel):
    totalPosts: int = 0
    totalReach: int = 0
    totalImpressions: int = 0
    totalEngagements: int = 0
    avgEngagementRate: float = 0.0
    followerGrowth: int = 0
    totalClicks: int = 0


class PlatformComparisonRow(BaseModel):
    platform: str
    posts: int = 0
    reach: int = 0
    impressions: int = 0
    engagementRate: float = 0.0
    topPost: Optional[str] = None


class AnalyticsOverviewOut(BaseModel):
    fromDate: str
    toDate: str
    metrics: AnalyticsMetricsOut
    engagementSeries: List[Dict[str, Any]] = Field(default_factory=list)
    reachByPlatform: List[Dict[str, Any]] = Field(default_factory=list)
    platformComparison: List[PlatformComparisonRow] = Field(default_factory=list)


class PlatformAnalyticsOut(BaseModel):
    platform: str
    fromDate: str
    toDate: str
    metrics: Dict[str, Any]
    series: List[Dict[str, Any]] = Field(default_factory=list)
    postTypes: List[Dict[str, Any]] = Field(default_factory=list)


class PostPerformanceItemOut(BaseModel):
    postId: str
    platformRowId: str
    caption: str
    platform: str
    publishedAt: Optional[str] = None
    reach: int = 0
    impressions: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    clicks: int = 0
    engagementRate: float = 0.0
    engagements: int = 0
    imageUrl: Optional[str] = None


class PostPerformanceOut(BaseModel):
    fromDate: str
    toDate: str
    items: List[PostPerformanceItemOut] = Field(default_factory=list)


class AudienceGrowthOut(BaseModel):
    fromDate: str
    toDate: str
    series: List[Dict[str, Any]] = Field(default_factory=list)
    netNewFollowers: List[Dict[str, Any]] = Field(default_factory=list)
    platformCards: List[Dict[str, Any]] = Field(default_factory=list)
