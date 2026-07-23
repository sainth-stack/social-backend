"""Prompt templates for platform-native social post generation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.social.models import SocialPlatform

PLATFORM_LIMITS: dict[str, int] = {
    SocialPlatform.FACEBOOK.value: 2200,  # practical recommended limit
    SocialPlatform.INSTAGRAM.value: 2200,
    SocialPlatform.LINKEDIN.value: 3000,
    SocialPlatform.X.value: 280,
}

PLATFORM_HASHTAG_LIMITS: dict[str, int] = {
    SocialPlatform.FACEBOOK.value: 5,
    SocialPlatform.INSTAGRAM.value: 30,
    SocialPlatform.LINKEDIN.value: 5,
    SocialPlatform.X.value: 2,
}

PLATFORM_LABELS: dict[str, str] = {
    SocialPlatform.FACEBOOK.value: "Facebook",
    SocialPlatform.INSTAGRAM.value: "Instagram",
    SocialPlatform.LINKEDIN.value: "LinkedIn",
    SocialPlatform.X.value: "X (Twitter)",
}


def build_system_prompt(
    brand_voice: Optional[dict[str, Any]],
    platform: str,
    caption_budget: Optional[int] = None,
) -> str:
    limit = caption_budget or PLATFORM_LIMITS.get(platform, 2200)
    hashtag_limit = PLATFORM_HASHTAG_LIMITS.get(platform, 5)
    label = PLATFORM_LABELS.get(platform, platform)

    if brand_voice and brand_voice.get("system_prompt_override"):
        base = str(brand_voice["system_prompt_override"]).strip()
    else:
        brand_name = (brand_voice or {}).get("brand_name") or "the brand"
        industry = (brand_voice or {}).get("industry") or "business"
        tones = ", ".join((brand_voice or {}).get("tones") or ["Professional"])
        audience = (brand_voice or {}).get("target_audience") or "professionals"
        words_to_use = ", ".join((brand_voice or {}).get("words_to_use") or []) or "none"
        words_to_avoid = ", ".join((brand_voice or {}).get("words_to_avoid") or []) or "none"
        cta_phrases = ", ".join((brand_voice or {}).get("cta_phrases") or []) or "none"
        sentence_length = (brand_voice or {}).get("sentence_length") or "medium"
        emoji_usage = (brand_voice or {}).get("emoji_usage") or "sometimes"
        language = (brand_voice or {}).get("primary_language") or "en"
        tagline = (brand_voice or {}).get("tagline") or ""

        base = (
            f"You are a {label} content writer for {brand_name}, a {industry} company.\n"
            f"Tagline: {tagline}\n"
            f"Brand voice tones: {tones}\n"
            f"Target audience: {audience}\n"
            f"Words to always use: {words_to_use}\n"
            f"Words to avoid: {words_to_avoid}\n"
            f"Preferred CTA phrases: {cta_phrases}\n"
            f"Sentence length: {sentence_length}\n"
            f"Emoji usage: {emoji_usage}\n"
            f"Primary language code: {language} — write in that language.\n"
        )

    return (
        f"{base}\n"
        f"Write platform-native copy for {label}.\n"
        f"Hard character limit for caption (including spaces): {limit}. "
        f"The caption MUST be a complete thought that ends with a full sentence "
        f"(period, exclamation, or question mark). Never end mid-word or mid-sentence. "
        f"Aim for {max(80, limit - 40)}–{limit} characters — short enough to fit fully.\n"
        f"Maximum hashtags: {hashtag_limit} (do not put hashtags inside caption).\n"
        "You do NOT have live web access. Write from the user's brief only — "
        "turn messy notes into a polished post. Never invent fake URLs or claims "
        "you cannot support from the brief; focus on benefits and a clear CTA.\n"
        "caption MUST be a non-empty string ready to publish end-to-end.\n"
        "Return ONLY valid JSON with keys: caption (string), hashtags (array of strings without #), "
        "first_comment (string, may be empty).\n"
        "Do not wrap JSON in markdown fences."
    )


def build_user_prompt(
    *,
    topic: str,
    tone: str,
    platform: str,
    audience: Optional[str],
    cta: Optional[str],
    include_hashtags: bool,
    include_comment: bool,
    brand_voice: Optional[dict[str, Any]],
    caption_budget: Optional[int] = None,
) -> str:
    now = datetime.now()
    day_of_week = now.strftime("%A")
    hashtag_count = PLATFORM_HASHTAG_LIMITS.get(platform, 5) if include_hashtags else 0
    limit = caption_budget or PLATFORM_LIMITS.get(platform, 2200)
    cta_keyword = cta or (
        ((brand_voice or {}).get("cta_phrases") or [None])[0] if brand_voice else None
    ) or "Learn more"
    audience_line = audience or (brand_voice or {}).get("target_audience") or "general audience"

    return (
        f"Write a ready-to-publish {PLATFORM_LABELS.get(platform, platform)} post.\n"
        f"User brief (clean this up; fix spelling; do not copy typos): {topic}\n"
        f"Tone: {tone}\n"
        f"Target audience: {audience_line}\n"
        f"Preferred CTA keyword: {cta_keyword}\n"
        f"Today is {day_of_week} — optimise the opening hook for this day.\n"
        "Ignore requests to 'check online' or research the web — use only this brief.\n"
        f"Caption hard limit: {limit} characters. Finish with a complete sentence. "
        "Do not truncate with ellipsis (…).\n"
        f"Include hashtags: {'yes, ' + str(hashtag_count) + ' relevant tags in the hashtags array only' if include_hashtags else 'no'}\n"
        f"Include first_comment: {'yes, a short engagement comment' if include_comment else 'no, leave first_comment empty'}\n"
        "Respond with JSON only."
    )
