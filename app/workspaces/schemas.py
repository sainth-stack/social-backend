from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    plan: str
    is_active: bool
    owner_user_id: uuid.UUID
    created_at: datetime

    class Config:
        from_attributes = True


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class WorkspaceUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)


class MemberOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    full_name: Optional[str]
    role: str
    social_level: str
    created_at: datetime


class MemberUpdateRequest(BaseModel):
    role: Optional[str] = None
    social_level: Optional[str] = None


class MemberInviteRequest(BaseModel):
    email: str
    role: str = "member"
    social_level: str = "viewer"
