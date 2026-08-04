"""공공 영양DB 적재 — 전국통합식품영양성분정보 표준데이터(식약처) → nutrition_items.

사용법 (BE 루트에서):
    python -m scripts.import_public_nutrition <CSV 경로> [<CSV 경로> ...]

입력 형식: 공공데이터포털 표준데이터 CSV (CP949/UTF-8 자동 감지).
식품분류 컬럼(식품대분류명)이 있는 버전만 지원한다 — 분류 없는 구버전 파일은
이름 기반 판별이 필요하므로 2단계에서 별도 처리한다.

처리 규칙
- 멱등: `external_id`(식품코드) 를 자연키로 upsert. 재실행해도 중복이 생기지 않는다.
- 필터(가공식품 P): 재료성 대분류(식용유지류·조미식품·장류 등) 제외.
  농산가공식품류·기타식품류는 재료/건강기능식품성 이름 패턴을 추가로 제외.
- 필터(음식 D): 전부 포함.
- 이름 정리(음식 D): "대표식품_상세명" 형태는 상세명을 표시명으로 쓰되,
  normalized_name 은 원본 전체(공백 제거)로 두어 "허브차" 같은 접두어 검색도 잡히게 한다.
- 영양값 기준: 원본 그대로 100g/100ml 당 값. base_amount=100, total_weight 에 총량 저장.
- 결측 탄수/지방 (프랜차이즈 음식 대부분): 열량 균형식(kcal=4C+4P+9F)으로 추정 보완.
  지방은 포화지방×2 를 상한 내에서 사용. 열량·단백질·당류·나트륨은 원본 실측값 그대로다.
- 카테고리: 기존 시드 체계(한식/분식/면류/중식/외식/배달/편의점/간식/샐러드/음료)에 매핑
  — AI recommend 의 카테고리 그룹핑이 이 체계를 사용하므로 유지해야 한다.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import NutritionItem

BATCH_SIZE = 1000

# ---------------------------------------------------------------- 필터 규칙

# 가공식품(P)에서 통째로 제외하는 대분류 — 요리 재료·건강기능식품이라 식단 검색 대상이 아님
EXCLUDE_P_MAJOR = {
    "식용유지류",
    "조미식품",
    "장류",
    "잼류",
    "당류",
    "특수영양식품",
    "특수의료용도식품",
    "벌꿀 및 화분가공 식품류",
    "동물성가공식품류",
    "알가공품류",
}

# 농산가공식품류에서 포함하는 중분류 (나머지는 밀가루·전분 등 재료성)
INCLUDE_FARM_MID = {"땅콩 또는 견과류가공품류", "시리얼류", "기타 농산가공품류"}

# 농산가공식품류·기타식품류에 한해 적용하는 이름 제외 패턴 (재료·보충제)
NAME_BLACKLIST = re.compile(
    r"프리믹스|믹스|분말|가루|페이스트|퓨레|엑기스|원액|농축|통조림|다이스|"
    r"유산균|콜라겐|아르기닌|커큐민|프로틴|단백질보충|NMN|추출|효소|올리고당"
)

# 가공식품(P) 대분류 → 카테고리 (미기재 대분류는 편의점)
P_CATEGORY = {
    "즉석식품류": "편의점",
    "면류": "면류",
    "음료류": "음료",
    "과자류·빵류 또는 떡류": "간식",
    "코코아가공품류 또는 초콜릿류": "간식",
    "빙과류": "간식",
    "유가공품류": "간식",
}

# 음식(D) 대분류 → 카테고리 (미기재 대분류는 한식)
D_CATEGORY = {
    "음료 및 차류": "음료",
    "빵 및 과자류": "간식",
    "유제품류 및 빙과류": "간식",
    "면 및 만두류": "면류",
    "튀김류": "분식",
}

# 음식(D) 대표식품명 우선 매핑 — 대분류보다 먼저 적용 (피자가 '빵 및 과자류'로 잡히는 것 보정)
D_REPR_CATEGORY = {
    "피자": "배달",
    "버거": "배달",
    "닭튀김": "배달",
    "떡볶이": "분식",
    "김밥": "분식",
    "샐러드": "샐러드",
}

_INVALID_BRAND = {"", "해당없음", "알수없음"}

# 원본이 ml 로 기재해도 g 으로 고칠 대분류 — 음식편(D) 한정.
# 급식 조사 데이터라 밥·볶음·구이·나물까지 ml 로 적힌 행이 3,155건 있다(2026-08-04 전수조사).
# 액체가 아닌 음식에 ml 는 맞지 않고, 국·탕·찌개도 음식으로는 g 이 통상 표기다.
# 밀도 1 가정으로 단위만 바꾼다 — 수치는 건드리지 않는다.
_LIQUID_D_MAJOR = {"음료 및 차류"}

_AMOUNT_RE = re.compile(r"^([\d.,]+)\s*(g|ml|kg|l)\b", re.IGNORECASE)


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_amount(text: str | None) -> tuple[float, str] | None:
    """'1640g' / '100ml' / '1.5kg' → (양, 단위 g/ml). 실패 시 None."""
    if not text:
        return None
    m = _AMOUNT_RE.match(text.strip())
    if not m:
        return None
    amount = float(m.group(1).replace(",", ""))
    unit = m.group(2).lower()
    if unit == "kg":
        return amount * 1000, "g"
    if unit == "l":
        return amount * 1000, "ml"
    return amount, unit


def normalize_name(name: str) -> str:
    # app.services.matching.normalize_name 과 동일 규칙 (공백 제거)
    return name.replace(" ", "").strip()


def _pick_brand(row: dict) -> str | None:
    for col in ("제조사명", "업체명", "유통업체명", "수입업체명"):
        value = (row.get(col) or "").strip()
        if value not in _INVALID_BRAND:
            return value[:100]
    return None


def _fill_missing_macros(
    calories: float, carbs: float | None, protein: float | None,
    fat: float | None, sugar: float | None, saturated_fat: float | None,
) -> tuple[float, float, float, bool]:
    """결측 탄수/지방/단백질을 열량 균형식으로 추정. (carbs, protein, fat, estimated) 반환."""
    estimated = False
    if protein is None:
        protein = 0.0
        estimated = True
    if fat is None and carbs is None:
        remaining = max(calories - 4 * protein, 0.0)
        if saturated_fat is not None:
            fat = min(saturated_fat * 2, remaining / 9)  # 포화지방:전체지방 ≈ 1:2 가정
        else:
            fat = 0.35 * remaining / 9  # 잔여 열량의 35%를 지방으로 가정
        carbs = max((remaining - 9 * fat) / 4, sugar or 0.0)
        estimated = True
    elif fat is None:
        fat = max((calories - 4 * (carbs or 0) - 4 * protein) / 9, saturated_fat or 0.0, 0.0)
        estimated = True
    elif carbs is None:
        carbs = max((calories - 4 * protein - 9 * fat) / 4, sugar or 0.0, 0.0)
        estimated = True
    return round(carbs, 2), round(protein, 2), round(fat, 2), estimated


def _resolve_category(row: dict) -> str:
    if row["데이터구분코드"] == "D":
        repr_name = (row.get("대표식품명") or "").strip()
        if repr_name in D_REPR_CATEGORY:
            return D_REPR_CATEGORY[repr_name]
        return D_CATEGORY.get(row["식품대분류명"], "한식")
    return P_CATEGORY.get(row["식품대분류명"], "편의점")


def _exclude_reason(row: dict) -> str | None:
    """제외 대상이면 사유 문자열, 아니면 None."""
    if row["데이터구분코드"] != "P":
        return None
    major = row["식품대분류명"]
    mid = row.get("식품중분류명", "")
    if major in EXCLUDE_P_MAJOR:
        return f"재료성 대분류({major})"
    if major == "농산가공식품류":
        if mid not in INCLUDE_FARM_MID:
            return f"재료성 중분류({mid})"
        if NAME_BLACKLIST.search(row["식품명"]):
            return "재료·보충제 이름 패턴"
    if major == "기타식품류" and NAME_BLACKLIST.search(row["식품명"]):
        return "재료·보충제 이름 패턴"
    return None


def transform(row: dict) -> dict | None:
    """CSV 행 → nutrition_items 필드 dict. 적재 불가 행은 None."""
    calories = _num(row.get("에너지(kcal)"))
    if calories is None:
        return None

    raw_name = row["식품명"].strip()
    display_name = raw_name
    if row["데이터구분코드"] == "D" and "_" in raw_name:
        # "대표식품_상세명" → 상세명이 대표식품을 이미 포함하면 상세명만
        # (예: 피자_불고기 피자 → 불고기 피자), 아니면 붙여서 맥락 유지
        # (예: 삼각김밥_숯불갈비 → 삼각김밥 숯불갈비)
        prefix, suffix = (part.strip() for part in raw_name.split("_", 1))
        if suffix:
            if normalize_name(prefix) in normalize_name(suffix):
                display_name = suffix
            else:
                display_name = f"{prefix} {suffix}"

    base = _parse_amount(row.get("영양성분함량기준량")) or (100.0, "g")
    total = _parse_amount(row.get("식품중량"))

    # 음식편의 ml 오기재 교정 (음료·차류만 ml 유지)
    if (
        row["데이터구분코드"] == "D"
        and base[1] == "ml"
        and row.get("식품대분류명", "").strip() not in _LIQUID_D_MAJOR
    ):
        base = (base[0], "g")
        if total and total[1] == "ml":
            total = (total[0], "g")

    carbs, protein, fat, estimated = _fill_missing_macros(
        calories,
        _num(row.get("탄수화물(g)")),
        _num(row.get("단백질(g)")),
        _num(row.get("지방(g)")),
        _num(row.get("당류(g)")),
        _num(row.get("포화지방산(g)")),
    )

    return {
        "external_id": row["식품코드"].strip()[:40],
        "name": display_name[:100],
        "normalized_name": normalize_name(raw_name)[:100],
        "base_amount": base[0],
        "base_unit": base[1],
        "calories": calories,
        "carbs": carbs,
        "protein": protein,
        "fat": fat,
        "sugar": _num(row.get("당류(g)")),
        "fiber": _num(row.get("식이섬유(g)")),
        "sodium": _num(row.get("나트륨(mg)")),
        "cholesterol": _num(row.get("콜레스테롤(mg)")),
        "saturated_fat": _num(row.get("포화지방산(g)")),
        "trans_fat": _num(row.get("트랜스지방산(g)")),
        "brand": _pick_brand(row),
        "category": _resolve_category(row),
        "total_weight": total[0] if total and total[1] == base[1] else None,
        "source": "public",
        "_estimated": estimated,  # 통계용 — DB 컬럼 아님
    }


def read_rows(path: Path) -> list[dict]:
    for encoding in ("cp949", "utf-8-sig"):
        try:
            with path.open(newline="", encoding=encoding) as fp:
                rows = list(csv.DictReader(fp))
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"[import] 인코딩 인식 실패: {path}")
    if not rows or "식품대분류명" not in rows[0]:
        raise SystemExit(
            f"[import] 지원하지 않는 형식(식품분류 컬럼 없음): {path}\n"
            "  → 분류 컬럼이 있는 표준데이터 CSV 를 사용하세요. 구버전 파일은 2단계에서 처리합니다."
        )
    return rows


def run(paths: list[Path], session_factory=SessionLocal) -> dict:
    stats: Counter = Counter()
    excluded: Counter = Counter()
    category_dist: Counter = Counter()
    pending: dict[str, dict] = {}  # external_id → values (파일 간 중복 제거)

    for path in paths:
        rows = read_rows(path)
        stats["read"] += len(rows)
        for row in rows:
            reason = _exclude_reason(row)
            if reason:
                excluded[reason] += 1
                continue
            values = transform(row)
            if values is None:
                excluded["열량 없음"] += 1
                continue
            pending[values["external_id"]] = values

    with session_factory() as session:
        existing_ids = set(
            session.scalars(
                select(NutritionItem.external_id).where(NutritionItem.external_id.is_not(None))
            )
        )
        inserted = updated = 0
        batch: list[NutritionItem] = []
        for external_id, values in pending.items():
            if values.pop("_estimated", False):
                stats["macros_estimated"] += 1
            category_dist[values["category"]] += 1
            if external_id in existing_ids:
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
        "read": stats["read"],
        "excluded": dict(excluded),
        "macros_estimated": stats["macros_estimated"],
        "inserted": inserted,
        "updated": updated,
        "total": total,
        "categories": dict(category_dist.most_common()),
    }


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("사용법: python -m scripts.import_public_nutrition <CSV 경로> ...")
    paths = [Path(p) for p in sys.argv[1:]]
    for p in paths:
        if not p.exists():
            raise SystemExit(f"[import] 파일 없음: {p}")
    result = run(paths)
    print(f"[import] 읽음 {result['read']}건")
    for reason, count in sorted(result["excluded"].items(), key=lambda x: -x[1]):
        print(f"[import]   제외 - {reason}: {count}건")
    print(f"[import] 탄수/지방 추정 보완: {result['macros_estimated']}건")
    print(f"[import] 신규 {result['inserted']} / 갱신 {result['updated']} / 테이블 총 {result['total']}행")
    print("[import] 카테고리 분포:")
    for cat, count in result["categories"].items():
        print(f"[import]   {cat}: {count}")


if __name__ == "__main__":
    main()
