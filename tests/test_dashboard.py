from __future__ import annotations

from app.core.config import settings

API = settings.api_v1_prefix


def _setup(client):
    body = client.post(
        f"{API}/auth/register",
        json={"email": "dash@example.com", "password": "s3cret-pass"},
    ).json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    return body["workspace"]["id"], headers


def test_dashboard_home_payload_shape(client):
    ws, headers = _setup(client)
    res = client.get(f"{API}/workspaces/{ws}/social/dashboard/home", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"stats", "activity", "recommendations", "scheduledPosts"}
    stats = body["stats"]
    for key in (
        "connectedAccounts",
        "expiredAccounts",
        "postsThisWeek",
        "totalReach",
        "avgEngagementRate",
        "usage",
        "accounts",
    ):
        assert key in stats
    assert stats["usage"]["accounts"]["used"] == 0
    assert len(body["recommendations"]) == 3


def test_dashboard_home_is_cached_and_invalidated_by_writes(client, db, fake_redis):
    ws, headers = _setup(client)
    url = f"{API}/workspaces/{ws}/social/dashboard/home"
    assert client.get(url, headers=headers).json()["stats"]["postsThisWeek"] == 0
    assert any(k.startswith(f"dash:home:{ws}:") for k in fake_redis.keys())

    from app.core.mongo_utils import new_id, utcnow

    now = utcnow()
    db["social_posts"].insert_one(
        {"id": new_id(), "workspace_id": ws, "title": "x", "status": "draft", "created_at": now, "updated_at": now}
    )
    assert client.get(url, headers=headers).json()["stats"]["postsThisWeek"] == 0

    res = client.put(
        f"{API}/workspaces/{ws}/social/settings", headers=headers, json={"defaultTone": "Friendly"}
    )
    assert res.status_code < 500, res.text
    assert client.get(url, headers=headers).json()["stats"]["postsThisWeek"] == 1


def test_usage_endpoint(client):
    ws, headers = _setup(client)
    res = client.get(f"{API}/workspaces/{ws}/social/usage", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["plan"] == "starter"
    assert body["postsThisMonth"]["used"] == 0
    assert body["aiTextThisMonth"]["used"] == 0


def test_dashboard_requires_membership(client):
    ws, _ = _setup(client)
    other = client.post(
        f"{API}/auth/register", json={"email": "other@example.com", "password": "s3cret-pass"}
    ).json()
    res = client.get(
        f"{API}/workspaces/{ws}/social/dashboard/home",
        headers={"Authorization": f"Bearer {other['access_token']}"},
    )
    assert res.status_code == 403
