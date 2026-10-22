from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app.core.config import settings
from app.social.ai.compositor import OverlayCopy, compose_creative
from app.social.ai.creative import CreativeSpec, art_direct, build_visual_prompt, generation_size, render_creative, target_size

API = settings.api_v1_prefix


def _png(size=(1024, 1024), color=(40, 90, 160), mode="RGB") -> bytes:
    out = BytesIO()
    Image.new(mode, size, color).save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["post", "banner", "story", "carousel", "quote", "product"])
def test_compose_creative_exact_size(fmt):
    width, height = target_size(fmt, "linkedin")
    data = compose_creative(
        _png((1280, 720)),
        width=width,
        height=height,
        layout=fmt,
        copy=OverlayCopy(headline="Grow faster with smarter social", subtext="Plan, create and publish in one place", cta="Start free"),
        logo_bytes=_png((400, 120), (255, 0, 0, 255), "RGBA"),
        logo_position="bottom-right",
        primary_color="#1e3a8a",
        accent_color="#facc15",
        slide_label="2/5" if fmt == "carousel" else None,
    )
    img = Image.open(BytesIO(data))
    assert img.format == "JPEG"
    assert img.size == (width, height)


def test_target_and_generation_sizes():
    assert target_size("post", "instagram") == (1080, 1350)
    assert target_size("banner", "linkedin") == (1200, 627)
    assert target_size("story", None) == (1080, 1920)
    w, h = (int(v) for v in generation_size(1080, 1920).split("x"))
    assert w % 16 == 0 and h % 16 == 0 and max(w, h) == 1280


@pytest.fixture
def art_direction_response(monkeypatch):
    from app.social.ai import generator

    message = SimpleNamespace(content=json.dumps({
        "visual": "A gardener tending seedlings in warm morning light.",
        "headline": "Generated headline",
        "subtext": "Generated supporting line",
        "cta": "Learn more",
    }))
    completion = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
    monkeypatch.setattr(generator, "_get_llm", lambda: (object(), "test-text-model"))
    monkeypatch.setattr(generator, "_chat_completion", completion)
    return completion


def test_art_direct_grounded_in_full_brief_caption_brand_and_size(art_direction_response):
    topic = "A community garden. " * 35 + "Only show seedlings, no mature trees."
    caption = "Our neighborhood grows together. " * 25 + "Volunteers plant herbs this Saturday."
    spec = CreativeSpec(platform="instagram", caption=caption, text_overlay=True)
    visual, copy = art_direct(topic, spec, {
        "brand_name": "Green Neighbors", "industry": "Community gardening",
        "target_audience": "Local volunteers", "tones": ["Warm"],
        "primary_color": "#0f766e", "accent_color": "#f59e0b",
    }, "watercolor")
    messages = art_direction_response.call_args.kwargs["messages"]
    system, user = [message["content"] for message in messages]
    assert topic in user and caption in user
    assert "Green Neighbors" in user and "Local volunteers" in user and "Warm" in user
    assert "teal" in user and "orange" in user and "watercolor" in user
    assert "1080x1350" in user and "instagram" in user
    assert "4:5 portrait" in system
    assert "primary visual source" in system and "Do not invent" in system
    assert "UI or screenshots" in system
    assert "seedlings" in visual and copy.headline == "Generated headline"


def test_art_direct_manual_copy_wins(art_direction_response):
    spec = CreativeSpec(text_overlay=True, headline="Plant with us", subtext="Saturday in the garden", cta="Join us")
    _, copy = art_direct("Community gardening", spec, {}, None)
    assert copy == OverlayCopy(headline=spec.headline, subtext=spec.subtext, cta=spec.cta)
    prompt = art_direction_response.call_args.kwargs["messages"][1]["content"]
    assert spec.headline in prompt and spec.subtext in prompt and spec.cta in prompt


def test_art_direct_no_overlay_copy_when_disabled(art_direction_response):
    spec = CreativeSpec(headline="Hidden manual headline", subtext="Hidden subtext", cta="Hidden CTA")
    _, copy = art_direct("Community gardening", spec, {}, None)
    system, user = [message["content"] for message in art_direction_response.call_args.kwargs["messages"]]
    assert '{"visual":"..."}' in system
    assert "Text overlays are disabled" in system
    assert "lower third" not in system and "upper two-thirds" not in system
    assert spec.headline not in user and spec.subtext not in user and spec.cta not in user
    assert copy == OverlayCopy()


@pytest.mark.parametrize("platform,fmt,frame", [
    ("instagram", "post", "4:5 portrait"),
    ("linkedin", "post", "1:1 square"),
    ("twitter", "post", "16:9 landscape"),
    ("pinterest", "post", "2:3 portrait"),
    ("instagram", "story", "9:16 portrait"),
    ("linkedin", "carousel", "1:1 square"),
])
def test_visual_prompt_composition_matches_target_without_overlay(platform, fmt, frame):
    spec = CreativeSpec(platform=platform, format=fmt)
    prompt = build_visual_prompt("Garden volunteers", spec, {}, None)
    width, height = target_size(fmt, platform)
    assert f"{width}x{height}" in prompt and frame in prompt
    assert "lower third" not in prompt and "upper two-thirds" not in prompt
    assert "no reserved headline area" in prompt


def test_visual_prompt_reserves_overlay_space_only_when_enabled():
    prompt = build_visual_prompt("Garden volunteers", CreativeSpec(text_overlay=True), {}, None)
    assert "calm uncluttered lower third" in prompt


@pytest.mark.parametrize("directed", [False, True])
def test_render_preserves_full_brief_caption_and_safety_rules(monkeypatch, directed):
    from app.social.ai import creative

    topic = "A gardener among seedlings. " * 25 + "Only show green seedlings."
    caption = "We grow as a community. " * 30 + "Plant herbs with neighbors this Saturday."
    visual = "Morning light on young plants. " * 30 + "Keep the watering can visible."
    if directed:
        monkeypatch.setattr(creative, "art_direct", lambda *args: (visual, OverlayCopy()))
    else:
        monkeypatch.setattr(creative, "art_direct", Mock(side_effect=RuntimeError("offline")))
    generate = Mock(return_value={"imageB64": _png()})
    compose = Mock(return_value=b"composed")
    monkeypatch.setattr(creative, "generate_post_image", generate)
    monkeypatch.setattr(creative, "compose_creative", compose)
    result = render_creative(topic=topic, style=None, spec=CreativeSpec(caption=caption), brand={})
    prompt = generate.call_args.kwargs["prompt_override"]
    assert topic in prompt and caption in prompt
    assert "Absolutely no text, letters, numbers, logos, watermarks or UI elements." in prompt
    assert "without inventing claims" in prompt
    assert result.prompt == prompt and len(prompt) > 1000
    assert compose.call_args.kwargs["copy"] is None and result.copy == OverlayCopy()
    if directed:
        assert visual in prompt


def test_use_brand_false_strips_context_but_preserves_independent_logo(monkeypatch, art_direction_response):
    from app.social.ai import creative

    brand = {
        "brand_name": "Secret Brand", "industry": "Secret industry", "tagline": "Secret tagline",
        "target_audience": "Secret audience", "tones": ["Secret tone"],
        "words_to_use": ["Secret keyword"], "primary_color": "#0f766e", "accent_color": "#f59e0b",
        "primary_language": "Secret language",
    }
    spec = CreativeSpec(use_brand=False, include_logo=True)
    art_direct("Garden volunteers", spec, brand, None)
    messages = art_direction_response.call_args.kwargs["messages"]
    assert "Secret" not in str(messages) and "teal" not in str(messages)
    prompt = build_visual_prompt("Garden volunteers", spec, brand, None)
    assert "Secret" not in prompt and "teal" not in prompt and "orange" not in prompt
    generate = Mock(return_value={"imageB64": _png()})
    compose = Mock(return_value=b"composed")
    monkeypatch.setattr(creative, "generate_post_image", generate)
    monkeypatch.setattr(creative, "compose_creative", compose)
    logo = _png((100, 100))
    result = render_creative(topic="Garden volunteers", style=None, spec=spec, brand=brand, logo_bytes=logo)
    assert result.logo_applied
    assert compose.call_args.kwargs["logo_bytes"] == logo
    assert compose.call_args.kwargs["primary_color"] is None
    assert compose.call_args.kwargs["accent_color"] is None
    assert "Secret" not in generate.call_args.kwargs["prompt_override"]


@pytest.mark.parametrize("image_model", ["stability.sd3-5-large-v1:0", "stability.stable-image-ultra-v1:1", "stability.stable-image-core-v1:1"])
def test_render_uses_context_selected_text_and_image_models(monkeypatch, image_model):
    from app.providers.llm import bedrock_image, factory
    from app.providers.llm.model_selection import ModelSelection, current_models, selected_models

    text_model = "openai.gpt-oss-120b-1:0"
    text_runtime = Mock()
    text_runtime.converse.return_value = {"output": {"message": {"content": [{"text": json.dumps({
        "visual": "A gardener tending seedlings in warm light.",
    })}]}}}
    image_runtime = Mock()
    image_runtime.invoke_model.return_value = {"body": BytesIO(json.dumps({
        "images": [base64.b64encode(_png()).decode()],
    }).encode())}
    monkeypatch.setattr(factory, "get_bedrock_runtime_client", lambda **kwargs: text_runtime)
    monkeypatch.setattr(bedrock_image, "get_bedrock_runtime_client", lambda **kwargs: image_runtime)
    monkeypatch.setattr(settings, "bedrock_aws_access_key_id", "test-key")
    monkeypatch.setattr(settings, "bedrock_aws_secret_access_key", "test-secret")
    monkeypatch.setattr(settings, "bedrock_model_id", "different-text-default")
    monkeypatch.setattr(settings, "bedrock_image_model_id", "different-image-default")
    before = current_models()
    factory._get_llm_client.cache_clear()
    try:
        with selected_models(ModelSelection(text=text_model, image=image_model)):
            result = render_creative(
                topic="Garden volunteers", style=None,
                spec=CreativeSpec(platform="instagram", caption="Plant herbs this Saturday"), brand={},
            )
        assert text_runtime.converse.call_args.kwargs["modelId"] == text_model
        assert image_runtime.invoke_model.call_args.kwargs["modelId"] == image_model
        assert Image.open(BytesIO(result.image)).size == (1080, 1350)
        assert current_models() == before
    finally:
        factory._get_llm_client.cache_clear()


class _FakeStorage:
    def __init__(self, prefix: str = ""):
        self.objects: dict[str, bytes] = {}
        self.prefix = prefix

    def upload_bytes(self, key, data, content_type=None):
        self.objects[key] = data

    def presigned_get_url(self, key, expires_in=0):
        full = f"{self.prefix}/{key}" if self.prefix else key
        return f"https://bucket.s3.ap-south-1.amazonaws.com/{full}?X-Amz-Signature=abc"

    def download_bytes(self, key, bucket=None):
        return self.objects[key]

    def delete_object(self, key, bucket=None):
        self.objects.pop(key, None)


@pytest.fixture()
def fake_storage(monkeypatch):
    import app.providers.storage.factory as factory
    import app.social.media as media

    storage = _FakeStorage()
    monkeypatch.setattr(media, "get_storage_provider", lambda: storage)
    monkeypatch.setattr(factory, "get_storage_provider", lambda: storage)
    return storage


def test_generate_image_creative_with_brand(client, db, monkeypatch, fake_storage):
    import app.social.ai.creative as creative

    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return {"imageB64": _png((1024, 1280)), "source": "ai_generated"}

    def no_llm(*args, **kwargs):
        raise RuntimeError("llm off")

    monkeypatch.setattr(creative, "generate_post_image", fake_generate)
    monkeypatch.setattr(creative, "art_direct", no_llm)
    monkeypatch.setattr("app.social.limits.enforce_ai_image_limit", lambda db, ws: None)

    body = client.post(
        f"{API}/auth/register",
        json={"email": "creative@example.com", "password": "s3cret-pass"},
    ).json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    ws = body["workspace"]["id"]

    fake_storage.objects[f"logos/{ws}/logo.png"] = _png((300, 100), (0, 0, 0, 255), "RGBA")
    saved = client.put(
        f"{API}/workspaces/{ws}/social/brand-voice",
        headers=headers,
        json={
            "brandName": "Acme",
            "industry": "Fintech",
            "primaryColor": "#0f766e",
            "accentColor": "#f59e0b",
            "logoUrl": f"https://bucket.s3.amazonaws.com/logos/{ws}/logo.png",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["primaryColor"] == "#0f766e"

    res = client.post(
        f"{API}/workspaces/{ws}/social/generate-image",
        headers=headers,
        json={
            "topic": "Launch of our instant payouts feature. Get paid in seconds.",
            "format": "post",
            "platform": "instagram",
            "textOverlay": True,
            "logoPosition": "top-left",
        },
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert (out["width"], out["height"]) == (1080, 1350)
    assert out["logoApplied"] is True
    assert out["headline"]
    assert "Fintech" in captured["prompt_override"]
    assert captured["size"] == "1024x1280"
    key = out["imageUrl"].split(".com/", 1)[1].split("?", 1)[0]
    assert Image.open(BytesIO(fake_storage.objects[key])).size == (1080, 1350)


def test_logo_library_with_storage_prefix(client, db, monkeypatch, fake_storage):
    import app.social.ai.creative as creative

    fake_storage.prefix = "peers"
    monkeypatch.setattr(settings, "aws_storage_prefix", "peers")
    monkeypatch.setattr(
        creative,
        "generate_post_image",
        lambda **kwargs: {"imageB64": _png((1024, 1280)), "source": "ai_generated"},
    )
    monkeypatch.setattr(creative, "art_direct", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    monkeypatch.setattr("app.social.limits.enforce_ai_image_limit", lambda db, ws: None)

    body = client.post(
        f"{API}/auth/register",
        json={"email": "logos@example.com", "password": "s3cret-pass"},
    ).json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    base = f"{API}/workspaces/{body['workspace']['id']}/social"

    first = client.post(
        f"{base}/upload-logo",
        headers=headers,
        files={"file": ("primary.png", _png((300, 100), (0, 0, 0, 255), "RGBA"), "image/png")},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"{base}/upload-logo?setDefault=false",
        headers=headers,
        files={"file": ("mono.png", _png((200, 200), (255, 255, 255, 255), "RGBA"), "image/png")},
    )
    assert second.status_code == 200, second.text
    svg = client.post(
        f"{base}/upload-logo",
        headers=headers,
        files={"file": ("logo.svg", b"<svg/>", "image/svg+xml")},
    )
    assert svg.status_code == 400

    logos = client.get(f"{base}/brand-logos", headers=headers).json()["items"]
    assert [l["name"] for l in logos] == ["mono.png", "primary.png"]
    assert [l["isDefault"] for l in logos] == [False, True]

    for logo_id in (None, second.json()["id"]):
        res = client.post(
            f"{base}/generate-image",
            headers=headers,
            json={"topic": "Weekend flash sale on all plans", "format": "post", "logoId": logo_id},
        )
        assert res.status_code == 200, res.text
        assert res.json()["logoApplied"] is True

    after = client.delete(f"{base}/brand-logos/{first.json()['id']}", headers=headers).json()["items"]
    assert len(after) == 1 and after[0]["isDefault"] is True
    assert client.get(f"{base}/brand-voice", headers=headers).json()["logoUrl"]
