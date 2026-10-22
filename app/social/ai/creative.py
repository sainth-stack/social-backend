from __future__ import annotations

import colorsys
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.social.ai.compositor import OverlayCopy, compose_creative, load_logo, parse_hex
from app.social.ai.image_generator import generate_post_image

logger = logging.getLogger(__name__)

FORMATS = ("post", "banner", "story", "carousel", "quote", "product")

_SIZES: dict[str, dict[str, tuple[int, int]]] = {
    "post": {"instagram": (1080, 1350), "facebook": (1080, 1350), "linkedin": (1200, 1200), "twitter": (1600, 900), "x": (1600, 900), "pinterest": (1000, 1500), "threads": (1080, 1350)},
    "banner": {"linkedin": (1200, 627), "facebook": (1200, 630), "twitter": (1600, 900), "x": (1600, 900), "youtube": (1280, 720)},
    "story": {},
    "carousel": {"linkedin": (1080, 1080)},
    "quote": {},
    "product": {"instagram": (1080, 1350), "facebook": (1080, 1350), "linkedin": (1200, 1200), "twitter": (1600, 900), "x": (1600, 900), "pinterest": (1000, 1500)},
}
_DEFAULT_SIZES = {
    "post": (1080, 1080),
    "banner": (1200, 628),
    "story": (1080, 1920),
    "carousel": (1080, 1350),
    "quote": (1080, 1080),
    "product": (1080, 1080),
}

_COMPOSITION = {
    "post": "Hero subject in the upper two-thirds, calm uncluttered lower third with soft tonal gradient for headline space.",
    "banner": "Wide cinematic composition, main subject on the right third, clean open negative space on the left half.",
    "story": "Vertical full-bleed composition, subject centered in the middle, clean top and bottom areas free of detail.",
    "carousel": "Editorial magazine composition, subject in the upper area, calm lower third, consistent series look.",
    "quote": "Abstract atmospheric background, soft textures, gentle depth of field, no people facing camera, minimal detail in the center.",
    "product": "Studio product hero shot on a styled surface, soft shadows, premium commercial lighting, subject centered with breathing room below.",
}

_DEFAULT_STYLE = "premium commercial photography, soft cinematic lighting, refined color grade, crisp focus, high detail"


@dataclass
class CreativeSpec:
    format: str = "post"
    platform: Optional[str] = None
    headline: str = ""
    subtext: str = ""
    cta: str = ""
    text_overlay: bool = False
    auto_copy: bool = True
    include_logo: bool = True
    logo_id: Optional[str] = None
    logo_position: str = "top-right"
    use_brand: bool = True
    caption: str = ""
    slide_index: Optional[int] = None
    slide_count: Optional[int] = None


@dataclass
class CreativeResult:
    image: bytes
    width: int
    height: int
    copy: OverlayCopy = field(default_factory=OverlayCopy)
    prompt: str = ""
    logo_applied: bool = False


def target_size(fmt: str, platform: Optional[str]) -> tuple[int, int]:
    fmt = fmt if fmt in FORMATS else "post"
    key = (platform or "").lower()
    return _SIZES.get(fmt, {}).get(key) or _DEFAULT_SIZES[fmt]


def generation_size(width: int, height: int) -> str:
    scale = 1280 / max(width, height)
    w = max(512, int(round(width * scale / 16)) * 16)
    h = max(512, int(round(height * scale / 16)) * 16)
    return f"{w}x{h}"


def describe_color(value: Optional[str]) -> Optional[str]:
    if not value or not re.fullmatch(r"#?[0-9a-fA-F]{6}", value.strip()):
        return None
    r, g, b = parse_hex(value, (0, 0, 0))
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if s < 0.12:
        if l < 0.18:
            return "near-black"
        if l > 0.88:
            return "off-white"
        return "charcoal grey" if l < 0.45 else "soft grey"
    hue = h * 360
    names = (
        (15, "red"), (40, "orange"), (65, "amber yellow"), (90, "lime"), (150, "green"),
        (185, "teal"), (210, "sky blue"), (245, "blue"), (275, "indigo"), (300, "violet"),
        (335, "magenta"), (360, "red"),
    )
    name = next(n for limit, n in names if hue <= limit)
    if l < 0.3:
        tone = "deep"
    elif l > 0.72:
        tone = "pastel"
    elif s > 0.65:
        tone = "vibrant"
    else:
        tone = "muted"
    return f"{tone} {name}"


def _clean(text: Optional[str], limit: int) -> str:
    value = " ".join((text or "").split())
    return value[:limit]


def _brand_lines(brand: dict) -> list[str]:
    lines: list[str] = []
    if brand.get("brand_name"):
        lines.append(f"Brand: {_clean(brand['brand_name'], 80)}")
    if brand.get("industry"):
        lines.append(f"Industry: {_clean(brand['industry'], 80)}")
    if brand.get("tagline"):
        lines.append(f"About: {_clean(brand['tagline'], 200)}")
    if brand.get("target_audience"):
        lines.append(f"Audience: {_clean(brand['target_audience'], 160)}")
    if brand.get("tones"):
        lines.append(f"Voice: {', '.join(brand['tones'][:3])}")
    if brand.get("words_to_use"):
        lines.append(f"Offer/keywords: {', '.join(brand['words_to_use'][:8])}")
    return lines


def _palette_phrase(brand: dict) -> str:
    colors = [c for c in (describe_color(brand.get("primary_color")), describe_color(brand.get("accent_color"))) if c]
    if not colors:
        return ""
    return f"Color palette harmonized with brand colors: {' and '.join(colors)} accents."


def build_visual_prompt(subject: str, spec: CreativeSpec, brand: dict, style: Optional[str]) -> str:
    parts = [
        f"Scroll-stopping social media {'ad creative' if spec.format != 'quote' else 'background'} for {spec.platform or 'social media'}.",
        f"Subject: {_clean(subject, 360)}.",
    ]
    if brand.get("industry"):
        parts.append(f"Context: {_clean(brand['industry'], 60)} brand.")
    if brand.get("target_audience") and spec.format != "quote":
        parts.append(f"Made for {_clean(brand['target_audience'], 90)}.")
    parts.append(_COMPOSITION.get(spec.format, _COMPOSITION["post"]))
    parts.append(f"Style: {_clean(style, 160) if style else _DEFAULT_STYLE}.")
    palette = _palette_phrase(brand)
    if palette:
        parts.append(palette)
    parts.append("Absolutely no text, letters, numbers, logos, watermarks or UI elements.")
    return " ".join(p for p in parts if p)[:1000]


def _fallback_copy(topic: str, spec: CreativeSpec, brand: dict) -> OverlayCopy:
    source = spec.headline or topic
    first = re.split(r"(?<=[.!?])\s+|\n", source.strip(), maxsplit=1)[0]
    words = re.sub(r"[#@]\S+", "", first).split()
    limit = 22 if spec.format == "quote" else 8
    headline = " ".join(words[:limit]).rstrip(",;:-")
    cta = spec.cta or ((brand.get("cta_phrases") or [""])[0] if spec.format in ("banner", "product") else "")
    subtext = spec.subtext
    if spec.format == "quote" and not subtext and brand.get("brand_name"):
        subtext = f"— {brand['brand_name']}"
    return OverlayCopy(headline=headline, subtext=_clean(subtext, 140), cta=_clean(cta, 28))


def art_direct(topic: str, spec: CreativeSpec, brand: dict, style: Optional[str]) -> tuple[str, OverlayCopy]:
    from app.social.ai.generator import _chat_completion, _get_llm, _message_text, _parse_json_content

    width, height = target_size(spec.format, spec.platform)
    language = brand.get("primary_language") or "en"
    is_quote = spec.format == "quote"
    headline_rule = (
        "headline: the quote itself, a memorable insight under 22 words, no quotation marks"
        if is_quote
        else "headline: 3-7 punchy words specific to the brief, benefit-led, Title or sentence case"
    )
    slide_line = ""
    if spec.slide_index and spec.slide_count:
        slide_line = f"This is slide {spec.slide_index} of {spec.slide_count} in a carousel; keep a consistent series look."
    system_prompt = (
        "You are the creative director of a top digital agency. Turn a social media brief into a precise "
        "text-to-image prompt and short on-image copy. Reply with JSON only: "
        '{"visual":"...","headline":"...","subtext":"...","cta":"..."}. '
        "visual: 45-90 words describing one clear subject, setting, composition, lighting, lens or medium, mood and palette. "
        "Photorealistic commercial quality unless the brief implies illustration. "
        "Never request text, letters, numbers, logos, watermarks, UI or screenshots in the visual. "
        f"Composition requirement: {_COMPOSITION.get(spec.format, _COMPOSITION['post'])} "
        f"{headline_rule}. subtext: optional supporting line under 14 words or empty. "
        "cta: 1-3 words only when the brief implies an action, else empty. "
        f"No hashtags, emojis or quotation marks in copy. Copy language: {language}."
    )
    user_lines = [
        f"Brief: {_clean(topic, 500)}",
        f"Format: {spec.format} ({width}x{height}) for {spec.platform or 'social media'}",
    ]
    if spec.caption:
        user_lines.append(f"Post caption: {_clean(spec.caption, 600)}")
    if spec.headline:
        user_lines.append(f"Preferred headline: {_clean(spec.headline, 120)}")
    if spec.cta:
        user_lines.append(f"Call to action: {_clean(spec.cta, 40)}")
    if style:
        user_lines.append(f"Visual style: {_clean(style, 160)}")
    user_lines.extend(_brand_lines(brand))
    palette = _palette_phrase(brand)
    if palette:
        user_lines.append(palette)
    if slide_line:
        user_lines.append(slide_line)

    client, model = _get_llm()
    response = _chat_completion(
        client,
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(user_lines)},
        ],
        max_completion_tokens=1500,
    )
    data: dict[str, Any] = _parse_json_content(_message_text(response.choices[0].message))
    visual = _clean(str(data.get("visual") or ""), 700)
    copy = OverlayCopy(
        headline=_clean(spec.headline or str(data.get("headline") or ""), 160 if is_quote else 80).strip("\"'“”"),
        subtext=_clean(spec.subtext or str(data.get("subtext") or ""), 140),
        cta=_clean(spec.cta or str(data.get("cta") or ""), 28),
    )
    return visual, copy


def render_creative(
    *,
    topic: str,
    style: Optional[str],
    spec: CreativeSpec,
    brand: dict,
    logo_bytes: Optional[bytes] = None,
) -> CreativeResult:
    fmt = spec.format if spec.format in FORMATS else "post"
    spec.format = fmt
    brand = brand if spec.use_brand else {}
    width, height = target_size(fmt, spec.platform)

    visual = ""
    copy = OverlayCopy(headline=spec.headline, subtext=spec.subtext, cta=spec.cta)
    wants_copy = spec.text_overlay or fmt == "quote"
    if spec.auto_copy:
        try:
            visual, directed = art_direct(topic, spec, brand, style)
            if wants_copy:
                copy = directed
        except Exception as exc:
            logger.warning("Creative art direction failed, using fallback: %s", exc)
    if wants_copy and not copy.headline:
        copy = _fallback_copy(topic, spec, brand)

    subject = visual or (f"{topic}. Post context: {spec.caption}" if spec.caption else topic)
    prompt = build_visual_prompt(subject, spec, brand, style) if not visual else (
        f"{visual} {_COMPOSITION.get(fmt, '')} {_palette_phrase(brand)} "
        "Absolutely no text, letters, numbers, logos, watermarks or UI elements."
    ).strip()[:1000]

    data = generate_post_image(
        topic=topic,
        style=style,
        size=generation_size(width, height),
        mode="create",
        prompt_override=prompt,
    )
    raw = data.get("imageB64")
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise RuntimeError("Image provider returned no bytes")

    slide_label = None
    if fmt == "carousel" and spec.slide_index and spec.slide_count:
        slide_label = f"{spec.slide_index}/{spec.slide_count}"

    logo_ok = bool(spec.include_logo and logo_bytes and load_logo(logo_bytes, 64, 64) is not None)
    image = compose_creative(
        bytes(raw),
        width=width,
        height=height,
        layout=fmt,
        copy=copy if wants_copy else None,
        logo_bytes=logo_bytes if logo_ok else None,
        logo_position=spec.logo_position,
        primary_color=brand.get("primary_color"),
        accent_color=brand.get("accent_color"),
        slide_label=slide_label,
    )
    return CreativeResult(
        image=image,
        width=width,
        height=height,
        copy=copy if wants_copy else OverlayCopy(),
        prompt=prompt,
        logo_applied=logo_ok,
    )
