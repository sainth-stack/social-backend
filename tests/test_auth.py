from __future__ import annotations

from app.core.config import settings

API = settings.api_v1_prefix


def _register(client, email="owner@example.com", password="s3cret-pass"):
    return client.post(
        f"{API}/auth/register",
        json={"email": email, "password": password, "full_name": "Owner"},
    )


def test_register_returns_session_and_sends_welcome_in_background(client):
    res = _register(client)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["access_token"]
    assert body["user"]["email"] == "owner@example.com"
    assert len(body["workspaces"]) == 1
    assert body["workspace"]["id"] == body["workspaces"][0]["id"]
    assert [kind for kind, _ in client.sent_emails] == ["welcome"]


def test_register_duplicate_email_conflicts(client):
    assert _register(client).status_code == 201
    assert _register(client).status_code == 409


def test_login_returns_user_and_workspaces(client):
    _register(client)
    res = client.post(
        f"{API}/auth/login", json={"email": "Owner@Example.com", "password": "s3cret-pass"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["access_token"]
    assert body["user"]["email"] == "owner@example.com"
    assert len(body["workspaces"]) == 1


def test_login_rejects_bad_credentials(client):
    _register(client)
    bad = client.post(f"{API}/auth/login", json={"email": "owner@example.com", "password": "nope-nope"})
    unknown = client.post(f"{API}/auth/login", json={"email": "ghost@example.com", "password": "x" * 9})
    assert bad.status_code == 401
    assert unknown.status_code == 401


def test_me_uses_access_token(client):
    token = _register(client).json()["access_token"]
    res = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert "password_hash" not in res.json()["user"]
    assert len(res.json()["workspaces"]) == 1


def test_forgot_password_sends_email_in_background(client):
    _register(client)
    client.sent_emails.clear()
    res = client.post(f"{API}/auth/forgot-password", json={"email": "owner@example.com"})
    assert res.status_code == 200
    assert [kind for kind, _ in client.sent_emails] == ["reset"]
