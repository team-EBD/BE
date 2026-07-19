"""사용자·식습관·설정·동의 스키마 (명세서 4·11·12·13장)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import KSTDateTime


class UpdateMeRequest(BaseModel):
    nickname: str | None = Field(default=None, min_length=2, max_length=10)
    household_type: Literal["single", "multi", "none"] | None = None
    daily_goal_calories: int | None = Field(default=None, ge=500, le=10000)
    # 신체 정보 — 소셜 가입 사용자는 가입 시 입력하지 않으므로 온보딩에서 보완한다.
    gender: Literal["male", "female"] | None = None
    height: float | None = Field(default=None, ge=100, le=250)
    weight: float | None = Field(default=None, ge=20, le=300)


class MeDetailResponse(BaseModel):
    id: int
    email: str | None
    nickname: str
    nickname_tag: str  # 표시형식 "닉네임#0001"
    household_type: str | None
    daily_goal_calories: int | None
    # 신체 정보 (미입력 시 null — FE가 프로필 보완 화면 노출 판단에 사용)
    gender: str | None
    height: float | None
    weight: float | None
    created_at: KSTDateTime


# --- 식습관 (11장) ---

class EatingHabitsUpdateRequest(BaseModel):
    default_portion: Literal["small", "normal", "large"] | None = None
    soup_preference: Literal["eat", "leave"] | None = None
    sauce_preference: Literal["eat", "leave"] | None = None
    leftover_frequency: Literal["never", "sometimes", "often"] | None = None
    meal_goal: Literal["diet", "bulk", "maintain"] | None = None


class EatingHabitsResponse(BaseModel):
    default_portion: str
    soup_preference: str
    sauce_preference: str
    leftover_frequency: str
    meal_goal: str


# --- 알림 설정 (12.1) ---

class NotificationSettingsUpdateRequest(BaseModel):
    is_enabled: bool | None = None
    lunch_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    dinner_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekly_report_enabled: bool | None = None


class NotificationSettingsResponse(BaseModel):
    is_enabled: bool
    lunch_time: str | None
    dinner_time: str | None
    weekly_report_enabled: bool


# --- 푸시 토큰 (12.2) ---

class PushTokenRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=255)
    push_token: str = Field(min_length=1, max_length=500)
    platform: Literal["android", "ios"]


class PushTokenResponse(BaseModel):
    push_token_id: int


# --- 위치 동의 (13.1~13.2) ---

class LocationConsentCreateRequest(BaseModel):
    consent_status: bool
    consent_version: str = Field(min_length=1, max_length=20)


class LocationConsentUpdateRequest(BaseModel):
    consent_status: bool


class LocationConsentResponse(BaseModel):
    consent_status: bool
    consent_version: str
    agreed_at: KSTDateTime
    revoked_at: KSTDateTime | None


# --- 약관 동의 (13.3) ---

class TermsAgreementRequest(BaseModel):
    terms_type: Literal["service", "privacy", "location"]
    version: str = Field(min_length=1, max_length=20)
    agreed_at: KSTDateTime | None = None


class TermsAgreementResponse(BaseModel):
    terms_agreement_id: int
    terms_type: str
    version: str
    agreed_at: KSTDateTime
