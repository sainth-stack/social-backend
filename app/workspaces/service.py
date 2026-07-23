from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.users.models import User
from app.workspaces.models import SocialLevel, Workspace, WorkspaceMember, WorkspacePlan, WorkspaceRole


def list_user_workspaces(db: Session, user: User) -> list[tuple[Workspace, WorkspaceMember]]:
    stmt = (
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == user.id)
        .join(WorkspaceMember.workspace)
    )
    memberships = db.execute(stmt).scalars().all()
    return [(m.workspace, m) for m in memberships]


def create_workspace(db: Session, owner: User, name: str) -> Workspace:
    workspace = Workspace(name=name, plan=WorkspacePlan.STARTER, owner_user_id=owner.id, is_active=True)
    db.add(workspace)
    db.flush()
    membership = WorkspaceMember(
        user_id=owner.id,
        workspace_id=workspace.id,
        role=WorkspaceRole.OWNER,
        social_level=SocialLevel.ADMIN,
    )
    db.add(membership)
    db.commit()
    return workspace


def list_members(db: Session, workspace_id: uuid.UUID) -> list[tuple[WorkspaceMember, User]]:
    stmt = (
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
    )
    return [(m, u) for m, u in db.execute(stmt).all()]


def invite_member(db: Session, workspace_id: uuid.UUID, email: str, role: str, social_level: str) -> WorkspaceMember:
    stmt = select(User).where(User.email == email.lower().strip())
    user = db.execute(stmt).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No user with that email")

    existing = db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member")

    try:
        role_enum = WorkspaceRole(role)
        level_enum = SocialLevel(social_level)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    member = WorkspaceMember(
        user_id=user.id, workspace_id=workspace_id, role=role_enum, social_level=level_enum
    )
    db.add(member)
    db.commit()
    return member


def update_member(
    db: Session,
    workspace_id: uuid.UUID,
    member_id: uuid.UUID,
    role: str | None,
    social_level: str | None,
) -> WorkspaceMember:
    member = db.get(WorkspaceMember, member_id)
    if not member or member.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    if role is not None:
        try:
            member.role = WorkspaceRole(role)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if social_level is not None:
        try:
            member.social_level = SocialLevel(social_level)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    db.commit()
    return member


def remove_member(db: Session, workspace_id: uuid.UUID, member_id: uuid.UUID) -> None:
    member = db.get(WorkspaceMember, member_id)
    if not member or member.workspace_id != workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    workspace = db.get(Workspace, workspace_id)
    if workspace and workspace.owner_user_id == member.user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the workspace owner")
    db.delete(member)
    db.commit()
