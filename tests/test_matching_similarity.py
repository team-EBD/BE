"""AI 음식명 매칭 — 정확 일치만 (2026-10-06). 포함·유사도 추측 매칭은 삭제됐다.

사과→사과차, 당근→당근칩, 무→무밥(530kcal) 처럼 다른 음식을 집던 단계라 틀린 매칭보다 미매칭(AI 추정)이 낫다.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai_client import get_ai_client
from app.ai_client.base import AICallLogPayload, AICandidate, AnalyzeResult
from app.main import app
from app.models import Base, NutritionItem
from app.services.matching import SIMILARITY_CUT, density_per_100g, match_food_name, trigram_similarity


def _session_with(items: list[NutritionItem]):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all(items)
    session.commit()
    return session


_seq = iter(range(1, 10_000))


def _item(name: str, **kw) -> NutritionItem:
    defaults = dict(name=name, normalized_name=name.replace(" ", ""), base_amount=400, base_unit="g",
                    calories=320, carbs=18, protein=22, fat=16, source="public", is_representative=True,
                    serving_basis="per_serving", external_id=f"D{next(_seq)}")
    defaults.update(kw)
    return NutritionItem(**defaults)


def test_trigram_helper_still_available():
    assert trigram_similarity("김치찌게", "김치찌개") >= SIMILARITY_CUT


def test_exact_match_and_marker_normalization():
    s = _session_with([_item("김치찌개"), _item("허브차")])
    assert match_food_name(s, "김치찌개")[1] == "exact"
    assert match_food_name(s, "허브차 아이스(ICED) (L)")[0].name == "허브차"


def test_no_substring_or_fuzzy_guessing():
    """사과→사과차, 무→무밥, 김치찌게(오타)→김치찌개 모두 매칭하지 않는다."""
    s = _session_with([_item("사과차"), _item("무밥", calories=531, base_amount=300), _item("김치찌개")])
    assert match_food_name(s, "사과") == (None, "none")
    assert match_food_name(s, "무") == (None, "none")
    assert match_food_name(s, "김치찌게") == (None, "none")


def test_per_100g_rows_match_and_rep_rows_are_excluded():
    s = _session_with([
        _item("달걀 삶은것", base_amount=100, calories=150, is_representative=False, serving_basis="per_100g", external_id="D327"),
        _item("두유", base_amount=200, calories=230.67, external_id="rep:abc"),
    ])
    egg, path = match_food_name(s, "달걀 삶은것")
    assert path == "exact" and density_per_100g(egg)["calories"] == 150.0
    assert match_food_name(s, "두유") == (None, "none")  # 동명 대표 행은 제외 → AI 100g 당 값으로


def test_duplicate_names_prefer_seed_then_dish_then_product():
    s = _session_with([
        _item("김치", external_id="P9001", calories=1), _item("김치", external_id="D9001", calories=2),
        _item("김치", source="seed", external_id=None, calories=3),
    ])
    assert float(match_food_name(s, "김치")[0].calories) == 3.0
    s2 = _session_with([_item("김치", external_id="P9002", calories=1), _item("김치", external_id="D9002", calories=2)])
    assert float(match_food_name(s2, "김치")[0].calories) == 2.0


def test_density_handles_missing_base():
    assert density_per_100g(_item("x", base_amount=0)) is None
    assert density_per_100g(_item("x", base_amount=50, calories=100))["calories"] == 200.0


class _OneCandidateAI:
    def __init__(self, name):
        self.name = name

    def analyze(self, image_url, eating_habits=None, **kw):
        return AnalyzeResult(status="success", draft_notice="x", candidates=[
            AICandidate(food_name=self.name, confidence=0.9, estimated_serving=1.0, estimated_serving_g=300)],
            ai_call_log=AICallLogPayload(task_type="analyze", status="success", latency_ms=1))

    def recommend(self, *a, **k):
        raise AssertionError


def test_api_unmatched_name_keeps_confidence_and_reports_source(client, auth_headers):
    """유사도 감산이 없어졌으니 confidence 는 AI 값 그대로. 미매칭이면 nutrition_source 가 db 가 아니다."""
    from tests.test_analyze import upload_image_id

    image_id = upload_image_id(client, auth_headers)
    app.dependency_overrides[get_ai_client] = lambda: _OneCandidateAI("김치찌게")
    try:
        res = client.post("/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id})
    finally:
        from app.ai_client.mock import MockAIClient

        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()
    [c] = res.json()["candidates"]
    assert c["confidence_score"] == 0.9 and c["nutrition_item_id"] is None and c["nutrition_source"] != "db"
