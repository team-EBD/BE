"""사진 삭제가 정답지나 입력 종류를 바꾸지 않는지 검증한다."""
import importlib.util
import io
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text

from app.ai_client import get_ai_client
from app.ai_client.base import failed_analyze
from app.ai_client.mock import MockAIClient
from app.main import app
from app.models import AiCallLog, FoodCandidate, MealImage
from tests.test_images import upload
from tests.test_meals import MEAL_PAYLOAD, create_meal


@pytest.mark.parametrize("mode", ["photo", "clarifier", "text"])
@pytest.mark.parametrize("failed", [False, True])
def test_input_snapshot_covers_success_failure_and_deleted_photo(
    client, auth_headers, db_factory, monkeypatch, mode, failed,
):
    class FailingAI(MockAIClient):
        def analyze(self, *args, **kwargs):
            result = failed_analyze("ai_timeout", latency_ms=100)
            if kwargs.get("candidate_depth") == "clarifier":
                result.ai_call_log.task_type = "analyze_clarifier"
            return result

        def parse_text(self, *args, **kwargs):
            return failed_analyze("no_candidates", latency_ms=1)

    if failed:
        app.dependency_overrides[get_ai_client] = lambda: FailingAI()
    image_id = None
    if mode == "text":
        res = client.post("/v1/meals/parse-text", headers=auth_headers, json={"text": "김밥 한 줄"})
    else:
        monkeypatch.setattr("app.services.analyze.candidate_depth", lambda *args: mode)
        image_id = upload(client, auth_headers).json()["meal_image_id"]
        res = client.post("/v1/meals/analyze", headers=auth_headers,
                          json={"meal_image_id": image_id, "text": "사진 설명도 사진 입력"})
    assert res.status_code == 200, res.text
    result = res.json()
    log_id = result["ai_call_log_id"]
    if not failed:
        chosen = result["candidates"][-1]
        create_meal(client, auth_headers, {
            **MEAL_PAYLOAD, "meal_image_id": image_id, "ai_call_log_id": log_id,
            "items": [{**MEAL_PAYLOAD["items"][0], "food_candidate_id": chosen["food_candidate_id"]}],
        })
    with db_factory() as db:
        log = db.get(AiCallLog, log_id)
        assert log.input_type == ("text" if mode == "text" else "photo")
        assert log.status == ("failed" if failed else "success")
        assert log.task_type == ("analyze_clarifier" if mode == "clarifier" else "analyze")
        before = _candidate_snapshot(db, log_id)
        if image_id is not None:
            db.delete(db.get(MealImage, image_id))
            db.commit()
    # 새 세션에서 읽어 ORM 캐시가 아닌 실제 FK 동작을 확인한다.
    with db_factory() as db:
        log = db.get(AiCallLog, log_id)
        assert log.meal_image_id is None
        assert log.input_type == ("text" if mode == "text" else "photo")
        assert _candidate_snapshot(db, log_id) == before
        rows = list(db.scalars(select(FoodCandidate).where(FoodCandidate.ai_call_log_id == log_id)))
        assert all(row.meal_image_id is None for row in rows)
        assert sum(row.is_selected for row in rows) == (0 if failed else 1)


def _candidate_snapshot(db, log_id):
    return db.execute(select(
        FoodCandidate.id, FoodCandidate.ai_call_log_id, FoodCandidate.food_name,
        FoodCandidate.rank, FoodCandidate.food_index, FoodCandidate.prediction_rank,
        FoodCandidate.is_selected,
    ).where(FoodCandidate.ai_call_log_id == log_id).order_by(FoodCandidate.id)).all()


def _migration():
    path = Path(__file__).parents[1] / "alembic/versions/2026_10_12_1100-d9b2f6a4c713_preserve_photo_analysis_logs.py"
    spec = importlib.util.spec_from_file_location("preserve_photo_analysis_logs", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "c8a1e5f3b602"
    return migration


@pytest.mark.parametrize("named_fk", [False, True])
def test_migration_replaces_existing_fk_and_only_backfills_known_photos(named_fk):
    migration = _migration()
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("CREATE TABLE meal_images (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE ai_call_logs (id INTEGER PRIMARY KEY, meal_image_id INTEGER "
                          "REFERENCES meal_images(id) ON DELETE SET NULL, task_type VARCHAR(30))"))
        constraint = "CONSTRAINT food_candidates_meal_image_id_fkey " if named_fk else ""
        conn.execute(text("CREATE TABLE food_candidates (id INTEGER PRIMARY KEY, meal_image_id INTEGER, "
                          "ai_call_log_id INTEGER REFERENCES ai_call_logs(id) ON DELETE SET NULL, "
                          "is_selected BOOLEAN, rank INTEGER, food_index SMALLINT, prediction_rank SMALLINT, "
                          + constraint + "FOREIGN KEY(meal_image_id) REFERENCES meal_images(id) ON DELETE CASCADE)"))
        conn.execute(text("INSERT INTO meal_images VALUES (1), (2)"))
        conn.execute(text("INSERT INTO ai_call_logs VALUES (1,1,'analyze'), (2,NULL,'analyze'), "
                          "(3,NULL,'analyze_clarifier'), (4,NULL,'recommend'), (5,2,'analyze_clarifier')"))
        conn.execute(text("INSERT INTO food_candidates VALUES (1,1,1,1,4,1,1), (2,2,5,0,1,NULL,NULL)"))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        assert conn.execute(text("SELECT input_type FROM ai_call_logs ORDER BY id")).scalars().all() == [
            "photo", None, None, None, "photo",
        ]
        fk = next(fk for fk in inspect(conn).get_foreign_keys("food_candidates")
                  if fk["constrained_columns"] == ["meal_image_id"])
        assert fk["options"]["ondelete"] == "SET NULL"
        conn.execute(text("DELETE FROM meal_images WHERE id=1"))
        assert conn.execute(text("SELECT meal_image_id, input_type FROM ai_call_logs WHERE id=1")).one() == (None, "photo")
        assert conn.execute(text("SELECT meal_image_id, ai_call_log_id, is_selected, rank, food_index, prediction_rank "
                                 "FROM food_candidates WHERE id=1")).one() == (None, 1, 1, 4, 1, 1)
        migration.downgrade()
        assert "input_type" not in {col["name"] for col in inspect(conn).get_columns("ai_call_logs")}
        conn.execute(text("DELETE FROM meal_images WHERE id=2"))
        assert conn.scalar(text("SELECT COUNT(*) FROM food_candidates WHERE id=2")) == 0
        migration.upgrade()
        assert conn.scalar(text("SELECT COUNT(*) FROM food_candidates")) == 1
    engine.dispose()


def test_postgres_migration_sql_drops_and_recreates_original_fk():
    migration = _migration()
    output = io.StringIO()
    migration.op = Operations(MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output},
    ))
    migration.upgrade()
    sql = output.getvalue()
    assert "DROP CONSTRAINT food_candidates_meal_image_id_fkey" in sql
    assert "FOREIGN KEY(meal_image_id) REFERENCES meal_images (id) ON DELETE SET NULL" in sql
    assert "WHERE meal_image_id IS NOT NULL" in sql
