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


def test_strip_variant_markers_removes_temp_and_size():
    """hot/ice·사이즈 변형 표기는 벗겨져 한 음식으로 정규화된다 (2026-08-05)."""
    from app.services.matching import normalize_name, strip_variant_markers

    assert strip_variant_markers("허브차 아이스(ICED) (L)") == "허브차"
    assert strip_variant_markers("민트모히또 라떼 핫(HOT) (Mini Venti)") == "민트모히또 라떼"
    assert strip_variant_markers("뱅쇼 티 (ICED)") == "뱅쇼 티"
    assert strip_variant_markers("핫치킨피자씬(L)") == "핫치킨피자씬"
    assert strip_variant_markers("건포도빵 (대)") == "건포도빵"
    assert strip_variant_markers("고구마피자 (1인)") == "고구마피자"
    # 같은 음식의 온도·사이즈 변형은 normalized_name 이 같아져 검색 접기로 합쳐진다
    assert normalize_name("커피 아메리카노 아이스(ICED) (R)") == normalize_name("커피 아메리카노 핫(HOT) (Tall)")


def test_strip_variant_markers_keeps_real_names():
    """온도·사이즈가 아닌 표기는 건드리지 않는다 — 상표 ®, 제품명, 얼린 음식, 개입 수."""
    from app.services.matching import strip_variant_markers

    keep = [
        "양반 카무트(R)브랜드밀 함유 현미밥",  # 중간 괄호 = ® 상표
        "핫도그",
        "HOT6 더킹포스",
        "오뚜기 THE HOT 열라면",
        "아이스 딸기 탕후루",  # '아이스'가 얼린 음식이라는 뜻
        "치즈피자 (8개입)",  # 사이즈가 아닌 수량
        "게살죽(미국)",
    ]
    for name in keep:
        assert strip_variant_markers(name) == name
