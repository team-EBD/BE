"""호출 전체 순위와 음식별 순위를 혼동하지 않는 정답지 회귀 테스트."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text

from app.ai_client import get_ai_client
from app.ai_client.base import AICandidate
from app.ai_client.mock import MockAIClient
from app.main import app
from app.models import AiCallLog, FoodCandidate
from tests.test_images import upload
from tests.test_meals import MEAL_PAYLOAD, create_meal


@pytest.mark.parametrize("mode, expected", [
    ("photo", [(0, 1), (0, 2), (0, 3), (1, 1)]),
    ("clarifier", [(0, 1), (0, 2), (0, 3), (0, 4), (1, 1)]),
    ("text", [(0, 1), (1, 1)]),
])
def test_group_rank_and_second_food_top_choice(client, auth_headers, db_factory, monkeypatch, mode, expected):
    if mode == "text":
        res = client.post("/v1/meals/parse-text", headers=auth_headers, json={"text": "김밥과 라면"})
    else:
        monkeypatch.setattr("app.services.analyze.candidate_depth", lambda *args: mode)
        image_id = upload(client, auth_headers).json()["meal_image_id"]
        res = client.post("/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id})
    assert res.status_code == 200, res.text
    result = res.json()
    candidates = result["candidates"]
    chosen = [next(c for c in candidates if c["food_index"] == index) for index in (0, 1)]
    create_meal(client, auth_headers, {
        **MEAL_PAYLOAD,
        "ai_call_log_id": result["ai_call_log_id"],
        "items": [{**MEAL_PAYLOAD["items"][0], "food_name": c["normalized_name"],
                   "food_candidate_id": c["food_candidate_id"]} for c in chosen],
    })
    with db_factory() as db:
        rows = list(db.scalars(select(FoodCandidate).order_by(FoodCandidate.rank)))
        assert [(row.food_index, row.prediction_rank) for row in rows] == expected
        assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
        assert len({row.food_index for row in rows}) == 2
        assert sum(row.prediction_rank == 1 and row.is_selected for row in rows) == 2
        assert [row.prediction_rank for row in rows if row.is_selected] == [1, 1]
        log = db.get(AiCallLog, result["ai_call_log_id"])
        assert log.task_type == ("analyze_clarifier" if mode == "clarifier" else "analyze")


@pytest.mark.parametrize("limit", [3, 4])
def test_interleaved_groups_reindex_and_limit_ranks(client, auth_headers, db_factory, monkeypatch, limit):
    class InterleavedAI(MockAIClient):
        def analyze(self, *args, **kwargs):
            result = super().analyze(*args, **kwargs)
            result.candidates = [
                AICandidate(food_index=group, food_name=f"음식{group}-{rank}", confidence=0.8)
                for rank in range(6) for group in range(10, 16)
            ]
            return result

    monkeypatch.setattr("app.services.analyze.candidate_depth", lambda *args: "clarifier" if limit == 4 else "standard")
    app.dependency_overrides[get_ai_client] = lambda: InterleavedAI()
    image_id = upload(client, auth_headers).json()["meal_image_id"]
    res = client.post("/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id})
    assert res.status_code == 200, res.text
    with db_factory() as db:
        rows = list(db.scalars(select(FoodCandidate).order_by(FoodCandidate.rank)))
        assert [(row.food_index, row.prediction_rank) for row in rows] == [
            (group, rank) for rank in range(1, limit + 1) for group in range(5)
        ]
        assert [row.rank for row in rows] == list(range(1, 5 * limit + 1))


def test_legacy_ai_without_food_index_uses_one_group(client, auth_headers, db_factory):
    class LegacyAI(MockAIClient):
        def parse_text(self, *args, **kwargs):
            result = super().parse_text(*args, **kwargs)
            result.candidates = [AICandidate(food_name=f"음식{n}", confidence=0.8) for n in range(3)]
            return result

    app.dependency_overrides[get_ai_client] = lambda: LegacyAI()
    res = client.post("/v1/meals/parse-text", headers=auth_headers, json={"text": "음식"})
    assert res.status_code == 200, res.text
    with db_factory() as db:
        rows = list(db.scalars(select(FoodCandidate).order_by(FoodCandidate.rank)))
        assert [(row.food_index, row.prediction_rank) for row in rows] == [(0, 1), (0, 2), (0, 3)]


def test_migration_preserves_legacy_groups_and_global_rank_without_backfill():
    path = Path(__file__).parents[1] / "alembic/versions/2026_10_12_1000-c8a1e5f3b602_candidate_prediction_rank.py"
    spec = importlib.util.spec_from_file_location("candidate_prediction_rank", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "b4d6f8a1c3e5"
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE food_candidates (id INTEGER PRIMARY KEY, rank INTEGER NOT NULL, food_index SMALLINT)"))
        conn.execute(text("INSERT INTO food_candidates VALUES (1, 1, NULL), (2, 4, 1)"))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        assert conn.execute(text("SELECT rank, food_index, prediction_rank FROM food_candidates ORDER BY id")).all() == [
            (1, None, None), (4, 1, None),
        ]
        migration.downgrade()
        assert conn.execute(text("SELECT rank, food_index FROM food_candidates ORDER BY id")).all() == [(1, None), (4, 1)]
        migration.upgrade()
    engine.dispose()
