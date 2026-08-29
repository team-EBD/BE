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
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import NutritionItem

# 정규화 규칙의 정의처는 app.services.matching — 검색·매칭과 적재가 반드시 같은 규칙이어야
# 정확일치가 성립한다. 여기서 re-export 해 다른 적재 스크립트들이 가져다 쓴다.
from app.services.matching import normalize_name, strip_variant_markers  # noqa: F401

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




# 음료 머리어 — 상세명이 이걸로 끝나면 자기 설명이 되므로 분류 접두어가 불필요하다.
# 한국어 합성어는 head-final: 이름의 끝이 음식의 정체다 (총칭 대표와 같은 원리).
_DRINK_HEADS = (
    "티", "차", "라떼", "주스", "스무디", "에이드", "커피", "아메리카노",
    "에스프레소", "콜드브루", "프라페", "프라푸치노", "쉐이크", "셰이크",
    "블렌디드", "리프레셔", "밀크티", "버블티",
)
_DRINK_HEAD_BLACKLIST = ("스파게티",)  # '티'로 끝나지만 음료가 아닌 것


def _suffix_self_describing(suffix: str) -> bool:
    n = normalize_name(suffix)
    if any(n.endswith(b) for b in _DRINK_HEAD_BLACKLIST):
        return False
    return any(n.endswith(h) for h in _DRINK_HEADS)


def display_name_from_raw(raw_name: str) -> str:
    """음식(D)의 '대표식품명_상세명' 원본명 → 표시명. (curate 도 이 함수를 쓴다)

    - 상세명이 대표식품명을 이미 포함하면 상세명만 (피자_불고기 피자 → 불고기 피자).
    - 상세명이 음료 머리어(티·라떼·주스 등)로 끝나면 접두어를 뗀다 —
      "기타차_딸기티" 는 "딸기티" 로 충분하다 (2026-08-05 PM 지적).
    - 그 외에는 접두어를 붙여 맥락을 유지한다: "볶음밥_채소"·"김밥_계란" 은
      접두어를 떼면 재료명만 남아 엉뚱한 음식이 된다 (제거 규칙 전수 검증 08-05).
    """
    name = raw_name.strip()
    if "_" not in name:
        return name
    prefix, suffix = (part.strip() for part in name.split("_", 1))
    if not suffix:
        return name
    if normalize_name(prefix) in normalize_name(suffix):
        return suffix
    if _suffix_self_describing(suffix):
        return suffix
    return f"{prefix} {suffix}"


def _pick_brand(row: dict) -> str | None:
    for col in ("제조사명", "업체명", "유통업체명", "수입업체명"):
        value = (row.get(col) or "").strip()
        if value not in _INVALID_BRAND:
            return value[:100]
    return None


# ---------------------------------------------------------------- 탄단지 결측 추정
#
# 탄수+지방 동시 결측(프랜차이즈 12,653행)은 방정식(kcal=4C+4P+9F) 하나에 미지수가
# 둘이라 자유도를 하나 정해야 한다. 초판의 "잔여열량의 35%가 지방" 상수는 근거 없는
# 가정이었다 (실측 지방 몫: 밥류 15% ~ 구이류 78%, 2026-08-05 PM 재조사 지시).
# → **실측 앵커식**으로 교체: 그 행의 포화지방 실측 × 같은 대표식품명 완전실측 행들의
#   지방/포화 비율(절사 중앙값, 표본 10+)로 지방을 정하고, 탄수는 열량 항등식 역산에
#   당류 실측을 하한으로 강제한다. 백테스트(완전실측 행에서 가리고 맞히기):
#   탄수 MAE 2.6→1.2 g, 지방 1.2→0.5 g. 대분류 폴백은 두지 않는다(PM 결정) —
#   표본 미달 99종은 사람 검증 참조표(data/manual_macro_shares.json)가 담당한다.

_MANUAL_SHARES_PATH = Path(__file__).resolve().parent / "data" / "manual_macro_shares.json"
MACRO_RATIOS_ARTIFACT = (
    Path(__file__).resolve().parents[3] / "ref" / "source" / "macro_ratios_computed.json"
)
_MIN_SAMPLES = 10  # 그룹 실측 비율을 인정하는 최소 표본 (2026-08-05 PM 확정)


def _trimmed_median(values: list[float]) -> float:
    """상하위 10% 절사 후 중앙값 — 극단값(오기재)이 비율을 끌고 가지 못하게."""
    if len(values) >= 10:
        values = sorted(values)
        k = len(values) // 10
        values = values[k: len(values) - k or None]
    return statistics.median(values)


class MacroEstimator:
    """대표식품명별 실측 통계로 결측 탄수·지방을 추정한다.

    비율 풀은 **탄단지 완전실측 행만** 쓴다 — 추정치가 통계 재료로 되돌아오는 순환 없음.
    위생 필터: 지방<포화(물리 모순), 탄단지 열량이 표기 열량과 30% 이상 어긋나는 행 제외.
    """

    def __init__(self, ratio: dict, share: dict):
        self.ratio = ratio  # 대표식품명 → 지방/포화지방 비율 (절사 중앙값)
        self.share = share  # 대표식품명 → 잔여열량 중 지방 몫 (포화 실측 없는 행용)
        try:
            manual = json.loads(_MANUAL_SHARES_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            manual = {}
        self.manual = {
            k: v["fat_share"] for k, v in manual.items() if not k.startswith("_")
        }

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "MacroEstimator":
        ratio_by: dict[str, list[float]] = {}
        share_by: dict[str, list[float]] = {}
        for r in rows:
            if r.get("데이터구분코드") != "D":
                continue
            kcal = _num(r.get("에너지(kcal)"))
            c = _num(r.get("탄수화물(g)"))
            p = _num(r.get("단백질(g)"))
            f = _num(r.get("지방(g)"))
            if None in (kcal, c, p, f):
                continue
            sat = _num(r.get("포화지방산(g)"))
            macro_kcal = 4 * c + 4 * p + 9 * f
            # 위생 필터 — 자기모순 행은 통계 풀에서 제외
            if sat is not None and f < sat:
                continue
            if kcal >= 20 and abs(macro_kcal - kcal) > 0.3 * kcal:
                continue
            repr_name = (r.get("대표식품명") or "").strip()
            remaining = kcal - 4 * p
            if sat and sat > 0.2 and f >= sat:
                ratio_by.setdefault(repr_name, []).append(f / sat)
            if remaining > 10:
                share_by.setdefault(repr_name, []).append(
                    min(max(9 * f / remaining, 0.0), 1.0)
                )
        return cls(
            {k: _trimmed_median(v) for k, v in ratio_by.items() if len(v) >= _MIN_SAMPLES},
            {k: _trimmed_median(v) for k, v in share_by.items() if len(v) >= _MIN_SAMPLES},
        )

    def save(self, path: Path = MACRO_RATIOS_ARTIFACT) -> None:
        """가공식품(import_mfds_api) 프로세스가 같은 통계를 쓰도록 아티팩트 저장."""
        path.write_text(
            json.dumps({"ratio": self.ratio, "share": self.share}, ensure_ascii=False),
            encoding="utf-8",
        )

    @classmethod
    def from_artifact(cls, path: Path = MACRO_RATIOS_ARTIFACT) -> "MacroEstimator | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return cls(data["ratio"], data["share"])

    def fill(
        self, repr_name: str, calories: float, protein: float,
        sugar: float | None, saturated_fat: float | None,
    ) -> tuple[float, float] | None:
        """(carbs, fat) 추정. 그룹 실측도 참조표도 없으면 None (커버리지 밖)."""
        remaining = max(calories - 4 * protein, 0.0)
        if saturated_fat and saturated_fat > 0 and repr_name in self.ratio:
            fat = min(max(saturated_fat * self.ratio[repr_name], saturated_fat), remaining / 9)
        else:
            fat_share = self.share.get(repr_name, self.manual.get(repr_name))
            if fat_share is None:
                return None
            fat = fat_share * remaining / 9
        carbs = (remaining - 9 * fat) / 4
        if sugar and carbs < sugar:
            # 탄수는 당류보다 작을 수 없다 → 탄수를 당류로 고정하고 지방을 재역산
            carbs = sugar
            fat = max((remaining - 4 * carbs) / 9, saturated_fat or 0.0, 0.0)
        return max(carbs, 0.0), fat


# 적재 스크립트가 프로세스 시작 시 설정한다 (import_public: from_rows / import_mfds: from_artifact)
_macro_estimator: MacroEstimator | None = None


def set_macro_estimator(estimator: MacroEstimator | None) -> None:
    global _macro_estimator
    _macro_estimator = estimator


def _fill_missing_macros(
    calories: float, carbs: float | None, protein: float | None,
    fat: float | None, sugar: float | None, saturated_fat: float | None,
    repr_name: str = "",
) -> tuple[float, float, float, bool]:
    """결측 탄수/지방/단백질 보완. (carbs, protein, fat, estimated) 반환.

    1개 결측은 열량 항등식으로 정확 역산(가정 불필요), 탄수+지방 동시 결측은
    MacroEstimator(실측 앵커식). 커버리지 밖이면 최소가정 보수 채움(지방=포화 하한).
    """
    estimated = False
    if protein is None:
        protein = 0.0
        estimated = True
    if fat is None and carbs is None:
        filled = (
            _macro_estimator.fill(repr_name, calories, protein, sugar, saturated_fat)
            if _macro_estimator
            else None
        )
        if filled is not None:
            carbs, fat = filled
        else:
            # 커버리지 밖 — 확실한 하한만 쓴다 (지방=포화 실측, 탄수=잔여·당류 하한)
            remaining = max(calories - 4 * protein, 0.0)
            fat = min(saturated_fat or 0.0, remaining / 9)
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
    # 물리적으로 불가능한 밀도(순수 지방 9kcal/g 초과)는 원본 오기재 — 적재하지 않는다
    # (2026-08-04 실측: 팔공티 마카롱 7건이 100g당 1,070~1,338kcal 로 기재돼 있었다)
    if calories > 900:
        return None
    # 음료가 100ml당 200kcal 초과면 오기재 — 가장 진한 밀크셰이크도 150 수준이다.
    # (2026-08-04 실측: 전체 음료·차류의 kcal 분포는 중앙값 57 · p99 146 인데
    #  프랜차이즈 스무디 12건이 200~351 로 기재돼 1인분 환산 시 1,229kcal 가 됐다)
    if (
        row["데이터구분코드"] == "D"
        and row.get("식품대분류명", "").strip() == "음료 및 차류"
        and calories > 200
    ):
        return None

    raw_name = row["식품명"].strip()
    display_name = (
        display_name_from_raw(raw_name)
        if row["데이터구분코드"] == "D"
        else raw_name
    )
    # 온도·사이즈 마커는 비대표 행의 표시명에서도 벗긴다 (B안, 2026-08-05 PM 결정).
    # 브랜드 검색에서 대표가 다른 브랜드일 때 "핫(HOT) (L)" 이름이 새어 나오고,
    # 그 행을 기록하면 스냅샷에도 남는다 — 사용자는 어디서도 이 문구를 보지 않는다.
    # 원본 변형 정보는 CSV·식품코드(external_id)로 언제든 복원 가능.
    display_name = strip_variant_markers(display_name) or display_name

    base = _parse_amount(row.get("영양성분함량기준량")) or (100.0, "g")
    total = _parse_amount(row.get("식품중량"))

    # 단위 불변식: 음료는 ml, 액체 아닌 음식(음식편)은 g.
    # 수치는 그대로 둔다 — 마시는 제품 밀도 ≈ 1g/ml 라 표기 교정만이다.
    # ① 음식편의 ml 오기재 교정 (밥·볶음까지 ml 로 기재된 3,155건, 2026-08-04)
    if (
        row["데이터구분코드"] == "D"
        and base[1] == "ml"
        and row.get("식품대분류명", "").strip() not in _LIQUID_D_MAJOR
    ):
        base = (base[0], "g")
        if total and total[1] == "ml":
            total = (total[0], "g")
    # ② 역방향 — 음료인데 g 로 기재된 3,361건 (2026-08-05 PM 지적: 녹차·쿠키라떼가 g 표기)
    category = _resolve_category(row)
    if category == "음료" and base[1] == "g":
        base = (base[0], "ml")
        if total and total[1] == "g":
            total = (total[0], "ml")

    # 보조 영양소 오기재 무력화 — 기준량보다 크거나 그 성분만으로 총열량을 초과하면
    # 물리적으로 불가능한 값이라 결측 취급한다 (2026-08-05 실측: "당류 441g/100g",
    # "노슈거 티 당류 71g" 등 30행이 당류 하한·포화 하한 앵커를 오염시켜 대표까지 승격됐다)
    sugar = _num(row.get("당류(g)"))
    if sugar is not None and (sugar > base[0] or 4 * sugar > calories * 1.2 + 5):
        sugar = None
    saturated = _num(row.get("포화지방산(g)"))
    if saturated is not None and (saturated > base[0] or 9 * saturated > calories * 1.2 + 5):
        saturated = None

    # 성분 하한 열량 합이 총열량을 초과하면 자기모순 행 — 적재하지 않는다.
    # 단백질은 전량, 당류는 탄수의 부분집합, 포화는 지방의 부분집합이라
    # 4P + 4×당류 + 9×포화 ≤ 총열량이 물리적으로 항상 성립해야 한다
    # (2026-08-05 실측: "링티 스무디 단백질 363g/100ml" · "팥빙수 단백질 52g+당류 40g" 등
    #  43행, 그중 19행이 대표로 승격돼 있었다. 자기모순 행은 탄단지 추정도 오염시킨다)
    protein_raw = _num(row.get("단백질(g)"))
    implied_min_kcal = 4 * (protein_raw or 0) + 4 * (sugar or 0) + 9 * (saturated or 0)
    if implied_min_kcal > calories * 1.2 + 5:
        return None

    carbs, protein, fat, estimated = _fill_missing_macros(
        calories,
        _num(row.get("탄수화물(g)")),
        _num(row.get("단백질(g)")),
        _num(row.get("지방(g)")),
        sugar,
        saturated,
        repr_name=(row.get("대표식품명") or "").strip(),
    )

    return {
        "external_id": row["식품코드"].strip()[:40],
        "name": display_name[:100],
        # 표시명 기준으로 정규화 — 원본명("피자_불고기피자") 기준이면 밑줄이 남아
        # "불고기피자" 정확일치가 영원히 실패한다 (2026-08-04, 대표의 93.4%가 해당)
        "normalized_name": normalize_name(display_name)[:100],
        "base_amount": base[0],
        "base_unit": base[1],
        "calories": calories,
        "carbs": carbs,
        "protein": protein,
        "fat": fat,
        "sugar": sugar,
        "fiber": _num(row.get("식이섬유(g)")),
        "sodium": _num(row.get("나트륨(mg)")),
        "cholesterol": _num(row.get("콜레스테롤(mg)")),
        "saturated_fat": saturated,
        "trans_fat": _num(row.get("트랜스지방산(g)")),
        "brand": _pick_brand(row),
        "category": category,
        "total_weight": total[0] if total and total[1] == base[1] else None,
        "source": "public",
        "macros_estimated": estimated,  # 탄단지가 실측이 아니라 추정으로 채워진 행 (2026-08-05)
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
        # 결측 추정용 실측 통계를 이 파일의 완전실측 행에서 먼저 산출 (2026-08-05).
        # 아티팩트로 저장해 가공식품 적재(import_mfds_api)도 같은 통계를 쓴다.
        estimator = MacroEstimator.from_rows(rows)
        estimator.save()
        set_macro_estimator(estimator)
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
            if values["macros_estimated"]:
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
