"""원재료성식품 적재 — 총칭 행 만들기 규칙 (생것 > 평균 > 전체, '류' 떼기, 상태·월 토큰 제외)."""
from scripts.import_mfds_material import base_name, build_generics, display_name, generic_keys


def _row(nm, lv4="사과", lv4cd="08050", lv5="", lv5cd="", kcal=50.0):
    api = {"foodCd": nm, "foodNm": nm, "foodLv4Nm": lv4, "foodLv4Cd": lv4cd, "foodLv5Nm": lv5, "foodLv5Cd": lv5cd}
    values = {"base_unit": "g", "calories": kcal, "carbs": 10.0, "protein": 1.0, "fat": 0.1, "macros_estimated": False}
    return api, values


def test_display_and_base_name():
    assert display_name("사과_감홍_생것") == "사과 감홍 생것"
    assert base_name("가자미류") == "가자미" and base_name("게류") == "게" and base_name("사과") == "사과"


def test_generic_keys_skip_state_month_grade_and_part_tokens():
    api, _ = _row("사과_감홍_생것", lv5="감홍", lv5cd="0805043")
    assert [k[1] for k in generic_keys(api)] == ["사과", "사과 감홍"]
    api, _ = _row("무_조선무_뿌리_6월", lv4="무", lv5="6월")
    assert [k[1] for k in generic_keys(api)] == ["무"]
    api, _ = _row("소고기_한우(1++등급)_등심_생것", lv4="소고기", lv5="한우(1++등급)")
    api["foodLv6Nm"], api["foodLv6Cd"] = "등심", "0200112"
    assert [k[1] for k in generic_keys(api)] == ["소고기", "소고기 등심"]  # 등급은 빼고 부위는 넣는다
    api, _ = _row("돼지고기_삼겹살(삼겹살)_생것", lv4="돼지고기", lv5="해당없음")
    api["foodLv6Nm"] = "삼겹살(삼겹살)"
    assert [k[1] for k in generic_keys(api)] == ["돼지고기", "돼지고기 삼겹살"]
    api, _ = _row("달걀_난황_생것", lv4="달걀", lv5="해당없음")
    api["foodLv6Nm"] = "난황"
    assert [k[1] for k in generic_keys(api)] == ["달걀"]


def test_base_generic_excludes_part_rows():
    whole, _ = _row("달걀_유정란_생것", lv4="달걀", lv4cd="10003", lv5="유정란", kcal=156)
    yolk, yv = _row("달걀_난황_생것", lv4="달걀", lv4cd="10003", lv5="해당없음", kcal=326)
    yolk["foodLv6Nm"] = "난황"
    out = build_generics([(whole, _row("x", kcal=156)[1]), (yolk, yv)])
    assert out["R-gen:10003"]["calories"] == 156.0  # 난황은 '달걀' 총칭에서 제외


def test_build_generics_prefers_raw_rows_then_average_then_all():
    rows = [
        _row("사과_감홍_생것", kcal=51), _row("사과_양광_생것", kcal=47), _row("사과_동결건조", kcal=332),
        _row("배_평균", lv4="배", lv4cd="08060", kcal=40), _row("배_말린것", lv4="배", lv4cd="08060", kcal=300),
        _row("감_말린것", lv4="감", lv4cd="08070", kcal=250), _row("감_반건시", lv4="감", lv4cd="08070", kcal=150),
    ]
    out = build_generics(rows)
    assert out["R-gen:08050"]["calories"] == 49.0 and out["R-gen:08050"]["name"] == "사과"  # 생것 두 행 평균, 동결건조 제외
    assert out["R-gen:08060"]["calories"] == 40.0  # 생것이 없으면 '평균' 행
    assert out["R-gen:08070"]["calories"] == 200.0  # 둘 다 없으면 전체 평균
    g = out["R-gen:08050"]
    assert (g["base_amount"], g["serving_basis"], g["is_representative"], g["category"], g["source"]) == (100.0, "per_100g", False, "식재료", "public")
