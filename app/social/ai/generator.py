"""Generate platform-native social post copy via AWS Bedrock."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from fastapi import HTTPException, status

from app.social.ai.caption_sanitize import sanitize_caption_and_hashtags
from app.social.ai.prompts import (
    GENERIC_FILLER_PHRASES,
    PLATFORM_HASHTAG_LIMITS,
    PLATFORM_LIMITS,
    QUALITY_BAR,
    brief_fidelity_score,
    build_brand_context,
    build_fidelity_revision_prompt,
    build_system_prompt,
    build_user_prompt,
    extract_significant_terms,
    is_substantive_brief,
)
from app.social.models import SocialPlatform
from app.social.publishers.base import format_caption

logger = logging.getLogger(__name__)

# gpt-5-mini is a reasoning model: max_completion_tokens covers reasoning + output.
# Too low (e.g. 1200) → finish_reason=length with empty content.
_MAX_COMPLETION_TOKENS = 4096
_RETRY_COMPLETION_TOKENS = 8192


def _message_text(message: Any) -> str:
    """Extract text from a chat completion message (string or content parts)."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if text:
                    parts.append(str(text))
            else:
                text = getattr(part, "text", None) or getattr(part, "content", None)
                if text:
                    parts.append(str(text))
        return "".join(parts).strip()
    return ""


def _parse_json_content(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"caption": text, "hashtags": [], "first_comment": ""}


def _trim_caption(caption: str, limit: int) -> str:
    """Fit caption to limit without cutting mid-word when possible."""
    caption = (caption or "").strip()
    if limit <= 0:
        return ""
    if len(caption) <= limit:
        return caption
    window = caption[:limit]
    endings = list(re.finditer(r"[.!?](?=\s|$)", window))
    if endings and endings[-1].end() >= min(40, limit // 3):
        return window[:endings[-1].end()].strip()
    idx = window[:limit - 1].rfind(" ")
    if idx > 0:
        return window[:idx].rstrip(" ,;:-") + "."
    return window[:limit - 1].rstrip(" ,;:-") + "."


def _fit_caption_and_hashtags(caption: str, hashtags: list[str], limit: int) -> tuple[str, list[str]]:
    caption = _trim_caption(caption, limit)
    fitted: list[str] = []
    for tag in hashtags:
        if len(format_caption(caption, [*fitted, tag])) <= limit:
            fitted.append(tag)
    return caption, fitted


def _caption_needs_polish(caption: str, platform_key: str, length_hint: Optional[str] = None) -> bool:
    text = (caption or "").strip()
    low = text.lower()
    if any(phrase in low for phrase in GENERIC_FILLER_PHRASES):
        return True
    if (length_hint or "").strip().lower() == "short":
        return False
    if len(text) < 40:
        return True
    if platform_key == SocialPlatform.X.value:
        return len(text) < 80
    if len(text) < 180:
        return True
    return False


def _normalize_hashtags(raw: Any, limit: int) -> list[str]:
    if not isinstance(raw, list) or limit <= 0:
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item).strip().lstrip("#")
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def _extract_fields(parsed: dict[str, Any]) -> tuple[str, list[Any], str]:
    caption = (
        parsed.get("caption")
        or parsed.get("text")
        or parsed.get("post")
        or parsed.get("content")
        or ""
    )
    hashtags = parsed.get("hashtags") or parsed.get("tags") or []
    first_comment = (
        parsed.get("first_comment")
        or parsed.get("firstComment")
        or parsed.get("comment")
        or ""
    )
    return str(caption), hashtags, str(first_comment)


def _is_response_format_error(exc: Exception) -> bool:
    """True only when the model/API rejects response_format (not network errors)."""
    text = str(exc).lower()
    needles = (
        "response_format",
        "json_object",
        "unsupported",
        "invalid_request",
        "not supported",
        "unknown parameter",
    )
    # Connection / timeout errors must not trigger a second full wait.
    if any(
        token in text
        for token in ("connection", "timeout", "timed out", "connect", "network", "ssl")
    ):
        return False
    return any(n in text for n in needles)


def _chat_completion(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, str]],
    max_completion_tokens: int,
    prefer_json: bool = True,
    reasoning_effort: Optional[str] = "low",
    temperature: Optional[float] = None,
) -> Any:
    """Call chat completions; tolerate deployments that reject optional params."""
    base: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
    }
    if temperature is not None:
        base["temperature"] = temperature
    # Prefer low reasoning so output tokens are not starved (gpt-5 family).
    attempts: list[dict[str, Any]] = []
    if prefer_json and reasoning_effort:
        attempts.append({**base, "response_format": {"type": "json_object"}, "reasoning_effort": reasoning_effort})
    if prefer_json:
        attempts.append({**base, "response_format": {"type": "json_object"}})
    if reasoning_effort:
        attempts.append({**base, "reasoning_effort": reasoning_effort})
    attempts.append(base)

    last_exc: Exception | None = None
    for kwargs in attempts:
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            # Only continue when the failure is about optional request shape.
            msg = str(exc).lower()
            optional_fail = any(
                token in msg
                for token in (
                    "response_format",
                    "json_object",
                    "reasoning_effort",
                    "unsupported",
                    "unknown parameter",
                    "invalid_request",
                    "not supported",
                )
            )
            if not optional_fail and not _is_response_format_error(exc):
                raise
            logger.info("Social generate retrying without optional params: %s", exc)
    assert last_exc is not None
    raise last_exc


def _complete_json(
    *,
    client: Any,
    model: str,
    system_prompt: str,
    user_prompt: str,
    platform_key: str,
    temperature: Optional[float] = None,
) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response = _chat_completion(
        client,
        model=model,
        messages=messages,
        max_completion_tokens=_MAX_COMPLETION_TOKENS,
        temperature=temperature,
    )
    choice = response.choices[0]
    message = choice.message
    raw = _message_text(message)
    finish_reason = getattr(choice, "finish_reason", None)
    refusal = getattr(message, "refusal", None)

    if not raw and finish_reason == "length":
        logger.warning(
            "Social generate empty+length for %s; retrying with larger budget",
            platform_key,
        )
        retry_messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": (
                    f"{user_prompt}\nReturn the requested JSON directly, without extra explanation."
                ),
            },
        ]
        response = _chat_completion(
            client,
            model=model,
            messages=retry_messages,
            max_completion_tokens=_RETRY_COMPLETION_TOKENS,
            reasoning_effort="low",
            temperature=temperature,
        )
        choice = response.choices[0]
        message = choice.message
        raw = _message_text(message)
        finish_reason = getattr(choice, "finish_reason", None)
        refusal = getattr(message, "refusal", None)

    if not raw:
        logger.error(
            "Social generate empty content platform=%s finish_reason=%s refusal=%s",
            platform_key,
            finish_reason,
            refusal,
        )
        detail = (
            f"AI returned empty content for {platform_key}"
            + (f" (finish_reason={finish_reason})" if finish_reason else "")
            + (f": {refusal}" if refusal else "")
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )
    return raw


def _generate_one_platform(
    *,
    client: Any,
    model: str,
    platform_key: str,
    topic: str,
    tone: str,
    audience: Optional[str],
    cta: Optional[str],
    include_hashtags: bool,
    include_comment: bool,
    brand_voice: Optional[dict[str, Any]],
    content_series_mode: bool = False,
    content_type: Optional[str] = None,
    length_hint: Optional[str] = None,
    include_emojis: bool = True,
    media_note: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    limit = PLATFORM_LIMITS.get(platform_key, 2200)
    hashtag_limit = PLATFORM_HASHTAG_LIMITS.get(platform_key, 5)
    caption_budget = limit
    if include_hashtags and platform_key == SocialPlatform.X.value:
        caption_budget = max(120, limit - 36)

    series_mode = content_series_mode
    substantive = is_substantive_brief(topic)
    if substantive:
        temp = 0.35
    elif series_mode:
        temp = 0.5
    else:
        temp = 0.45

    system_prompt = build_system_prompt(
        brand_voice,
        platform_key,
        caption_budget=caption_budget,
        content_series_mode=series_mode,
        substantive_brief=substantive,
    )
    user_prompt = build_user_prompt(
        topic=topic,
        tone=tone,
        platform=platform_key,
        audience=audience,
        cta=cta,
        include_hashtags=include_hashtags,
        include_comment=include_comment,
        brand_voice=brand_voice,
        caption_budget=caption_budget,
        content_series_mode=series_mode,
        content_type=content_type,
        length_hint=length_hint,
        include_emojis=include_emojis,
        media_note=media_note,
    )

    def _run(user: str) -> str:
        return _complete_json(
            client=client,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user,
            platform_key=platform_key,
            temperature=temp,
        )

    try:
        raw = _run(user_prompt)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Social generate failed for %s: %s", platform_key, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI generation failed for {platform_key}: {exc}",
        ) from exc

    parsed = _parse_json_content(raw)
    caption_raw, hashtags_raw, first_comment_raw = _extract_fields(parsed)
    hashtags = (
        _normalize_hashtags(hashtags_raw, hashtag_limit) if include_hashtags else []
    )
    first_comment = first_comment_raw.strip() if include_comment else ""

    caption_raw, hashtags = sanitize_caption_and_hashtags(
        caption_raw,
        hashtags,
        hashtag_limit=hashtag_limit if include_hashtags else 0,
    )
    caption, hashtags = _fit_caption_and_hashtags(caption_raw, hashtags, limit)

    fidelity_low = substantive and brief_fidelity_score(topic, caption) < 0.22
    needs_polish = caption and (
        _caption_needs_polish(caption, platform_key, length_hint) or fidelity_low
    )
    if needs_polish:
        reason = "low brief fidelity" if fidelity_low else "weak first draft"
        logger.info("Social generate polish retry for %s (%s)", platform_key, reason)
        missing = [
            t
            for t in extract_significant_terms(topic)
            if t.lower() not in (caption or "").lower()
        ]
        polish_user = build_fidelity_revision_prompt(user_prompt, missing)
        if series_mode and not fidelity_low and (length_hint or "").strip().lower() != "short":
            polish_user += (
                "\nAlso aim for ~350–900 characters on Instagram/Facebook when limit allows."
            )
        try:
            raw2 = _run(polish_user)
            parsed2 = _parse_json_content(raw2)
            cap2, tags2, fc2 = _extract_fields(parsed2)
            hashtags2 = (
                _normalize_hashtags(tags2, hashtag_limit) if include_hashtags else []
            )
            cap2, hashtags2 = sanitize_caption_and_hashtags(
                cap2,
                hashtags2,
                hashtag_limit=hashtag_limit if include_hashtags else 0,
            )
            caption2, hashtags2 = _fit_caption_and_hashtags(cap2, hashtags2, limit)
            score2 = brief_fidelity_score(topic, caption2)
            better = caption2 and (
                (fidelity_low and score2 > brief_fidelity_score(topic, caption))
                or (not fidelity_low and not _caption_needs_polish(caption2, platform_key, length_hint))
            )
            if better:
                caption = caption2
                hashtags = hashtags2
                first_comment = fc2.strip() if include_comment else first_comment
        except Exception:
            logger.warning("Social generate polish failed for %s; retaining original draft", platform_key, exc_info=True)

    if not caption:
        logger.error(
            "Social generate missing caption platform=%s raw=%s",
            platform_key,
            raw[:500],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"AI generation returned no caption for {platform_key}. "
                "Try again with a clearer topic."
            ),
        )

    full_text = format_caption(caption, hashtags)
    return platform_key, {
        "caption": caption,
        "hashtags": hashtags,
        "firstComment": first_comment,
        "characterCount": len(full_text),
    }


def generate_platform_content(
    *,
    topic: str,
    tone: str,
    platforms: list[str],
    audience: Optional[str],
    cta: Optional[str],
    include_hashtags: bool,
    include_comment: bool,
    brand_voice: Optional[dict[str, Any]],
    content_series_mode: bool = False,
    content_type: Optional[str] = None,
    length_hint: Optional[str] = None,
    include_emojis: bool = True,
    media_note: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    """Return { platform: { caption, hashtags, firstComment, characterCount } }.

    Platforms are generated in parallel so multi-platform requests stay under
    the frontend timeout (sequential gpt-5-mini calls were 40s+ for 4 platforms).
    """
    try:
        from app.providers.llm.factory import get_llm_client, get_llm_model
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI generation is not configured",
        ) from exc

    try:
        client = get_llm_client()
        model = get_llm_model()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    platform_keys: list[str] = []
    for platform in platforms:
        try:
            platform_keys.append(SocialPlatform(platform).value)
        except ValueError:
            continue

    if not platform_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid platforms provided",
        )

    results: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    # One worker per platform (max 4) — wall time ≈ slowest single call.
    with ThreadPoolExecutor(max_workers=min(4, len(platform_keys))) as pool:
        futures = {
            pool.submit(
                _generate_one_platform,
                client=client,
                model=model,
                platform_key=key,
                topic=topic,
                tone=tone,
                audience=audience,
                cta=cta,
                include_hashtags=include_hashtags,
                include_comment=include_comment,
                brand_voice=brand_voice,
                content_series_mode=content_series_mode,
                content_type=content_type,
                length_hint=length_hint,
                include_emojis=include_emojis,
                media_note=media_note,
            ): key
            for key in platform_keys
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                platform_key, payload = future.result()
                results[platform_key] = payload
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                errors.append(detail)
            except Exception as exc:
                logger.exception("Social generate worker failed for %s", key)
                errors.append(f"AI generation failed for {key}: {exc}")

    if not results:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=errors[0] if errors else "AI generation failed",
        )
    if errors:
        logger.warning("Partial social generate success; errors=%s", errors)
    return results


def generate_brand_voice_sample(brand_voice: dict[str, Any]) -> dict[str, Any]:
    """Generate a single sample post using brand voice settings."""
    topic = (
        f"Introduce {brand_voice.get('brand_name') or 'our brand'} "
        f"and what makes us different in {brand_voice.get('industry') or 'our industry'}"
    )
    tones = brand_voice.get("tones") or ["Professional"]
    tone = tones[0] if tones else "Professional"
    platforms = generate_platform_content(
        topic=topic,
        tone=tone,
        platforms=[SocialPlatform.LINKEDIN.value],
        audience=brand_voice.get("target_audience"),
        cta=(brand_voice.get("cta_phrases") or [None])[0],
        include_hashtags=True,
        include_comment=False,
        brand_voice=brand_voice,
    )
    sample = platforms.get(SocialPlatform.LINKEDIN.value) or next(iter(platforms.values()))
    return {
        "platform": SocialPlatform.LINKEDIN.value,
        "caption": sample["caption"],
        "hashtags": sample["hashtags"],
        "firstComment": sample.get("firstComment") or "",
    }


# ── Format-specific generators ────────────────────────────────────────────────

def _get_llm():
    """Return (client, model) or raise 503."""
    try:
        from app.providers.llm.factory import get_llm_client, get_llm_model

        return get_llm_client(), get_llm_model()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI generation is not configured",
        ) from exc


def generate_carousel_content(
    *,
    topic: str,
    tone: str,
    audience: Optional[str],
    cta: Optional[str],
    brand_voice: Optional[dict[str, Any]],
    num_slides: int = 6,
) -> dict[str, Any]:
    """Return { slides: [{headline, body, imagePrompt}], caption, hashtags }.

    The carousel format generates an intro caption + individual slide copy.
    Each slide has a cinematic imagePrompt for AI image generation.
    """
    client, model = _get_llm()

    brand_note = ""
    if brand_voice:
        name = brand_voice.get("brand_name") or ""
        tones_str = ", ".join(brand_voice.get("tones") or [])
        industry = brand_voice.get("industry") or ""
        if name or tones_str or industry:
            brand_note = f"\nBrand: {name}. Industry: {industry}. Tone traits: {tones_str}."

    audience_note = f"\nTarget audience: {audience}." if audience else ""
    cta_note = f"\nCall to action: {cta}." if cta else ""

    system_prompt = (
        f"{QUALITY_BAR}\n{build_brand_context(brand_voice, SocialPlatform.LINKEDIN.value)}\n"
        "Design an educational LinkedIn/Instagram carousel with one useful idea per slide. "
        "Use an intro hook and a logical slide narrative grounded in the brief. "
        "Use at most one CTA across the caption and slides, on the final slide if appropriate. "
        f"Use up to {PLATFORM_HASHTAG_LIMITS[SocialPlatform.LINKEDIN.value]} relevant hashtags, not a quota. "
        "Return ONLY a JSON object with no extra text.\n"
        "Schema:\n"
        '{"caption":"<intro caption, max 300 chars>",'
        '"hashtags":["tag1","tag2"],'
        '"slides":[\n'
        '  {"headline":"<bold 6-word max headline>","body":"<2-3 sentence supporting copy>","imagePrompt":"<vivid premium image prompt>"}\n'
        "]}"
    )
    user_prompt = (
        f"=== USER BRIEF (primary source) ===\n{topic.strip()}\n=== END BRIEF ===\n\n"
        f"Create a {num_slides}-slide LinkedIn/Instagram carousel from this brief.\n"
        f"Tone: {tone}.{brand_note}{audience_note}{cta_note}\n"
        f"Every slide must reflect the brief — do not replace with generic marketing.\n"
        f"Rules:\n"
        f"- Slide 1: scroll-stopping hook / painful problem\n"
        f"- Slides 2-{num_slides - 1}: one concrete insight, tip, or step (specific, useful)\n"
        f"- Last slide: one natural next step aligned with the brief; no forced sales CTA\n"
        f"- Headlines must be punchy; body must sound human and expert\n"
        f"- imagePrompt: cinematic, commercial, photorealistic, no text overlays\n"
        f"Return exactly {num_slides} slides."
    )

    raw = _complete_json(
        client=client,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        platform_key="carousel",
    )
    parsed = _parse_json_content(raw)

    slides_raw = parsed.get("slides") or []
    slides = []
    for i, s in enumerate(slides_raw):
        if not isinstance(s, dict):
            continue
        slides.append({
            "headline": str(s.get("headline") or f"Slide {i + 1}"),
            "body": str(s.get("body") or ""),
            "imagePrompt": str(s.get("imagePrompt") or topic),
        })

    # Ensure we always have at least 2 slides
    while len(slides) < 2:
        slides.append({"headline": f"Slide {len(slides) + 1}", "body": "", "imagePrompt": topic})

    hashtag_limit = PLATFORM_HASHTAG_LIMITS[SocialPlatform.LINKEDIN.value]
    hashtags = _normalize_hashtags(parsed.get("hashtags") or [], hashtag_limit)
    caption, hashtags = sanitize_caption_and_hashtags(
        str(parsed.get("caption") or topic), hashtags, hashtag_limit=hashtag_limit,
    )
    caption, hashtags = _fit_caption_and_hashtags(
        _trim_caption(caption, 300), hashtags, PLATFORM_LIMITS[SocialPlatform.INSTAGRAM.value],
    )

    return {"slides": slides, "caption": caption, "hashtags": hashtags}


def generate_thread_content(
    *,
    topic: str,
    tone: str,
    audience: Optional[str],
    cta: Optional[str],
    brand_voice: Optional[dict[str, Any]],
    num_tweets: int = 5,
) -> dict[str, Any]:
    """Return { tweets: [{text}], hashtags }.

    Generates a connected X/Twitter thread — hook + insights + CTA.
    """
    client, model = _get_llm()

    brand_note = ""
    if brand_voice:
        name = brand_voice.get("brand_name") or ""
        if name:
            brand_note = f"\nBrand: {name}."
    audience_note = f"\nTarget audience: {audience}." if audience else ""
    cta_note = f"\nThread CTA (last tweet): {cta}." if cta else ""

    system_prompt = (
        f"{QUALITY_BAR}\n{build_brand_context(brand_voice, SocialPlatform.X.value)}\n"
        "Write a connected X thread: one clear idea per tweet with a logical progression. "
        "Use only one CTA, in the last tweet, if appropriate to the brief. "
        "Each tweet including numbering must fit 270 characters; the last tweet plus "
        "hashtags and the two-newline separator must fit 280 characters. "
        f"Use up to {PLATFORM_HASHTAG_LIMITS[SocialPlatform.X.value]} relevant hashtags in the array only. "
        "No empty hype. Return ONLY a JSON object.\n"
        'Schema: {"tweets":[{"text":"<tweet text, max 270 chars>"}],"hashtags":["tag1","tag2"]}'
    )
    user_prompt = (
        f"=== USER BRIEF (primary source) ===\n{topic.strip()}\n=== END BRIEF ===\n\n"
        f"Write a {num_tweets}-tweet thread from this brief.\n"
        f"Tone: {tone}.{brand_note}{audience_note}{cta_note}\n"
        "Rules:\n"
        "- Tweet 1: specific hook grounded in provided facts, without clickbait\n"
        "- Middle tweets: one clear, useful point each, numbered (2/, 3/ etc.)\n"
        "- Last tweet: one natural next step aligned with the brief, not a forced sales CTA\n"
        "- Expand only for clarity; do not invent supporting claims\n"
        f"- Each tweet MUST be under 270 characters\n"
        f"Return exactly {num_tweets} tweets."
    )

    raw = _complete_json(
        client=client,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        platform_key="thread",
    )
    parsed = _parse_json_content(raw)

    tweets_raw = parsed.get("tweets") or []
    tweets = []
    for t in tweets_raw:
        if isinstance(t, dict):
            text = str(t.get("text") or "").strip()
        else:
            text = str(t).strip()
        if text:
            text, _ = sanitize_caption_and_hashtags(text, [], hashtag_limit=0)
            tweets.append({"text": _trim_caption(text, 270)})

    while len(tweets) < 2:
        tweets.append({"text": ""})

    hashtags = _normalize_hashtags(parsed.get("hashtags") or [], PLATFORM_HASHTAG_LIMITS[SocialPlatform.X.value])
    tweets[-1]["text"], hashtags = _fit_caption_and_hashtags(
        tweets[-1]["text"], hashtags, PLATFORM_LIMITS[SocialPlatform.X.value],
    )
    return {"tweets": tweets, "hashtags": hashtags}


def generate_poll_content(
    *,
    topic: str,
    tone: str,
    audience: Optional[str],
    brand_voice: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Return { question, options: [str, str, str, str], caption, hashtags }.

    Generates a compelling poll with 4 options and an intro caption.
    """
    client, model = _get_llm()

    brand_note = ""
    if brand_voice:
        name = brand_voice.get("brand_name") or ""
        if name:
            brand_note = f"\nBrand: {name}."
    audience_note = f"\nTarget audience: {audience}." if audience else ""

    system_prompt = (
        f"{QUALITY_BAR}\n{build_brand_context(brand_voice, SocialPlatform.LINKEDIN.value)}\n"
        "Design a relevant, neutral poll with balanced, mutually exclusive options. "
        "Avoid loaded questions, assumed audience traits, and manufactured controversy. "
        "Voting is the single CTA; do not add a competing sales request. "
        f"Use up to {PLATFORM_HASHTAG_LIMITS[SocialPlatform.LINKEDIN.value]} relevant hashtags, not a quota. "
        "Return ONLY a JSON object.\n"
        'Schema: {"question":"<poll question, max 140 chars>","options":["<opt1>","<opt2>","<opt3>","<opt4>"],'
        '"caption":"<intro text to post above poll, max 300 chars>","hashtags":["tag1","tag2"]}'
    )
    user_prompt = (
        f"=== USER BRIEF (primary source) ===\n{topic.strip()}\n=== END BRIEF ===\n\n"
        f"Create an engaging poll from this brief.\n"
        f"Tone: {tone}.{brand_note}{audience_note}\n"
        "Rules:\n"
        "- Question must invite useful feedback relevant to the brief, without leading voters\n"
        "- Options: mutually exclusive, no 'other' catch-all, max 25 chars each\n"
        "- Caption: a clear reason to vote; voting is the only CTA\n"
        "- Expand only for clarity using provided facts\n"
        "- Return exactly 4 options."
    )

    raw = _complete_json(
        client=client,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        platform_key="poll",
    )
    parsed = _parse_json_content(raw)

    options_raw = parsed.get("options") or []
    options = [str(o)[:25] for o in options_raw if o][:4]
    while len(options) < 4:
        options.append(f"Option {len(options) + 1}")

    hashtag_limit = PLATFORM_HASHTAG_LIMITS[SocialPlatform.LINKEDIN.value]
    caption, hashtags = sanitize_caption_and_hashtags(
        str(parsed.get("caption") or ""),
        _normalize_hashtags(parsed.get("hashtags") or [], hashtag_limit),
        hashtag_limit=hashtag_limit,
    )
    caption, hashtags = _fit_caption_and_hashtags(
        _trim_caption(caption, 300), hashtags, PLATFORM_LIMITS[SocialPlatform.LINKEDIN.value],
    )
    return {
        "question": _trim_caption(str(parsed.get("question") or topic), 140),
        "options": options,
        "caption": caption,
        "hashtags": hashtags,
    }
