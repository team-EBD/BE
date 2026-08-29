"""음식 검색·영양 매칭 (Phase 5).

- 검색: nutrition_items.normalized_name / name 부분일치(MVP 는 단순 LIKE).
- 매칭: AI 후보 food_name → nutrition_items 1건 매칭 (정확일치 우선 → 부분일치).
- 정규화 규칙: 온도·사이즈 변형 표기 제거 + 공백 제거. 적재 스크립트도 이 함수를
  import 해 같은 규칙으로 normalized_name 을 저장한다 — 여기가 유일한 정의처.
"""
from __future__ import annotations

import re

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session

from app.core.pagination import PageParams
from app.models import NutritionItem

# 온도 표기 — 항상 '아이스(ICED)'/'핫(HOT)' 결합형이라 위치 무관 제거해도 안전하다.
# 무괄호 '핫'·'아이스'는 핫도그·아이스크림·HOT6(제품명)·아이스 딸기 탕후루(얼린 음식)처럼
# 온도가 아닌 경우가 많아 건드리지 않는다 (2026-08-05 전수 조사).
_TEMP_MARKER_RE = re.compile(r"아이스\(ICED?\)|핫\(HOT\)|\(ICED?\)|\(HOT\)", re.IGNORECASE)

# 사이즈 괄호 — 이름 **끝**에 연달아 붙은 것만 벗긴다. 중간 괄호는 '카무트(R)브랜드밀'의
# ®처럼 사이즈가 아닐 수 있다. 어휘는 로컬 DB 꼬리 괄호 토큰 전수 조사로 확정했고
# 무작위 표본 오탐 0 을 확인했다 (프랜차이즈 음료 L/R/EX/J/V, 피자 F/P/G, 빵 소/대/홀 등).
_TRAILING_SIZE_RE = re.compile(
    r"(?:\s*\((?:Mini Venti|하프벤티|더벤티|Grande|Venti|Short|Tall|Solo|Half|Max"
    r"|XL|ML|EX|싱글|더블|미니|[0-9]+인치|[0-9]+호|[0-9]+인|홀|소|대|중"
    r"|[LRMSPFGHJV])\))+\s*$",
    re.IGNORECASE,
)


def strip_variant_markers(name: str) -> str:
    """같은 음식의 온도(hot/ice)·사이즈(S/M/L 등) 변형 표기를 벗긴 기본 이름.

    '허브차 아이스(ICED) (L)' → '허브차'. 온도·사이즈별 행은 밀도 차이가 미미해
    (동일 브랜드 병합 그룹 2,384개 실측: 100g당 편차 중앙값 5kcal) 한 음식으로 취급한다
    (2026-08-05 PM 결정).
    """
    name = _TEMP_MARKER_RE.sub(" ", name)
    name = _TRAILING_SIZE_RE.sub("", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_name(name: str) -> str:
    return strip_variant_markers(name).replace(" ", "").strip()


def base_serving_text(item: NutritionItem) -> str:
    """기준 제공량 표기 (예: '1인분(400g)', 비대표 공공 항목은 '100g당')."""
    amount = float(item.base_amount)
    amount_text = f"{amount:g}"
    if item.source == "public" and not item.is_representative and amount == 100:
        # 원본 기준량(100g/100ml) 그대로인 항목 — '1인분' 으로 표기하면 오해라 기준량 노출.
        # 브랜드 제품 중 1회섭취참고량으로 1인분 환산된 것(base_amount != 100)은
        # 아래 분기로 내려가 '1인분(30g)' 으로 표기된다 — 값이 이미 1인분이므로
        # '30g당' 이라고 하면 사용자가 섭취량을 다시 계산해야 한다 (2026-08-04).
        return f"{amount_text}{item.base_unit}당"
    if item.base_unit in ("g", "ml"):
        # 시드·대표 항목은 1인분 기준으로 환산돼 있다 (curate_representative_foods)
        return f"1인분({amount_text}{item.base_unit})"
    return f"{amount_text}{item.base_unit}"


def db_candidates_for_text(
    db: Session, text: str, limit: int = 12
) -> list[NutritionItem]:
    """문장 안에 이름이 등장하는 영양 DB 항목 (자연어 파싱 선(先)-매칭용).

    문장을 토큰화하는 대신 '항목 이름이 문장에 포함되는가'를 뒤집어 검사한다 —
    조사("김밥이랑")·띄어쓰기 문제를 SQL 한 번으로 피한다. 공백 제거한
    normalized_name 기준이며, 1글자 이름("밥")은 과다 매칭이라 제외한다.

    **대표(is_representative) 항목만** 후보로 준다 — match_food_name 이 대표만
    매칭하므로, 비대표 이름에 AI 를 정렬시키면 사후 매칭이 오히려 실패하고
    기준량(100g당)도 1인분 의미가 아니다. 동명 중복은 1건만 남기고,
    구체적(긴) 이름 우선 + 이름순으로 정렬을 고정한다(프롬프트 결정론).
    """
    normalized_text = normalize_name(text)
    if not normalized_text:
        return []
    condition = and_(
        NutritionItem.is_representative.is_(True),
        func.length(NutritionItem.normalized_name) >= 2,
        literal(normalized_text).contains(NutritionItem.normalized_name),
    )
    dedupe_order = (NutritionItem.id,)
    row_rank = (
        func.row_number()
        .over(partition_by=NutritionItem.normalized_name, order_by=dedupe_order)
        .label("row_rank")
    )
    ranked = select(NutritionItem.id.label("item_id"), row_rank).where(condition).subquery()
    return list(
        db.scalars(
            select(NutritionItem)
            .join(ranked, ranked.c.item_id == NutritionItem.id)
            .where(ranked.c.row_rank == 1)
            .order_by(
                func.length(NutritionItem.normalized_name).desc(),
                NutritionItem.normalized_name,
            )
            .limit(limit)
        )
    )


def search_items(
    db: Session, query: str, params: PageParams
) -> tuple[list[NutritionItem], int]:
    """부분일치 검색 + 페이지네이션. (items, total)

    - 관련도 정렬: 정확일치 → 대표 음식 → 전방일치 → 이름 짧은 순.
    - 동명 중복 접기: 같은 normalized_name 은 최상위 1건만 노출 (2026-08-01 PM 결정
      — 포기김치 x30 브랜드 행 문제). total 도 접힌 기준으로 센다.
    - 브랜드명(brand)도 검색 대상에 포함.
    """
    normalized = normalize_name(query)
    condition = or_(
        NutritionItem.normalized_name.contains(normalized),
        NutritionItem.name.contains(query.strip()),
        NutritionItem.brand.contains(query.strip()),
    )
    exact_match = NutritionItem.normalized_name == normalized
    prefix_match = NutritionItem.normalized_name.startswith(normalized)
    rank_order = (
        exact_match.desc(),
        NutritionItem.is_representative.desc(),
        prefix_match.desc(),
        func.length(NutritionItem.name),
        NutritionItem.id,
    )
    # 동명 그룹 내 1위 행만 선별 (row_number — SQLite·Postgres 공통 지원)
    row_rank = (
        func.row_number()
        .over(partition_by=NutritionItem.normalized_name, order_by=rank_order)
        .label("row_rank")
    )
    ranked = select(NutritionItem.id.label("item_id"), row_rank).where(condition).subquery()
    total = (
        db.scalar(
            select(func.count(func.distinct(NutritionItem.normalized_name))).where(condition)
        )
        or 0
    )
    items = list(
        db.scalars(
            select(NutritionItem)
            .join(ranked, ranked.c.item_id == NutritionItem.id)
            .where(ranked.c.row_rank == 1)
            .order_by(*rank_order)
            .offset(params.offset)
            .limit(params.limit)
        )
    )
    return items, total


# --- 트라이그램 유사도 (SCRUM-246) ---------------------------------------
# pg_trgm 과 같은 알고리즘(양끝 패딩 3-gram + Jaccard)을 파이썬으로 구현한다.
# SQL 확장 대신 파이썬인 이유: 테스트가 SQLite 로 돌아 pg_trgm 코드 경로를
# 자동 검증할 수 없고(Postgres 테스트 트랙은 보류), 파이썬 구현은 두 엔진에서
# 동작이 동일하다. 유사도 단계는 정확 일치 실패 시에만 타므로(선매칭 이후 소수)
# 대표 항목 전수 채점(1.5만 건, 수십 ms)이 병목이 되지 않는다.

# 컷 미달이면 매칭 포기 — 4글자 음식명의 끝 한 글자 오타("김치찌게")가 약 0.43,
# 이름만 형제인 다른 음식("물냉면" vs "비빔냉면")이 약 0.13 으로 그 사이 값.
SIMILARITY_CUT = 0.35
# 1등-2등 격차가 이보다 작으면 어느 쪽인지 확신할 수 없다고 보고 매칭 포기.
SIMILARITY_MARGIN = 0.10


def _trigrams(s: str) -> frozenset[str]:
    # pg_trgm 규약: 앞 2칸·뒤 1칸 공백 패딩 후 3글자 슬라이딩
    padded = f"  {s} "
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


def trigram_similarity(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_food_name(db: Session, food_name: str) -> tuple[NutritionItem | None, str]:
    """AI 후보 음식명을 영양 DB 1건에 매칭. (item, path) 를 반환한다.

    path: "exact"(정확 일치) | "substring"(DB 이름이 AI 이름을 포함) |
    "fuzzy"(트라이그램 유사도) | "none"(매칭 포기 → 호출부가 AI 추정 폴백).
    fuzzy 매칭은 호출부에서 confidence 를 감산해 내려보낸다 (SCRUM-246 —
    별도 "유사 매칭" UI 없이 기존 확신도 채널로 불확실성을 전달).

    매칭 대상은 **대표(is_representative) 항목만**이다. 분석 흐름은 매칭값을
    1인분 기준으로 간주해 AI 추정치를 대체하는데, 대표 항목(시드 + 큐레이션)만
    1인분 기준으로 환산돼 있다. 비대표 공공 항목은 100g/100ml 당 값이라
    그대로 쓰면 "김치찌개 19kcal" 같은 오답이 된다 — 검색 화면에서만 노출한다.
    """
    normalized = normalize_name(food_name)
    if not normalized:
        return None, "none"
    representative_only = NutritionItem.is_representative.is_(True)
    exact = db.scalar(
        select(NutritionItem)
        .where(NutritionItem.normalized_name == normalized, representative_only)
        .order_by(NutritionItem.id)
        .limit(1)
    )
    if exact is not None:
        return exact, "exact"

    # 유사도 단계 — 대표 항목 전수를 파이썬에서 채점.
    # 포함(substring) 후보는 구 부분일치의 계승이라 컷 없이 통과시키되 정렬만
    # 유사도 기준으로 바꾼다(동률이면 짧은 이름 → 낮은 id — 구 동작 유지).
    # 포함 후보가 하나도 없을 때만 순수 유사도(fuzzy)로 넘어가며, 이때는
    # 컷·격차 규칙을 모두 통과해야 한다 — 틀린 매칭이 매칭 실패보다 나쁘다.
    query_tri = _trigrams(normalized)
    substring: list[tuple[float, int, int, str]] = []  # (sim, len, id, name)
    fuzzy: list[tuple[float, int, int, str]] = []
    rows = db.execute(
        select(NutritionItem.id, NutritionItem.normalized_name).where(representative_only)
    ).all()
    for item_id, name in rows:
        name_tri = _trigrams(name)
        union = len(query_tri | name_tri)
        sim = len(query_tri & name_tri) / union if union else 0.0
        if normalized in name:
            substring.append((sim, len(name), item_id, name))
        elif sim >= SIMILARITY_CUT:
            fuzzy.append((sim, len(name), item_id, name))

    def _best(cands: list[tuple[float, int, int, str]]) -> tuple[float, int, int, str]:
        return min(cands, key=lambda t: (-t[0], t[1], t[2]))

    if substring:
        return db.get(NutritionItem, _best(substring)[2]), "substring"
    if fuzzy:
        top = _best(fuzzy)
        # 격차 비교는 **다른 이름**끼리만 — 동명 중복 행은 같은 음식이다
        rest = [c for c in fuzzy if c[3] != top[3]]
        if rest and top[0] - _best(rest)[0] < SIMILARITY_MARGIN:
            return None, "none"  # 격차 근소 — 물냉면/비빔냉면류 오연결 방지
        return db.get(NutritionItem, top[2]), "fuzzy"
    return None, "none"
