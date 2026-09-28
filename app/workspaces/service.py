from __future__ import annotations

from fastapi import HTTPException, status
from pymongo.database import Database

from app.core.mongo_utils import new_id, utcnow
from app.workspaces.models import SocialLevel, WorkspacePlan, WorkspaceRole


def list_user_workspaces(db: Database, user: dict) -> list[tuple[dict, dict]]:
    memberships = list(db["workspace_members"].find({"user_id": user["id"]}))
    result = []
    for m in memberships:
        workspace = db["workspaces"].find_one({"id": m["workspace_id"]})
        if workspace:
            result.append((workspace, m))
    return result


def create_workspace(db: Database, owner: dict, name: str) -> dict:
    now = utcnow()
    ws_id = new_id()
    workspace = {
        "id": ws_id,
        "name": name,
        "plan": WorkspacePlan.STARTER.value,
        "owner_user_id": owner["id"],
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    db["workspaces"].insert_one(workspace)

    db["workspace_members"].insert_one(
        {
            "id": new_id(),
            "user_id": owner["id"],
            "workspace_id": ws_id,
            "role": WorkspaceRole.OWNER.value,
            "social_level": SocialLevel.ADMIN.value,
            "created_at": now,
        }
    )
    return workspace


def list_members(db: Database, workspace_id: str) -> list[tuple[dict, dict]]:
    memberships = list(db["workspace_members"].find({"workspace_id": workspace_id}))
    result = []
    for m in memberships:
        user = db["users"].find_one({"id": m["user_id"]})
        if user:
            result.append((m, user))
    return result


def invite_member(
    db: Database,
    workspace_id: str,
    email: str,
    role: str,
    social_level: str,
) -> dict:
    user = db["users"].find_one({"email": email.lower().strip()})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No user with that email")

    existing = db["workspace_members"].find_one(
        {"workspace_id": workspace_id, "user_id": user["id"]}
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")

    try:
        role_enum = WorkspaceRole(role)
        level_enum = SocialLevel(social_level)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    member = {
        "id": new_id(),
        "user_id": user["id"],
        "workspace_id": workspace_id,
        "role": role_enum.value,
        "social_level": level_enum.value,
        "created_at": utcnow(),
    }
    db["workspace_members"].insert_one(member)
    return member


def update_member(
    db: Database,
    workspace_id: str,
    member_id: str,
    role: str | None,
    social_level: str | None,
) -> dict:
    member = db["workspace_members"].find_one({"id": member_id})
    if not member or member["workspace_id"] != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    updates: dict = {}
    if role is not None:
        try:
            updates["role"] = WorkspaceRole(role).value
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if social_level is not None:
        try:
            updates["social_level"] = SocialLevel(social_level).value
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if updates:
        db["workspace_members"].update_one({"id": member_id}, {"$set": updates})
        member = db["workspace_members"].find_one({"id": member_id})
    return member


def remove_member(db: Database, workspace_id: str, member_id: str) -> None:
    member = db["workspace_members"].find_one({"id": member_id})
    if not member or member["workspace_id"] != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    workspace = db["workspaces"].find_one({"id": workspace_id})
    if workspace and workspace.get("owner_user_id") == member["user_id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove the workspace owner",
        )
    db["workspace_members"].delete_one({"id": member_id})
