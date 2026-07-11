"""AI 도메인 모델 (ERD 1:1).

포함 테이블: ai_call_logs, food_candidates, recommendation_logs
- ai_call_logs: BE→AI 서버 호출 로깅. model_name/provider 등은 AI 서버 응답값 저장.
- food_candidates: AI 분석 원본 후보 + nutrition_items 매칭 결과.
- recommendation_logs: 추천 호출 컨텍스트/결과.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import created_at_column, pk_column


class AiCallLog(Base):
    __tablename__ = "ai_call_logs"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    meal_image_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meal_images.id", ondelete="SET NULL"), nullable=True
    )
    task_type: Mapped[str] = mapped_column(String(30), nullable=False)  # image_analysis/recommendation
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # success/timeout/error
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    token_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_estimate: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()


class FoodCandidate(Base):
    __tablename__ = "food_candidates"

    id: Mapped[int] = pk_column()
    meal_image_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meal_images.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ai_call_log_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_call_logs.id", ondelete="SET NULL"), nullable=True
    )
    nutrition_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("nutrition_items.id", ondelete="SET NULL"), nullable=True
    )
    food_name: Mapped[str] = mapped_column(String(100), nullable=False)  # AI raw
    normalized_name: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    estimated_serving: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-3
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_column()


class RecommendationLog(Base):
    __tablename__ = "recommendation_logs"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ai_call_log_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_call_logs.id", ondelete="SET NULL"), nullable=True
    )
    meal_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 입력 컨텍스트
    preferred_category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    recommendation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_items: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 3 menus+reason
    caution_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # FR-REC-003
    created_at: Mapped[datetime] = created_at_column()
