"""사용자·인증·설정·동의 도메인 모델 (ERD 1:1).

포함 테이블: users, refresh_tokens, user_profiles, eating_habits,
notification_settings, push_tokens, location_consents, terms_agreements
"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import (
    TZDateTime,
    created_at_column,
    pk_column,
    updated_at_column,
)


class UserRole(StrEnum):
    """계정 역할 (users.role). 서버에서만 지정한다 — 어떤 API 로도 바꿀 수 없고 응답에도 싣지 않는다.

    지정 도구: pjt_eatlog/deploy_aws/set_user_roles.sh (운영 DB 에 직접 반영).

    - USER   : 일반 사용자 (기본값)
    - TESTER : 앱을 점검하는 팀원·테스터. AI 사용 한도(무료 10회·일일 한도)를 적용하지 않는다.
               구독 상태는 그대로라 구독·결제 화면은 일반 사용자처럼 시험할 수 있다.
               무료 사용자 화면(한도 소진 안내)을 시험하려면 USER 계정을 쓴다.
    - ADMIN  : 운영 관리자. 지금은 TESTER 와 같은 면제만 받는다 — 관리자 전용 API 가 생기면 이 값으로 구분한다.

    분석 대시보드는 TESTER·ADMIN 계정을 지표에서 제외한다 (dashboard/models/00_internal_users.sql).
    """

    USER = "user"
    TESTER = "tester"
    ADMIN = "admin"


# AI 사용 한도를 받지 않는 역할 (app/services/usage_limit.py)
AI_LIMIT_EXEMPT_ROLES = frozenset({UserRole.TESTER, UserRole.ADMIN})


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = pk_column()
    social_provider: Mapped[str] = mapped_column(String(20), nullable=False)  # google/kakao/apple
    social_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    # 닉네임 중복 허용용 식별 태그(0001~9999) — 표시형식 "닉네임#0001".
    # (닉네임, 태그) 쌍이 유일 → 닉네임 선점(스쿼팅)이 성립하지 않는다.
    nickname_tag: Mapped[str] = mapped_column(String(4), nullable=False, server_default="0000")
    profile_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 이메일 가입 사용자만 사용 (소셜 전용 계정은 NULL)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tutorial_completed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # 앱이 로그인·가입 요청에 실어 보내는 '테스트 기기' 여부 (2026-09-21 분석 로그).
    #   True  = 구글 플레이 사전 점검 로봇 등 Firebase Test Lab 기기 (안드로이드 설정 firebase.test.lab)
    #   False = 앱이 일반 기기라고 보고함 / NULL = 보고한 적 없음(이 기능 이전 빌드)
    # 분석 대시보드가 True 인 계정을 지표에서 제외한다. 기록 규칙은 app/services/test_device.py.
    is_test_device: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 계정 역할 user/tester/admin — UserRole 참고. 서버에서만 지정한다 (2026-09-22).
    role: Mapped[str] = mapped_column(
        String(10), nullable=False, default=UserRole.USER.value, server_default=UserRole.USER.value
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("social_provider", "social_id", name="uq_users_provider_social_id"),
        UniqueConstraint("nickname", "nickname_tag", name="uq_users_nickname_tag"),
        CheckConstraint("role IN ('user', 'tester', 'admin')", name="ck_users_role"),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    created_at: Mapped[datetime] = created_at_column()


class PasswordResetCode(Base):
    """비밀번호 재설정 인증코드 (이메일 가입 계정 전용).

    코드 원문은 저장하지 않고 SHA-256 해시만 남긴다 (refresh_tokens 와 동일 원칙).
    """

    __tablename__ = "password_reset_codes"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # 코드 불일치 시도 횟수 — 상한 초과 시 코드 폐기(무차별 대입 방지)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = created_at_column()


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    household_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # single/multi/none
    goal_calories: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_carbs: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_protein: Mapped[int] = mapped_column(Integer, nullable=False)
    goal_fat: Mapped[int] = mapped_column(Integer, nullable=False)
    # 목표 출처: auto(BMR/TDEE 자동 산정) / manual(사용자 직접 설정 — 자동 재계산 금지)
    goal_source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="auto", server_default="auto"
    )
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    weight: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    updated_at: Mapped[datetime] = updated_at_column()


class EatingHabit(Base):
    __tablename__ = "eating_habits"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    default_portion: Mapped[str | None] = mapped_column(String(10), nullable=True)  # small/normal/large
    soup_preference: Mapped[str | None] = mapped_column(String(10), nullable=True)  # eat/leave
    sauce_preference: Mapped[str | None] = mapped_column(String(10), nullable=True)  # eat/leave
    leftover_frequency: Mapped[str | None] = mapped_column(String(10), nullable=True)  # never/sometimes/often
    meal_goal: Mapped[str | None] = mapped_column(String(10), nullable=True)  # diet/bulk/maintain
    updated_at: Mapped[datetime] = updated_at_column()


class NotificationSetting(Base):
    __tablename__ = "notification_settings"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lunch_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:mm
    dinner_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:mm
    weekly_report_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = updated_at_column()


class PushToken(Base):
    __tablename__ = "push_tokens"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_id: Mapped[str] = mapped_column(String(255), nullable=False)
    push_token: Mapped[str] = mapped_column(String(500), nullable=False)
    platform: Mapped[str] = mapped_column(String(10), nullable=False)  # android/ios
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_push_tokens_user_device"),
    )


class LocationConsent(Base):
    __tablename__ = "location_consents"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    consent_status: Mapped[bool] = mapped_column(Boolean, nullable=False)
    consent_version: Mapped[str] = mapped_column(String(20), nullable=False)
    agreed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    updated_at: Mapped[datetime] = updated_at_column()


class TermsAgreement(Base):
    __tablename__ = "terms_agreements"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    terms_type: Mapped[str] = mapped_column(String(20), nullable=False)  # service/privacy/location
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    agreed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
