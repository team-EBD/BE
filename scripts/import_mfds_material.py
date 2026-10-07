"""식약처 전국통합식품영양성분정보(원재료성식품) JSONL → nutrition_items 적재.

`fetch_mfds_api material` 이 받아둔 농축수산물 3,704종(사과·당근·무·달걀·소고기 부위 …)을 넣는다.
전부 100g 당 값(serving_basis=per_100g)이다 — 분석은 AI 가 본 g × 100g 당 값으로 계산하므로(2026-10-06)
1인분 표가 필요 없고, 검색 화면(1인분 대표만 노출)에는 나오지 않는다.

두 층으로 넣는다:
1. **변형 행** — 원본 그대로 ("사과_감홍_생것" → "사과 감홍 생것"). external_id = 식품코드.
2. **총칭 행** — AI 가 말하는 이름("사과", "당근", "돼지고기 삼겹살")과 정확히 맞는 행. 같은 대표식품명(또는
   대표식품명+중분류)의 **생것** 행 평균(생것이 없으면 '평균' 행, 그마저 없으면 전체 평균).
   external_id = "R-gen:<분류코드>". "가자미류"처럼 '류'로 끝나는 이름은 '류'를 뗀다(가자미).

사용법:
    python -m scripts.import_mfds_material                       # scripts/data/mfds_material.jsonl (저장소 동봉)
    python -m scripts.import_mfds_material --path /tmp/m.jsonl --dry-run
재실행해도 external_id 기준으로 갱신만 된다(멱등).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as stats
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import NutritionItem
from app.services.matching import normalize_name
from scripts.import_mfds_api import to_csv_row
from scripts.import_public_nutrition import BATCH_SIZE, transform

# 원본(3MB)은 저장소에 함께 둔다 — 운영 컨테이너에서 API 키 없이 바로 적재하기 위해 (2026-10-07)
DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "mfds_material.jsonl"
CATEGORY = "식재료"
# 조리 상태·시기 토큰 — 총칭 이름을 만들 때 제외하고, '생것'은 총칭 값의 1순위 재료
STATE_TOKENS = frozenset({
    "생것", "말린것", "데친것", "삶은것", "구운것", "찐것", "볶은것", "튀긴것", "평균", "껍질제거", "껍질포함",
    "조린것", "절인것", "염장", "훈제", "냉동", "통조림", "가루", "즙", "건조", "동결건조",
})
_MONTH_RE = re.compile(r"^\d{1,2}월$")
_PAREN_RE = re.compile(r"\(.*?\)")
# 부위 토큰 — 기본 총칭('달걀')을 낼 때 제외한다 (난황 326kcal 이 섞이면 달걀이 175 가 된다)
PART_TOKENS = frozenset({"난황", "난백", "내장", "껍질", "뼈", "머리", "꼬리", "지느러미", "알"})
# 등급·원산지 토큰 — 이름에 넣지 않는다 ("소고기 한우(1++등급)" 은 아무도 안 쓴다)
_GRADE_RE = re.compile(r"등급|수입산|국내산|한우|육우|한돈|토종|성계|영계|오골계|해당없음")
_NUTRIENTS = ("calories", "carbs", "protein", "fat")
_OPTIONAL = ("sugar", "fiber", "sodium", "cholesterol", "saturated_fat", "trans_fat")


def display_name(raw: str) -> str:
    return " ".join(part for part in raw.split("_") if part).strip()


def base_name(lv4: str) -> str:
    """'가자미류' → '가자미'. 두 글자 이름('게류' → '게')도 뗀다."""
    lv4 = (lv4 or "").strip()
    return lv4[:-1] if lv4.endswith("류") and len(lv4) >= 2 else lv4


def _is_state(token: str) -> bool:
    return (not token) or token in STATE_TOKENS or bool(_MONTH_RE.match(token))


def _last_token(raw: str) -> str:
    return raw.split("_")[-1] if raw else ""


def _clean(token: str) -> str:
    """'삼겹살(삼겹살)' → '삼겹살', '가슴(껍질 제거)' → '가슴'."""
    return _PAREN_RE.sub("", token or "").strip()


def _is_part(token: str) -> bool:
    return _clean(token) in PART_TOKENS


def generic_keys(api_row: dict) -> list[tuple[str, str]]:
    """이 행이 기여하는 총칭 (external 코드, 이름) 목록 — 대표식품명, 대표식품명+중분류."""
    lv4 = base_name(api_row.get("foodLv4Nm") or "")
    if not lv4:
        return []
    keys = [(f"R-gen:{api_row.get('foodLv4Cd') or lv4}", lv4)]
    for level in ("5", "6"):
        tok = _clean(api_row.get(f"foodLv{level}Nm") or "")
        if not tok or _is_state(tok) or _GRADE_RE.search(tok) or _is_part(tok):
            continue
        if normalize_name(tok) == normalize_name(lv4):
            continue
        code = api_row.get(f"foodLv{level}Cd") or (lv4 + tok)
        keys.append((f"R-gen:{code}", f"{lv4} {tok}"))
    return keys


def build_generics(members: list[tuple[dict, dict]]) -> dict[str, dict]:
    """[(api_row, 변형 values)] → {external_id: 총칭 values}. 생것 > 평균 > 전체 순으로 재료를 고른다."""
    groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    names: dict[str, str] = {}
    for api_row, values in members:
        for ext, name in generic_keys(api_row):
            # 기본 총칭(대표식품명 하나)에는 부위 행(난황·난백·내장)을 섞지 않는다
            if " " not in name and any(_is_part(api_row.get(f"foodLv{lv}Nm") or "") for lv in ("5", "6")):
                continue
            groups[ext].append((api_row, values))
            names[ext] = name
    out: dict[str, dict] = {}
    for ext, rows in groups.items():
        raw = [r for r in rows if _last_token(r[0].get("foodNm", "")) == "생것"]
        chosen = raw or [r for r in rows if _last_token(r[0].get("foodNm", "")) == "평균"] or rows
        vals = [v for _, v in chosen]
        unit = Counter(v["base_unit"] for v in vals).most_common(1)[0][0]
        same = [v for v in vals if v["base_unit"] == unit] or vals
        values = {k: round(stats.fmean(float(v[k]) for v in same), 2) for k in _NUTRIENTS}
        for k in _OPTIONAL:
            present = [float(v[k]) for v in same if v.get(k) is not None]
            values[k] = round(stats.fmean(present), 2) if present else None
        name = names[ext]
        out[ext] = {
            "name": name[:100], "normalized_name": normalize_name(name)[:100],
            "base_amount": 100.0, "base_unit": unit, "category": CATEGORY, "source": "public",
            "brand": None, "external_id": ext, "total_weight": None, "is_representative": False,
            "macros_estimated": any(v.get("macros_estimated") for v in same), "serving_basis": "per_100g",
            **values,
        }
    return out


def load(path: Path) -> tuple[list[tuple[dict, dict]], Counter]:
    members: list[tuple[dict, dict]] = []
    excluded: Counter = Counter()
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            api_row = json.loads(line)
            code = api_row.get("foodCd")
            if not code or code in seen:
                excluded["중복"] += 1
                continue
            seen.add(code)
            values = transform(to_csv_row(api_row))
            if values is None:
                excluded["열량 없음·비현실"] += 1
                continue
            name = display_name(api_row.get("foodNm") or "")
            values.update({
                "name": name[:100], "normalized_name": normalize_name(name)[:100], "category": CATEGORY,
                "brand": None, "is_representative": False, "serving_basis": "per_100g",
            })
            members.append((api_row, values))
    return members, excluded


def run(path: Path, dry_run: bool = False) -> dict:
    members, excluded = load(path)
    generics = build_generics(members)
    payload = {v["external_id"]: v for _, v in members}
    payload.update(generics)
    if dry_run:
        return {"variants": len(members), "generics": len(generics), "excluded": dict(excluded), "dry_run": True}
    inserted = updated = 0
    with SessionLocal() as session:
        existing = set(session.scalars(select(NutritionItem.external_id).where(NutritionItem.external_id.is_not(None))))
        batch: list[NutritionItem] = []
        for ext, values in payload.items():
            if ext in existing:
                item = session.scalar(select(NutritionItem).where(NutritionItem.external_id == ext))
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
    return {"variants": len(members), "generics": len(generics), "excluded": dict(excluded),
            "inserted": inserted, "updated": updated}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(args.path, dry_run=args.dry_run)
    print(f"[material] 변형 행 {result['variants']:,} · 총칭 행 {result['generics']:,} · 제외 {result['excluded']}")
    if not result.get("dry_run"):
        print(f"[material] 삽입 {result['inserted']:,} · 갱신 {result['updated']:,}")


if __name__ == "__main__":
    main()
