"""총칭 대표 생성 — "치킨"·"라면"처럼 사람이 흔히 쓰는 이름을 DB에 만든다.

사용법 (다른 적재 스크립트를 모두 돌린 뒤 마지막에):
    python -m scripts.build_generic_foods

왜 필요한가
-----------
AI 가 사진에서 "치킨"이라고만 알려주는데 DB 에는 `후라이드치킨`·`양념치킨`은 있어도
`치킨` 이라는 항목이 없었다. 그러면 정확일치가 실패하고 부분일치가 **이름 짧은 순**으로
고르는데, `치킨무`(3자)가 `후라이드치킨`(6자)을 이겨 **치킨을 3kcal 로 기록**했다.
2026-08-01 운영에서 터진 "김치찌개 19kcal"와 같은 계열의 사고다.

흔한 총칭 30개를 점검했더니 16개가 정확일치에 실패했다 —
치킨→치킨무(3kcal), 커피→커피번, 계란→계란빵, 샐러드→햄샐러드(54kcal) 등.

그룹을 어떻게 묶나
-----------------
한국어는 **뒤쪽이 핵심어**다(head-final). `후라이드치킨`은 치킨이지만 `치킨무`는 무이고,
`계란빵`은 빵이지 계란이 아니다. 그래서 **이름이 총칭어로 끝나는 항목만** 묶는다.
이 규칙 하나로 치킨무·계란빵·커피번이 자동으로 빠진다.

값은 어떻게 정하나
-----------------
가공식품 동명 대표(import_mfds_api)와 같은 방식이다.
    1인분  = median(그룹의 1인분 중량)
    영양소 = 절사평균(항목별 'g당 값') × 1인분
절사평균(상하위 10%)은 `스모크치킨 30g` 같은 이질적 항목이 평균을 끌지 못하게 한다.

**이미 1인분으로 환산된 항목(is_representative)만 재료로 쓴다.** 100g 당 값이 섞이면
총칭값 자체가 오염된다.
"""
from __future__ import annotations

import argparse
import hashlib
import statistics as stats
from collections import Counter, defaultdict

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import NutritionItem
from scripts.import_mfds_api import trimmed_mean
from scripts.import_public_nutrition import normalize_name

# 만들 총칭어. AI 가 사진 분석에서 실제로 내놓을 법한 상위어만 둔다 —
# 너무 넓은 말(음식·식사)이나 재료명(고기·야채)은 대표값의 의미가 없어 제외한다.
GENERIC_TERMS = [
    # 육류·튀김
    "치킨", "닭튀김", "돈까스", "탕수육", "튀김", "삼겹살", "스테이크", "소시지", "햄",
    # 밥·면
    "라면", "국수", "우동", "파스타", "볶음밥", "비빔밥", "덮밥", "김밥", "죽", "짜장면", "짬뽕",
    # 국물
    "찌개", "국", "탕", "전골",
    # 분식·간식
    "떡볶이", "만두", "핫도그", "샌드위치", "햄버거", "피자", "토스트",
    # 빵·디저트
    "빵", "케이크", "도넛", "쿠키", "과자", "초콜릿", "아이스크림", "요거트", "마카롱",
    # 음료
    "커피", "우유", "주스", "차", "스무디", "에이드",
    # 기타
    "샐러드", "두부", "김치", "나물", "전",
]

# 접미사 규칙이 속는 경우가 있어 재료에서 걸러낸다.
# 원본이 "대표식품_상세명" 형식이라 `김밥_계란` → 표시명 "김밥 계란" 이 되고,
# 이게 "계란"으로 끝나 계란 그룹에 들어간다 — 실제로는 김밥이다.
# **표시명에서 총칭어 바로 앞이 공백이면** 그 총칭의 하위어가 아니라 다른 음식의
# 부재료 표기로 보고 제외한다 (2026-08-04: 계란 재료 3건이 전부 이 경우였다).
def _is_misleading(item_name: str, term: str) -> bool:
    stripped = item_name.strip()
    return stripped.endswith(" " + term)

# 총칭 대표를 만들 최소 그룹 크기 — 표본이 적으면 "일반적인" 값이라 하기 어렵다
MIN_GROUP = 5

# 1인분 상한 — 국·탕(400g)까지 인정하되 그 이상은 이질적 항목이 섞인 것으로 본다
SERVING_MIN, SERVING_MAX = 5.0, 600.0

_NUTRIENTS = (
    "calories", "carbs", "protein", "fat",
    "sugar", "fiber", "sodium", "cholesterol", "saturated_fat", "trans_fat",
)


def _external_id(term: str) -> str:
    digest = hashlib.md5(normalize_name(term).encode()).hexdigest()  # noqa: S324 — 식별자용
    return f"gen:{digest}"[:40]


def build(term: str, members: list[NutritionItem]) -> dict | None:
    """총칭어 하나에 대한 대표 항목. 재료가 부족하거나 이상하면 None."""
    unit = Counter(m.base_unit for m in members).most_common(1)[0][0]
    same_unit = [m for m in members if m.base_unit == unit]
    if len(same_unit) < MIN_GROUP:
        return None

    servings = [
        float(m.base_amount) for m in same_unit
        if SERVING_MIN <= float(m.base_amount) <= SERVING_MAX
    ]
    if len(servings) < MIN_GROUP:
        return None
    serving = stats.median(servings)

    values: dict[str, float] = {}
    for key in _NUTRIENTS:
        densities = [
            float(getattr(m, key)) / float(m.base_amount)
            for m in same_unit
            if getattr(m, key) is not None and float(m.base_amount) > 0
        ]
        if not densities:
            continue
        values[key] = round(trimmed_mean(densities) * serving, 2)

    if not values.get("calories"):
        return None
    if any(v >= 1_000_000 for v in values.values()):
        return None

    return {
        "external_id": _external_id(term),
        "name": term,
        "normalized_name": normalize_name(term),
        "base_amount": round(serving, 2),
        "base_unit": unit,
        "brand": None,
        "category": Counter(m.category for m in same_unit).most_common(1)[0][0],
        "total_weight": round(serving, 2),
        "source": "public",
        "is_representative": True,
        **values,
    }


def run(session_factory=SessionLocal) -> dict:
    stats_out = {"created": 0, "updated": 0, "skipped": 0, "detail": {}}

    with session_factory() as session:
        # 재료: 이미 1인분으로 환산된 항목만. 총칭끼리 서로를 재료로 삼지 않도록 제외.
        pool = list(
            session.scalars(
                select(NutritionItem).where(
                    NutritionItem.is_representative.is_(True),
                    NutritionItem.external_id.is_(None)
                    | NutritionItem.external_id.not_like("gen:%"),
                )
            )
        )

        buckets: dict[str, list[NutritionItem]] = defaultdict(list)
        for term in GENERIC_TERMS:
            norm = normalize_name(term)
            for item in pool:
                name = item.normalized_name or ""
                # 핵심어가 뒤에 오는 한국어 특성 — '치킨'으로 끝나는 것만 치킨이다.
                # 자기 자신(이미 '치킨'인 항목)은 재료에서 뺀다.
                if not name.endswith(norm) or name == norm:
                    continue
                if _is_misleading(item.name or "", term):
                    continue
                buckets[term].append(item)

        for term in GENERIC_TERMS:
            members = buckets.get(term, [])
            payload = build(term, members) if len(members) >= MIN_GROUP else None
            if payload is None:
                stats_out["skipped"] += 1
                continue
            existing = session.scalar(
                select(NutritionItem).where(NutritionItem.external_id == payload["external_id"])
            )
            if existing is None:
                session.add(NutritionItem(**payload))
                stats_out["created"] += 1
            else:
                for k, v in payload.items():
                    setattr(existing, k, v)
                stats_out["updated"] += 1
            stats_out["detail"][term] = (
                len(members), payload["base_amount"], payload["base_unit"], payload["calories"]
            )
        session.commit()
    return stats_out


def main() -> None:
    argparse.ArgumentParser(description="총칭 대표 생성").parse_args()
    r = run()
    print(f"[generic] 생성 {r['created']} / 갱신 {r['updated']} / 재료 부족으로 건너뜀 {r['skipped']}")
    for term, (n, amount, unit, kcal) in sorted(r["detail"].items(), key=lambda x: -x[1][0]):
        print(f"[generic]   {term:<8} 재료 {n:>5}건 → 1인분({amount:g}{unit}) {kcal:g}kcal")


if __name__ == "__main__":
    main()
