from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.core.mongo_utils import new_id, utcnow
from app.social.permissions import get_user_permission
from app.social.polish import SocialPolishService

API = settings.api_v1_prefix


def _register(client, email):
    response = client.post(
        f"{API}/auth/register",
        json={"email": email, "password": "s3cret-pass", "full_name": "Owner"},
    )
    assert response.status_code == 201, response.text
    session = response.json()
    return session, {"Authorization": f"Bearer {session['access_token']}"}


def _add_member(db, workspace_id, user_id, level):
    db["workspace_members"].insert_one(
        {
            "id": new_id(),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "role": "member",
            "social_level": level,
            "created_at": utcnow(),
        }
    )


def test_settings_roundtrip_preserves_partial_updates_and_usage(client, db):
    session, headers = _register(client, "settings@example.com")
    workspace_id = session["workspace"]["id"]
    url = f"{API}/workspaces/{workspace_id}/social/settings"
    initial = client.get(url, headers=headers)
    assert initial.status_code == 200, initial.text
    assert db["social_settings"].count_documents({}) == 0
    assert initial.json()["hashtagCount"] == 5
    payload = {
        "timezone": "Europe/London",
        "defaultLanguage": "fr",
        "approvalRequired": False,
        "approverUserIds": [],
        "approvalSlaHours": 48,
        "approvalSlaAction": "none",
        "defaultPostingTimes": {"mon": ["10:30"], "tue": []},
        "queueGapMinutes": 45,
        "blackoutDates": ["2026-12-25"],
        "defaultTone": "Friendly",
        "defaultCta": "Learn more",
        "hashtagCount": 0,
        "autoFirstComment": True,
        "imageGenerationStyle": "Illustration",
        "openaiModel": initial.json()["openaiModel"],
        "systemPromptOverride": "Use concise captions",
        "enabledPlatforms": {"facebook": False, "instagram": True, "linkedin": False, "x": False},
        "notificationEvents": {"post_published": False, "post_failed": True},
        "notificationDelivery": "in_app",
    }
    updated = client.put(url, headers=headers, json=payload)
    assert updated.status_code == 200, updated.text
    body = updated.json()
    for key, value in payload.items():
        assert body[key] == value
    assert body["workspaceId"] == workspace_id
    assert set(body) == set(initial.json())
    usage = client.get(f"{API}/workspaces/{workspace_id}/social/usage", headers=headers)
    assert usage.status_code == 200, usage.text
    assert body["usage"] == usage.json()
    assert client.get(url, headers=headers).json() == body
    assert db["social_settings"].find_one({"workspace_id": workspace_id})["hashtag_count"] == 0

    patch = {"defaultCta": "", "autoFirstComment": False, "systemPromptOverride": None}
    response = client.put(url, headers=headers, json=patch)
    assert response.status_code == 200, response.text
    expected = {**body, **patch}
    assert response.json() == expected
    assert client.get(url, headers=headers).json() == expected
    audit = list(db["social_audit_logs"].find({"workspace_id": workspace_id}))
    assert len(audit) == 2
    assert all(row["action"] == "setting.updated" for row in audit)
    assert all(row["entity_id"] == body["id"] for row in audit)
    assert all(row["user_id"] == session["user"]["id"] for row in audit)


@pytest.mark.parametrize("level", ["viewer", "editor", "publisher", "admin"])
def test_settings_update_requires_social_admin(client, db, level):
    owner, _ = _register(client, "owner@example.com")
    member, headers = _register(client, "member@example.com")
    workspace_id = owner["workspace"]["id"]
    _add_member(db, workspace_id, member["user"]["id"], level)
    url = f"{API}/workspaces/{workspace_id}/social/settings"
    assert client.get(url, headers=headers).status_code == 200
    response = client.put(url, headers=headers, json={"defaultTone": "Friendly"})
    if level == "admin":
        assert response.status_code == 200, response.text
        assert response.json()["defaultTone"] == "Friendly"
        assert db["social_audit_logs"].count_documents({"workspace_id": workspace_id}) == 1
    else:
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Requires social permission: admin"
        assert db["social_settings"].count_documents({}) == 0
        assert db["social_audit_logs"].count_documents({}) == 0


def test_settings_workspace_isolation(client, db):
    first, first_headers = _register(client, "first@example.com")
    second, second_headers = _register(client, "second@example.com")
    first_id = first["workspace"]["id"]
    second_id = second["workspace"]["id"]
    first_url = f"{API}/workspaces/{first_id}/social/settings"
    second_url = f"{API}/workspaces/{second_id}/social/settings"
    for url, headers, tone in (
        (first_url, first_headers, "Friendly"),
        (second_url, second_headers, "Formal"),
    ):
        response = client.put(url, headers=headers, json={"defaultTone": tone})
        assert response.status_code == 200, response.text
    before = list(db["social_settings"].find({}))
    for url, headers in ((first_url, second_headers), (second_url, first_headers)):
        assert client.get(url, headers=headers).status_code == 403
        assert client.put(url, headers=headers, json={"defaultTone": "Wrong"}).status_code == 403
    assert list(db["social_settings"].find({})) == before
    assert client.get(first_url, headers=first_headers).json()["defaultTone"] == "Friendly"
    assert client.get(second_url, headers=second_headers).json()["defaultTone"] == "Formal"
    assert db["social_audit_logs"].count_documents({}) == 2


def test_settings_approval_plan_gate_is_unchanged(client, db):
    session, headers = _register(client, "approval@example.com")
    workspace_id = session["workspace"]["id"]
    url = f"{API}/workspaces/{workspace_id}/social/settings"
    response = client.put(url, headers=headers, json={"approvalRequired": True})
    assert response.status_code == 402, response.text
    assert response.json()["detail"]["code"] == "PLAN_LIMIT_APPROVAL"
    assert db["social_settings"].count_documents({}) == 0
    assert db["social_audit_logs"].count_documents({}) == 0


@pytest.mark.parametrize("stored", [None, 0, 3])
def test_settings_hashtag_count_fallback(db, stored):
    workspace = {"id": new_id(), "plan": "starter"}
    db["social_settings"].insert_one(
        {"id": new_id(), "workspace_id": workspace["id"], "hashtag_count": stored}
    )
    assert SocialPolishService(db).get_settings(workspace)["hashtagCount"] == (
        5 if stored is None else stored
    )


@pytest.mark.parametrize("member_count", [0, 1, 20])
@pytest.mark.parametrize("duplicate_memberships", [False, True])
def test_team_user_lookup_is_batched(db, monkeypatch, member_count, duplicate_memberships):
    workspace = {"id": new_id(), "owner_user_id": new_id()}
    levels = ["viewer", "editor", "publisher", "admin", "invalid", None, "missing"]
    users = []
    for index in range(member_count):
        user = {
            "id": workspace["owner_user_id"] if index == 0 else new_id(),
            "email": f"member{index}@example.com",
            "full_name": f"Member {index}" if index % 2 else "",
            "is_platform_admin": index == 1,
        }
        users.append(user)
        _add_member(db, workspace["id"], user["id"], levels[index % len(levels)])
        if index % len(levels) == 6:
            db["workspace_members"].update_one(
                {"workspace_id": workspace["id"], "user_id": user["id"]},
                {"$unset": {"social_level": ""}},
            )
    expected_users = list(users)
    if users:
        db["users"].insert_many(list(reversed(users)))
        _add_member(db, workspace["id"], new_id(), "admin")
        if duplicate_memberships:
            db["workspace_members"].drop_index("user_id_1_workspace_id_1")
            for user in reversed(users):
                level = get_user_permission(db, workspace, user).value
                _add_member(
                    db, workspace["id"], user["id"], "viewer" if level == "admin" else "admin"
                )
                expected_users.append(user)
    outsider = {"id": new_id(), "email": "outsider@example.com"}
    db["users"].insert_one(outsider)
    _add_member(db, new_id(), outsider["id"], "admin")
    expected = [
        {
            "userId": str(user["id"]),
            "name": user.get("full_name") or user["email"],
            "email": user["email"],
            "permission": get_user_permission(db, workspace, user).value,
        }
        for user in expected_users
    ]
    member_ids = [
        row["user_id"]
        for row in db["workspace_members"].find({"workspace_id": workspace["id"]})
    ]
    find = Mock(wraps=db["users"].find)
    find_one = Mock(wraps=db["users"].find_one)
    membership_find = Mock(wraps=db["workspace_members"].find)
    membership_find_one = Mock(wraps=db["workspace_members"].find_one)
    monkeypatch.setattr(db["users"], "find", find)
    monkeypatch.setattr(db["users"], "find_one", find_one)
    monkeypatch.setattr(db["workspace_members"], "find", membership_find)
    monkeypatch.setattr(db["workspace_members"], "find_one", membership_find_one)

    assert SocialPolishService(db).list_team_permissions(workspace) == expected

    find_one.assert_not_called()
    membership_find.assert_called_once_with({"workspace_id": workspace["id"]})
    membership_find_one.assert_not_called()
    if member_count:
        find.assert_called_once_with({"id": {"$in": member_ids}})
    else:
        find.assert_not_called()


def test_team_unknown_users_are_skipped(db):
    workspace = {"id": new_id()}
    _add_member(db, workspace["id"], new_id(), "admin")
    assert SocialPolishService(db).list_team_permissions(workspace) == []