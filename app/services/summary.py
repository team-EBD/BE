"""영양 요약 서비스 (Phase 7) — LLM 미사용, DB 집계 + 규칙 기반 문구 (NFR-011).

- Phase 6(식단 저장/수정/삭제)이 recompute_daily_summary 를 호출해 캐시를 갱신한다.
- 목표치는 user_profiles 에서 읽고, 미설정 시 기본 목표를 사용한다.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutil import kst_day_bounds
from app.models import DailyNutritionSummary, MealRecord, UserProfile

# 목표 미설정 사용자 기본값 (명세서 9.1 예시 준용)
DEFAULT_GOALS = {"calories": 2000, "carbs": 250, "protein": 120, "fat": 65}

# 탄:단:지 칼로리 비율 50:30:20, 탄수화물·단백질 4kcal/g, 지방 9kcal/g
MACRO_SPLIT = {"carbs": (0.5, 4), "protein": (0.3, 4), "fat": (0.2, 9)}


def derive_macro_goals(goal_calories: int) -> dict[str, int]:
    """목표 칼로리에서 탄단지 목표(g)를 유도한다 (50:30:20)."""
    return {
        key: round(goal_calories * ratio / kcal_per_g)
        for key, (ratio, kcal_per_g) in MACRO_SPLIT.items()
    }


def get_goals(db: Session, user_id: int) -> dict[str, int]:
    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
    if profile is None:
        return dict(DEFAULT_GOALS)
    return {
        "calories": profile.goal_calories,
        "carbs": profile.goal_carbs,
        "protein": profile.goal_protein,
        "fat": profile.goal_fat,
    }


def aggregate_day(db: Session, user_id: int, day: date) -> dict:
    """해당 KST 날짜의 합계·끼니 수 (soft delete 제외)."""
    start, end = kst_day_bounds(day)
    row = db.execute(
        select(
            func.coalesce(func.sum(MealRecord.total_calories), 0),
            func.coalesce(func.sum(MealRecord.total_carbs), 0),
            func.coalesce(func.sum(MealRecord.total_protein), 0),
            func.coalesce(func.sum(MealRecord.total_fat), 0),
            func.count(MealRecord.id),
        ).where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
        )
    ).one()
    return {
        "calories": round(float(row[0]), 2),
        "carbs": round(float(row[1]), 2),
        "protein": round(float(row[2]), 2),
        "fat": round(float(row[3]), 2),
        "meal_count": int(row[4]),
    }


def build_summary_text(total: dict, goals: dict, meal_count: int) -> str:
    """규칙 기반 요약 문구. 우선순위: 기록없음 > 초과 > 단백질 부족 > 정상."""
    if meal_count == 0:
        return "아직 오늘의 식사 기록이 없어요. 첫 끼니를 기록해보세요."
    if goals["calories"] and total["calories"] > goals["calories"]:
        return "오늘 목표 칼로리를 초과했어요. 다음 식사는 가볍게 해보세요."
    protein_ratio = total["protein"] / goals["protein"] if goals["protein"] else 1.0
    if protein_ratio < 0.8:
        return "오늘 단백질이 목표보다 조금 부족해요. 다음 식사에서 보충해보세요."
    if goals["calories"] and total["calories"] < goals["calories"] * 0.5 and meal_count < 3:
        return "오늘 섭취량이 목표의 절반에 못 미쳐요. 끼니를 거르지 않도록 해요."
    return "오늘 균형 잡힌 식사를 잘 하고 있어요!"


def recompute_daily_summary(db: Session, user_id: int, day: date) -> DailyNutritionSummary:
    """daily_nutrition_summaries 캐시 upsert. 커밋은 호출자(트랜잭션 경계) 담당."""
    total = aggregate_day(db, user_id, day)
    goals = get_goals(db, user_id)
    summary = db.scalar(
        select(DailyNutritionSummary).where(
            DailyNutritionSummary.user_id == user_id,
            DailyNutritionSummary.summary_date == day,
        )
    )
    if summary is None:
        summary = DailyNutritionSummary(user_id=user_id, summary_date=day)
        db.add(summary)
    summary.total_calories = total["calories"]
    summary.total_carbs = total["carbs"]
    summary.total_protein = total["protein"]
    summary.total_fat = total["fat"]
    summary.meal_count = total["meal_count"]
    summary.summary_text = build_summary_text(total, goals, total["meal_count"])
    return summary


def daily_summary_response(db: Session, user_id: int, day: date) -> dict:
    """GET /nutrition/daily-summary 응답 (명세서 9.1). 조회 시점 재계산으로 정확성 보장."""
    total = aggregate_day(db, user_id, day)
    goals = get_goals(db, user_id)
    progress = {
        k: round(total[k] / goals[k], 2) if goals[k] else 0.0
        for k in ("calories", "carbs", "protein", "fat")
    }
    return {
        "date": day.isoformat(),
        "total": {k: total[k] for k in ("calories", "carbs", "protein", "fat")},
        "goal": goals,
        "progress": progress,
        "remaining_calories": max(round(goals["calories"] - total["calories"]), 0),
        "summary_text": build_summary_text(total, goals, total["meal_count"]),
    }


def weekly_summary_response(db: Session, user_id: int, week_start: date) -> dict:
    """GET /nutrition/weekly-summary 응답 (명세서 9.2). 평균은 기록 있는 날 기준."""
    goals = get_goals(db, user_id)
    week_end = week_start + timedelta(days=6)

    recorded_days = 0
    sum_calories = 0.0
    sum_protein = 0.0
    for offset in range(7):
        day_total = aggregate_day(db, user_id, week_start + timedelta(days=offset))
        if day_total["meal_count"] > 0:
            recorded_days += 1
            sum_calories += day_total["calories"]
            sum_protein += day_total["protein"]

    avg_calories = round(sum_calories / recorded_days) if recorded_days else 0
    avg_protein = round(sum_protein / recorded_days) if recorded_days else 0
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "average": {"calories": avg_calories, "protein": avg_protein},
        "goal_achievement": {
            "protein": round(avg_protein / goals["protein"], 2) if goals["protein"] else 0.0
        },
        "recorded_days": recorded_days,
    }
