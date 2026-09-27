"""실제 식품 분류와 추천 역할 경계의 회귀 검증 (외부 DB/원본 파일 불필요)."""
from __future__ import annotations

import pytest

from scripts.food_group_taxonomy import (
    BROAD_GROUPS,
    COMPANION_GROUPS,
    D_FAMILY,
    EXTRA_GROUPS,
    FAMILIES,
    GROUP_FAMILY_OVERRIDE,
    MERGES,
    P_FAMILY,
    SYNONYM_ALIASES,
    canonical_group,
    canonical_source_group,
    companion_for,
    family_for,
    role_for,
)


@pytest.mark.parametrize(
    "kind,raw_family,group,expected",
    [
        ("D", "죽 및 스프류", "전복죽", "죽·스프류"),
        ("P", "즉석식품류", "죽", "죽·스프류"),
        ("P", "즉석식품류", "스프", "죽·스프류"),
        ("D", "찜류", "갈비찜", "구이·볶음·조림·찜·전류"),
        ("D", "전·적 및 부침류", "김치전", "구이·볶음·조림·찜·전류"),
        ("P", "두부류 또는 묵류", "두부", "두부·묵·콩·견과류"),
        ("P", "두부류 또는 묵류", "묵", "두부·묵·콩·견과류"),
        ("P", "농산가공식품류", "견과류", "두부·묵·콩·견과류"),
        ("D", "두류, 견과 및 종실류", "견과", "두부·묵·콩·견과류"),
        ("P", "알가공품류", "달걀/메추리알", "육류·수산물·달걀"),
        ("P", "당류", "설탕", "소스·양념"),
        ("P", "잼류", "잼", "소스·양념"),
        ("P", "유가공품류", "버터", "소스·양념"),
        ("D", "음료 및 차류", "코코아", "음료"),
        ("P", "코코아가공품류 또는 초콜릿류", "코코아가공품", "빵·과자·디저트"),
        ("D", "유제품류 및 빙과류", "아이스크림", "빵·과자·디저트"),
        ("P", "빙과류", "아이스크림", "빵·과자·디저트"),
        ("D", "유제품류 및 빙과류", "빙수", "빵·과자·디저트"),
        ("P", "즉석식품류", "즉석 피자", "버거·피자·샌드위치"),
        ("P", "면류", "라면과자", "빵·과자·디저트"),
        ("P", "수산가공식품류", "젓갈/액젓", "김치·절임"),
        ("P", "수산가공식품류", "김", "샐러드·채소·나물"),
        ("D", "찜류", "고구마", "샐러드·채소·나물"),
        ("D", "곡류, 서류 제품", "고구마", "샐러드·채소·나물"),
        ("P", " 알가공품류 ", "달걀/메추리알", "육류·수산물·달걀"),
        ("unknown", "밥류", "알수없는식품", "기타"),
    ],
)
def test_food_family(kind, raw_family, group, expected):
    assert family_for(kind, raw_family, group) == expected


@pytest.mark.parametrize("group", ["전복죽", "흰죽", "스프", "감자스프"])
def test_porridge_and_soup_do_not_add_rice(group):
    family = family_for("D", "죽 및 스프류", group)
    role = role_for(family, group)
    assert role == "meal"
    assert companion_for(family, group, role) is None


@pytest.mark.parametrize("group", sorted(COMPANION_GROUPS))
def test_plain_grain_rice_is_companion(group):
    assert role_for("밥류", group) == "companion"


@pytest.mark.parametrize("group", ["비빔밥", "김밥", "덮밥", "볶음밥", "국밥"])
def test_rice_meals_are_not_plain_rice(group):
    assert role_for("밥류", group) == "meal"
    assert companion_for("밥류", group, "meal") is None


@pytest.mark.parametrize("group", sorted(BROAD_GROUPS))
def test_unresolved_mixed_source_buckets_are_not_recommended_as_one_dish(group):
    assert role_for("밥류", group) == "exclude"
    assert companion_for("국·탕·찌개류", group, "exclude") is None


@pytest.mark.parametrize(
    "family,group,expected",
    [
        ("육류·수산물·달걀", "연어", "exclude"),
        ("육류·수산물·달걀", "달걀/메추리알", "exclude"),
        ("육류·수산물·달걀", "육회", "meal"),
        ("육류·수산물·달걀", "닭가슴살", "meal"),
        ("육류·수산물·달걀", "달걀", "snack"),
        ("두부·묵·콩·견과류", "두부", "exclude"),
        ("두부·묵·콩·견과류", "견과류", "snack"),
        ("빵·과자·디저트", "반제품/생지", "exclude"),
        ("음료", "농축음료/베이스", "exclude"),
        ("음료", "원두/원두분말", "exclude"),
        ("음료", "인스턴트커피", "exclude"),
        ("유제품", "분유", "exclude"),
        ("소스·양념", "설탕", "exclude"),
        ("면류", "우동면", "exclude"),
        ("구이·볶음·조림·찜·전류", "달걀말이", "exclude"),
        ("샐러드·채소·나물", "감자샐러드", "exclude"),
        ("샐러드·채소·나물", "닭가슴살 샐러드", "meal"),
    ],
)
def test_food_type_does_not_imply_ready_to_eat_meal(family, group, expected):
    assert role_for(family, group) == expected


@pytest.mark.parametrize(
    "family,group,expected",
    [
        ("국·탕·찌개류", "김치찌개", "쌀밥"),
        ("구이·볶음·조림·찜·전류", "불고기", "쌀밥"),
        ("튀김류", "돈가스", "쌀밥"),
        ("튀김류", "닭튀김", None),
        ("분식류", "만두", None),
    ],
)
def test_default_companions(family, group, expected):
    assert companion_for(family, group, role_for(family, group)) == expected


def test_union_categories_and_different_meats_are_not_synonyms():
    assert canonical_group("비스킷/쿠키/크래커") == "비스킷/쿠키/크래커"
    assert canonical_group("밀크티/버블티") == "밀크티/버블티"
    assert "치킨카츠" not in SYNONYM_ALIASES
    assert "쌈무" not in SYNONYM_ALIASES
    assert canonical_group("즉석 피자") == canonical_group("피자")
    assert canonical_group("크로켓") == canonical_group("크로켓(고로케)") == "고로케"


def test_prepared_drinks_and_dry_ingredients_are_not_one_nutrition_group():
    drink = canonical_source_group("D", "코코아")
    ingredient = canonical_source_group("P", "코코아")
    assert drink == "코코아"
    assert ingredient == "코코아가공품"
    assert role_for(family_for("D", "음료 및 차류", drink), drink) == "snack"
    assert role_for(family_for("P", "코코아가공품류 또는 초콜릿류", ingredient), ingredient) == "exclude"
    assert canonical_source_group("P", "인스턴트커피") == "인스턴트커피"
    assert canonical_source_group("P", "액상커피") == "커피"


def test_taxonomy_uses_only_declared_families_and_stable_canonical_names():
    assert len(FAMILIES) == len(set(FAMILIES)) == 18
    for mapping in (D_FAMILY, P_FAMILY, GROUP_FAMILY_OVERRIDE):
        assert set(mapping.values()) <= set(FAMILIES)
    for source, target in MERGES.items():
        assert canonical_group(source) == canonical_group(target) == target
    for group, spec in EXTRA_GROUPS.items():
        assert spec["family"] in FAMILIES
        assert spec["role"] == role_for(spec["family"], group)


@pytest.mark.parametrize("name", ["멸치볶음", "연근조림", "진미채볶음", "애호박볶음", "어묵볶음"])
def test_typical_side_dishes_are_excluded_even_if_cooked_family(name):
    family = "구이·볶음·조림·찜·전류"
    role = role_for(family, name)
    assert role == "exclude"
    assert companion_for(family, name, role) is None


def test_never_recommend_keywords_force_exclude():
    """개고기·보신탕류는 계열이 meal 이어도 추천 후보가 되지 않는다."""
    from scripts.food_group_taxonomy import NEVER_RECOMMEND_KEYWORDS, role_for

    assert "개고기" in NEVER_RECOMMEND_KEYWORDS
    assert role_for("구이·볶음·조림·찜·전류", "개고기 수육") == "exclude"
    assert role_for("국·탕·찌개류", "보신탕") == "exclude"
    assert role_for("국·탕·찌개류", "개고기전골") == "exclude"
    assert role_for("구이·볶음·조림·찜·전류", "수육") == "meal"  # 일반 수육은 그대로
