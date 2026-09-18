"""Phase 1 검증 — 모델 등록 + 시드 멱등성.

Postgres 없이 도는 것만 확인한다(SQLite in-memory). 실제 마이그레이션은
`alembic upgrade head`(Postgres)로 별도 확인.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, NutritionItem
from scripts.seed_nutrition_items import seed

EXPECTED_TABLES = {
    "users",
    "refresh_tokens",
    "password_reset_codes",
    "user_profiles",
    "eating_habits",
    "notification_settings",
    "push_tokens",
    "location_consents",
    "terms_agreements",
    "nutrition_items",
    "daily_nutrition_summaries",
    "meal_images",
    "meal_records",
    "meal_items",
    "correction_logs",
    "ai_call_logs",
    "food_candidates",
    "recommendation_logs",
    "recommendation_items",
    # 음식군 (docs/음식군-DB-계약.md) — 계열>군>상품 3층의 2층 + alias + 삭제 아카이브
    "food_groups",
    "food_group_aliases",
    "nutrition_items_pruned",
    # 계측(텔레메트리)
    "request_logs",
    "client_events",
    # 즐겨찾기
    "favorite_foods",
    # 인앱 결제(구독)
    "subscriptions",
    # 게이미피케이션 (함께 크는 펫)
    "game_profiles",
    "catalog_items",
    "user_items",
    "stage_placements",
    "unlock_progress",
    "reward_ledger",
    "pet_bonds",
    "user_skills",
    "skill_usage_ledger",
}


def test_all_tables_registered():
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES


def _sqlite_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_seed_loads_46_and_is_idempotent():
    factory = _sqlite_factory()

    first = seed(session_factory=factory)
    assert first["inserted"] == 46
    assert first["updated"] == 0
    assert first["total"] == 46

    # 재실행: 중복 생성 없이 46행 유지 (전부 update 경로)
    second = seed(session_factory=factory)
    assert second["inserted"] == 0
    assert second["updated"] == 46
    assert second["total"] == 46

    with factory() as s:
        assert s.query(NutritionItem).count() == 46
        kimchi = s.query(NutritionItem).filter_by(normalized_name="김치찌개").one()
        assert kimchi.category == "한식"
        assert float(kimchi.calories) == 320
