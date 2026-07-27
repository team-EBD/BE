"""식단 도메인 모델 (ERD 1:1).

포함 테이블: meal_images, meal_records, meal_items, correction_logs
- meal_records 는 soft delete(`deleted_at`) 사용.
- meal 삭제 시 하위 items/correction_logs 는 CASCADE.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import TZDateTime, created_at_column, pk_column


class MealImage(Base):
    __tablename__ = "meal_images"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    image_url: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)  # camera/gallery
    taken_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )


class MealRecord(Base):
    __tablename__ = "meal_records"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meal_image_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meal_images.id", ondelete="SET NULL"), nullable=True
    )
    meal_type: Mapped[str] = mapped_column(String(10), nullable=False)  # breakfast/lunch/dinner/snack
    eaten_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    is_skipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_false()
    )  # 식사 생략(안 먹음) 기록 — items 없이 저장
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_calories: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)  # 합계 캐시
    total_carbs: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_protein: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_fat: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)  # soft delete


class MealItem(Base):
    __tablename__ = "meal_items"

    id: Mapped[int] = pk_column()
    meal_record_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meal_records.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nutrition_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("nutrition_items.id", ondelete="SET NULL"), nullable=True
    )
    food_name: Mapped[str] = mapped_column(String(100), nullable=False)  # 스냅샷
    serving_amount: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)  # factor/gram
    calories: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)  # 보정 반영값
    carbs: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    protein: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    fat: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    # AI 가 준 사진 속 위치 스냅샷 {x, y, width, height} (0.0~1.0). 직접 검색으로
    # 담은 음식이나 구버전 기록은 NULL — 확대 보기에서 오버레이만 생략된다.
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = created_at_column()


class CorrectionLog(Base):
    __tablename__ = "correction_logs"

    id: Mapped[int] = pk_column()
    meal_record_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meal_records.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meal_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meal_items.id", ondelete="SET NULL"), nullable=True
    )
    correction_type: Mapped[str] = mapped_column(String(20), nullable=False)  # half/large/no_soup/...
    before_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
