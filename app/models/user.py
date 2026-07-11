"""사용자·인증·설정·동의 도메인 모델 (ERD 1:1).

포함 테이블: users, refresh_tokens, user_profiles, eating_habits,
notification_settings, push_tokens, location_consents, terms_agreements
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = pk_column()
    social_provider: Mapped[str] = mapped_column(String(20), nullable=False)  # google/kakao/apple
    social_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    profile_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 이메일 가입 사용자만 사용 (소셜 전용 계정은 NULL)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("social_provider", "social_id", name="uq_users_provider_social_id"),
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
