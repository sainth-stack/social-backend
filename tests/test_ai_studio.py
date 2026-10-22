from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.mongo_utils import new_id, utcnow

API = settings.api_v1_prefix


def _setup(client):
    body = client.post(
        f"{API}/auth/register",
        json={"email": "studio@example.com", "password": "s3cret-pass"},
    ).json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    return body["workspace"]["id"], headers


def _ai_usage(db, ws):
    return db["plans_ai_usage"].count_documents({"workspace_id": ws})


def _fake_platform_content(**kwargs):
    return {
        p: {"caption": f"Caption for {p}", "hashtags": ["brand"], "firstComment": "", "characterCount": 20}
        for p in kwargs["platforms"]
    }


def test_generate_post_records_usage_on_success(client, db, monkeypatch):
    import app.social.ai.generator as generator

    monkeypatch.setattr(generator, "generate_platform_content", _fake_platform_content)
    ws, headers = _setup(client)
    res = client.post(
        f"{API}/workspaces/{ws}/social/generate",
        headers=headers,
        json={"topic": "Launch our spring collection", "platforms": ["linkedin", "facebook"]},
    )
    assert res.status_code == 200, res.text
    assert set(res.json()["platforms"]) == {"linkedin", "facebook"}
    assert _ai_usage(db, ws) == 1


def test_generate_post_failure_does_not_consume_quota(client, db, monkeypatch):
    import app.social.ai.generator as generator

    def boom(**kwargs):
        raise HTTPException(status_code=502, detail="AI generation failed")

    monkeypatch.setattr(generator, "generate_platform_content", boom)
    ws, headers = _setup(client)
    res = client.post(
        f"{API}/workspaces/{ws}/social/generate",
        headers=headers,
        json={"topic": "Launch our spring collection", "platforms": ["linkedin"]},
    )
    assert res.status_code == 502
    assert _ai_usage(db, ws) == 0


def test_brand_voice_upsert_roundtrip(client, db):
    ws, headers = _setup(client)
    url = f"{API}/workspaces/{ws}/social/brand-voice"
    assert client.get(url, headers=headers).json()["brandName"] == ""

    first = client.put(url, headers=headers, json={"brandName": "Acme", "tones": ["Friendly"]})
    assert first.status_code == 200, first.text
    second = client.put(url, headers=headers, json={"brandName": "Acme 2", "industry": "Retail"})
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert client.get(url, headers=headers).json()["industry"] == "Retail"
    assert db["social_brand_voices"].count_documents({"workspace_id": ws}) == 1


@pytest.fixture()
def fake_presign(monkeypatch):
    import app.social.media as media

    monkeypatch.setattr(media, "refresh_blob_url", lambda key: f"https://cdn.test/{key}")


def _asset(db, ws, media_type, size, prompt=None, deleted=False):
    now = utcnow()
    doc = {
        "id": new_id(),
        "workspace_id": ws,
        "media_type": media_type,
        "source": "uploaded",
        "blob_key": f"k/{new_id()}",
        "mime_type": "image/png" if media_type == "image" else "video/mp4",
        "file_size_bytes": size,
        "prompt": prompt,
        "is_deleted": deleted,
        "created_at": now,
        "updated_at": now,
    }
    db["social_media_assets"].insert_one(doc)
    return doc["id"]


def test_media_list_stats_filter_and_search(client, db, fake_presign):
    ws, headers = _setup(client)
    _asset(db, ws, "image", 100, prompt="Sunset (beach)")
    _asset(db, ws, "image", 200, prompt="Office")
    _asset(db, ws, "video", 1000)
    _asset(db, ws, "image", 999, deleted=True)
    url = f"{API}/workspaces/{ws}/social/media-assets"

    body = client.get(url, headers=headers, params={"mediaType": "video"}).json()
    assert body["total"] == 1
    assert body["stats"] == {"images": 2, "videos": 1, "totalBytes": 1300}

    found = client.get(url, headers=headers, params={"search": "(beach"}).json()
    assert found["total"] == 1
    assert found["items"][0]["prompt"] == "Sunset (beach)"


def test_media_delete_and_missing(client, db, fake_presign):
    ws, headers = _setup(client)
    asset_id = _asset(db, ws, "image", 10)
    url = f"{API}/workspaces/{ws}/social/media-assets/{asset_id}"
    assert client.delete(url, headers=headers).status_code == 204
    assert client.delete(url, headers=headers).status_code == 404
    assert client.get(url, headers=headers).status_code == 404


def test_media_pagination_uses_one_aggregation(client, db, fake_presign, monkeypatch):
    ws, headers = _setup(client)
    for index in range(5):
        _asset(db, ws, "image", 100, prompt=f"Image {index}")
    _asset(db, "another-workspace", "video", 9000)
    collection = db["social_media_assets"]
    aggregate = collection.aggregate
    calls = []

    def tracked(pipeline, *args, **kwargs):
        calls.append(pipeline)
        return aggregate(pipeline, *args, **kwargs)

    monkeypatch.setattr(collection, "aggregate", tracked)
    url = f"{API}/workspaces/{ws}/social/media-assets"
    first = client.get(url, headers=headers, params={"page": 1, "pageSize": 2}).json()
    assert len(calls) == 1
    assert first["total"] == 5
    assert first["totalPages"] == 3
    assert len(first["items"]) == 2
    assert first["stats"] == {"images": 5, "videos": 0, "totalBytes": 500}
    second = client.get(url, headers=headers, params={"page": 2, "pageSize": 2}).json()
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    last = client.get(url, headers=headers, params={"page": 3, "pageSize": 2}).json()
    assert len(last["items"]) == 1


def test_media_empty_filter_preserves_workspace_stats(client, db, fake_presign):
    ws, headers = _setup(client)
    _asset(db, ws, "image", 120)
    url = f"{API}/workspaces/{ws}/social/media-assets"
    body = client.get(url, headers=headers, params={"source": "ai_generated"}).json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["totalPages"] == 0
    assert body["stats"] == {"images": 1, "videos": 0, "totalBytes": 120}


def test_media_empty_workspace(client, db, fake_presign):
    ws, headers = _setup(client)
    response = client.get(f"{API}/workspaces/{ws}/social/media-assets", headers=headers)
    assert response.status_code == 200
    assert response.json()["stats"] == {"images": 0, "videos": 0, "totalBytes": 0}
    assert response.json()["items"] == []
