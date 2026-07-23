"""Social template seed loading and placeholder resolution."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

SOCIAL_TEMPLATES_FILE = (
    Path(__file__).resolve().parents[2] / "data" / "social-templates.json"
)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def load_social_template_seed_rows() -> list[dict[str, Any]]:
    if not SOCIAL_TEMPLATES_FILE.exists():
        return []
    with SOCIAL_TEMPLATES_FILE.open(encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise ValueError(f"{SOCIAL_TEMPLATES_FILE} must contain a JSON array")
    return rows


def extract_placeholder_keys(text: str) -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text or ""):
        key = match.group(1)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def resolve_template_text(text: str, values: dict[str, str]) -> str:
    if not text:
        return ""

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(replacer, text)


def build_default_values(
    template_row: dict[str, Any],
    *,
    brand_name: str = "",
    industry: str = "",
    organization_name: str = "",
) -> dict[str, str]:
    """Pre-fill placeholder values from brand voice and org context."""
    defaults: dict[str, str] = {}
    company = brand_name or organization_name
    if company:
        defaults["company_name"] = company
    if industry:
        defaults["target_audience"] = f"businesses in {industry}"

    for item in template_row.get("placeholders") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        example = str(item.get("example", "")).strip()
        if key and example and key not in defaults:
            defaults[key] = example
    return defaults


def merge_placeholder_values(
    template_row: dict[str, Any],
    user_values: Optional[dict[str, str]] = None,
    *,
    brand_name: str = "",
    industry: str = "",
    organization_name: str = "",
) -> dict[str, str]:
    merged = build_default_values(
        template_row,
        brand_name=brand_name,
        industry=industry,
        organization_name=organization_name,
    )
    if user_values:
        for key, value in user_values.items():
            if value is not None and str(value).strip():
                merged[key] = str(value).strip()
    return merged


def apply_template_fields(
    template_row: dict[str, Any],
    values: dict[str, str],
) -> dict[str, Any]:
    caption = resolve_template_text(str(template_row.get("captionTemplate", "")), values)
    image_prompt = resolve_template_text(str(template_row.get("imagePrompt", "")), values)
    first_comment = resolve_template_text(
        str(template_row.get("firstCommentTemplate", "") or ""), values
    )
    suggested_cta = resolve_template_text(
        str(template_row.get("suggestedCta", "") or ""), values
    )
    hashtags = list(template_row.get("hashtags") or [])
    return {
        "captionTemplate": caption,
        "imagePrompt": image_prompt,
        "firstComment": first_comment,
        "suggestedCta": suggested_cta,
        "hashtags": hashtags,
        "topic": caption or str(template_row.get("name", "")),
    }
