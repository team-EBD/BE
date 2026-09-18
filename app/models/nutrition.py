"""영양 도메인 모델 (ERD 1:1).

포함 테이블: nutrition_items(음식 영양 마스터), daily_nutrition_summaries(일간 요약)
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    JSON,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.food_taxonomy import FAMILIES
from app.models._common import created_at_column, pk_column, updated_at_column


class FoodGroup(Base):
    """음식군 — 3층 구조의 2층 (계열 > 군 > 상품). docs/음식군-DB-계약.md §1·§2.1

    식약처 대표식품명을 기준으로 만들고 동명 병합(버거=햄버거)한다. 추천은 군 단위로
    후보를 고르고, 카드 표시명은 사용자 이력의 상품명이 있으면 그것, 없으면 군명.
    role 은 계열 규칙으로 유도한다 (§5) — 사람이 라벨링하지 않는다.
    """

    __tablename__ = "food_groups"
    __table_args__ = (
        CheckConstraint("family IN (" + ",".join(repr(f) for f in FAMILIES) + ")", name="ck_food_groups_family"),
        CheckConstraint("role IN ('meal','companion','snack','exclude')", name="ck_food_groups_role"),
        CheckConstraint("member_count >= 0", name="ck_food_groups_member_count"),
        CheckConstraint("companion_group_id IS NULL OR (companion_group_id <> id AND role = 'meal')",
                        name="ck_food_groups_companion"),
    )

    id: Mapped[int] = pk_column()
    name: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    family: Mapped[str] = mapped_column(String(30), nullable=False, index=True)  # 계열 18개 (§4)
    role: Mapped[str] = mapped_column(String(12), nullable=False, index=True)  # meal|companion|snack|exclude
    # 기본 동반 군 (찌개 → 쌀밥). NULL = 없음 (버거·면·김밥)
    companion_group_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("food_groups.id", ondelete="SET NULL"), nullable=True
    )
    # 1인분 대표 영양값 — serving_basis=per_serving 구성원의 절사평균. NULL 이면 유사 후보 풀에서 제외
    calories: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    carbs: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    protein: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    fat: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    base_amount: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    base_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    source_names: Mapped[str | None] = mapped_column(Text, nullable=True)  # 병합 전 대표식품명 "버거|햄버거"
    note: Mapped[str | None] = mapped_column(Text, nullable=True)  # role 예외 등 수동 조정 이유
    created_at: Mapped[datetime] = created_at_column()


class FoodGroupAlias(Base):
    """이름 → 군. 사용자 기록 이름·시드·동의어를 군에 잇는다 (§2.2).

    alias 는 normalize_name() 을 적용한 키다 (공백·온도·사이즈 표기 제거) — 조회 측도 같은
    함수로 정규화해서 찍는다. kind: synonym | seed | manual | auto
    """

    __tablename__ = "food_group_aliases"
    __table_args__ = (
        CheckConstraint("kind IN ('synonym','seed','manual','auto')", name="ck_food_group_aliases_kind"),
    )

    alias: Mapped[str] = mapped_column(String(100), primary_key=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("food_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class NutritionItemPruned(Base):
    """삭제한 nutrition_items 행의 아카이브 (§2.6·§6). 되돌리기용.

    행 전체를 JSON 으로 보존한다 — 컬럼을 복제하면 원본 스키마가 바뀔 때마다 따라가야 한다.
    survivor_id 는 참조(meal_items 등)를 넘긴 대표 행.
    """

    __tablename__ = "nutrition_items_pruned"

    id: Mapped[int] = pk_column()
    original_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    survivor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)  # duplicate | exclude_family
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    pruned_at: Mapped[datetime] = created_at_column()


class NutritionItem(Base):
    __tablename__ = "nutrition_items"
    __table_args__ = (
        CheckConstraint("serving_basis IS NULL OR serving_basis IN ('per_serving','per_100g')",
                        name="ck_nutrition_items_serving_basis"),
    )

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
    # 음식군 (docs/음식군-DB-계약.md). NULL = 미분류 → 추천 후보에서 제외, 매칭·검색은 정상
    food_group_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("food_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 영양값 기준량 — per_serving(1인분 환산) | per_100g(원본 100g/100ml). NULL = 미판정.
    # 지금까지는 "대표 = 1인분"이라는 관례로만 구분했다 (김치찌개 19kcal 사고의 뿌리).
    serving_basis: Mapped[str | None] = mapped_column(String(12), nullable=True)


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
