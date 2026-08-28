from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.users.models import User
from app.workspaces import service
from app.workspaces.deps import require_social_level, require_workspace_access
from app.workspaces.models import SocialLevel, Workspace
from app.workspaces.schemas import (
    MemberInviteRequest,
    MemberOut,
    MemberUpdateRequest,
    WorkspaceCreateRequest,
    WorkspaceOut,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceOut])
def list_my_workspaces(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[WorkspaceOut]:
    pairs = service.list_user_workspaces(db, current_user)
    return [WorkspaceOut.model_validate(w) for w, _m in pairs]


@router.post("", response_model=WorkspaceOut, status_code=201)
def create_workspace(
    payload: WorkspaceCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceOut:
    workspace = service.create_workspace(db, current_user, payload.name)
    return WorkspaceOut.model_validate(workspace)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(workspace: Workspace = Depends(require_workspace_access)) -> WorkspaceOut:
    return WorkspaceOut.model_validate(workspace)


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
def list_members(
    workspace_id: uuid.UUID,
    workspace: Workspace = Depends(require_workspace_access),
    db: Session = Depends(get_db),
) -> list[MemberOut]:
    pairs = service.list_members(db, workspace_id)
    return [
        MemberOut(
            id=m.id,
            user_id=u.id,
            email=u.email,
            full_name=u.full_name,
            role=m.role.value,
            social_level=m.social_level.value,
            created_at=m.created_at,
        )
        for m, u in pairs
    ]


@router.post("/{workspace_id}/members", response_model=MemberOut, status_code=201)
def invite_member(
    workspace_id: uuid.UUID,
    payload: MemberInviteRequest,
    _member=Depends(require_social_level(SocialLevel.ADMIN)),
    db: Session = Depends(get_db),
) -> MemberOut:
    member = service.invite_member(db, workspace_id, payload.email, payload.role, payload.social_level)
    user = db.get(User, member.user_id)
    return MemberOut(
        id=member.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=member.role.value,
        social_level=member.social_level.value,
        created_at=member.created_at,
    )


@router.patch("/{workspace_id}/members/{member_id}", response_model=MemberOut)
def update_member(
    workspace_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: MemberUpdateRequest,
    _member=Depends(require_social_level(SocialLevel.ADMIN)),
    db: Session = Depends(get_db),
) -> MemberOut:
    member = service.update_member(db, workspace_id, member_id, payload.role, payload.social_level)
    user = db.get(User, member.user_id)
    return MemberOut(
        id=member.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=member.role.value,
        social_level=member.social_level.value,
        created_at=member.created_at,
    )


@router.delete("/{workspace_id}/members/{member_id}", status_code=204)
def remove_member(
    workspace_id: uuid.UUID,
    member_id: uuid.UUID,
    _member=Depends(require_social_level(SocialLevel.ADMIN)),
    db: Session = Depends(get_db),
) -> None:
    service.remove_member(db, workspace_id, member_id)
