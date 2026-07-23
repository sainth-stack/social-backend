from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: Optional[str] = Field(default=None, max_length=160)
    workspace_name: Optional[str] = Field(default=None, max_length=160)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str]
    is_active: bool
    is_platform_admin: bool
    created_at: datetime

    class Config:
        from_attributes = True


class WorkspaceSummaryOut(BaseModel):
    id: uuid.UUID
    name: str
    plan: str
    role: str
    social_level: str

    class Config:
        from_attributes = True


class MeResponse(BaseModel):
    user: UserOut
    workspaces: list[WorkspaceSummaryOut]


class RegisterResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
    workspace: WorkspaceSummaryOut
