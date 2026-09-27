"""대분류·영양 비율만 같아도 비슷하다고 오인했던 음식 조합의 회귀 검증."""
from __future__ import annotations

import pytest

from app.services.recommend.similarity import compare_foods, food_profile


def _profile(name, **kw):
    values = {"carbs": 30, "protein": 20, "fat": 10, **kw}
    return food_profile(name, **values)


@pytest.mark.parametrize("family,anchor,item", [
    ("버거·피자·샌드위치", "불고기버거", "불고기피자"),
    ("구이·볶음·조림·찜·전류", "고등어조림", "고등어구이"),
    ("구이·볶음·조림·찜·전류", "김치전", "김치볶음"),
    ("죽·스프류", "닭죽", "닭수프"),
    ("밥류", "참치김밥", "참치덮밥"),
    ("분식류", "떡볶이", "만두"),
])
def test_same_family_and_identical_macros_do_not_override_dish_form(family, anchor, item):
    assert compare_foods(_profile(anchor, family=family), _profile(item, family=family)) == (0, None)


def test_concrete_form_precedes_related_form_with_identical_macros():
    anchor = _profile("김치찌개", family="국·탕·찌개류")
    same_score, same_kind = compare_foods(anchor, _profile("된장찌개", family="국·탕·찌개류"))
    related_score, related_kind = compare_foods(anchor, _profile("된장국", family="국·탕·찌개류"))
    assert same_score > related_score > 0
    assert same_kind == "찌개" and related_kind == "국·탕·찌개류"


def test_related_noodles_remain_exploration_with_lower_score():
    anchor = _profile("칼국수")
    same = compare_foods(anchor, _profile("잔치국수"))
    related = compare_foods(anchor, _profile("비빔냉면"))
    assert same[0] > related[0] > 0
    assert related[1] == "면"


@pytest.mark.parametrize("anchor,item,kind", [
    ("돈까스", "치즈돈가스", "돈가스"),
    ("돈카츠", "돈가스", "돈가스"),
    ("양념치킨", "닭튀김", "치킨"),
    ("햄버거", "불고기버거", "버거"),
    ("양송이스프", "크림수프", "수프"),
    ("보쌈", "돼지수육", "수육"),
])
def test_same_dish_spelling_variants_share_concrete_form(anchor, item, kind):
    assert compare_foods(_profile(anchor), _profile(item))[1] == kind


def test_canonical_group_name_overrides_product_wording():
    anchor = _profile("고기 도시락 세트", group_name="김치찌개", family="국·탕·찌개류")
    assert compare_foods(anchor, _profile("된장찌개", family="국·탕·찌개류"))[1] == "찌개"
    assert _profile("불고기버거", group_name="특선모음", family="버거·피자·샌드위치").kind is None


@pytest.mark.parametrize("name,kind", [
    ("김치찌개 삼겹살", "찌개"),
    ("김밥 계란", "김밥"),
    ("닭볶음(닭갈비)", "볶음"),
    ("닭볶음(매운맛)", "볶음"),
    ("불고기 피자", "피자"),
    ("김치 볶음밥", "볶음밥"),
])
def test_source_details_preserve_complete_dish_context(name, kind):
    assert _profile(name).kind == kind


@pytest.mark.parametrize("family,role", [("면류", "meal"), ("밥류", "snack")])
def test_known_family_or_role_conflict_rejects_even_same_name(family, role):
    anchor = _profile("볶음밥", family="밥류", role="meal")
    assert compare_foods(anchor, _profile("볶음밥", family=family, role=role)) == (0, None)


def test_missing_family_or_role_allows_recognized_form_fallback():
    assert compare_foods(_profile("된장찌개", family="국·탕·찌개류", role="meal"), _profile("김치찌개"))[1] == "찌개"


@pytest.mark.parametrize("name", ["특선메뉴", "새로운음식", "토스트/샌드위치", "갈비"])
def test_unknown_or_ambiguous_form_does_not_use_family_as_evidence(name):
    assert compare_foods(_profile(name, family="분식류"), _profile("떡볶이", family="분식류")) == (0, None)


def test_headless_name_overlap_is_only_a_tiebreaking_signal():
    anchor = _profile("해물칼국수")
    same_word = compare_foods(anchor, _profile("해물국수"))[0]
    other_word = compare_foods(anchor, _profile("잔치국수"))[0]
    assert same_word > other_word


def test_food_similarity_is_independent_of_portion_and_nutritional_composition():
    anchor = _profile("된장찌개")
    same = compare_foods(anchor, _profile("김치찌개"))[0]
    different = compare_foods(anchor, _profile("김치찌개", carbs=100, protein=0, fat=0))[0]
    doubled = compare_foods(anchor, _profile("김치찌개", carbs=60, protein=40, fat=20))[0]
    assert same == different == doubled


@pytest.mark.parametrize("values", [
    (0, 0, 0), (-1, 2, 3), (float("nan"), 2, 3), (1, float("inf"), 3), (1e308, 2, 3),
])
def test_missing_or_invalid_macros_do_not_change_intrinsic_food_similarity(values):
    unknown = _profile("된장찌개", carbs=values[0], protein=values[1], fat=values[2])
    assert compare_foods(unknown, _profile("김치찌개")) == compare_foods(_profile("된장찌개"), _profile("김치찌개"))


def test_only_named_ingredients_are_read_without_recipe_guesses():
    assert _profile("갈비찜").ingredients == frozenset()
    assert _profile("돈가스").ingredients == frozenset()
    assert _profile("소고기볶음").ingredients == frozenset({"소고기"})
    assert _profile("계란볶음밥").ingredients == _profile("달걀볶음밥").ingredients
    assert _profile("베이컨치즈스페셜", group_name="김치찌개").ingredients == frozenset({"김치"})


def test_shared_named_ingredient_is_stronger_than_shared_character_fragments():
    anchor = _profile("소고기볶음")
    same_ingredient = compare_foods(anchor, _profile("쇠고기볶음"))[0]
    different_meat = compare_foods(anchor, _profile("돼지고기볶음"))[0]
    vegetable = compare_foods(anchor, _profile("버섯볶음"))[0]
    assert same_ingredient > different_meat == vegetable


def test_explicit_noodle_preparation_changes_similarity_without_inferred_broth():
    anchor = _profile("새우볶음우동")
    same_method = compare_foods(anchor, _profile("해물볶음우동"))[0]
    different_method = compare_foods(anchor, _profile("해물비빔우동"))[0]
    assert same_method > different_method > 0
    assert _profile("우동").preparation is None


@pytest.mark.parametrize("name,family,kind", [
    ("불고기도시락", "밥류", "도시락"),
    ("돼지국밥", "밥류", "국밥"),
    ("전복누룽지", "밥류", "누룽지"),
    ("오므라이스", "밥류", "오므라이스"),
    ("카레라이스", "밥류", "카레"),
    ("꼬치어묵", "분식류", "어묵"),
    ("소고기육개장", "국·탕·찌개류", "육개장"),
    ("청국장", "국·탕·찌개류", "청국장"),
    ("소고기샤브샤브", "국·탕·찌개류", "샤브샤브"),
    ("바지락수제비", "면류", "수제비"),
    ("돈코츠라멘", "면류", "라멘"),
    ("메밀소바", "면류", "소바"),
    ("간자장", "면류", "간자장"),
    ("버섯잡채", "구이·볶음·조림·찜·전류", "잡채"),
    ("고추잡채", "구이·볶음·조림·찜·전류", "고추잡채"),
    ("돼지고기 피망잡채", "구이·볶음·조림·찜·전류", "고추잡채"),
    ("떡갈비", "구이·볶음·조림·찜·전류", "떡갈비"),
    ("생선까스", "튀김류", "생선가스"),
])
def test_observed_complete_dishes_have_specific_forms_in_their_family(name, family, kind):
    assert _profile(name, family=family, role="meal").kind == kind


@pytest.mark.parametrize("name,wrong_family", [
    ("청국장", "소스·양념"),
    ("어묵", "육류·수산물·달걀"),
    ("누룽지", "빵·과자·디저트"),
    ("카레", "소스·양념"),
    ("카레라이스", "소스·양념"),
    ("육개장", "기타"),
])
def test_new_form_rules_do_not_override_conflicting_canonical_family(name, wrong_family):
    assert _profile("완성요리 상품명", group_name=name, family=wrong_family, role="meal").kind is None


@pytest.mark.parametrize("name,family", [
    ("청국장", "국·탕·찌개류"), ("어묵", "분식류"), ("누룽지", "밥류"),
])
def test_ambiguous_ingredient_names_require_both_family_and_meal_role(name, family):
    assert _profile(name).kind is None
    assert _profile(name, family=family).kind is None
    assert _profile(name, family=family, role="exclude").kind is None
    assert _profile(name, family=family, role="snack").kind is None
    assert _profile(name, family=family, role="meal").kind == name


def test_gukbap_is_distinct_from_plain_rice_even_without_role_metadata():
    assert _profile("돼지국밥").kind == "국밥"
    assert compare_foods(_profile("돼지국밥"), _profile("소고기국밥"))[1] == "국밥"
    assert compare_foods(_profile("돼지국밥"), _profile("쌀밥")) == (0, None)


def test_new_soup_and_noodle_forms_keep_compatible_exploration():
    soup = _profile("육개장", family="국·탕·찌개류", role="meal")
    assert compare_foods(soup, _profile("갈비탕", family="국·탕·찌개류", role="meal"))[1] == "국·탕·찌개류"
    noodle = _profile("수제비", family="면류", role="meal")
    assert compare_foods(noodle, _profile("칼국수", family="면류", role="meal"))[1] == "면"


def test_pepper_japchae_is_not_assumed_to_have_same_noodle_form_as_japchae():
    family = "구이·볶음·조림·찜·전류"
    assert compare_foods(_profile("고추잡채", family=family), _profile("잡채", family=family)) == (0, None)
