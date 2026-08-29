"""영양 도메인 모델 (ERD 1:1).

포함 테이블: nutrition_items(음식 영양 마스터), daily_nutrition_summaries(일간 요약)
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import created_at_column, pk_column, updated_at_column


class NutritionItem(Base):
    __tablename__ = "nutrition_items"

    id: Mapped[int] = pk_column()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)  # 검색 키
    base_amount: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)  # 기준 제공량
    base_unit: Mapped[str] = mapped_column(String(20), nullable=False)  # g/serving
    calories: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    carbs: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    protein: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    fat: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="seed")  # seed/public
    # 공공 영양DB(전국통합식품영양성분정보) 확장 컬럼 — 시드 40건은 미보유라 전부 nullable
    sugar: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)  # 당류 g
    fiber: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)  # 식이섬유 g
    sodium: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)  # 나트륨 mg
    cholesterol: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)  # mg
    saturated_fat: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)  # g
    trans_fat: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)  # g
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 제조사/프랜차이즈명
    external_id: Mapped[str | None] = mapped_column(
        String(40), nullable=True, unique=True
    )  # 공공DB 식품코드 (적재 멱등키)
    total_weight: Mapped[float | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )  # 총 내용량(base_unit 기준, 예: 피자 1판 1640g)
    is_representative: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )  # 대표 음식(1인분 기준) — 검색 최상위 노출·AI 매칭 대상. 시드 + 큐레이션 선정분
    macros_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )  # 탄단지가 원본 실측이 아니라 적재 시 추정으로 채워진 행 (실측/추정 추적, 2026-08-05)


class FavoriteFood(Base):
    """즐겨찾기 음식 — 영양값 스냅샷 포함 (영양 DB 미매칭 음식도 등록 가능)."""

    __tablename__ = "favorite_foods"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nutrition_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("nutrition_items.id", ondelete="SET NULL"), nullable=True
    )
    food_name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_serving: Mapped[str] = mapped_column(String(50), nullable=False)
    calories: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    carbs: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    protein: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    fat: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("user_id", "food_name", name="uq_favorite_foods_user_food"),
    )


class DailyNutritionSummary(Base):
    __tablename__ = "daily_nutrition_summaries"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    summary_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_calories: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_carbs: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_protein: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_fat: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    meal_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # rule-based 문구
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("user_id", "summary_date", name="uq_daily_summary_user_date"),
    )
