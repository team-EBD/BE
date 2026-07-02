"""영양 도메인 모델 (ERD 1:1).

포함 테이블: nutrition_items(음식 영양 마스터), daily_nutrition_summaries(일간 요약)
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import pk_column, updated_at_column


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
