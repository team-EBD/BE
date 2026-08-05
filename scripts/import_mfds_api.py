"""식약처 가공식품 OpenAPI(JSONL) → nutrition_items 적재.

`fetch_mfds_api.py` 가 받아둔 JSONL 을 읽어 **두 층**으로 적재한다.

1. **브랜드 제품** — 유명 제조사(BRAND_WHITELIST) 것만 개별 항목으로 넣는다.
   무명 제조사·OEM·급식업체 제품(21만 종)은 개별 검색에 노출하지 않는다. 이름만 같고
   실체를 모르는 항목이 검색을 덮어버리기 때문이다.
2. **동명 대표** — 같은 이름을 3개 이상 제조사가 만드는 경우, 그 그룹 전체(무명 포함)의
   값으로 "일반적인 ○○" 1건을 만들어 `is_representative=True` 로 넣는다.
   사진 분석이 '초콜릿'을 인식했을 때 붙일 값이 필요하기 때문이다.
   **무명 제조사 데이터를 버리는 게 아니라 대표값 재료로만 쓴다.**

대표값 산출 (2026-08-04 PM 확정):
    1인분 중량 = median(1회섭취참고량)
    영양소     = 절사평균(제품별 '기준량당 값') × 1인분 중량
절사평균은 상하위 10% 를 잘라낸 평균이다 — 같은 이름에 업소용 대용량이 섞여 있어
단순 평균이 끌려간다. 표본이 5개 미만이면 자르지 않고 중앙값을 쓴다.

**중량은 `servSize`(1회섭취참고량)를 쓴다. `foodSize`(식품중량)가 아니다.**
foodSize 는 포장 총량이라 300톤짜리 업소용까지 있어 1인분과 무관하다
(실측 분포: servSize 중앙 70g·최대 300g / foodSize 중앙 238g·최대 3억g).
servSize 표본이 부족한 그룹만 foodSize 를 상한 1000g 로 잘라 폴백한다.

사용법:
    python -m scripts.fetch_mfds_api processed        # 먼저 수집
    python -m scripts.import_mfds_api                 # 적재
    python -m scripts.import_mfds_api --min-group 5   # 대표 생성 임계값 변경
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics as stats
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import NutritionItem
from scripts.import_public_nutrition import (
    BATCH_SIZE,
    _exclude_reason,
    _parse_amount,
    normalize_name,
    strip_variant_markers,
    transform,
)

DEFAULT_JSONL = Path(__file__).resolve().parents[3] / "ref" / "source" / "mfds_processed.jsonl"

# API 필드 → 표준데이터 CSV 컬럼명. transform()/_exclude_reason() 을 그대로 재사용하기 위한 어댑터.
# 계층 매핑은 실제 데이터 대조로 확인했다 (2026-08-04):
#   foodLv3=대분류 / foodLv4=대표식품명 / foodLv5=중분류 / foodLv6=소분류 / foodLv7=세분류
API_TO_CSV = {
    "식품코드": "foodCd",
    "식품명": "foodNm",
    "데이터구분코드": "dataCd",
    "식품대분류명": "foodLv3Nm",
    "대표식품명": "foodLv4Nm",
    "식품중분류명": "foodLv5Nm",
    "식품소분류명": "foodLv6Nm",
    "영양성분함량기준량": "nutConSrtrQua",
    "에너지(kcal)": "enerc",
    "단백질(g)": "prot",
    "지방(g)": "fatce",
    "탄수화물(g)": "chocdf",
    "당류(g)": "sugar",
    "식이섬유(g)": "fibtg",
    "나트륨(mg)": "nat",
    "콜레스테롤(mg)": "chole",
    "포화지방산(g)": "fasat",
    "트랜스지방산(g)": "fatrn",
    "제조사명": "mfrNm",
    "유통업체명": "distNm",
    "수입업체명": "imptNm",
    "식품중량": "foodSize",
    "1회섭취참고량": "servSize",  # transform() 은 안 쓰고 대표 1인분 산출에만 사용
}

# 1인분으로 인정할 중량 범위(g/ml).
# 상한 300 은 servSize(1회섭취참고량)의 실측 최대값이다 — 그보다 큰 값은 1인분이 아니라
# 포장 총량이 흘러든 것으로 본다. 1000 으로 뒀더니 "원두커피 1인분 1000g/1520kcal",
# "유자청 1000g" 같은 대표가 335건 생겼다 (2026-08-04).
SERVING_MIN, SERVING_MAX = 1.0, 300.0

# 개별 항목으로 노출할 제조사. 데이터에 "유명한가"를 나타내는 필드가 없고,
# 제품 수로 자르면 OEM·급식업체(쿡베이스·현대그린푸드 등)가 상위를 차지해 역효과가 난다.
# → 소비자가 아는 브랜드를 직접 열거한다. 제조사명 부분일치(공백 제거·소문자)로 판정하며,
#   법인이 쪼개진 곳은 각각 적는다 (롯데웰푸드/롯데푸드/롯데제과/롯데칠성).
# 편의점 PB(CU·GS25·세븐일레븐)는 유통사가 아니라 실제 제조사명으로 등록돼 있어 잡히지 않는다.
BRAND_WHITELIST = [
    # 제과·제빵
    "오리온", "롯데웰푸드", "롯데제과", "롯데푸드", "해태제과", "크라운제과",
    "에스피씨삼립", "샤니", "파리크라상", "씨제이푸드빌", "서울식품",
    # 면·라면
    "농심", "오뚜기", "삼양식품", "팔도", "풀무원",
    # 음료
    "롯데칠성", "코카콜라", "웅진식품", "동아오츠카", "광동제약", "일화", "정식품", "델몬트",
    # 유가공
    "매일유업", "남양유업", "빙그레", "서울우유", "푸르밀", "연세", "한국야쿠르트", "에치와이",
    # 종합식품
    "씨제이제일제당", "대상", "동원에프앤비", "동원f&b", "동원홈푸드", "사조", "하림",
    "한성기업", "진주햄", "목우촌", "아워홈", "신세계푸드", "대림선", "대한제분",
    # 유통 PB
    "비지에프", "이마트", "홈플러스",
]

# 대표값 계산에 쓰는 영양소 — transform() 결과 키 기준
_REP_NUTRIENTS = (
    "calories", "carbs", "protein", "fat",
    "sugar", "fiber", "sodium", "cholesterol", "saturated_fat", "trans_fat",
)


def to_csv_row(api_row: dict) -> dict:
    """API 레코드를 표준데이터 CSV 행 모양으로 바꾼다 (기존 로직 재사용용)."""
    return {csv_col: (api_row.get(api_key) or "") for csv_col, api_key in API_TO_CSV.items()}


def _norm(text: str | None) -> str:
    return (text or "").replace(" ", "").lower()


def is_brand(row: dict) -> str | None:
    """제조사명이 화이트리스트에 걸리면 그 키워드, 아니면 None."""
    mfr = _norm(row.get("제조사명"))
    if not mfr:
        return None
    return next((w for w in BRAND_WHITELIST if _norm(w) in mfr), None)


def trimmed_mean(values: list[float], trim_ratio: float = 0.1) -> float:
    """상하위 trim_ratio 를 잘라낸 평균. 표본이 적으면(<5) 중앙값으로 대체한다."""
    if not values:
        raise ValueError("empty")
    if len(values) < 5:
        return stats.median(values)
    ordered = sorted(values)
    cut = int(len(ordered) * trim_ratio)
    core = ordered[cut : len(ordered) - cut] or ordered
    return sum(core) / len(core)


def build_representative(name: str, members: list[dict]) -> dict | None:
    """동명 그룹 → 1인분 기준 대표 항목 1건.

    members 는 transform() 을 통과한 dict 목록(브랜드 무관 전체).
    중량을 모르는 항목은 1인분 중량 산출에서만 빠지고, 영양 밀도 계산에는 참여한다.
    """
    # g/ml 은 수치로 동일 취급한다 (음식 밀도 ≈ 1g/ml) — 단위별로 갈라 세면
    # 죠스바(g 2·ml 2)처럼 최소 표본(3) 미달로 쪼개져 대표가 안 생긴다.
    # 총칭 빌더(build_generic_foods)와 같은 규칙 (2026-08-04). 표기 단위는 최빈값.
    unit = Counter(m["base_unit"] for m in members).most_common(1)[0][0]
    same_unit = members
    if len(same_unit) < 3:
        return None

    # 1인분 = 1회섭취참고량(servSize)의 중앙값. 표본이 3개 미만이면 식품중량으로
    # 폴백한다 — 라면·컵라면·즉석죽·아이스크림 바는 포장 전체가 1회 섭취량이라
    # 폴백이 정답이다 (신라면 120g, 죠스바 75g, 단호박죽 280g).
    # 단, **정확히 100인 식품중량은 표본에서 뺀다** (2026-08-05 PM 결정): 기준량(100g당)을
    # 중량 칸에 복사한 오기재와 구분이 불가능해 "자몽청 1인분(100g) 234kcal" ·
    # "갈비탕 1인분(100g)" 같은 거짓 1인분 대표를 만들었다. servSize 의 100은 실측
    # 신고값이므로 그대로 쓴다 (LA갈비 100g 반찬, 홍삼진액 100ml 파우치 등은 정당).
    servings = [m["_serv"] for m in same_unit if m.get("_serv")]
    if len(servings) < 3:
        servings = [
            m["total_weight"]
            for m in same_unit
            if m.get("total_weight") and m["total_weight"] != 100
            and SERVING_MIN <= m["total_weight"] <= SERVING_MAX
        ]
    if len(servings) < 3:
        return None
    serving = stats.median(servings)
    if not (SERVING_MIN <= serving <= SERVING_MAX):
        return None

    values: dict[str, float] = {}
    for key in _REP_NUTRIENTS:
        # 기준량당 밀도 → 절사평균 → 1인분 환산
        densities = [
            float(m[key]) / float(m["base_amount"])
            for m in same_unit
            if m.get(key) is not None and m.get("base_amount")
        ]
        if not densities:
            continue
        values[key] = round(trimmed_mean(densities) * serving, 2)

    if "calories" not in values:
        return None
    # numeric(8,2) 컬럼 상한(10^6) 방어 — 이상치가 남아 있으면 대표를 만들지 않는다
    if any(v >= 1_000_000 for v in values.values()):
        return None

    category = Counter(m["category"] for m in same_unit).most_common(1)[0][0]
    # 단위 불변식: 음료는 ml (2026-08-05). 단위와 카테고리가 각각 멤버 최빈값이라
    # "복숭아: 음료 + g" 처럼 어긋난 짝이 나올 수 있다 — 카테고리 쪽에 맞춘다.
    if category == "음료":
        unit = "ml"
    # 1인분 열량 상식 상한 — 넘으면 대표를 만들지 않는다 (원본은 검색용으로 남는다).
    # 분말 제품(율무차 등)은 100g당 값이 가루 기준인데 1인분은 타 먹은 잔 기준이라
    # "율무차 200ml 944kcal" 가 됐다 (2026-08-04). 기준이 섞인 건 판별 불가 → 상한으로 방어.
    if float(values["calories"]) > (500 if category == "음료" else 900):
        return None

    digest = hashlib.md5(normalize_name(name).encode()).hexdigest()  # noqa: S324 — 식별자용
    return {
        "external_id": f"rep:{digest}"[:40],
        "name": name[:100],
        "normalized_name": normalize_name(name)[:100],
        "base_amount": round(serving, 2),
        "base_unit": unit,
        "brand": None,
        "category": category,
        "total_weight": round(serving, 2),
        "source": "public",
        "is_representative": True,
        **values,
    }


def run(path: Path, min_group: int, session_factory=SessionLocal) -> dict:
    seen: set[str] = set()
    excluded: Counter = Counter()
    converted: Counter = Counter()
    kept: list[dict] = []  # transform 통과분 전체 (대표값 재료)
    raw = 0

    with path.open(encoding="utf-8") as fp:
        for line in fp:
            raw += 1
            api_row = json.loads(line)
            code = api_row.get("foodCd")
            if not code or code in seen:
                excluded["원본 중복 행"] += 1
                continue
            seen.add(code)

            row = to_csv_row(api_row)
            reason = _exclude_reason(row)
            if reason:
                excluded[reason] += 1
                continue
            values = transform(row)
            if values is None:
                excluded["열량 없음"] += 1
                continue
            values.pop("_estimated", None)
            values["_brand_hit"] = is_brand(row)
            serv = _parse_amount(row.get("1회섭취참고량"))
            # 기준량과 단위가 같을 때만 1인분 후보로 인정. 단 음료는 g/ml 동치 —
            # 기준 단위를 ml 로 강제(2026-08-05)했는데 servSize 가 g 로 적힌 제품이 있다.
            unit_ok = serv and (
                serv[1] == values["base_unit"] or values["category"] == "음료"
            )
            values["_serv"] = serv[0] if unit_ok else None
            kept.append(values)

    # 1층: 브랜드 제품 — 1회섭취참고량이 있으면 1인분 기준으로 환산한다.
    # 원본은 100g 당 값이라 그대로 두면 "츄파춥스 100g 390kcal"(720g 봉지 기준) 같은
    # 표기가 나온다. 원본에 servSize=10g 이 있는데 안 쓰던 것을 쓴다 (2026-08-04).
    brand_rows = [v for v in kept if v["_brand_hit"]]
    for v in brand_rows:
        serv = v.get("_serv")
        if not serv or not (SERVING_MIN <= serv <= SERVING_MAX):
            continue
        base = float(v["base_amount"])
        if base <= 0 or serv == base:
            continue
        factor = serv / base
        for key in _REP_NUTRIENTS:
            if v.get(key) is not None:
                v[key] = round(float(v[key]) * factor, 2)
        v["base_amount"] = round(serv, 2)
        converted["brand_serving"] += 1

    # 2층: 동명 대표 (그룹은 무명 포함 전체로 구성 — 표본이 많을수록 대표값이 안정적)
    groups: dict[str, list[dict]] = defaultdict(list)
    for v in kept:
        groups[v["normalized_name"]].append(v)

    # 같은 이름의 대표(시드·음식 D)가 이미 있으면 rep: 를 만들지 않는다 (2026-08-05).
    # 총칭 빌더의 스킵 규칙과 동일한 이유 — 검색 랭킹의 이름 짧은 순 타이브레이크 때문에
    # 적재 순서(id)만으로는 기존 대표가 이긴다고 보장할 수 없다. 실제로 분말 스틱 제품군
    # rep:"딸기스무디"(30g 60kcal)가 음료 대표 "딸기 스무디"(350g 256kcal)를 가렸다.
    with session_factory() as session:
        existing_rep_names = set(
            session.scalars(
                select(NutritionItem.normalized_name).where(
                    NutritionItem.is_representative.is_(True)
                )
            )
        )

    reps: list[dict] = []
    skipped_existing = 0
    for norm_name, members in groups.items():
        if len(members) < min_group:
            continue
        if norm_name in existing_rep_names:
            skipped_existing += 1
            continue
        # 그룹 키(normalized_name)가 온도·사이즈 마커를 벗긴 값이므로 최빈 원본명에
        # "(대)" 같은 꼬리가 남을 수 있다 — 대표 표시명은 기본 이름으로 통일한다.
        display = strip_variant_markers(
            Counter(m["name"] for m in members).most_common(1)[0][0]
        )
        rep = build_representative(display, members)
        if rep:
            reps.append(rep)

    # 적재 — 브랜드 제품이 대표와 이름이 겹쳐도 external_id 가 달라 공존한다
    payload: dict[str, dict] = {}
    for v in brand_rows:
        v.pop("_brand_hit", None)
        v.pop("_serv", None)
        payload[v["external_id"]] = v
    for r in reps:
        payload[r["external_id"]] = r

    with session_factory() as session:
        existing = set(
            session.scalars(
                select(NutritionItem.external_id).where(NutritionItem.external_id.is_not(None))
            )
        )
        inserted = updated = 0
        batch: list[NutritionItem] = []
        for external_id, values in payload.items():
            if external_id in existing:
                item = session.scalar(
                    select(NutritionItem).where(NutritionItem.external_id == external_id)
                )
                for k, v in values.items():
                    setattr(item, k, v)
                updated += 1
            else:
                batch.append(NutritionItem(**values))
                inserted += 1
            if len(batch) >= BATCH_SIZE:
                session.add_all(batch)
                session.flush()
                batch = []
        session.add_all(batch)
        session.commit()
        total = session.query(NutritionItem).count()

    return {
        "raw": raw,
        "unique": len(seen),
        "excluded": dict(excluded),
        "filtered": len(kept),
        "brand": len(brand_rows),
        "brand_serving": converted["brand_serving"],
        "groups": sum(1 for m in groups.values() if len(m) >= min_group),
        "reps": len(reps),
        "skipped_existing": skipped_existing,
        "inserted": inserted,
        "updated": updated,
        "total": total,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="가공식품 OpenAPI JSONL 적재 (브랜드 + 동명 대표)")
    ap.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    ap.add_argument(
        "--min-group", type=int, default=3,
        help="대표를 만들 최소 동명 제품 수 (기본 3)",
    )
    args = ap.parse_args()
    if not args.jsonl.exists():
        raise SystemExit(f"[mfds] 파일 없음: {args.jsonl}\n  → python -m scripts.fetch_mfds_api processed 먼저 실행")

    r = run(args.jsonl, args.min_group)
    print(f"[mfds] 원본 {r['raw']:,}행 → 고유 {r['unique']:,}종")
    for reason, cnt in sorted(r["excluded"].items(), key=lambda x: -x[1]):
        print(f"[mfds]   제외 - {reason}: {cnt:,}건")
    print(f"[mfds] 필터 통과 {r['filtered']:,}종")
    print(f"[mfds]   ├ 브랜드 제품     {r['brand']:,}종 (1회섭취참고량으로 1인분 환산 {r['brand_serving']:,}종)")
    print(f"[mfds]   └ 동명 대표       {r['reps']:,}건 (동명 {args.min_group}개 이상 그룹 {r['groups']:,}개,"
          f" 기존 대표와 동명이라 스킵 {r['skipped_existing']:,}개)")
    print(f"[mfds] 신규 {r['inserted']:,} / 갱신 {r['updated']:,} / 테이블 총 {r['total']:,}행")


if __name__ == "__main__":
    main()
