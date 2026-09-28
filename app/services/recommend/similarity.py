"""음식 유사도 — 확인된 이름·분류의 형태, 재료 표현, 조리 방식을 비교한다.

대분류가 같다는 이유로 햄버거와 피자, 조림과 전을 같은 음식처럼 취급하지 않는다.
음식군 이름이 있으면 상품명보다 우선하며, 모르는 형태는 대분류만으로 추정하지 않는다.
개인 선호·최근 섭취·열량·탄단지는 음식 자체의 유사도에 섞지 않는다.
가중치는 초기 규칙이며 실제 채택 데이터로 학습한 선호 확률이 아니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.matching import normalize_name

from .dish_type import DISH_TYPES

# 같은 음식 형태의 표기 차이만 합친다. 원재료·맛의 임의 사전은 사용하지 않는다.
_HEAD_ALIASES = {
    "돈까스": "돈가스", "돈카츠": "돈가스", "돈가스": "돈가스",
    "햄버거": "버거", "닭튀김": "치킨",
    "스프": "수프", "수프": "수프", "보쌈": "수육", "수육": "수육",
    "자장면": "짜장면", "스파게티": "파스타",
    "생선까스": "생선가스", "카레라이스": "카레", "피망잡채": "고추잡채",
}
# 원본 음식군 감사에서 확인한 완성 요리 형태. 계열이 알려져 있는데 맞지 않으면
# 원료·가공품 또는 오분류일 수 있으므로 이름 어미만으로 요리라고 판정하지 않는다.
_FORM_FAMILIES = {
    "도시락": "밥류", "국밥": "밥류", "누룽지": "밥류", "오므라이스": "밥류", "카레": "밥류",
    "어묵": "분식류",
    "육개장": "국·탕·찌개류", "청국장": "국·탕·찌개류", "샤브샤브": "국·탕·찌개류",
    "수제비": "면류", "라멘": "면류", "소바": "면류", "간자장": "면류",
    "잡채": "구이·볶음·조림·찜·전류", "고추잡채": "구이·볶음·조림·찜·전류",
    "떡갈비": "구이·볶음·조림·찜·전류", "생선가스": "튀김류",
}
# 원료 청국장, 조리 전 어묵, 간식 누룽지의 이름만으로 한 끼 요리를 추측하지 않는다.
_CONTEXT_REQUIRED = frozenset({"청국장", "어묵", "누룽지"})
_HEADS = sorted(
    (set(DISH_TYPES) | set(_HEAD_ALIASES) | set(_FORM_FAMILIES) | {"불고기"}) - {"삼겹살", "갈비"},
    key=lambda term: (-len(term), term),
)
_SOUPS = frozenset({"국", "탕", "찌개", "전골", "육개장", "청국장", "샤브샤브"})
_NOODLES = frozenset({
    "국수", "냉면", "라면", "우동", "파스타", "짜장면", "짬뽕", "면", "수제비", "라멘", "소바", "간자장",
})
_PARENTHESES = re.compile(r"\([^()]*\)")
# 이름에 실제로 적힌 표현만 읽는다. 돈가스→돼지고기, 갈비→소고기 같은 추론은 하지 않는다.
# 이는 재료명 표현의 겹침이며 레시피·실제 함유 성분을 검증한 정보는 아니다.
_NAMED_INGREDIENTS = {
    "김치": "김치", "된장": "된장", "두부": "두부", "달걀": "달걀", "계란": "달걀",
    "소고기": "소고기", "쇠고기": "소고기", "돼지고기": "돼지고기",
    "닭고기": "닭고기", "닭가슴살": "닭고기", "참치": "참치", "연어": "연어",
    "고등어": "고등어", "새우": "새우", "오징어": "오징어", "해물": "해물",
    "버섯": "버섯", "감자": "감자", "고구마": "고구마", "치즈": "치즈", "메밀": "메밀",
}
_PREPARATIONS = ("볶음", "비빔", "구이", "조림", "찜", "튀김")
_PREPARATION_BY_KIND = {
    **{kind: kind for kind in _PREPARATIONS},
    "볶음밥": "볶음", "비빔밥": "비빔",
}


@dataclass(frozen=True)
class FoodProfile:
    family: str | None
    role: str | None
    kind: str | None
    stem: str
    ingredients: frozenset[str]
    preparation: str | None


def _dish_parts(name: str) -> tuple[str | None, str]:
    # 닭볶음(닭갈비) 같은 원본 설명은 형태를 바꾸지 않는다.
    cleaned = _PARENTHESES.sub("", name).strip()
    normalized = normalize_name(cleaned)
    if "/" in normalized:
        return None, ""  # 여러 종류를 묶은 군에서 하나를 임의로 고르지 않는다.
    if normalized == "삼겹살":
        return "구이", "삼겹살"  # 기존 시드의 완성 메뉴. 복합명의 재료 삼겹살과 구분.
    for head in _HEADS:
        if normalized.endswith(head):
            return _HEAD_ALIASES.get(head, head), normalized[:-len(head)]
    # 원본 '대표식품명_상세명' 표시 규칙: 김치찌개 삼겹살, 김밥 계란.
    # 뒤쪽에 완성 음식 형태가 있으면 위의 head-final 판정이 먼저 적용된다.
    first, *details = re.split(r"[\s_]+", cleaned)
    if details:
        for head in _HEADS:
            if first.endswith(head):
                stem = first[:-len(head)] + "".join(details)
                return _HEAD_ALIASES.get(head, head), normalize_name(stem)
    return None, ""


def food_profile(
    name: str,
    *,
    carbs: float = 0.0,
    protein: float = 0.0,
    fat: float = 0.0,
    group_name: str | None = None,
    family: str | None = None,
    role: str | None = None,
) -> FoodProfile:
    """음식군 우선. 탄단지 인자는 기존 호출 호환용이며 음식 특성에 사용하지 않는다."""
    kind, stem = _dish_parts(group_name or name)
    expected_family = _FORM_FAMILIES.get(kind) if kind is not None else None
    if expected_family is not None:
        wrong_context = (family is not None and family != expected_family) or role not in (None, "meal")
        missing_context = kind in _CONTEXT_REQUIRED and (family != expected_family or role != "meal")
        if wrong_context or missing_context:
            kind, stem = None, ""
    ingredients = frozenset(value for term, value in _NAMED_INGREDIENTS.items() if term in stem)
    methods = {method for method in _PREPARATIONS if method in stem}
    preparation = _PREPARATION_BY_KIND.get(kind) if kind is not None else None
    if preparation is None and len(methods) == 1:
        preparation = next(iter(methods))
    return FoodProfile(family, role, kind, stem, ingredients, preparation)


def compare_foods(anchor: FoodProfile, item: FoodProfile) -> tuple[float, str | None]:
    """(유사도 0~1, 설명 가능한 공통 형태). 공통 형태가 없으면 후보로 삼지 않는다.

    형태 80% + 명시된 재료 표현의 겹침 15% + 명시된 조리 방식 일치 5%.
    국/탕/찌개/전골, 서로 다른 면 형태는 탐색을 위해 형태 점수 0.75로 허용한다.
    두 조리 방식이 명확히 다르면 형태 점수를 추가로 낮춘다. 모르는 재료·방식은 가산하지 않는다.
    """
    if anchor.family and item.family and anchor.family != item.family:
        return 0.0, None
    if anchor.role and item.role and anchor.role != item.role:
        return 0.0, None
    if anchor.kind is None or item.kind is None:
        return 0.0, None
    if anchor.kind == item.kind:
        form, label = 1.0, anchor.kind
    elif anchor.kind in _SOUPS and item.kind in _SOUPS:
        form, label = 0.75, "국·탕·찌개류"
    elif anchor.kind in _NOODLES and item.kind in _NOODLES:
        form, label = 0.75, "면"
    else:
        return 0.0, None
    ingredients = 0.0
    if anchor.ingredients and item.ingredients:
        ingredients = len(anchor.ingredients & item.ingredients) / len(anchor.ingredients | item.ingredients)
    same_preparation = 0.0
    if anchor.preparation is not None and item.preparation is not None:
        if anchor.preparation == item.preparation:
            same_preparation = 1.0
        else:
            form *= 0.8
    score = 0.80 * form + 0.15 * ingredients + 0.05 * same_preparation
    return min(1.0, max(0.0, score)), label
