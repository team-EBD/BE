"""인증 스키마 (명세서 3장)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.common import KSTDateTime


class SocialLoginRequest(BaseModel):
    provider: str  # google (확장: kakao/apple)
    token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    social_provider: str
    email: str | None
    nickname: str
    profile_image_url: str | None


class SocialLoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: UserOut


class RefreshRequest(BaseModel):
    refresh_token: str


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str | None
    nickname: str
    created_at: KSTDateTime
