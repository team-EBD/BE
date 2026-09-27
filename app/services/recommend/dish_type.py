"""음식 종류 판정 — 이름 끝 어미로 '찌개·덮밥·버거…'를 뽑는다.

한국어 음식명은 뒤쪽이 핵심어다(head-final): 김치찌개는 찌개, 치킨무는 무.
그래서 이름이 어미로 끝나는지만 본다 — scripts/build_generic_foods 와 같은 규칙.
영양 유사 후보(candidates.nutrient_similar)가 "같은 종류인가"를 판단하는 데 쓴다.
"""
from __future__ import annotations

from app.services.matching import normalize_name

# 긴 어미가 먼저 매칭되도록 정렬해서 쓴다 (볶음밥 > 밥, 짜장면 > 면).
DISH_TYPES: tuple[str, ...] = (
    # 국물
    "찌개", "전골", "국", "탕",
    # 밥
    "볶음밥", "비빔밥", "덮밥", "김밥", "초밥", "주먹밥", "밥", "죽", "카레",
    # 면
    "짜장면", "짬뽕", "냉면", "라면", "국수", "우동", "파스타", "면",
    # 고기·튀김
    "치킨", "돈까스", "탕수육", "튀김", "삼겹살", "갈비", "족발", "보쌈", "스테이크",
    "구이", "볶음", "찜", "조림", "전",
    # 분식·간식
    "떡볶이", "순대", "만두", "핫도그", "샌드위치", "햄버거", "버거", "피자", "토스트",
    # 빵·디저트
    "빵", "케이크", "도넛", "쿠키", "과자", "초콜릿", "아이스크림", "요거트", "떡",
    # 음료
    "커피", "라떼", "우유", "두유", "주스", "스무디", "에이드", "차",
    # 기타
    "샐러드", "두부", "김치", "나물",
)
_BY_LENGTH = sorted(DISH_TYPES, key=len, reverse=True)


def dish_type(name: str) -> str | None:
    """이름의 음식 종류. 어미가 없으면 None.

    총칭 스크립트의 "어미 앞이 공백이면 부재료" 예외는 쓰지 않는다 — 그 예외는
    공공DB 의 '대표식품_상세명' 표기("김밥 계란")용이고, 사용자 기록·시드 이름에서는
    "와퍼 버거"처럼 띄어 쓴 어미가 곧 종류다. 재료명(계란 등)은 목록에 없어 어차피 None.
    """
    normalized = normalize_name(name)
    for term in _BY_LENGTH:
        if normalized.endswith(term):
            return term
    return None
