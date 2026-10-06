"""AI 분석 서비스 (Phase 8, 명세서 6.1).

역할 분담(아키텍처 §8): AI 서버는 원본 후보만 반환하고, BE 가
① food_name→nutrition_items 매칭 ② 식습관 보정(habit_adjusted)
③ food_candidates 저장 ④ ai_call_logs 기록을 담당한다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_client.base import AIClient, AICallLogPayload
from app.core.errors import APIError
from app.models import AiCallLog, EatingHabit, FoodCandidate, MealImage, User
from app.schemas.meal import (
    AnalyzeCandidate,
    AnalyzeFailedResponse,
    AnalyzeSuccessResponse,
    BoundingBox,
    CandidateNutrition,
    HabitAdjusted,
    NutritionPer100g,
    PackageInfo,
)
from app.services.correction import FACTORS, habit_factor
from app.services.game_skills import DEPTH_CLARIFIER, candidate_depth
from app.services.matching import (
    density_per_100g,
    base_serving_text,
    db_candidates_for_text,
    match_food_name,
    normalize_name,
)


logger = logging.getLogger("eatlog.analyze")

# AI 서버와 동일한 상한 (AI 서버가 이미 지키지만 방어적으로 재적용)
MAX_FOODS = 5
MAX_PREDICTIONS_PER_FOOD = 3
# '발견 돋보기'(candidate_depth="clarifier")는 음식당 대체 후보를 1개 더 준다.
# 여기 상한을 올려 주지 않으면 4번째 후보가 방어 컷에 잘려 스킬이 무효가 된다.
MAX_PREDICTIONS_PER_FOOD_CLARIFIER = MAX_PREDICTIONS_PER_FOOD + 1

# 유사도(fuzzy) 매칭은 확신도를 한 단계 감산해 내려보낸다 (SCRUM-246).
# 별도 "유사 매칭" UI 를 만들지 않고 기존 confidence 채널로 불확실성을 전달
# — 이미지 분석의 정답률 표시와 신호가 이원화되지 않게 한다 (PM 결정 08-11).


def _grouped_candidates(
    raw: list, max_per_food: int = MAX_PREDICTIONS_PER_FOOD
) -> list[tuple[int, object]]:
    """(food_index, candidate) 목록으로 정규화한다.

    - food_index 는 등장 순서 기준으로 0부터 재부여
    - 서로 다른 음식 최대 MAX_FOODS 개, 음식당 예측 최대 max_per_food 개
    - 구버전 AI 서버(food_index 없음)는 전부 0 그룹 → 기존 상위 3개와 동일 동작
    """
    counts: dict[int, int] = {}
    reindex: dict[int, int] = {}
    grouped: list[tuple[int, object]] = []
    for cand in raw:
        original = getattr(cand, "food_index", 0) or 0
        if original not in reindex:
            if len(reindex) >= MAX_FOODS:
                continue
            reindex[original] = len(reindex)
        if counts.get(original, 0) >= max_per_food:
            continue
        counts[original] = counts.get(original, 0) + 1
        grouped.append((reindex[original], cand))
    return grouped


def _bbox_of(cand) -> BoundingBox | None:
    """AI 후보의 위치 좌표를 응답 스키마로 옮긴다.

    범위를 벗어난 좌표(구/오작동 AI 서버)는 분석 전체를 500 으로 만들지 않고
    좌표만 버린다 — 오버레이가 없을 뿐 기록은 정상 진행된다.
    """
    raw = getattr(cand, "bbox", None)
    if raw is None:
        return None
    try:
        return BoundingBox(**raw.model_dump())
    except ValidationError:
        return None


# 포장 단위 — 포장 용량(355ml·98g)을 알면 AI 의 g 눈대중 대신 용량 × 개수를 쓴다.
# '개' 는 봉지 속 낱개(미니샌드 4개)일 수도 있어 1개일 때만 포장으로 본다 — 4개 × 봉지 100g = 1,560kcal 사고(2026-10-06 평가)
CONTAINER_UNITS = frozenset({"캔", "병"})
_NUTRIENTS = ("calories", "carbs", "protein", "fat")


@dataclass
class ResolvedNutrition:
    """후보 하나의 양·영양 계산 결과 (2026-10-06 g × 100g 당 모델).

    - grams: 섭취 양(g/ml). AI 가 본 양, 포장 제품은 용량 × 개수
    - density: 100g 당 영양. 출처(source)는 label(표시 성분 검색) > db(정확 일치 행) > ai(AI 추정)
    - count/count_unit: 셀 수 있으면 개수(0.5 단위)·단위. 이때 nutrition 은 **1단위** 값, estimated_serving = 개수
    - 그릇 요리는 nutrition 이 **보이는 양 전체** 값, estimated_serving = 1.0 ("1인분 = 사진에 보이는 한 접시")
    - 구 AI 서버(1인분형 nutrition 만 있음)는 ai_serving 으로 폴백해 이전 동작 유지
    """

    grams: float | None
    source: str
    count: float | None
    count_unit: str | None
    estimated_serving: float
    nutrition: CandidateNutrition | None
    grams_per_unit: float | None
    per_100g: dict | None
    sources: list[str]


def resolve_nutrition(cand, matched) -> ResolvedNutrition:
    count = getattr(cand, "count", None)
    unit = getattr(cand, "count_unit", None)
    if not count or not unit:  # 둘 다 있어야 낱개
        count = unit = None
    grams = getattr(cand, "estimated_serving_g", None)
    label = getattr(cand, "label", None)
    package = getattr(cand, "package", None)
    sources = list(getattr(label, "sources", []) or []) if label is not None else []

    # 1) 100g 당 영양과 출처
    density: dict | None = None
    source = "none"
    db_density = density_per_100g(matched) if matched is not None else None
    if label is not None and label.per_100g is not None:
        density, source = label.per_100g.model_dump(), "label"
    elif db_density is not None:
        density, source = db_density, "db"
    elif getattr(cand, "nutrition_per_100g", None) is not None:
        density, source = cand.nutrition_per_100g.model_dump(), "ai"

    # 2) 양 — 포장 용량을 알면 그것 × 개수, 아니면 AI 가 본 g. 둘 다 없고 매칭 행이 있으면 구 방식(배수 × 기준량)
    #    사진에서 읽은 용량(40g 파우치)이 검색이 찾은 용량(280g 7개입 묶음)보다 우선 — 사진 속 포장이 기준이다
    package_size = (getattr(package, "size_g", None) if package is not None else None) or (
        getattr(label, "package_size_g", None) if label is not None else None
    )
    if package_size and (unit is None or unit in CONTAINER_UNITS or (unit == "개" and float(count or 1.0) == 1.0)):
        grams = float(package_size) * float(count or 1.0)
    if grams is None and matched is not None and matched.base_amount:
        grams = float(matched.base_amount) * float(getattr(cand, "estimated_serving", 1.0) or 1.0)
    grams = round(float(grams), 1) if grams else None

    # 3) 영양값 — 포장에 인쇄된 열량("9g(45 kcal)")이 읽혔으면 그것이 가장 정확하다: kcal 은 인쇄값 × 개수,
    #    탄단지는 밀도 × g (g 을 모르면 밀도 비율로 인쇄 kcal 에 맞춘다)
    nutrition: CandidateNutrition | None = None
    printed = getattr(package, "printed_kcal", None) if package is not None else None
    if printed and (unit is None or unit in CONTAINER_UNITS or unit == "개"):
        units = float(count or 1.0)
        if density is not None and grams:
            macros = {k: density[k] * grams / 100.0 for k in ("carbs", "protein", "fat")}
        elif density is not None and density.get("calories"):
            ratio = printed * units / density["calories"]  # 인쇄 kcal 에 해당하는 g/100
            macros = {k: density[k] * ratio for k in ("carbs", "protein", "fat")}
        else:
            macros = {k: 0.0 for k in ("carbs", "protein", "fat")}
        per_div = units if count else 1.0
        size_txt = f"({grams / units:g}g)" if grams else ""
        nutrition = CandidateNutrition(
            base_serving=(f"1{unit}{size_txt}" if count else f"포장 1개{size_txt}"),
            calories=round(printed, 1), **{k: round(v / per_div, 1) for k, v in macros.items()},
        )
        source = "printed"
        return ResolvedNutrition(
            grams=grams, source=source, count=float(count) if count else None, count_unit=unit,
            estimated_serving=round(float(count) if count else 1.0, 2), nutrition=nutrition,
            grams_per_unit=round(grams / float(count), 2) if (count and grams) else None,
            per_100g={k: round(v, 2) for k, v in density.items()} if density else None, sources=sources,
        )
    if density is not None and grams:
        total = {k: density[k] * grams / 100.0 for k in _NUTRIENTS}
        per = {k: v / float(count) for k, v in total.items()} if count else total
        base_text = f"1{unit}({grams / float(count):g}g)" if count else f"보이는 양({grams:g}g)"
        nutrition = CandidateNutrition(base_serving=base_text, **{k: round(per[k], 1) for k in _NUTRIENTS})
        estimated_serving = float(count) if count else 1.0
    elif getattr(cand, "nutrition", None) is not None:
        n = cand.nutrition  # 구 AI 서버: 1인분형 추정치 + 배수 그대로
        nutrition = CandidateNutrition(
            base_serving=n.base_serving, calories=float(n.calories), carbs=float(n.carbs),
            protein=float(n.protein), fat=float(n.fat),
        )
        source = "ai_serving"
        estimated_serving = float(count) if count else float(getattr(cand, "estimated_serving", 1.0) or 1.0)
    else:
        estimated_serving = float(count) if count else float(getattr(cand, "estimated_serving", 1.0) or 1.0)
    return ResolvedNutrition(
        grams=grams, source=source, count=float(count) if count else None, count_unit=unit,
        estimated_serving=round(estimated_serving, 2), nutrition=nutrition,
        grams_per_unit=round(grams / float(count), 2) if (count and grams) else None,
        per_100g={k: round(v, 2) for k, v in density.items()} if density else None, sources=sources,
    )


def _save_call_log(
    db: Session, user_id: int, meal_image_id: int | None, payload: AICallLogPayload,
    error_message: str | None = None,
    is_tutorial: bool | None = None,
) -> AiCallLog:
    log = AiCallLog(
        user_id=user_id,
        meal_image_id=meal_image_id,
        task_type=payload.task_type,
        provider=payload.provider,
        model_name=payload.model_name,
        status=payload.status,
        latency_ms=payload.latency_ms,
        error_message=error_message,
        is_tutorial=is_tutorial,
    )
    db.add(log)
    db.flush()
    return log


def analyze_meal_image(
    db: Session,
    user: User,
    meal_image_id: int,
    ai: AIClient,
    user_text: str | None = None,
    is_tutorial: bool | None = None,
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    started = time.perf_counter()
    image = db.get(MealImage, meal_image_id)
    if image is None:
        raise APIError(404, "NOT_FOUND", "업로드된 이미지를 찾을 수 없습니다.")
    if image.user_id != user.id:
        raise APIError(403, "FORBIDDEN", "다른 사용자의 이미지입니다.")

    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
    habits_payload = None
    if habit is not None:
        habits_payload = {
            "default_portion": habit.default_portion,
            "soup_preference": habit.soup_preference,
            "sauce_preference": habit.sauce_preference,
        }

    # '발견 돋보기' 판정 — 켜져 있고 장착·충전이 맞으면 후보를 1개 더 받는다.
    # 판정은 예외를 밖으로 내지 않으므로 여기서 분석이 막히지는 않는다.
    depth = candidate_depth(db, user.id)

    # 사용자가 사진과 함께 적은 설명 — AI 가 식별·수량 힌트로 쓴다.
    # 설명이 있을 때만 키워드를 넘겨 구 시그니처 클라이언트와의 호환을 유지한다.
    # candidate_depth 도 같은 이유로 **clarifier 일 때만** 넘긴다 — 기본값이면
    # 구버전 AI 서버·테스트 더블이 모르는 필드를 받을 일이 없다.
    cleaned_text = (user_text or "").strip() or None
    extra: dict = {}
    if cleaned_text:
        extra["user_text"] = cleaned_text
    if depth == DEPTH_CLARIFIER:
        extra["candidate_depth"] = depth
    result = ai.analyze(image.image_url, habits_payload, **extra)
    return _postprocess(
        db, user, habit, result, meal_image_id, started,
        max_per_food=(
            MAX_PREDICTIONS_PER_FOOD_CLARIFIER
            if depth == DEPTH_CLARIFIER
            else MAX_PREDICTIONS_PER_FOOD
        ),
        is_tutorial=is_tutorial,
    )


def analyze_meal_text(
    db: Session, user: User, text: str, ai: AIClient, is_tutorial: bool | None = None
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    """자연어 식사 서술 → 기록 초안 (이미지 분석과 동일한 후처리·응답 계약).

    이미지가 없으므로 food_candidates.meal_image_id 는 NULL 로 저장된다.
    사용량은 이미지 분석과 같은 analyze 쿼터를 공유한다 (라우터에서 enforce).

    선(先)-매칭: 문장에 이름이 등장하는 영양 DB 항목을 먼저 찾아 AI 에
    함께 보낸다 — AI 음식명이 DB 명명에 정렬돼 사후 매칭 실패(→ 추정
    영양 폴백)가 줄어든다. 후보가 없으면 기존과 동일하게 자유 추출.
    """
    started = time.perf_counter()
    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
    cleaned = text.strip()
    rows = db_candidates_for_text(db, cleaned)
    if rows:
        db_cands = [
            {"name": r.name, "base_serving": base_serving_text(r)} for r in rows
        ]
        result = ai.parse_text(cleaned, db_candidates=db_cands)
    else:
        # 후보 없을 땐 구 시그니처 호출 — 테스트 더블·구버전 클라이언트 호환
        result = ai.parse_text(cleaned)
    return _postprocess(db, user, habit, result, None, started, is_tutorial=is_tutorial)


def _postprocess(
    db: Session,
    user: User,
    habit: EatingHabit | None,
    result,
    meal_image_id: int | None,
    started: float,
    max_per_food: int = MAX_PREDICTIONS_PER_FOOD,
    is_tutorial: bool | None = None,
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    """AI 결과 공통 후처리 — 로그 기록, 영양 매칭, 식습관 보정, 후보 저장."""
    call_log = _save_call_log(
        db, user.id, meal_image_id, result.ai_call_log,
        error_message=result.reason, is_tutorial=is_tutorial,
    )

    if result.status == "failed":
        call_log.total_ms = int((time.perf_counter() - started) * 1000)
        db.commit()
        return AnalyzeFailedResponse(
            reason=result.reason or "provider_error",
            fallback_action=result.fallback_action or "manual_food_search",
            ai_call_log_id=call_log.id,
        )

    factor, applied = habit_factor(habit)
    candidates: list[AnalyzeCandidate] = []
    for rank, (food_index, cand) in enumerate(
        _grouped_candidates(result.candidates, max_per_food), start=1
    ):
        matched, match_path = match_food_name(db, cand.food_name)
        # 경로별 비율(exact/none)과 영양 출처(label/db/ai)가 매칭 품질 지표 (2026-10-06 정확 일치만)
        resolved = resolve_nutrition(cand, matched)
        logger.info(
            "nutrition_match path=%s source=%s food=%s item_id=%s grams=%s",
            match_path, resolved.source, cand.food_name, matched.id if matched else None, resolved.grams,
        )
        confidence = float(cand.confidence)
        row = FoodCandidate(
            meal_image_id=meal_image_id,
            ai_call_log_id=call_log.id,
            nutrition_item_id=matched.id if matched else None,
            food_name=cand.food_name,
            normalized_name=normalize_name(cand.food_name),
            confidence_score=confidence,
            estimated_serving=resolved.estimated_serving,
            quantity=resolved.count,
            quantity_unit=resolved.count_unit,
            grams_per_unit=resolved.grams_per_unit,
            estimated_grams=resolved.grams,
            nutrition_source=resolved.source,
            food_index=food_index,
            rank=rank,
        )
        db.add(row)
        db.flush()

        nutrition = resolved.nutrition
        habit_adjusted = None
        # 식습관 보정 중 국물/소스 관련 항목은 그 음식에 국물/소스가 있을 때만
        # 적용한다 (예: soup_preference=leave 여도 공기밥엔 no_soup 미적용).
        cand_applied = [
            c for c in applied
            if not (c == "no_soup" and not cand.has_soup)
            and not (c == "no_sauce" and not cand.has_sauce)
        ]
        if cand_applied == applied:
            cand_factor = factor
        else:
            cand_factor = 1.0
            for c in cand_applied:
                cand_factor *= FACTORS[c]
            cand_factor = round(cand_factor, 4)
        if nutrition is not None and cand_applied:  # 식습관 설정 없으면 생략 (명세서 6.1)
            habit_adjusted = HabitAdjusted(
                applied_factor=cand_factor,
                applied_corrections=cand_applied,
                calories=round(nutrition.calories * cand_factor, 1),
            )
        package = getattr(cand, "package", None)
        candidates.append(
            AnalyzeCandidate(
                food_candidate_id=row.id,
                nutrition_item_id=row.nutrition_item_id,
                food_index=food_index,
                normalized_name=row.normalized_name,
                confidence_score=confidence,
                estimated_serving=resolved.estimated_serving,
                has_soup=cand.has_soup,
                has_sauce=cand.has_sauce,
                bbox=_bbox_of(cand),
                nutrition=nutrition,
                habit_adjusted=habit_adjusted,
                quantity=resolved.count,
                quantity_unit=resolved.count_unit,
                serving_per_unit=1.0 if resolved.count else None,
                grams=resolved.grams,
                nutrition_source=resolved.source,
                matched_name=matched.name if matched else None,
                per_100g=NutritionPer100g(**resolved.per_100g) if resolved.per_100g else None,
                package=PackageInfo(**package.model_dump()) if package is not None else None,
                label_sources=resolved.sources,
            )
        )

    # BE 전체 처리 시간 — AI 내부(latency_ms)와의 차이가 매칭·저장 오버헤드
    call_log.total_ms = int((time.perf_counter() - started) * 1000)
    db.commit()
    return AnalyzeSuccessResponse(
        draft_notice=result.draft_notice or "AI가 분석한 기록 초안입니다.",
        candidates=candidates,
        ai_call_log_id=call_log.id,
    )
