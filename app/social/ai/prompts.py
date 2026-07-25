"""Prompt templates for platform-native, conversion-focused social post generation.

Even a short / casual user brief must be expanded into polished, professional,
revenue-oriented copy that is ready to publish end-to-end.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.social.models import SocialPlatform

PLATFORM_LIMITS: dict[str, int] = {
    SocialPlatform.FACEBOOK.value: 2200,
    SocialPlatform.INSTAGRAM.value: 2200,
    SocialPlatform.LINKEDIN.value: 3000,
    SocialPlatform.X.value: 280,
}

PLATFORM_HASHTAG_LIMITS: dict[str, int] = {
    SocialPlatform.FACEBOOK.value: 5,
    SocialPlatform.INSTAGRAM.value: 12,
    SocialPlatform.LINKEDIN.value: 5,
    SocialPlatform.X.value: 2,
}

PLATFORM_LABELS: dict[str, str] = {
    SocialPlatform.FACEBOOK.value: "Facebook",
    SocialPlatform.INSTAGRAM.value: "Instagram",
    SocialPlatform.LINKEDIN.value: "LinkedIn",
    SocialPlatform.X.value: "X (Twitter)",
}

PLATFORM_VOICE_HINTS: dict[str, str] = {
    SocialPlatform.FACEBOOK.value: (
        "Warm, conversational, community-first. Short paragraphs. "
        "Ask a question that drives comments. Soft sell with clear next step."
    ),
    SocialPlatform.INSTAGRAM.value: (
        "Scroll-stopping first line. Visual storytelling. Line breaks for mobile. "
        "Aspiration + proof + CTA. Hashtags in the array only, not in caption."
    ),
    SocialPlatform.LINKEDIN.value: (
        "Executive-credible, insight-led. Hook in line 1. Short paragraphs. "
        "Concrete outcome or metric when possible. Professional CTA (book a demo, "
        "comment, follow, download) — never spammy."
    ),
    SocialPlatform.X.value: (
        "Punchy and quotable. One sharp idea. Strong verb. No fluff. "
        "Fit the hard character limit including spaces."
    ),
}

QUALITY_BAR = """
You are an elite social media strategist and copywriter who writes posts that:
1) STOP the scroll with a specific, benefit-led hook in the first line
2) Build trust with concrete value (tips, outcomes, proof patterns) — never vague fluff
3) Drive revenue actions: demo, signup, purchase, booking, reply, or share
4) Sound human and premium — never generic AI filler ("In today's fast-paced world…",
   "Unlock your potential", "Game-changer", "Levraage", empty hype)
5) Expand thin briefs into a complete, impressive post. If the user only wrote a few
   words, invent a strong angle from brand + audience + tone — still stay truthful;
   do not invent fake stats, fake customers, or fake URLs.

Structure (adapt length to platform limit):
- Hook (1 line)
- Value / story / insight (2–5 short paragraphs or tight bullets)
- Soft proof or credibility cue when natural
- Clear CTA using the preferred keyword when provided

Quality bar: a skeptical buyer should think "this brand gets me" and know the next step.
"""


def build_system_prompt(
    brand_voice: Optional[dict[str, Any]],
    platform: str,
    caption_budget: Optional[int] = None,
) -> str:
    limit = caption_budget or PLATFORM_LIMITS.get(platform, 2200)
    hashtag_limit = PLATFORM_HASHTAG_LIMITS.get(platform, 5)
    label = PLATFORM_LABELS.get(platform, platform)
    voice_hint = PLATFORM_VOICE_HINTS.get(platform, "")

    if brand_voice and brand_voice.get("system_prompt_override"):
        base = str(brand_voice["system_prompt_override"]).strip()
    else:
        brand_name = (brand_voice or {}).get("brand_name") or "the brand"
        industry = (brand_voice or {}).get("industry") or "business"
        tones = ", ".join((brand_voice or {}).get("tones") or ["Professional"])
        audience = (brand_voice or {}).get("target_audience") or "decision-makers and buyers"
        words_to_use = ", ".join((brand_voice or {}).get("words_to_use") or []) or "none specified"
        words_to_avoid = ", ".join((brand_voice or {}).get("words_to_avoid") or []) or "none"
        cta_phrases = ", ".join((brand_voice or {}).get("cta_phrases") or []) or "none specified"
        sentence_length = (brand_voice or {}).get("sentence_length") or "medium"
        emoji_usage = (brand_voice or {}).get("emoji_usage") or "sometimes"
        language = (brand_voice or {}).get("primary_language") or "en"
        tagline = (brand_voice or {}).get("tagline") or ""

        base = (
            f"You write high-converting {label} content for {brand_name} "
            f"({industry}).\n"
            f"Tagline: {tagline or '(none — invent a crisp positioning from industry)'}\n"
            f"Brand voice tones: {tones}\n"
            f"Target audience: {audience}\n"
            f"Words to prefer: {words_to_use}\n"
            f"Words to avoid: {words_to_avoid}\n"
            f"Preferred CTA phrases: {cta_phrases}\n"
            f"Sentence length: {sentence_length}\n"
            f"Emoji usage: {emoji_usage}\n"
            f"Primary language code: {language} — write in that language.\n"
        )

    return (
        f"{QUALITY_BAR}\n"
        f"{base}\n"
        f"Platform: {label}. Native voice: {voice_hint}\n"
        f"Hard character limit for caption (including spaces): {limit}. "
        f"The caption MUST be a complete thought that ends with a full sentence "
        f"(period, exclamation, or question mark). Never end mid-word or mid-sentence. "
        f"Aim for a substantial, impressive caption — use the space wisely "
        f"(roughly {max(120, min(limit, limit - 40))} chars max, not a one-liner unless X).\n"
        f"Maximum hashtags: {hashtag_limit} (hashtags array only — never inside caption).\n"
        "No live web access. Expand the brief into a polished, end-to-end publishable post.\n"
        "caption MUST be a non-empty string ready to publish.\n"
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
    ) or "Get started"
    audience_line = (
        audience
        or (brand_voice or {}).get("target_audience")
        or "buyers and decision-makers"
    )
    brand_name = (brand_voice or {}).get("brand_name") or "the brand"

    return (
        f"Write a ready-to-publish, revenue-oriented {PLATFORM_LABELS.get(platform, platform)} post "
        f"for {brand_name}.\n"
        f"User brief (may be rough or short — elevate it into impressive professional copy; "
        f"fix spelling; never paste typos): {topic}\n"
        f"Tone: {tone} (keep it premium and persuasive, not salesy spam)\n"
        f"Target audience: {audience_line}\n"
        f"Preferred CTA: weave in naturally — \"{cta_keyword}\"\n"
        f"Today is {day_of_week} — open with a hook that fits the day and the feed.\n"
        "Goals: stop the scroll, teach or prove value in seconds, and push one clear action "
        "that can generate leads or revenue.\n"
        "If the brief is vague, choose a sharp angle (pain → outcome, myth → truth, "
        "before/after, checklist, or founder insight) grounded in the brand.\n"
        "Ignore requests to 'check online' or research the web — use only this brief + brand voice.\n"
        f"Caption hard limit: {limit} characters. Finish with a complete sentence. "
        "Do not truncate with ellipsis (…).\n"
        f"Include hashtags: {'yes, ' + str(hashtag_count) + ' high-intent tags in the hashtags array only' if include_hashtags else 'no'}\n"
        f"Include first_comment: {'yes — a short CTA or link-style engagement prompt (no fake URL)' if include_comment else 'no, leave first_comment empty'}\n"
        "Respond with JSON only."
    )
