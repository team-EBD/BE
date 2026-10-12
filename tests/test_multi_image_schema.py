"""다중 사진 마이그레이션·SQLite 제약 검증."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import AiCallLogImage, MealRecordImage


def test_multi_image_migration_backfill_and_roundtrip():
    path = Path(__file__).parents[1] / "alembic/versions/2026_10_12_1200-e3c7a9b2d804_multi_image_meals.py"
    spec = importlib.util.spec_from_file_location("multi_image", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "d9b2f6a4c713"
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        for sql in (
            "CREATE TABLE meal_images (id INTEGER PRIMARY KEY)",
            "CREATE TABLE meal_records (id INTEGER PRIMARY KEY, meal_image_id BIGINT REFERENCES meal_images(id) ON DELETE SET NULL, deleted_at TEXT)",
            "CREATE TABLE meal_items (id INTEGER PRIMARY KEY)",
            "CREATE TABLE ai_call_logs (id INTEGER PRIMARY KEY, meal_image_id BIGINT REFERENCES meal_images(id) ON DELETE SET NULL, input_type TEXT, task_type TEXT)",
            "INSERT INTO meal_images VALUES (1), (2)",
            "INSERT INTO meal_records VALUES (1,1,NULL), (2,2,'deleted'), (3,NULL,NULL)",
            "INSERT INTO meal_items VALUES (1)",
            "INSERT INTO ai_call_logs VALUES (1,1,'photo','analyze'), (2,NULL,'photo','analyze_clarifier'), (3,NULL,'text','analyze'), (4,NULL,NULL,'analyze'), (5,2,NULL,'analyze'), (6,1,'photo','recommend')",
        ):
            conn.execute(text(sql))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        migration.backfill()
        assert conn.execute(text("SELECT * FROM meal_record_images ORDER BY meal_record_id")).all() == [(1,1,0), (2,2,0)]
        assert conn.execute(text("SELECT input_image_count FROM ai_call_logs ORDER BY id")).scalars().all() == [1,1,None,None,1,None]
        assert conn.execute(text("SELECT ai_call_log_id, meal_image_id, position, status FROM ai_call_log_images ORDER BY id")).all() == [(1,1,0,'ok'), (2,None,0,'ok'), (5,2,0,'ok')]
        conn.execute(text("UPDATE meal_items SET bbox_image_id=1"))
        conn.execute(text("DELETE FROM meal_images WHERE id=1"))
        assert conn.scalar(text("SELECT COUNT(*) FROM meal_record_images")) == 1
        assert conn.scalar(text("SELECT COUNT(*) FROM ai_call_log_images")) == 3
        assert conn.scalar(text("SELECT meal_image_id FROM ai_call_log_images WHERE ai_call_log_id=1")) is None
        assert conn.scalar(text("SELECT bbox_image_id FROM meal_items")) is None
        conn.execute(text("DELETE FROM ai_call_logs WHERE id=5"))
        assert conn.scalar(text("SELECT COUNT(*) FROM ai_call_log_images")) == 2
        conn.execute(text("DELETE FROM meal_records WHERE id=2"))
        assert conn.scalar(text("SELECT COUNT(*) FROM meal_record_images")) == 0
        migration.downgrade()
        assert "bbox_image_id" not in {c['name'] for c in inspect(conn).get_columns('meal_items')}
        migration.upgrade()
    engine.dispose()


@pytest.mark.parametrize("model,values", [
    (MealRecordImage, {"meal_record_id": 1, "meal_image_id": 1, "position": 3}),
    (MealRecordImage, {"meal_record_id": 1, "meal_image_id": 1, "position": -1}),
    (AiCallLogImage, {"ai_call_log_id": 1, "position": 3, "status": "ok"}),
    (AiCallLogImage, {"ai_call_log_id": 1, "position": 0, "status": "bad"}),
])
def test_model_checks_reject_invalid_values(model, values):
    engine = create_engine("sqlite://")
    model.__table__.create(engine)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(model.__table__.insert().values(**values))
    engine.dispose()


@pytest.mark.parametrize('model,first,second', [
    (MealRecordImage, {'meal_record_id': 1, 'meal_image_id': 1, 'position': 0}, {'meal_record_id': 1, 'meal_image_id': 2, 'position': 0}),
    (MealRecordImage, {'meal_record_id': 1, 'meal_image_id': 1, 'position': 0}, {'meal_record_id': 1, 'meal_image_id': 1, 'position': 1}),
    (AiCallLogImage, {'ai_call_log_id': 1, 'position': 0, 'status': 'ok'}, {'ai_call_log_id': 1, 'position': 0, 'status': 'ok'}),
])
def test_model_unique_constraints(model, first, second):
    engine = create_engine('sqlite://')
    model.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(model.__table__.insert().values(**first))
        with pytest.raises(IntegrityError):
            conn.execute(model.__table__.insert().values(**second))
    engine.dispose()
