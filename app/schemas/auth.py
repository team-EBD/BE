"""인증 스키마 (명세서 3장)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import KSTDateTime

# email-validator 의존성 없이 쓰는 단순 이메일 형식 검사
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


class EmailSignupRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(min_length=2, max_length=20)
    height: float = Field(ge=100, le=250)  # cm
    weight: float = Field(ge=20, le=300)  # kg
    gender: Literal["male", "female"]

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        value = value.strip()
        if not EMAIL_PATTERN.match(value):
            raise ValueError("올바른 이메일 형식이 아닙니다.")
        return value

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
            raise ValueError("비밀번호는 영문자와 숫자를 각각 1자 이상 포함해야 합니다.")
        return value


class EmailLoginRequest(BaseModel):
    email: str
    password: str


class EmailAuthUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str | None
    nickname: str


class EmailAuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    user: EmailAuthUserOut


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
