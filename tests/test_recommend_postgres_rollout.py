"""Opt-in PostgreSQL 배포 검증. 운영 URL은 받지 않으며 매번 임시 schema만 만든다.

EATLOG_TEST_POSTGRES_URL=postgresql+psycopg://...@127.0.0.1:.../eatlog_test_rollout
python -m pytest tests/test_recommend_postgres_rollout.py -q

앱의 DATABASE_URL/ALEMBIC_DATABASE_URL은 사용하지 않는다. 미지정 시 skip한다.
"""
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from app.ai_client import get_ai_client
from app.ai_client.mock import MockAIClient
from app.core.config import settings
from app.core.database import get_db
from app.core.migrations import _MIGRATION_LOCK_KEY, _alembic_config, migration_connection
from app.core.security import create_access_token
from app.main import app
from app.models import RecommendationItem

PREVIOUS = "e1e74fab8c35"
HEAD = "e3a96bcd0e57"


@pytest.fixture()
def pg_rollout(monkeypatch):
    raw = os.getenv("EATLOG_TEST_POSTGRES_URL")
    if not raw:
        pytest.skip("EATLOG_TEST_POSTGRES_URL 로 로컬 일회용 PostgreSQL을 지정해야 합니다")
    url = make_url(raw)
    if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}
            or not (url.database or "").startswith("eatlog_test_") or url.query):
        pytest.fail("로컬 loopback + eatlog_test_ DB만 허용합니다 (URL/query 내용은 출력하지 않음)")
    # supplied connection을 무시하면 이 무해한 메모리 DB로 빠져 검증이 실패한다.
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", "sqlite://")
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    schema = "rec_rollout_" + uuid4().hex
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = _alembic_config()
            config.attributes["connection"] = connection
            try:
                yield connection, config
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                connection.commit()
    finally:
        engine.dispose()


def _old_data(connection):
    uid = connection.scalar(text("INSERT INTO users(social_provider,social_id,nickname,email) "
                                 "VALUES('google','rollout','롤아웃','rollout@example.com') RETURNING id"))
    connection.execute(text("INSERT INTO food_groups(id,name,family,role,calories,carbs,protein,fat,companion_group_id) VALUES "
                            "(1,'닭가슴살','육·수산 가공','exclude',150,0,30,2,1),"
                            "(2,'김치찌개','국·탕·찌개류','meal',320,18,22,16,NULL),"
                            "(3,'불고기','구이·볶음·조림류','meal',400,20,30,25,NULL),"
                            "(4,'김밥','밥류','meal',400,50,20,15,NULL)"))
    nid = connection.scalar(text("INSERT INTO nutrition_items(name,normalized_name,base_amount,base_unit,"
                                "calories,carbs,protein,fat,source,food_group_id,serving_basis,is_representative) VALUES "
                                "('김치찌개','김치찌개',1,'serving',320,18,22,16,'seed',2,'per_serving',TRUE) RETURNING id"))
    eaten = datetime.now(UTC) - timedelta(days=1)
    mid = connection.scalar(text("INSERT INTO meal_records(user_id,meal_type,eaten_at,total_calories,total_carbs,total_protein,total_fat) "
                                "VALUES(:uid,'lunch',:at,320,18,22,16) RETURNING id"), {"uid": uid, "at": eaten})
    connection.execute(text("INSERT INTO meal_items(meal_record_id,nutrition_item_id,food_name,food_group_id,"
                            "serving_amount,calories,carbs,protein,fat) VALUES(:mid,:nid,'김치찌개',2,1,320,18,22,16)"),
                       {"mid": mid, "nid": nid})
    lid = connection.scalar(text("INSERT INTO recommendation_logs(user_id,meal_context,recommended_items) "
                                "VALUES(:uid,'{}','[]') RETURNING id"), {"uid": uid})
    rid = connection.scalar(text("INSERT INTO recommendation_items(log_id,food_group_id,name,source,rank,accepted_at,eaten_at,eaten_meal_record_id) "
                                "VALUES(:lid,2,'김치찌개','personal',1,:at,:at,:mid) RETURNING id"),
                            {"lid": lid, "mid": mid, "at": eaten})
    connection.commit()
    return uid, mid, rid


def test_postgres_upgrade_preserves_old_data_and_old_column_writes(pg_rollout):
    connection, config = pg_rollout
    command.upgrade(config, PREVIOUS)
    uid, mid, rid = _old_data(connection)
    before = connection.execute(text("SELECT id,meal_record_id,nutrition_item_id,food_name,food_group_id,calories FROM meal_items")).all()
    connection.commit()
    command.upgrade(config, "head")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    assert connection.execute(text("SELECT id,meal_record_id,nutrition_item_id,food_name,food_group_id,calories FROM meal_items")).all() == before
    assert connection.execute(text("SELECT family,companion_group_id FROM food_groups WHERE id=1")).one() == ("육류·수산물·달걀", None)
    assert connection.scalar(text("SELECT family FROM food_groups WHERE id=3")) == "구이·볶음·조림·찜·전류"
    row = connection.execute(text("SELECT shown_at,features,selection_probability,policy_version,eaten_meal_record_id FROM recommendation_items WHERE id=:id"), {"id": rid}).one()
    assert row == (None, None, None, None, mid)  # 과거 노출·정책을 임의 생성하지 않는다.
    assert connection.scalar(text("SELECT decision FROM recommendation_logs LIMIT 1")) is None
    # 구 코드의 INSERT는 새 nullable 필드를 몰라도 계속 성공한다.
    lid = connection.scalar(text("INSERT INTO recommendation_logs(user_id) VALUES(:uid) RETURNING id"), {"uid": uid})
    connection.execute(text("INSERT INTO recommendation_items(log_id,name,source,rank) VALUES(:lid,'검증','collaborative',1)"), {"lid": lid})
    columns = {column["name"]: column for column in inspect(connection).get_columns("recommendation_items")}
    assert columns["source"]["type"].length == 20
    assert all(columns[name]["nullable"] for name in ("shown_at", "features", "policy_version", "selection_probability"))
    indexes = {index["name"]: index["column_names"] for index in inspect(connection).get_indexes("recommendation_items")}
    assert indexes["ix_recommendation_items_eaten_meal_record_id"] == ["eaten_meal_record_id"]
    for invalid_sql in (
        "UPDATE food_groups SET family='unknown' WHERE id=2",
        "UPDATE food_groups SET role='unknown' WHERE id=2",
        "UPDATE nutrition_items SET serving_basis='unknown'",
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text(invalid_sql))
    connection.commit()
    # 코드 rollback은 이 additive schema를 그대로 쓰는 것이 기본이다.
    # 실제 schema downgrade도 source=collaborative(13자)를 자르지 않는다.
    command.downgrade(config, "e2f85abc9d46")
    assert connection.scalar(text("SELECT source FROM recommendation_items WHERE log_id=:lid"), {"lid": lid}) == "collaborative"
    assert connection.scalar(text("SELECT count(*) FROM meal_items")) == len(before)
    connection.commit()
    command.upgrade(config, "head")


def test_postgres_new_feedback_and_old_frontend_requests(pg_rollout, monkeypatch):
    connection, config = pg_rollout
    command.upgrade(config, PREVIOUS)
    uid, _, _ = _old_data(connection)
    command.upgrade(config, "head")
    factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    previous = dict(app.dependency_overrides)

    def local_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = local_db
    app.dependency_overrides[get_ai_client] = lambda: MockAIClient()
    monkeypatch.setattr(settings, "recommend_engine", "v2")
    monkeypatch.setattr(settings, "recommend_bandit_epsilon", 0)
    headers = {"Authorization": f"Bearer {create_access_token(uid)}"}
    try:
        with TestClient(app) as client:
            menu = client.post("/v1/recommendations/menu", headers=headers, json={"meal_type": "dinner"})
            assert menu.status_code == 200, menu.text
            cards = menu.json()["recommended_menus"]
            assert len(cards) == 3
            chosen = cards[0]
            item_id = chosen["recommendation_item_id"]
            feedback = f"/v1/recommendations/items/{item_id}/feedback"
            assert client.post(feedback, headers=headers, json={"action": "impression"}).status_code == 200
            assert client.post(feedback, headers=headers, json={"action": "accept"}).status_code == 200
            body = {"meal_type": "dinner", "eaten_at": datetime.now(UTC).isoformat(),
                    "recommendation_item_id": item_id, "items": [{"food_name": chosen["name"],
                    "calories": 400, "carbs": 40, "protein": 25, "fat": 15}]}
            saved = client.post("/v1/meals", headers=headers, json=body)
            assert saved.status_code == 201, saved.text
            with factory() as db:
                assert db.get(RecommendationItem, item_id).eaten_meal_record_id == saved.json()["meal_id"]
            assert client.delete(f"/v1/meals/{saved.json()['meal_id']}", headers=headers).status_code == 200
            with factory() as db:
                assert db.get(RecommendationItem, item_id).eaten_at is None
            old_card = cards[1]
            assert client.post(f"/v1/recommendations/{menu.json()['recommendation_log_id']}/accept",
                               headers=headers, json={"name": old_card["name"]}).status_code == 200
            body.pop("recommendation_item_id")
            body["items"][0]["food_name"] = old_card["name"]
            body["eaten_at"] = datetime.now(UTC).isoformat()
            saved_old = client.post("/v1/meals", headers=headers, json=body)
            assert saved_old.status_code == 201, saved_old.text
            with factory() as db:
                old_row = db.get(RecommendationItem, old_card["recommendation_item_id"])
                assert old_row.accepted_at is not None and old_row.eaten_at is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_postgres_invalid_legacy_data_rolls_back_upgrade_atomically(pg_rollout):
    connection, config = pg_rollout
    command.upgrade(config, PREVIOUS)
    _old_data(connection)
    connection.execute(text("UPDATE food_groups SET role='invalid' WHERE id=1"))
    connection.commit()
    with pytest.raises(IntegrityError):
        command.upgrade(config, "head")
    connection.rollback()
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS
    assert connection.execute(text("SELECT family,role,companion_group_id FROM food_groups WHERE id=1")).one() == ("육·수산 가공", "invalid", 1)
    assert "shown_at" not in {column["name"] for column in inspect(connection).get_columns("recommendation_items")}
    # 정정 근거가 있는 값만 바꾼 뒤 재시도하면 복구된다.
    connection.execute(text("UPDATE food_groups SET role='exclude' WHERE id=1"))
    connection.commit()
    command.upgrade(config, "head")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def test_postgres_upstream_game_revision_upgrades_without_losing_data(pg_rollout):
    connection, config = pg_rollout
    command.upgrade(config, "e7a3c95d18b4")
    mission_id = connection.scalar(text(
        "INSERT INTO game_missions(code,title,description,scope,rule,params,target_value) "
        "VALUES('rollout','기존 미션','기존 데이터 보존','daily','meal_count','{}',1) RETURNING id"
    ))
    connection.commit()
    command.upgrade(config, "head")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    assert connection.execute(text("SELECT id,code,title FROM game_missions")).one() == (mission_id, "rollout", "기존 미션")
    assert "recommendation_items" in inspect(connection).get_table_names()


def test_postgres_migration_lock_excludes_other_sessions_and_releases_after_error(pg_rollout):
    connection, _ = pg_rollout
    with pytest.raises(DBAPIError):
        with migration_connection(connection.engine) as migrating:
            assert connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}) is False
            connection.commit()
            migrating.execute(text("SELECT 1/0"))
    assert connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}) is True
    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY})
    connection.commit()
