"""사용자·식습관·설정·동의 스키마 (명세서 4·11·12·13장)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import KSTDateTime

PrimaryGoal = Literal["lose_weight", "maintain", "gain_muscle", "gain_weight", "eat_healthy"]
ActivityLevel = Literal["sedentary", "light", "moderate", "active", "very_active"]
GoalPace = Literal["slow", "normal", "fast"]
FocusArea = Literal["protein", "overeating", "skipping", "balance"]


class UpdateMeRequest(BaseModel):
    nickname: str | None = Field(default=None, min_length=2, max_length=10)
    household_type: Literal["single", "multi", "none"] | None = None
    daily_goal_calories: int | None = Field(default=None, ge=500, le=10000)
    # 신체 정보 — 소셜 가입 사용자는 가입 시 입력하지 않으므로 온보딩에서 보완한다.
    gender: Literal["male", "female"] | None = None
    height: float | None = Field(default=None, ge=100, le=250)
    weight: float | None = Field(default=None, ge=20, le=300)
    birth_year: int | None = Field(default=None, ge=1900, le=2100)
    # "auto" 전송 시 직접 설정(manual) 목표를 버리고 BMR/TDEE 자동 산정으로 되돌린다
    goal_source: Literal["auto"] | None = None
    # 목표 세분화 (2026-10-09) — 왜 쓰는지·활동량·속도에 따라 목표 칼로리와 탄단지가 달라진다.
    primary_goal: PrimaryGoal | None = None
    activity_level: ActivityLevel | None = None
    # null 을 명시해 보내면 목표 체중을 지운다 (다른 필드는 null 이 '변경 없음')
    target_weight: float | None = Field(default=None, ge=20, le=300)
    goal_pace: GoalPace | None = None
    focus_areas: list[FocusArea] | None = Field(default=None, max_length=4)
    tutorial_completed_at: datetime | None = None


class GoalPlan(BaseModel):
    """자동 산정 목표의 근거 — 목표 설정 결과 화면이 그대로 보여 준다."""

    bmr: int
    tdee: int  # 유지 열량
    adjustment: int  # 유지 열량에 더한 하루 열량 (음수면 적자)
    weekly_change_kg: float  # 예상 주간 체중 변화 (부호 있음)
    weeks_to_target: int | None  # 목표 체중까지 예상 주 수
    floor_applied: bool  # 최소 권장 열량으로 받쳤는지


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
    birth_year: int | None
    # 목표 출처: auto(BMR 자동 산정) / manual(직접 설정). 프로필 없으면 null.
    goal_source: str | None
    # 목표 세분화 (2026-10-09). 설정 전에는 primary_goal 이 null — FE 가 목표 설정 화면 노출 판단에 쓴다.
    primary_goal: str | None = None
    activity_level: str | None = None
    target_weight: float | None = None
    goal_pace: str | None = None
    focus_areas: list[str] = []
    daily_goal_carbs: int | None = None
    daily_goal_protein: int | None = None
    daily_goal_fat: int | None = None
    # 신체정보가 없거나 칼로리를 직접 정한(manual) 사용자는 null
    goal_plan: GoalPlan | None = None
    tutorial_completed_at: KSTDateTime | None
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
