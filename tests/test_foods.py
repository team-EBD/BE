"""Phase 5 DoD — 음식 검색 (+ 대표 음식·동명 중복 접기, 2026-08-01)."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.pagination import PageParams
from app.models import Base, NutritionItem
from app.services.matching import base_serving_text, search_items


def _session_with(items: list[NutritionItem]):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add_all(items)
    session.commit()
    return session


def _item(name: str, **kw) -> NutritionItem:
    defaults = dict(
        name=name,
        normalized_name=name.replace(" ", ""),
        base_amount=100,
        base_unit="g",
        calories=40,
        carbs=5,
        protein=2,
        fat=1,
        source="public",
        is_representative=False,
    )
    defaults.update(kw)
    return NutritionItem(**defaults)


def test_search_collapses_same_name_duplicates():
    """같은 이름의 브랜드 행 여러 개는 1건으로 접힌다 (total 도 접힌 기준)."""
    session = _session_with(
        [
            _item("포기김치", brand="A사"),
            _item("포기김치", brand="B사"),
            _item("포기김치", brand="C사"),
            _item("배추김치", is_representative=True, base_amount=40, calories=15),
        ]
    )
    items, total = search_items(session, "김치", PageParams(page=1, size=10))
    names = [i.name for i in items]
    assert names.count("포기김치") == 1
    assert total == 2  # 포기김치(접힘) + 배추김치


def test_search_representative_ranks_first():
    """대표 음식이 비대표(브랜드·조사 행)보다 위에 노출된다."""
    session = _session_with(
        [
            _item("김치전골", calories=90),
            _item("배추김치", is_representative=True, base_amount=40, calories=15),
        ]
    )
    items, _ = search_items(session, "김치", PageParams(page=1, size=10))
    assert items[0].name == "배추김치"


def test_base_serving_text_by_tier():
    """대표는 1인분 표기, 비대표 공공은 기준량 표기."""
    rep = _item("배추김치", is_representative=True, base_amount=40, calories=15)
    raw = _item("포기김치", brand="A사")
    assert base_serving_text(rep) == "1인분(40g)"
    assert base_serving_text(raw) == "100g당"


def test_search_kimchi(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "김치"}
    )
    assert res.status_code == 200
    body = res.json()
    names = [item["name"] for item in body["items"]]
    assert "김치찌개" in names
    assert body["pagination"]["page"] == 1
    first = body["items"][0]
    assert {"nutrition_item_id", "name", "base_serving", "calories"} <= set(first)


def test_search_with_space_normalization(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "김치 찌개"}
    )
    assert res.status_code == 200
    assert any(i["name"] == "김치찌개" for i in res.json()["items"])


def test_search_no_result_empty_200(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "존재하지않는음식xyz"}
    )
    assert res.status_code == 200
    assert res.json()["items"] == []
    assert res.json()["pagination"]["total"] == 0


def test_search_missing_query_400(client, auth_headers):
    res = client.post("/v1/foods/search", headers=auth_headers, json={})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_pagination(client, auth_headers):
    res = client.post(
        "/v1/foods/search", headers=auth_headers, json={"query": "찌개", "size": 2}
    )
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) <= 2
    assert body["pagination"]["size"] == 2
