"""Normalize caption + hashtag separation for publish-ready social copy."""

from __future__ import annotations

import re

_HASHTAG_TOKEN = re.compile(r"#([A-Za-z][\w]{1,})")
_TRAILING_HASHTAG_RUN = re.compile(
    r"(?:\s+#[A-Za-z][\w]{1,})+\s*$",
)


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        tag = str(raw).strip().lstrip("#")
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def extract_inline_hashtags(caption: str) -> list[str]:
    return [m.group(1) for m in _HASHTAG_TOKEN.finditer(caption or "")]


def strip_hashtags_from_caption(caption: str) -> str:
    """Remove hashtag-only lines and trailing hashtag blocks from caption body."""
    text = (caption or "").strip()
    if not text:
        return ""

    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        remainder = _HASHTAG_TOKEN.sub("", stripped).strip()
        if not remainder and _HASHTAG_TOKEN.search(stripped):
            continue
        cleaned = _HASHTAG_TOKEN.sub("", line).rstrip()
        lines.append(cleaned)

    joined = "\n".join(lines).strip()
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    joined = _TRAILING_HASHTAG_RUN.sub("", joined).strip()
    joined = re.sub(r"[ \t]{2,}", " ", joined)
    return joined


def sanitize_caption_and_hashtags(
    caption: str,
    hashtags: list[str],
    *,
    hashtag_limit: int = 30,
) -> tuple[str, list[str]]:
    inline = extract_inline_hashtags(caption)
    clean_caption = strip_hashtags_from_caption(caption)
    merged = _dedupe_tags([*hashtags, *inline])[:hashtag_limit]
    return clean_caption, merged
