"""인증 스키마 (명세서 3장)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import KSTDateTime

# email-validator 의존성 없이 쓰는 단순 이메일 형식 검사.
# TLD 는 2자 이상 요구(통용 규칙) — 진짜 유효성은 이메일 인증(추후)으로만 보장된다.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


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
    # 길이 검사도 custom validator 에서 수행 — 어떤 위반이든 한국어 안내 한 문장으로 응답되게 한다.
    password: str
    nickname: str = Field(min_length=2, max_length=10)
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
        if len(value) > 128:
            raise ValueError("비밀번호는 128자 이하여야 합니다.")
        if (
            len(value) < 8
            or not re.search(r"[A-Za-z]", value)
            or not re.search(r"\d", value)
        ):
            raise ValueError("비밀번호는 영문자와 숫자를 포함해 8자 이상이어야 합니다.")
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
