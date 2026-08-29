"""SCRUM-246 — AI 음식명 매칭 부분일치 단계의 유사도(트라이그램) 매칭 전환.

매칭 사다리: ① 정확 일치 → ② 유사도 단계(포함 후보 우선, 없으면 컷·격차
통과한 fuzzy) → ③ 포기(None → 호출부가 AI 추정 폴백). fuzzy 매칭 건은
confidence 를 FUZZY_CONFIDENCE_PENALTY 만큼 감산해 내려보낸다.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai_client import get_ai_client
from app.ai_client.base import AICallLogPayload, AICandidate, AINutritionEstimate, AnalyzeResult
from app.main import app
from app.models import Base, NutritionItem
from app.services.matching import (
    SIMILARITY_CUT,
    match_food_name,
    trigram_similarity,
)


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
        base_amount=400,
        base_unit="g",
        calories=300,
        carbs=20,
        protein=15,
        fat=10,
        source="seed",
        is_representative=True,
    )
    defaults.update(kw)
    return NutritionItem(**defaults)


# ------------------------------- 트라이그램 유사도 자체


def test_trigram_similarity_typo_above_cut_sibling_below():
    """끝 글자 오타는 컷 위, 이름만 형제인 다른 음식은 컷 아래 — 컷 설계 근거."""
    assert trigram_similarity("김치찌게", "김치찌개") >= SIMILARITY_CUT
    assert trigram_similarity("물냉면", "비빔냉면") < SIMILARITY_CUT


# ------------------------------- ① 정확 일치 (기존 동작 불변)


def test_exact_match_unchanged():
    session = _session_with([_item("김치찌개"), _item("참치김치찌개")])
    matched, path = match_food_name(session, "김치찌개")
    assert matched.name == "김치찌개"
    assert path == "exact"


def test_exact_match_normalizes_markers():
    session = _session_with([_item("허브차")])
    matched, path = match_food_name(session, "허브차 아이스(ICED) (L)")
    assert matched.name == "허브차"
    assert path == "exact"


# ------------------------------- ② 포함(substring) 후보 — 구 부분일치 계승


def test_substring_backward_compat_shortest_then_id():
    """구 부분일치 케이스: 동률(같은 유사도·길이)이면 낮은 id — 기존과 동일 항목."""
    session = _session_with([_item("참치김밥"), _item("야채김밥")])
    matched, path = match_food_name(session, "김밥")
    assert matched.name == "참치김밥"  # 유사도·길이 동률 → 먼저 넣은(낮은 id) 행
    assert path == "substring"


def test_substring_has_no_cut():
    """포함 후보는 유사도가 낮아도 버리지 않는다 — 구 동작 보존."""
    session = _session_with([_item("참치김밥")])
    assert trigram_similarity("김밥", "참치김밥") < SIMILARITY_CUT
    matched, path = match_food_name(session, "김밥")
    assert matched.name == "참치김밥"
    assert path == "substring"


def test_substring_ranked_by_similarity():
    """정렬 기준이 이름 길이 → 유사도 점수로 바뀜: 같은 길이면 더 가까운 이름."""
    session = _session_with([_item("참치김밥"), _item("김밥나라")])
    matched, path = match_food_name(session, "김밥")
    # 접두 일치(김밥나라)가 접미 일치(참치김밥)보다 트라이그램 겹침이 크다
    assert matched.name == "김밥나라"
    assert path == "substring"


# ------------------------------- ② fuzzy — 오타 커버 + 컷·격차 안전장치


def test_fuzzy_matches_typo():
    session = _session_with([_item("김치찌개"), _item("된장찌개")])
    matched, path = match_food_name(session, "김치찌게")
    assert matched.name == "김치찌개"
    assert path == "fuzzy"


def test_fuzzy_cut_rejects_unrelated():
    session = _session_with([_item("김치찌개"), _item("공기밥")])
    matched, path = match_food_name(session, "타코야키")
    assert matched is None
    assert path == "none"


def test_fuzzy_margin_rejects_ambiguous():
    """상위 두 후보의 점수가 근소하면 자동 연결하지 않는다 — 오연결 방지."""
    session = _session_with([_item("소고기김치찌개"), _item("닭고기김치찌개")])
    matched, path = match_food_name(session, "돼지고기김치찌개")
    assert matched is None
    assert path == "none"


def test_fuzzy_single_candidate_passes():
    """격차 비교 상대가 없으면(후보 1개) 컷만 넘으면 매칭된다."""
    session = _session_with([_item("소고기김치찌개")])
    matched, path = match_food_name(session, "돼지고기김치찌개")
    assert matched is not None
    assert matched.name == "소고기김치찌개"
    assert path == "fuzzy"


def test_fuzzy_margin_ignores_same_name_duplicates():
    """동명 중복(브랜드 행)은 같은 음식 — 격차 규칙의 비교 대상이 아니다."""
    session = _session_with(
        [_item("소고기김치찌개", brand="A사"), _item("소고기김치찌개", brand="B사")]
    )
    matched, path = match_food_name(session, "돼지고기김치찌개")
    assert matched is not None
    assert path == "fuzzy"


def test_only_representative_items_matched():
    """비대표 공공 항목(100g당)은 유사도 단계에서도 제외 — SCRUM-216 원칙 유지."""
    session = _session_with([_item("김치찌개", is_representative=False, source="public")])
    matched, path = match_food_name(session, "김치찌게")
    assert matched is None
    assert path == "none"


# ------------------------------- confidence 감산 (분석 흐름 통합)


def _stub_parse_result(food_name: str, confidence: float) -> AnalyzeResult:
    return AnalyzeResult(
        status="success",
        draft_notice="문장에서 추출한 기록 초안입니다.",
        candidates=[
            AICandidate(
                food_index=0, food_name=food_name, confidence=confidence,
                estimated_serving=1.0, has_soup=True, has_sauce=False,
                nutrition=AINutritionEstimate(
                    base_serving="1인분(300g)", calories=999, carbs=1, protein=1, fat=1
                ),
            )
        ],
        ai_call_log=AICallLogPayload(
            provider="google", model_name="stub", task_type="analyze",
            status="success", latency_ms=1,
        ),
    )


def _parse_with_stub(client, headers, food_name: str, confidence: float = 0.9):
    class StubClient:
        def parse_text(self, text, db_candidates=None):
            return _stub_parse_result(food_name, confidence)

    app.dependency_overrides[get_ai_client] = lambda: StubClient()
    try:
        return client.post(
            "/v1/meals/parse-text", headers=headers, json={"text": food_name}
        )
    finally:
        from app.ai_client.mock import MockAIClient

        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()


def test_fuzzy_match_reduces_confidence(client, auth_headers):
    """fuzzy 매칭 건은 confidence 를 감산해 내려보낸다 (별도 UI 대신 기존 채널)."""
    body = _parse_with_stub(client, auth_headers, "김치찌게", confidence=0.9).json()
    top = body["candidates"][0]
    assert top["nutrition"]["calories"] != 999  # DB(시드 김치찌개) 값으로 대체됨
    assert top["confidence_score"] == 0.7


def test_exact_match_keeps_confidence(client, auth_headers):
    body = _parse_with_stub(client, auth_headers, "김치찌개", confidence=0.9).json()
    assert body["candidates"][0]["confidence_score"] == 0.9
