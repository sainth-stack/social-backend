from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.core.config import settings
from app.social.ai.compositor import OverlayCopy, compose_creative
from app.social.ai.creative import generation_size, target_size

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
