"""추천 엔진 v2 — 신호(감쇠 빈도·예산) · 후보 생성 · 랭킹 · 엔진 조립 검증.

SQLite(create_all) 세션에 ORM 으로 직접 기록을 만든다. now 는 고정해 결정론을 검증한다.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import MealItem, MealRecord, NutritionItem, User
from app.services.recommend import recommend
from app.services.recommend.candidates import (
    Candidate,
    macro_ratio,
    meal_worthy,
    merge,
    nutrient_similar,
    personal_frequent,
    popular,
    similarity,
)
from app.services.recommend.dish_type import dish_type
from app.services.recommend.ranking import RankContext, fit_score, rank
from app.services.recommend.signals import (
    budget_label,
    decayed_frequency,
    global_popularity,
    meal_budget,
    meal_type_for_hour,
    recency_penalties,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)  # KST 18:00 → dinner


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _user(db, social_id: str, email: str | None = None) -> User:
    user = User(social_provider="google", social_id=social_id, nickname=social_id, email=email)
    db.add(user)
    db.flush()
    return user


def _meal(db, user: User, meal_type: str, eaten_at: datetime, foods, serving: float = 1.0):
    """foods: [(name, kcal, carbs, protein, fat)] — 1인분 값. serving 배로 기록된 것으로 저장."""
    record = MealRecord(
        user_id=user.id,
        meal_type=meal_type,
        eaten_at=eaten_at,
        is_skipped=False,
        total_calories=sum(f[1] for f in foods) * serving,
        total_carbs=sum(f[2] for f in foods) * serving,
        total_protein=sum(f[3] for f in foods) * serving,
        total_fat=sum(f[4] for f in foods) * serving,
    )
    db.add(record)
    db.flush()
    for name, kcal, carbs, protein, fat in foods:
        db.add(
            MealItem(
                meal_record_id=record.id,
                food_name=name,
                serving_amount=serving,
                calories=kcal * serving,
                carbs=carbs * serving,
                protein=protein * serving,
                fat=fat * serving,
            )
        )
    db.flush()
    return record


def _seed_item(db, name: str, kcal: float, carbs: float, protein: float, fat: float, **kw):
    item = NutritionItem(
        name=name,
        normalized_name=name.replace(" ", ""),
        base_amount=300,
        base_unit="g",
        calories=kcal,
        carbs=carbs,
        protein=protein,
        fat=fat,
        source=kw.get("source", "seed"),
        is_representative=True,
        external_id=kw.get("external_id"),
    )
    db.add(item)
    db.flush()
    return item


def _days_ago(n: int, hour_kst: int = 19) -> datetime:
    return (NOW - timedelta(days=n)).replace(hour=hour_kst - 9, minute=0)


# --- 신호 ------------------------------------------------------------------

def test_decay_weights_today_two_weeks_four_weeks(db):
    u = _user(db, "u1")
    kimchi = ("김치찌개", 320, 18, 22, 16)
    _meal(db, u, "dinner", NOW - timedelta(hours=1), [kimchi])
    _meal(db, u, "dinner", NOW - timedelta(days=14), [kimchi])
    _meal(db, u, "dinner", NOW - timedelta(days=28), [kimchi])
    # 반감기를 14일로 주면 오늘 1.0 · 2주 전 0.5 · 4주 전 0.25
    stats = decayed_frequency(db, u.id, "dinner", now=NOW, min_items=1, half_life_days=14)
    assert len(stats) == 1
    assert stats[0].key == "김치찌개"
    assert stats[0].score == pytest.approx(1.0 + 0.5 + 0.25, abs=0.01)
    assert stats[0].count == 3
    # 기본 반감기(45일)에서는 4주 전 기록도 0.65 — 최근 여부가 점수를 크게 좌우하지 않는다
    habit = decayed_frequency(db, u.id, "dinner", now=NOW, min_items=1)
    assert habit[0].score == pytest.approx(1.0 + 0.5 ** (14 / 45) + 0.5 ** (28 / 45), abs=0.01)


def test_frequency_falls_back_to_all_meals_when_meal_type_sparse(db):
    u = _user(db, "u1")
    _meal(db, u, "dinner", _days_ago(1), [("된장찌개", 250, 14, 18, 12)])  # dinner 1건
    for d in range(1, 5):
        _meal(db, u, "lunch", _days_ago(d), [("김밥", 350, 55, 10, 8)])
    stats = decayed_frequency(db, u.id, "dinner", now=NOW)  # dinner 항목 1 < 5 → 전체
    assert [s.key for s in stats][:2] == ["김밥", "된장찌개"]


def test_group_key_merges_size_and_temperature_variants(db):
    u = _user(db, "u1")
    # normalize_name 규칙: '아이스(ICED)'·'핫(HOT)' 결합형과 꼬리 사이즈 괄호만 제거
    # (무괄호 '아이스'는 아이스크림 같은 오탐 때문에 건드리지 않는다 — matching.py 참고)
    _meal(db, u, "snack", _days_ago(1), [("아메리카노 아이스(ICED) (L)", 10, 0, 0, 0)])
    _meal(db, u, "snack", _days_ago(2), [("아메리카노", 10, 0, 0, 0)])
    stats = decayed_frequency(db, u.id, None, now=NOW)
    assert len(stats) == 1 and stats[0].key == "아메리카노" and stats[0].count == 2


def test_per_serving_normalization(db):
    u = _user(db, "u1")
    _meal(db, u, "lunch", _days_ago(1), [("라면", 500, 70, 10, 18)], serving=2.0)  # 2인분 기록
    stats = decayed_frequency(db, u.id, None, now=NOW)
    assert stats[0].calories == pytest.approx(500)  # 1인분으로 되돌림


def test_global_popularity_excludes_test_accounts_and_counts(db):
    real = _user(db, "real", email="a@gmail.com")
    tester = _user(db, "tester", email="v110@test.com")
    nomail = _user(db, "nomail", email=None)
    for d in range(3):
        _meal(db, real, "dinner", _days_ago(d), [("보쌈", 600, 10, 40, 40)])
        _meal(db, tester, "dinner", _days_ago(d), [("피자", 800, 90, 30, 35)])
    _meal(db, nomail, "dinner", _days_ago(1), [("보쌈", 600, 10, 40, 40)])
    stats = global_popularity(db, "dinner", now=NOW)
    assert [(s.key, s.count) for s in stats] == [("보쌈", 4)]  # 피자(테스트 계정) 제외, NULL 이메일 포함


def test_recency_penalty_uses_default_interval_when_few_eats(db):
    u = _user(db, "u1")
    _meal(db, u, "lunch", _days_ago(1), [("김밥", 350, 55, 10, 8)])  # 어제 → 1 − 1/5 = 0.8
    _meal(db, u, "lunch", _days_ago(5), [("라면", 500, 70, 10, 18)])  # 주기(5일) 도달 → 0
    _meal(db, u, "lunch", _days_ago(0), [("샐러드", 150, 10, 8, 6)])  # 오늘 → 1.0
    assert recency_penalties(db, u.id, now=NOW) == {"김밥": 0.8, "샐러드": 1.0}


def test_recency_penalty_learns_personal_interval(db):
    u = _user(db, "u1")
    for d in (21, 14, 7, 1):  # 7일 주기로 먹는 습관 → 마지막이 어제 → 1 − 1/7
        _meal(db, u, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16)])
    assert recency_penalties(db, u.id, now=NOW)["김치찌개"] == pytest.approx(1 - 1 / 7, abs=0.01)
    for d in (2, 1, 0):  # 매일 먹는 음식(주기 1일)은 오늘 먹었어도 1 − 0/1 = 1.0, 어제면 0
        _meal(db, u, "snack", _days_ago(d), [("우유", 120, 10, 6, 6)])
    assert recency_penalties(db, u.id, now=NOW)["우유"] == 1.0


def test_meal_type_for_hour_matches_fe_boundaries():
    assert [meal_type_for_hour(h) for h in (8, 11, 15, 16, 20, 21)] == [
        "breakfast", "lunch", "lunch", "dinner", "dinner", "snack",
    ]


def test_snack_does_not_fall_back_to_all_meals(db):
    """간식 기록이 적어도 식사에 곁들인 소스·반찬을 '간식에 자주 먹는 음식'으로 올리지 않는다."""
    u = _user(db, "u1")
    for d in range(1, 5):  # 점심마다 소스를 곁들여 기록
        _meal(db, u, "lunch", _days_ago(d, 12),
              [("돈까스", 720, 60, 34, 35), ("스위트칠리소스", 80, 18, 0.5, 0.2)])
    _meal(db, u, "snack", _days_ago(2, 15), [("초콜릿", 200, 24, 3, 11)])  # 간식은 1건뿐

    snack = decayed_frequency(db, u.id, "snack", now=NOW)
    assert [s.key for s in snack] == ["초콜릿"]  # 폴백 없음 → 소스 유입 없음
    # 다른 끼니는 기록이 부족하면 전과 같이 전체 끼니로 확장한다
    dinner = decayed_frequency(db, u.id, "dinner", now=NOW)
    assert {"돈까스", "스위트칠리소스"} <= {s.key for s in dinner}


def test_misrecorded_values_are_excluded_from_signals(db):
    """DB 대표값과 2배 이상 벌어진 매칭 기록은 집계에서 뺀다 (운영: '김치 320kcal')."""
    kimchi = _seed_item(db, "김치", 30, 4, 2, 0.5)
    stew = _seed_item(db, "김치찌개", 320, 18, 22, 16)
    u = _user(db, "u1")
    for d in range(1, 4):  # 김치찌개를 '김치'로 잘못 저장 (320kcal)
        rec = _meal(db, u, "dinner", _days_ago(d), [("김치", 320, 18, 22, 16)])
        db.query(MealItem).filter_by(meal_record_id=rec.id).update(
            {"nutrition_item_id": kimchi.id}
        )
    rec = _meal(db, u, "dinner", _days_ago(1), [("김치찌개", 320, 18, 22, 16)])
    db.query(MealItem).filter_by(meal_record_id=rec.id).update({"nutrition_item_id": stew.id})
    db.flush()

    stats = decayed_frequency(db, u.id, "dinner", now=NOW, min_items=1)
    assert [s.key for s in stats] == ["김치찌개"]  # 오기록된 '김치' 3건은 제외

    # 이름이 DB 대표 항목에 없으면 대조 근거가 없어 기록값을 그대로 쓴다
    _meal(db, u, "dinner", _days_ago(1), [("우리집특제수제비", 450, 60, 12, 14)])
    kept = {s.key: s for s in decayed_frequency(db, u.id, "dinner", now=NOW, min_items=1)}
    assert kept["우리집특제수제비"].calories == pytest.approx(450)


def test_unmatched_records_are_checked_by_name(db):
    """매칭 id 가 없어도 이름으로 대표값을 찾아 대조하고, 영양값은 대표값을 쓴다.

    운영 사례: 김치를 0.1인분 32kcal 로 저장 → 1인분으로 되돌리면 320kcal 이 되어
    "김치 320kcal"이 저녁 추천 2위로 나왔다 (nutrition_item_id 는 NULL).
    """
    _seed_item(db, "김치", 20, 3, 1, 0.5)
    u = _user(db, "u1")
    for d in range(1, 4):
        # 1인분 320kcal 기준으로 0.1인분(32kcal)을 먹은 기록 — 기준값 자체가 틀렸다
        _meal(db, u, "dinner", _days_ago(d), [("김치", 320, 18, 22, 16)], serving=0.1)
        _meal(db, u, "dinner", _days_ago(d), [("된장찌개", 250, 14, 18, 12)])

    stats = {s.key: s for s in decayed_frequency(db, u.id, "dinner", now=NOW, min_items=1)}
    assert "김치" not in stats  # 1인분 320 vs 대표 20 → 16배, 명백한 오기록
    assert "된장찌개" in stats  # 정상 기록은 유지


def test_db_representative_value_overrides_recorded(db):
    """기록값이 대표값과 어긋나면(상식 범위 안이라도) 대표값을 쓴다 — AI 추정 오차 흡수."""
    _seed_item(db, "공기밥", 300, 68, 5.5, 0.5)
    u = _user(db, "u1")
    for d in range(1, 4):
        _meal(db, u, "dinner", _days_ago(d), [("공기밥", 480, 100, 8, 1)])  # 기록값이 높게 잡힘
    stats = decayed_frequency(db, u.id, "dinner", now=NOW, min_items=1)
    assert stats[0].calories == pytest.approx(300)  # 480 이 아니라 대표값


def test_budget_blends_personal_ratio_with_default(db):
    """개인 비율을 그대로 쓰면 아침 예산이 100kcal 까지 떨어진다 — 기본 비율과 섞고 하한을 둔다."""
    u = _user(db, "u1")
    for d in range(1, 8):  # 아침은 거의 안 먹고 저녁에 몰아 먹는 사용자
        _meal(db, u, "dinner", _days_ago(d, 19), [("저녁", 900, 90, 40, 35)])
        _meal(db, u, "lunch", _days_ago(d, 12), [("점심", 600, 60, 30, 20)])
    _meal(db, u, "breakfast", _days_ago(3, 12), [("토스트", 100, 15, 3, 3)])

    breakfast = meal_budget(db, u.id, "breakfast", now=NOW, day_start_hour=6)
    assert breakfast.ratio_source == "personal"
    assert breakfast.ratio == pytest.approx(0.15, abs=0.01)  # 개인 ~0.007 → 섞고 하한 0.15
    assert breakfast.ratio_budget == 300  # 하한이 없으면 ~100kcal 이었다

    dinner = meal_budget(db, u.id, "dinner", now=NOW, day_start_hour=6)
    assert 0.35 < dinner.ratio < 0.6  # 개인 0.56 과 기본 0.35 의 중간


def test_budget_default_ratio_and_remaining_floor(db):
    u = _user(db, "u1")  # 프로필 없음 → 목표 2000 / 단백질 120
    # 오늘(KST 8/28) 아침·점심으로 1500kcal, 단백질 40g 섭취
    _meal(db, u, "breakfast", NOW - timedelta(hours=9), [("토스트", 500, 60, 15, 20)])
    _meal(db, u, "lunch", NOW - timedelta(hours=5), [("돈까스", 1000, 90, 25, 50)])
    b = meal_budget(db, u.id, "dinner", now=NOW, day_start_hour=6)
    assert b.ratio_source == "default" and b.ratio == pytest.approx(0.35)
    assert b.ratio_budget == 700
    assert b.remaining_today == 500
    assert b.meal_budget == 500  # 남은 500 < 700 이고 500 ≥ 700×0.3 → 500
    assert b.protein_gap == pytest.approx(80.0)

    light = meal_budget(db, u.id, "dinner", now=NOW, day_start_hour=6, mood="light")
    assert light.meal_budget == 400


def test_budget_floor_when_goal_exceeded(db):
    u = _user(db, "u1")
    _meal(db, u, "lunch", NOW - timedelta(hours=5), [("뷔페", 2400, 200, 80, 100)])
    b = meal_budget(db, u.id, "dinner", now=NOW, day_start_hour=6)
    assert b.remaining_today == -400
    assert b.meal_budget == 210  # 700 × 0.3 하한


def test_budget_personal_ratio_when_enough_records(db):
    u = _user(db, "u1")
    for d in range(1, 8):  # 7일 × (점심 800 / 저녁 200) → 저녁 비율 0.2
        _meal(db, u, "lunch", _days_ago(d, 12), [("점심", 800, 80, 30, 30)])
        _meal(db, u, "dinner", _days_ago(d, 19), [("저녁", 200, 20, 10, 5)])
    b = meal_budget(db, u.id, "dinner", now=NOW, day_start_hour=6)
    # 개인 비율 0.2 를 기본 0.35 와 반씩 섞어 0.275 (극단값 방지 — MIN_RATIO_BY_MEAL 참고)
    assert b.ratio_source == "personal" and b.ratio == pytest.approx(0.275)
    assert b.ratio_budget == 550


def test_budget_label_thresholds():
    assert budget_label(500, 500) == "fit"
    assert budget_label(390, 500) == "light"
    assert budget_label(610, 500) == "heavy"
    assert budget_label(100, 0) == "heavy"


# --- 후보 생성 ---------------------------------------------------------------

def _cand(key, source, kcal=500, protein=20, freq=0.0):
    return Candidate(key=key, name=key, calories=kcal, carbs=50, protein=protein, fat=15,
                     source=source, freq=freq)


def test_merge_dedupes_keeping_first_source():
    merged = merge(
        [_cand("김밥", "personal", freq=2)],
        [_cand("김밥", "popular"), _cand("라면", "popular")],
        [_cand("라면", "similar"), _cand("우동", "similar")],
    )
    assert [(c.key, c.source) for c in merged] == [
        ("김밥", "personal"), ("라면", "popular"), ("우동", "similar"),
    ]


def test_dish_type_head_final_rule():
    assert dish_type("김치찌개") == "찌개"
    assert dish_type("김치볶음밥") == "볶음밥"  # 긴 어미 우선 (밥 아님)
    assert dish_type("짜장면") == "짜장면" and dish_type("잔치국수") == "국수"
    assert dish_type("불고기버거") == "버거" and dish_type("할라피뇨와퍼 버거") == "버거"
    assert dish_type("치킨무") is None  # 치킨으로 끝나지 않음
    assert dish_type("김밥 계란") is None  # 재료명(계란)은 종류 목록에 없음
    assert dish_type("아메리카노") is None


def test_similarity_prefers_same_dish_type_then_macro_ratio():
    stew = macro_ratio(18, 22, 16)  # 김치찌개
    assert similarity("찌개", stew, "찌개", macro_ratio(14, 18, 12)) > similarity(
        None, stew, "치킨", macro_ratio(40, 60, 50)
    )
    same_ratio_other_type = similarity("찌개", stew, "볶음", stew)
    assert same_ratio_other_type[0] == pytest.approx(0.4) and not same_ratio_other_type[1]


def test_nutrient_similar_uses_generic_pool_and_budget_tiers(db):
    # 풀: 시드 3 + 총칭 1 + 브랜드(비대상) 1  (+ 테스트 DB 기본 시드 46종)
    _seed_item(db, "된장찌개", 250, 14, 18, 12)
    _seed_item(db, "순두부찌개", 280, 12, 20, 15)
    _seed_item(db, "치킨", 900, 40, 60, 50, source="public", external_id="gen:abc")
    _seed_item(db, "샐러드", 150, 10, 8, 6)
    _seed_item(db, "몬스터와퍼", 1000, 80, 50, 60, source="public", external_id="mfds:1")
    u = _user(db, "u1")
    for d in range(4):
        _meal(db, u, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16)])
    anchors = decayed_frequency(db, u.id, "dinner", now=NOW, min_items=1)

    got = nutrient_similar(db, anchors, budget_kcal=300, top=2)
    names = [c.name for c in got]
    assert "김치찌개" not in names and "몬스터와퍼" not in names
    assert set(names) <= {"된장찌개", "순두부찌개", "부대찌개"}  # 같은 '찌개'류가 먼저
    assert all(c.source == "similar" and c.similar_type == "찌개" for c in got)
    assert all(c.similar_to == "김치찌개" for c in got)

    # 유사도 순으로 뽑되 예산 2배(600)를 넘는 치킨(900)은 meal_worthy 에서 제외
    # (테스트 DB 에는 시드 46종이 함께 있어 풀이 위 4개보다 크다 — 찌개류가 앞에 온다)
    wide = nutrient_similar(db, anchors, budget_kcal=300, top=4)
    names = [c.name for c in wide]
    assert len(wide) == 4 and "치킨" not in names and "김치찌개" not in names
    assert all(c.calories <= 600 for c in wide)
    assert all(c.similar_type == "찌개" for c in wide[:3])  # 된장·순두부·부대찌개

    # 예산이 커도(1000) 찌개 anchor 에는 찌개가 먼저 — 예산은 유사도 뒤의 동점 처리일 뿐
    big = nutrient_similar(db, anchors, budget_kcal=1000, top=2)
    assert all(c.similar_type == "찌개" for c in big)


def test_nutrient_similar_without_anchors_is_empty(db):
    assert nutrient_similar(db, [], budget_kcal=500) == []


def test_personal_and_popular_top_n(db):
    u = _user(db, "u1", email="a@gmail.com")
    for i in range(10):
        _meal(db, u, "dinner", _days_ago(i), [(f"음식{i}", 400, 40, 20, 10)])
    stats = decayed_frequency(db, u.id, "dinner", now=NOW)
    assert len(personal_frequent(stats, budget_kcal=700)) == 8
    assert len(popular(global_popularity(db, "dinner", now=NOW), budget_kcal=700)) == 5


def test_side_dishes_are_cut_from_candidates(db):
    u = _user(db, "u1", email="a@gmail.com")
    for d in range(3):  # 김치·단무지가 매 끼 함께 기록됨 → 빈도는 최상위지만 메뉴가 아니다
        _meal(db, u, "dinner", _days_ago(d),
              [("김치", 20, 3, 1, 0), ("단무지", 16, 4, 0.5, 0), ("제육볶음", 480, 20, 30, 28)])
    stats = decayed_frequency(db, u.id, "dinner", now=NOW)
    assert [s.key for s in stats][:3] == ["김치", "단무지", "제육볶음"]  # 동점 → 키 순
    assert [c.key for c in personal_frequent(stats, budget_kcal=700)] == ["제육볶음"]
    assert [c.key for c in popular(global_popularity(db, "dinner", now=NOW), budget_kcal=700)] == [
        "제육볶음"
    ]
    assert meal_worthy(90, budget_kcal=100)  # 간식 예산에선 80kcal 하한만 작동 → 바나나는 통과
    assert not meal_worthy(320, budget_kcal=72)  # 간식 예산의 2배 초과 → 김치찌개는 간식 후보가 아님
    assert meal_worthy(720, budget_kcal=700) and not meal_worthy(1500, budget_kcal=700)


# --- 랭킹 --------------------------------------------------------------------

def test_fit_score_is_asymmetric_under_half_over_double():
    assert fit_score(500, 500) == pytest.approx(1.0)
    assert fit_score(400, 500) == pytest.approx(0.9)  # 20% 미달 → −0.1
    assert fit_score(600, 500) == pytest.approx(0.6)  # 20% 초과 → −0.4
    assert fit_score(900, 500) == 0.0


def test_rank_is_deterministic_and_limits_personal_to_two():
    cands = [
        _cand("개인A", "personal", freq=3.0),
        _cand("개인B", "personal", freq=2.5),
        _cand("개인C", "personal", freq=2.0),
        _cand("인기X", "popular"),
        _cand("유사Y", "similar"),
    ]
    ctx = RankContext(budget=500, protein_gap=0)
    top = rank(cands, ctx)
    keys = [r.candidate.key for r in top]
    assert keys[:2] == ["개인A", "개인B"]
    assert top[2].candidate.source != "personal"  # 3번째는 비개인으로 교체
    assert rank(cands, ctx) == top  # 결정론


def test_rank_recent_penalty_and_protein_bonus():
    fresh = _cand("A", "personal", freq=1.0, protein=10)
    recent = _cand("B", "personal", freq=1.0, protein=10)
    ctx = RankContext(budget=500, protein_gap=0, recency={"B": 1.0})
    ranked = {r.candidate.key: r for r in rank([fresh, recent], ctx)}
    assert ranked["A"].score - ranked["B"].score == pytest.approx(0.30)

    # "어제 먹은 최애"(freq 1.0, 감점 0.8)가 "먹을 때가 된 습관 음식"(freq 0.8, 감점 0)에 밀린다
    favorite = _cand("최애", "personal", freq=1.0)
    due = _cand("주기도달", "personal", freq=0.8)
    top = rank([favorite, due], RankContext(budget=500, protein_gap=0, recency={"최애": 0.8}))
    assert top[0].candidate.key == "주기도달"

    hi = _cand("H", "popular", protein=40)
    lo = _cand("L", "popular", protein=5)
    ranked = rank([hi, lo], RankContext(budget=500, protein_gap=40))
    assert ranked[0].candidate.key == "H"
    assert ranked[0].parts["protein"] == 1.0


def test_rank_keeps_all_personal_when_no_alternative():
    cands = [_cand(k, "personal", freq=f) for k, f in (("a", 3), ("b", 2), ("c", 1))]
    assert len(rank(cands, RankContext(budget=500, protein_gap=0))) == 3


# --- 엔진 조립 -----------------------------------------------------------------

def test_engine_new_user_gets_popular_only(db):
    other = _user(db, "other", email="o@gmail.com")
    for d in range(3):
        _meal(db, other, "dinner", _days_ago(d), [("보쌈", 600, 10, 40, 40), ("공기밥", 310, 68, 5, 0.5)])
    newbie = _user(db, "newbie")
    result = recommend(db, newbie.id, meal_type="dinner", now=NOW)
    assert result.anchors == []
    assert result.items and all(i.source == "popular" for i in result.items)
    assert {i.name for i in result.items} == {"보쌈", "공기밥"}


def test_engine_full_flow_sources_labels_and_reasons(db):
    _seed_item(db, "된장찌개", 250, 14, 22, 12)
    _seed_item(db, "순두부찌개", 280, 12, 20, 15)
    u = _user(db, "u1", email="u@gmail.com")
    for d in range(1, 6):  # 저녁마다 김치찌개+공기밥 → anchor 성립
        _meal(db, u, "dinner", _days_ago(d), [("김치찌개", 320, 18, 22, 16), ("공기밥", 310, 68, 5, 0.5)])
    _meal(db, u, "lunch", NOW - timedelta(hours=5), [("김밥", 350, 55, 10, 8)])  # 오늘 점심

    result = recommend(db, u.id, meal_type="dinner", now=NOW)
    assert result.meal_type == "dinner"
    assert "김치찌개" in result.anchors
    sources = {c.source for c in result.candidates}
    assert {"personal", "similar"} <= sources
    assert len(result.items) == 3
    assert sum(i.source == "personal" for i in result.items) <= 2
    for item in result.items:
        assert item.budget_label in ("fit", "light", "heavy")
        assert "저녁 예산" in item.reason and f"{result.budget.meal_budget} 중" in item.reason

    # 같은 입력 → 같은 출력
    again = recommend(db, u.id, meal_type="dinner", now=NOW)
    assert [i.key for i in again.items] == [i.key for i in result.items]


def test_engine_infers_meal_type_from_kst_hour(db):
    u = _user(db, "u1")
    assert recommend(db, u.id, now=NOW).meal_type == "dinner"  # 18:00 KST
    assert recommend(db, u.id, now=NOW.replace(hour=0)).meal_type == "breakfast"  # 09:00 KST
