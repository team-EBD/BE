"""영양 요약 서비스 (Phase 7) — LLM 미사용, DB 집계 + 규칙 기반 문구 (NFR-011).

- Phase 6(식단 저장/수정/삭제)이 recompute_daily_summary 를 호출해 캐시를 갱신한다.
- 목표치는 user_profiles 에서 읽고, 미설정 시 기본 목표를 사용한다.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.timeutil import kst_date_of, kst_day_bounds, now_utc
from app.models import DailyNutritionSummary, MealImage, MealItem, MealRecord, UserProfile
from app.services.image_retention import retention_cutoff_utc

# 목표 미설정 사용자 기본값 (명세서 9.1 예시 준용)
DEFAULT_GOALS = {"calories": 2000, "carbs": 250, "protein": 120, "fat": 65}

# 신체정보 부족 시 폴백 비율 50:30:20 (탄수화물·단백질 4kcal/g, 지방 9kcal/g)
MACRO_SPLIT = {"carbs": (0.5, 4), "protein": (0.3, 4), "fat": (0.2, 9)}

# 목표 유형별 단백질 계수(체중 kg당 g). 근거: ref/설계/영양_목표_산정_근거_v1.md
# - diet(감량): 근손실 방지 위해 상향 / maintain(유지): Morton 2018 플래토 ~1.6
# - bulk(증량): 잉여열량에서 근합성 지원
PROTEIN_G_PER_KG = {"diet": 2.0, "maintain": 1.6, "bulk": 1.8}
# 지방은 목표 칼로리의 25%(기본), 하한 20% — 20% 미만은 호르몬·필수지방산 저하 (AND/DC/ACSM 2016)
FAT_ENERGY_RATIO = 0.25
FAT_ENERGY_RATIO_MIN = 0.20


def derive_macro_goals(
    goal_calories: int, weight: float | None = None, meal_goal: str | None = None
) -> dict[str, int]:
    """목표 칼로리에서 탄단지 목표(g)를 유도한다.

    체중이 있으면 '단백질(체중당 g) 먼저 → 지방(칼로리 %, 하한 20%) → 탄수 나머지' 순으로
    목표 유형(감량/유지/증량)에 맞춰 산정한다. 체중이 없으면(신체정보 부족) 기존 비율(50:30:20)로
    폴백한다. (스포츠영양 근거: ref/설계/영양_목표_산정_근거_v1.md)
    """
    if weight is None:
        return {
            key: round(goal_calories * ratio / kcal_per_g)
            for key, (ratio, kcal_per_g) in MACRO_SPLIT.items()
        }

    coef = PROTEIN_G_PER_KG.get(meal_goal or "maintain", PROTEIN_G_PER_KG["maintain"])
    protein_g = round(float(weight) * coef)
    protein_kcal = protein_g * 4

    fat_kcal = goal_calories * FAT_ENERGY_RATIO
    # 마른 체형 + 큰 적자에서 단백질+지방이 목표를 넘으면 지방을 하한(20%)까지 낮춘다
    if protein_kcal + fat_kcal > goal_calories:
        fat_kcal = goal_calories * FAT_ENERGY_RATIO_MIN
    fat_g = round(fat_kcal / 9)

    carbs_kcal = max(goal_calories - protein_kcal - fat_g * 9, 0)
    carbs_g = round(carbs_kcal / 4)
    return {"carbs": carbs_g, "protein": protein_g, "fat": fat_g}


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


def aggregate_day(db: Session, user_id: int, day: date, day_start_hour: int = 0) -> dict:
    """해당 KST 날짜의 합계·끼니 수 (soft delete 제외).

    끼니 수(meal_count)는 실제로 먹은 기록만 센다 — 생략(is_skipped) 기록은
    영양 합계(0)에는 무해하지만 '몇 끼 먹었는지'에는 포함하면 안 된다.

    day_start_hour 가 0 이 아니면 하루 경계를 그 시각으로 옮긴다
    (예: 6 이면 06:00~다음날 06:00 — 새벽 야식이 전날 섭취로 잡힌다).
    """
    start, end = kst_day_bounds(day, day_start_hour)
    row = db.execute(
        select(
            func.coalesce(func.sum(MealRecord.total_calories), 0),
            func.coalesce(func.sum(MealRecord.total_carbs), 0),
            func.coalesce(func.sum(MealRecord.total_protein), 0),
            func.coalesce(func.sum(MealRecord.total_fat), 0),
            func.coalesce(
                func.sum(case((MealRecord.is_skipped.is_(False), 1), else_=0)), 0
            ),
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


def aggregate_range(
    db: Session, user_id: int, start_day: date, end_day: date,
    day_start_hour: int = 0,
) -> dict[date, dict]:
    """[start_day, end_day] 구간을 한 번의 쿼리로 KST 날짜별 집계한다.

    31일치를 날짜별 개별 쿼리로 도는 대신 기간 전체를 한 번에 읽고
    파이썬에서 KST 날짜로 group-by 한다 (DB 방언 무관하게 KST 경계 보장).
    필터 조건은 aggregate_day 와 동일: soft delete 제외, meal_count 는
    is_skipped=False 인 기록만 센다.
    """
    start, _ = kst_day_bounds(start_day, day_start_hour)
    _, end = kst_day_bounds(end_day, day_start_hour)
    days: dict[date, dict] = {
        start_day + timedelta(days=i): {
            "calories": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0, "meal_count": 0
        }
        for i in range((end_day - start_day).days + 1)
    }
    rows = db.execute(
        select(
            MealRecord.eaten_at,
            MealRecord.total_calories,
            MealRecord.total_carbs,
            MealRecord.total_protein,
            MealRecord.total_fat,
            MealRecord.is_skipped,
        ).where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
        )
    ).all()
    for eaten_at, cal, carbs, protein, fat, is_skipped in rows:
        total = days.get(kst_date_of(eaten_at, day_start_hour))
        if total is None:  # 경계 오차 방어 (범위 밖 KST 날짜)
            continue
        total["calories"] += float(cal)
        total["carbs"] += float(carbs)
        total["protein"] += float(protein)
        total["fat"] += float(fat)
        if not is_skipped:
            total["meal_count"] += 1
    for total in days.values():
        for key in ("calories", "carbs", "protein", "fat"):
            total[key] = round(total[key], 2)
    return days


def macro_ratio(carbs: float, protein: float, fat: float) -> dict[str, int]:
    """탄단지의 칼로리 기여 비율(%) — 탄4/단4/지9 kcal 환산, 반올림. 섭취 0이면 모두 0."""
    carbs_kcal, protein_kcal, fat_kcal = carbs * 4, protein * 4, fat * 9
    total_kcal = carbs_kcal + protein_kcal + fat_kcal
    if total_kcal <= 0:
        return {"carbs": 0, "protein": 0, "fat": 0}
    return {
        "carbs": round(carbs_kcal / total_kcal * 100),
        "protein": round(protein_kcal / total_kcal * 100),
        "fat": round(fat_kcal / total_kcal * 100),
    }


STREAK_LOOKBACK_DAYS = 365  # streak 계산 시 최대 조회 기간


def streak_days(db: Session, user_id: int, day: date, day_start_hour: int = 0) -> int:
    """해당 date 기준 연속 기록 일수.

    date 에 기록이 있으면 date 부터, 없으면 date-1 부터 거꾸로 센다.
    최대 365일까지만 조회한다. '기록'은 aggregate_day 와 동일하게
    is_skipped=False 인 살아있는(soft delete 제외) 식사가 있는 날.
    day_start_hour 는 aggregate_day 와 같은 하루 경계 규칙을 따른다.
    """
    start, _ = kst_day_bounds(day - timedelta(days=STREAK_LOOKBACK_DAYS), day_start_hour)
    _, end = kst_day_bounds(day, day_start_hour)
    eaten_ats = db.scalars(
        select(MealRecord.eaten_at).where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
        )
    ).all()
    recorded = {kst_date_of(dt, day_start_hour) for dt in eaten_ats}
    cursor = day if day in recorded else day - timedelta(days=1)
    streak = 0
    while cursor in recorded and streak < STREAK_LOOKBACK_DAYS:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _food_image_url(
    db: Session, user_id: int, food_name: str, start, end
) -> str | None:
    """해당 음식이 담긴 가장 최근 기록의 사진 URL (리포트 대표 이미지).

    이미지 보존 기간(저번달 1일~, image_retention) 밖 사진은 제외한다.
    """
    row = db.execute(
        select(MealImage.image_url)
        .join(MealRecord, MealRecord.meal_image_id == MealImage.id)
        .join(MealItem, MealItem.meal_record_id == MealRecord.id)
        .where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealItem.food_name == food_name,
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
            MealImage.uploaded_at >= retention_cutoff_utc(),
        )
        .order_by(MealRecord.eaten_at.desc())
        .limit(1)
    ).first()
    return row[0] if row else None


def top_foods_in_range(
    db: Session, user_id: int, start_day: date, end_day: date, limit: int,
    day_start_hour: int = 0,
) -> list[dict]:
    """기간 내 MealItem.food_name 최빈 상위 N — aggregate_day 와 동일한 meal 상태 조건.

    각 음식에는 대표 사진(image_url — 그 음식이 담긴 최근 기록의 사진)을 함께 담는다.
    """
    start, _ = kst_day_bounds(start_day, day_start_hour)
    _, end = kst_day_bounds(end_day, day_start_hour)
    count = func.count(MealItem.id)
    rows = db.execute(
        select(MealItem.food_name, count)
        .join(MealRecord, MealItem.meal_record_id == MealRecord.id)
        .where(
            MealRecord.user_id == user_id,
            MealRecord.deleted_at.is_(None),
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
        )
        .group_by(MealItem.food_name)
        .order_by(count.desc(), MealItem.food_name)
        .limit(limit)
    ).all()
    return [
        {
            "name": name,
            "count": int(cnt),
            "image_url": _food_image_url(db, user_id, name, start, end),
        }
        for name, cnt in rows
    ]


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


def daily_summary_response(
    db: Session, user_id: int, day: date, day_start_hour: int = 0
) -> dict:
    """GET /nutrition/daily-summary 응답 (명세서 9.1). 조회 시점 재계산으로 정확성 보장.

    day_start_hour 는 조회에만 적용된다 — daily_nutrition_summaries 캐시는
    자정 경계로 유지하고(recompute_daily_summary), 응답은 매번 재집계한다.
    """
    total = aggregate_day(db, user_id, day, day_start_hour)
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
        "streak_days": streak_days(db, user_id, day, day_start_hour),
        "macro_ratio": macro_ratio(total["carbs"], total["protein"], total["fat"]),
    }


def _week_stats(
    day_totals: dict[date, dict],
    start: date,
    goal_calories: int,
    today: date | None = None,
) -> dict:
    """aggregate_range 결과에서 한 주(7일) 통계를 뽑는다.

    today(진행 중인 논리 날짜) 이후의 날은 평균·달성·탄단지 집계에서 제외한다 —
    점심에 리포트를 열었을 때 '아침만 먹은 오늘'이 완결된 하루처럼 평균을
    끌어내리는 왜곡 방지. days[] 에는 오늘도 포함하되 in_progress 로 표시한다.
    """
    stats = {
        "recorded_days": 0,
        "achieved_days": 0,
        "sum_calories": 0.0,
        "sum_protein": 0.0,
        "sum_carbs": 0.0,
        "sum_fat": 0.0,
        "days": [],
    }
    for offset in range(7):
        day = start + timedelta(days=offset)
        total = day_totals[day]
        in_progress = today is not None and day >= today
        achieved = (
            not in_progress
            and total["meal_count"] > 0
            and total["calories"] <= goal_calories
        )
        if not in_progress:
            if total["meal_count"] > 0:
                stats["recorded_days"] += 1
                stats["sum_calories"] += total["calories"]
            if achieved:
                stats["achieved_days"] += 1
            stats["sum_protein"] += total["protein"]
            stats["sum_carbs"] += total["carbs"]
            stats["sum_fat"] += total["fat"]
        stats["days"].append(
            {
                "date": day.isoformat(),
                "calories": round(total["calories"]),
                "meal_count": total["meal_count"],
                "achieved": achieved,
                # 오늘(집계 중)만 True — 미래 날짜는 데이터가 없어 FE 가 구분 불필요
                "in_progress": today is not None and day == today,
            }
        )
    recorded = stats["recorded_days"]
    stats["avg_calories"] = round(stats["sum_calories"] / recorded) if recorded else 0
    return stats


def build_weekly_summary_text(
    recorded_days: int,
    achieved_days: int,
    prev_achieved_days: int,
    ratio: dict[str, int],
    avg_protein: float,
    goal_protein: int,
    today_recorded: bool = False,
) -> str:
    """주간 규칙 기반 문구. 우선순위: 기록없음 > 달성일 증가 > 탄수 과다 > 단백질 부족 > 격려."""
    if recorded_days == 0:
        if today_recorded:
            # 완결된 날이 아직 없고 오늘만 기록 중 (예: 주 첫날 아침)
            return "오늘 기록을 시작했어요! 오늘 하루가 끝나면 주간 집계에 반영돼요."
        return "이번 주 식사 기록이 없어요. 가볍게 한 끼부터 기록해보세요."
    if achieved_days > prev_achieved_days:
        return "지난주보다 목표 달성일이 늘었어요. 정말 잘하고 있어요!"
    if ratio["carbs"] > 60:
        return "이번 주는 탄수화물 비율이 높았어요. 다음 주엔 탄수화물을 조금 줄여보세요."
    if goal_protein and avg_protein < goal_protein * 0.8:
        return "단백질 평균이 목표에 못 미쳤어요. 다음 주엔 단백질 섭취를 늘려보세요."
    return "균형 잡힌 한 주였어요. 다음 주도 꾸준히 기록해보세요!"


def weekly_summary_response(
    db: Session, user_id: int, week_start: date, day_start_hour: int = 0
) -> dict:
    """GET /nutrition/weekly-summary 응답 (명세서 9.2 확장). 평균은 기록 있는 날 기준.

    prev_week 비교를 위해 직전 주까지 총 14일을 한 번의 쿼리로 집계한다.
    """
    goals = get_goals(db, user_id)
    week_end = week_start + timedelta(days=6)
    prev_start = week_start - timedelta(days=7)
    day_totals = aggregate_range(db, user_id, prev_start, week_end, day_start_hour)

    # 진행 중인 오늘(논리 날짜, 06시 경계)은 평균·달성 집계에서 제외한다
    today = kst_date_of(now_utc(), day_start_hour)
    cur = _week_stats(day_totals, week_start, goals["calories"], today=today)
    prev = _week_stats(day_totals, prev_start, goals["calories"], today=today)

    recorded_days = cur["recorded_days"]
    avg_calories = cur["avg_calories"]
    avg_protein = round(cur["sum_protein"] / recorded_days) if recorded_days else 0
    ratio = macro_ratio(cur["sum_carbs"], cur["sum_protein"], cur["sum_fat"])
    top = top_foods_in_range(db, user_id, week_start, week_end, limit=1,
                             day_start_hour=day_start_hour)
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "goal_calories": goals["calories"],
        "average": {"calories": avg_calories, "protein": avg_protein},
        "goal_achievement": {
            "protein": round(avg_protein / goals["protein"], 2) if goals["protein"] else 0.0
        },
        "recorded_days": recorded_days,
        "achieved_days": cur["achieved_days"],
        "days": cur["days"],
        "macro_ratio": ratio,
        "top_food": top[0] if top else None,
        "prev_week": {
            "achieved_days": prev["achieved_days"],
            "average_calories": prev["avg_calories"],
        },
        "summary_text": build_weekly_summary_text(
            recorded_days,
            cur["achieved_days"],
            prev["achieved_days"],
            ratio,
            avg_protein,
            goals["protein"],
            today_recorded=(
                week_start <= today <= week_end
                and day_totals[today]["meal_count"] > 0
            ),
        ),
    }


def _month_stats(
    day_totals: dict[date, dict],
    first: date,
    last: date,
    goal_calories: int,
    today: date | None = None,
) -> dict:
    """월 구간 통계: 기록/달성 일수, 기록일 평균 칼로리, 최장 연속 기록.

    진행 중인 달이면 오늘 이후는 집계에서 제외하고, 달성률 분모(days_counted)도
    '완결된(어제까지) 경과 일수'로 계산한다 — 월초에 볼 때 달성률이 무조건
    낮게 나오던 왜곡 방지.
    """
    # 집계 대상은 완결된 날까지만 (오늘·미래 제외)
    counted_last = last
    if today is not None and today <= last:
        counted_last = today - timedelta(days=1)

    recorded_days = 0
    achieved_days = 0
    sum_calories = 0.0
    streak = 0
    longest_streak = 0
    day = first
    while day <= counted_last:
        total = day_totals[day]
        if total["meal_count"] > 0:
            recorded_days += 1
            sum_calories += total["calories"]
            streak += 1
            longest_streak = max(longest_streak, streak)
            if total["calories"] <= goal_calories:
                achieved_days += 1
        else:
            streak = 0
        day += timedelta(days=1)
    days_counted = max((counted_last - first).days + 1, 0)
    return {
        "days_counted": days_counted,
        "recorded_days": recorded_days,
        "achieved_days": achieved_days,
        "achievement_rate": round(achieved_days / days_counted, 2) if days_counted else 0.0,
        "average_calories": round(sum_calories / recorded_days) if recorded_days else 0,
        "longest_streak": longest_streak,
    }


def _month_weeks(
    day_totals: dict[date, dict],
    first: date,
    last: date,
    goal_calories: int,
    today: date | None = None,
) -> list[dict]:
    """1일부터 7일 단위 청크(마지막은 잔여 일수) 주차 통계. 오늘 이후는 집계 제외."""
    weeks = []
    index = 1
    chunk_start = first
    while chunk_start <= last:
        chunk_end = min(chunk_start + timedelta(days=6), last)
        recorded = 0
        achieved = 0
        sum_calories = 0.0
        sum_carbs = sum_protein = sum_fat = 0.0
        day = chunk_start
        while day <= chunk_end:
            if today is not None and day >= today:
                break  # 오늘부터는 집계 중 — 완결된 날만 반영
            total = day_totals[day]
            if total["meal_count"] > 0:
                recorded += 1
                sum_calories += total["calories"]
                if total["calories"] <= goal_calories:
                    achieved += 1
            sum_carbs += total["carbs"]
            sum_protein += total["protein"]
            sum_fat += total["fat"]
            day += timedelta(days=1)
        weeks.append(
            {
                "index": index,
                "start": chunk_start.isoformat(),
                "end": chunk_end.isoformat(),
                "average_calories": round(sum_calories / recorded) if recorded else 0,
                "achieved_days": achieved,
                "recorded_days": recorded,
                "macro_ratio": macro_ratio(sum_carbs, sum_protein, sum_fat),
            }
        )
        index += 1
        chunk_start = chunk_end + timedelta(days=1)
    return weeks


def build_monthly_insights(weeks: list[dict]) -> list[str]:
    """규칙 기반 월간 인사이트 0~2개. 기록 있는 주가 2개 미만이면 빈 배열."""
    recorded_weeks = [w for w in weeks if w["recorded_days"] > 0]
    if len(recorded_weeks) < 2:
        return []
    insights: list[str] = []

    first_week, last_week = recorded_weeks[0], recorded_weeks[-1]
    protein_delta = last_week["macro_ratio"]["protein"] - first_week["macro_ratio"]["protein"]
    if protein_delta >= 3:
        insights.append(
            f"단백질 비율이 {first_week['index']}주차 {first_week['macro_ratio']['protein']}%에서 "
            f"{last_week['index']}주차 {last_week['macro_ratio']['protein']}%로 꾸준히 올랐어요."
        )
    elif protein_delta <= -3:
        insights.append(
            f"단백질 비율이 {first_week['index']}주차 {first_week['macro_ratio']['protein']}%에서 "
            f"{last_week['index']}주차 {last_week['macro_ratio']['protein']}%로 줄었어요. 단백질을 챙겨보세요."
        )

    first_cal, last_cal = first_week["average_calories"], last_week["average_calories"]
    if first_cal and last_cal:
        change = (last_cal - first_cal) / first_cal
        if change <= -0.1:
            insights.append("주차별 평균 칼로리가 월초보다 낮아지는 추세예요. 좋은 흐름이에요!")
        elif change >= 0.1:
            insights.append("주차별 평균 칼로리가 월초보다 높아지는 추세예요. 식사량을 점검해보세요.")

    return insights[:2]


def build_monthly_summary_text(stats: dict, prev_stats: dict) -> str:
    """월간 규칙 기반 코칭 한 문장."""
    if stats["recorded_days"] == 0:
        return "이번 달 식사 기록이 없어요. 하루 한 끼부터 기록을 시작해보세요."
    if stats["achievement_rate"] >= 0.7:
        return "이번 달 목표 달성률이 아주 높아요. 이 페이스를 유지해보세요!"
    if stats["longest_streak"] >= 7:
        return f"최장 {stats['longest_streak']}일 연속 기록을 달성했어요. 꾸준함이 돋보여요!"
    if stats["achievement_rate"] > prev_stats["achievement_rate"]:
        return "지난달보다 목표 달성률이 올랐어요. 잘하고 있어요!"
    return "기록이 쌓일수록 식습관이 보여요. 다음 달도 꾸준히 기록해보세요."


def monthly_summary_response(
    db: Session, user_id: int, year: int, month: int, day_start_hour: int = 0
) -> dict:
    """GET /nutrition/monthly-summary 응답. 당월/전월 각 1회 range 집계 쿼리."""
    goals = get_goals(db, user_id)
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    day_totals = aggregate_range(db, user_id, first, last, day_start_hour)
    # 진행 중인 오늘(논리 날짜)은 집계에서 제외 — 완결된 날 기준 통계
    today = kst_date_of(now_utc(), day_start_hour)
    stats = _month_stats(day_totals, first, last, goals["calories"], today=today)
    weeks = _month_weeks(day_totals, first, last, goals["calories"], today=today)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    prev_first = date(prev_year, prev_month, 1)
    prev_last = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
    prev_totals = aggregate_range(db, user_id, prev_first, prev_last, day_start_hour)
    prev_stats = _month_stats(prev_totals, prev_first, prev_last, goals["calories"])

    return {
        "month": f"{year:04d}-{month:02d}",
        "goal_calories": goals["calories"],
        "days_counted": stats["days_counted"],
        "recorded_days": stats["recorded_days"],
        "achieved_days": stats["achieved_days"],
        "achievement_rate": stats["achievement_rate"],
        "average_calories": stats["average_calories"],
        "longest_streak": stats["longest_streak"],
        "weeks": weeks,
        "top_foods": top_foods_in_range(db, user_id, first, last, limit=3,
                                        day_start_hour=day_start_hour),
        "prev_month": {
            "longest_streak": prev_stats["longest_streak"],
            "average_calories": prev_stats["average_calories"],
            "achievement_rate": prev_stats["achievement_rate"],
        },
        "insights": build_monthly_insights(weeks),
        "summary_text": build_monthly_summary_text(stats, prev_stats),
    }
