from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Iterator

from fastapi import Header, HTTPException

from app.core.config import settings


@dataclass(frozen=True)
class ModelSelection:
    text: str | None = None
    image: str | None = None


_selection: ContextVar[ModelSelection] = ContextVar("ai_model_selection", default=ModelSelection())
CATALOG_TTL_SECONDS = 120
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = Lock()


@dataclass(frozen=True)
class CuratedModel:
    id: str
    label: str
    provider: str
    profiles: tuple[str, ...] = ()
    profile_only: bool = False

    def candidates(self) -> tuple[str, ...]:
        region = settings.resolved_bedrock_region
        preferred = "eu" if region.startswith("eu-") else "us"
        if region in ("ap-southeast-2", "ap-southeast-4", "ap-southeast-6"):
            preferred = "au"
        elif region in ("ap-northeast-1", "ap-northeast-3"):
            preferred = "jp"
        elif region in ("ap-south-1", "ap-south-2"):
            preferred = "in"
        if preferred not in self.profiles and region.startswith("ap-") and "apac" in self.profiles:
            preferred = "apac"
        if region.startswith("ca-") and "ca" in self.profiles:
            preferred = "ca"
        prefixes = sorted(self.profiles, key=lambda p: (p != preferred, p != "global"))
        profiles = tuple(f"{prefix}.{self.id}" for prefix in prefixes)
        return profiles if self.profile_only else (*profiles, self.id)


_TEXT_GROUPS = (
    (
        CuratedModel("anthropic.claude-sonnet-4-6", "Claude Sonnet 4.6", "Anthropic", ("us", "eu", "au", "jp", "global")),
        CuratedModel("anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5", "Anthropic", ("us", "eu", "au", "jp", "global")),
    ),
    (CuratedModel("anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5", "Anthropic", ("us", "eu", "au", "jp", "in", "global"), True),),
    (CuratedModel("openai.gpt-oss-120b-1:0", "GPT-OSS 120B", "OpenAI"),),
    (CuratedModel("openai.gpt-oss-20b-1:0", "GPT-OSS 20B", "OpenAI"),),
    (CuratedModel("deepseek.r1-v1:0", "DeepSeek R1", "DeepSeek", ("us",)),),
    (CuratedModel("deepseek.v3-v1:0", "DeepSeek V3.1", "DeepSeek"),),
    (CuratedModel("amazon.nova-pro-v1:0", "Nova Pro", "Amazon", ("us", "eu", "apac")),),
    (CuratedModel("amazon.nova-lite-v1:0", "Nova Lite", "Amazon", ("us", "ca", "eu", "apac")),),
)
_IMAGE_MODELS = (
    CuratedModel("stability.sd3-5-large-v1:0", "Stable Diffusion 3.5 Large", "Stability AI"),
    CuratedModel("stability.stable-image-ultra-v1:1", "Stable Image Ultra", "Stability AI"),
    CuratedModel("stability.stable-image-core-v1:1", "Stable Image Core", "Stability AI"),
)
SUPPORTED_IMAGE_MODEL_IDS = frozenset(model.id for model in _IMAGE_MODELS)


def current_models() -> ModelSelection:
    return _selection.get()


@contextmanager
def selected_models(selection: ModelSelection) -> Iterator[None]:
    token = _selection.set(selection)
    try:
        yield
    finally:
        _selection.reset(token)


def image_region(model_id: str) -> str:
    explicit = (settings.bedrock_image_region or "").strip()
    if explicit:
        return explicit
    if model_id.startswith("stability."):
        return "us-west-2"
    return settings.resolved_bedrock_image_region


def _control_client(region: str) -> Any:
    import boto3
    from botocore.config import Config

    access = settings.resolved_bedrock_access_key_id
    secret = settings.resolved_bedrock_secret_access_key
    if not access or not secret:
        raise RuntimeError("Bedrock credentials are not configured")
    return boto3.client(
        "bedrock",
        region_name=region,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0}),
    )


def _discovery_error(exc: Exception) -> str:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return "access_denied" if code in ("AccessDeniedException", "UnauthorizedException") else "unavailable"


def _load_region(region: str) -> dict[str, Any]:
    result: dict[str, Any] = {"models": None, "profiles": None, "foundationModels": "unavailable", "inferenceProfiles": "unavailable"}
    try:
        client = _control_client(region)
    except Exception:
        return result
    try:
        response = client.list_foundation_models()
        result["models"] = {
            m["modelId"]: m
            for m in response.get("modelSummaries", [])
            if m.get("modelLifecycle", {}).get("status") in ("ACTIVE", "LEGACY")
            and "ON_DEMAND" in m.get("inferenceTypesSupported", [])
        }
        result["foundationModels"] = "ok"
    except Exception as exc:
        result["foundationModels"] = _discovery_error(exc)
    try:
        profiles = {}
        token = None
        while True:
            response = client.list_inference_profiles(**({"nextToken": token} if token else {}))
            profiles.update({
                p["inferenceProfileId"]: p
                for p in response.get("inferenceProfileSummaries", [])
                if p.get("status") == "ACTIVE" and p.get("type") == "SYSTEM_DEFINED"
            })
            token = response.get("nextToken")
            if not token:
                break
        result["profiles"] = profiles
        result["inferenceProfiles"] = "ok"
    except Exception as exc:
        result["inferenceProfiles"] = _discovery_error(exc)
    return result


def _discover_region(region: str) -> dict[str, Any]:
    credentials = f"{settings.resolved_bedrock_access_key_id}:{settings.resolved_bedrock_secret_access_key}"
    key = (region, hashlib.sha256(credentials.encode()).hexdigest())
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        result = _load_region(region)
        if len(_cache) >= 16:
            _cache.clear()
        _cache[key] = (time.monotonic() + CATALOG_TTL_SECONDS, result)
        return result


def _option(model: CuratedModel, model_id: str, region: str, status: str) -> dict[str, Any]:
    return {"id": model_id, "label": model.label, "provider": model.provider, "region": region, "availability": status, "invokeAccessVerified": False}


def get_model_catalog() -> dict[str, Any]:
    text_region = settings.resolved_bedrock_region
    regions = {text_region, *(image_region(m.id) for m in _IMAGE_MODELS)}
    discovery = {region: _discover_region(region) for region in sorted(regions)}
    text = []
    for group in _TEXT_GROUPS:
        options = []
        for model in group:
            for model_id in model.candidates():
                source = "models" if model_id == model.id else "profiles"
                found = discovery[text_region][source]
                if found is not None and model_id not in found:
                    continue
                if source == "models" and found is not None and "TEXT" not in found[model_id].get("outputModalities", []):
                    continue
                options.append(_option(model, model_id, text_region, "unknown" if found is None else "catalogued"))
        if options:
            text.append(next((o for o in options if o["availability"] == "catalogued"), options[0]))
    images = []
    for model in _IMAGE_MODELS:
        region = image_region(model.id)
        found = discovery[region]["models"]
        if found is None or (model.id in found and "IMAGE" in found[model.id].get("outputModalities", [])):
            option = _option(model, model.id, region, "unknown" if found is None else "catalogued")
            option["supportsImageVariation"] = model.id != "stability.stable-image-core-v1:1"
            images.append(option)
    return {
        "textModels": text,
        "imageModels": images,
        "defaultTextModelId": settings.bedrock_model_id or None,
        "defaultImageModelId": (settings.bedrock_image_model_id or "stability.sd3-5-large-v1:0").strip(),
        "discovery": [{"region": region, "foundationModels": data["foundationModels"], "inferenceProfiles": data["inferenceProfiles"]} for region, data in discovery.items()],
        "cacheTtlSeconds": CATALOG_TTL_SECONDS,
        "invokeAccessVerified": False,
        "message": "Catalog presence does not verify account invoke access. Unknown means discovery was unavailable or not permitted. Inference profile IDs may route across regions. No model invocations were made.",
    }


def validate_model_selection(
    text: str | None = Header(default=None, alias="X-AI-Text-Model"),
    image: str | None = Header(default=None, alias="X-AI-Image-Model"),
) -> ModelSelection:
    selection = ModelSelection(text=text, image=image)
    if text is None and image is None:
        return selection
    allowed_text = {candidate for group in _TEXT_GROUPS for model in group for candidate in model.candidates()}
    allowed_image = {model.id for model in _IMAGE_MODELS}
    for value, allowed, kind in ((text, allowed_text, "text"), (image, allowed_image, "image")):
        if value is not None and value not in allowed:
            raise HTTPException(status_code=422, detail=f"Unsupported AI {kind} model. Select an ID from /ai-models.")
    catalog = get_model_catalog()
    for value, key, kind in ((text, "textModels", "text"), (image, "imageModels", "image")):
        if value is not None and value not in {option["id"] for option in catalog[key]}:
            raise HTTPException(status_code=422, detail=f"AI {kind} model is not in the current regional catalog. Refresh /ai-models.")
    return selection