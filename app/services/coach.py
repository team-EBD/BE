"""펫의 하루 코칭 — 목표에 맞춰 오늘 식사를 한 줄로 정리한다 (2026-10-09 목표 세분화).

LLM 을 쓰지 않는 규칙 기반이다 (summary.build_summary_text 와 같은 원칙, NFR-011).
같은 섭취량이라도 목표에 따라 짚는 것이 달라진다 — 감량 중이면 초과를, 근육을 늘리는 중이면
단백질을, 체중을 늘리는 중이면 모자란 열량을 먼저 말한다. 사용자가 '특히 챙기고 싶은 것'으로
고른 항목(focus_areas)은 그보다도 앞에 둔다.

하루가 아직 진행 중일 때는 시간대에 맞춘 기대치로 본다 — 아침에 "오늘 적게 먹었다"고 하지 않는다.
저녁 8시 이후와 지난 날짜는 하루 전체를 놓고 정리한다(stage=wrap_up).

message 는 홈의 펫 말풍선에 그대로 들어간다 — 반말, 두 줄 안, 탓하지 않는 말투.
detail 은 카드·리포트용 존댓말 한두 문장이다.
"""
from __future__ import annotations

from datetime import date, datetime

from app.core.config import settings
from app.core.timeutil import kst_date_of, now_utc, to_kst
from app.services.goals import resolve_primary_goal

WRAP_UP_HOUR = 20  # 이 시각(KST)부터 오늘을 '하루 정리'로 본다
NUTRIENTS = ("calories", "protein", "carbs", "fat")
_LABEL = {"calories": "칼로리", "protein": "단백질", "carbs": "탄수화물", "fat": "지방"}

# 목표 대비 '부족' 하한·'초과' 상한 (하루 전체 기준 비율). 단백질은 넘겨도 문제 삼지 않는다.
_LOW = {"calories": 0.8, "protein": 0.8, "carbs": 0.6, "fat": 0.6}
_HIGH = {"calories": 1.1, "protein": None, "carbs": 1.2, "fat": 1.2}
CALORIES_FAR_LOW = 0.6  # 감량 중이어도 이 아래는 너무 적게 먹은 것으로 본다

# 목표별로 무엇을 먼저 짚을지. 뒤에 없는 항목은 그 목표에서 말하지 않는다.
_PRIORITY = {
    "lose_weight": (
        "calories_over", "calories_low", "protein_low", "fat_high", "carbs_high", "meals_skipped",
    ),
    "gain_muscle": ("protein_low", "calories_low", "fat_high", "meals_skipped"),
    "gain_weight": ("calories_low", "protein_low", "meals_skipped"),
    "maintain": (
        "calories_over", "protein_low", "carbs_high", "fat_high", "calories_low", "meals_skipped",
    ),
}
_PRIORITY["eat_healthy"] = _PRIORITY["maintain"]
# '특히 챙기고 싶은 것' → 맨 앞으로 올릴 항목
_FOCUS_CODES = {
    "protein": ("protein_low",),
    "overeating": ("calories_over",),
    "skipping": ("meals_skipped",),
    "balance": ("carbs_high", "fat_high"),
}
_GAIN_GOALS = ("gain_muscle", "gain_weight")


def coach_stage(day: date, now: datetime | None = None, day_start_hour: int | None = None) -> str:
    """지난 날짜, 또는 오늘이라도 저녁 8시 이후(자정 넘겨 하루 경계 전까지)는 wrap_up."""
    now = now or now_utc()
    start_hour = settings.day_start_hour if day_start_hour is None else day_start_hour
    if day != kst_date_of(now, start_hour):
        return "wrap_up"
    hour = to_kst(now).hour
    return "wrap_up" if hour >= WRAP_UP_HOUR or hour < start_hour else "in_progress"


def _expected_fraction(stage: str, now: datetime) -> float:
    """지금쯤 하루 목표의 어느 정도를 먹었으면 무난한지 (진행 중일 때의 '부족' 기준)."""
    if stage == "wrap_up":
        return 1.0
    hour = to_kst(now).hour
    if hour < 11:
        return 0.2
    if hour < 14:
        return 0.35
    if hour < 17:
        return 0.5
    return 0.75


def _num(value: float) -> str:
    return f"{round(value):,}"


def _low_threshold(key: str, goal: str, expected: float) -> float:
    # 감량 중에는 목표보다 적게 먹는 게 정상이라, 칼로리는 지나치게 적을 때만 부족으로 본다
    base = CALORIES_FAR_LOW if key == "calories" and goal == "lose_weight" else _LOW[key]
    return base * expected


def _detect(ratios: dict, meal_count: int, stage: str, goal: str, expected: float) -> set[str]:
    codes: set[str] = set()
    if ratios["calories"] > _HIGH["calories"]:
        codes.add("calories_over")
    if ratios["protein"] < _low_threshold("protein", goal, expected):
        codes.add("protein_low")
    if ratios["carbs"] > _HIGH["carbs"]:
        codes.add("carbs_high")
    if ratios["fat"] > _HIGH["fat"]:
        codes.add("fat_high")
    if stage == "wrap_up":
        if ratios["calories"] < _low_threshold("calories", goal, expected):
            codes.add("calories_low")
        if meal_count < 2:
            codes.add("meals_skipped")
    return codes


def _message(code: str, stage: str, goal: str) -> tuple[str, str]:
    """(tone, 펫 한마디)."""
    wrap = stage == "wrap_up"
    if code == "calories_over":
        if goal in _GAIN_GOALS:
            return "praise", "오늘 목표 칼로리 다 채웠어! 든든하게 잘 먹었다."
        if wrap:
            return "warn", "오늘은 목표보다 조금 많이 먹었어. 내일은 가볍게 가자!"
        return "warn", "벌써 오늘 목표를 넘었어. 남은 끼니는 가볍게 어때?"
    if code == "calories_low":
        if goal == "lose_weight":
            return "warn", "오늘은 너무 적게 먹었어. 굶으면 내가 걱정돼."
        if goal in _GAIN_GOALS:
            return "nudge", "오늘 칼로리가 모자라. 내일은 한 끼 더 든든하게!"
        return "nudge", "오늘은 좀 적게 먹었네. 내일은 든든히 챙겨 먹자."
    if code == "protein_low":
        if wrap:
            return "nudge", "오늘은 단백질이 부족했어. 내일은 더 챙겨 보자!"
        return "nudge", "단백질이 아직 부족해. 다음 끼니에 챙겨 줘!"
    if code == "carbs_high":
        return "nudge", "오늘은 탄수화물이 많았어. 다음엔 반찬을 더 먹자!"
    if code == "fat_high":
        return "nudge", "오늘은 지방이 좀 많았어. 다음엔 담백하게 가 보자."
    if code == "meals_skipped":
        return "nudge", "오늘 끼니를 걸렀네. 내일은 나랑 세 끼 다 보자!"
    # on_track
    if not wrap:
        return "praise", "지금까지 딱 좋아. 이대로만 가자!"
    if goal == "lose_weight":
        return "praise", "오늘 목표 안에서 잘 먹었어. 정말 멋져!"
    if goal == "gain_muscle":
        return "praise", "단백질까지 잘 채웠어. 근육이 좋아하겠다!"
    if goal == "gain_weight":
        return "praise", "오늘 든든하게 잘 먹었어. 이 느낌 그대로!"
    return "praise", "오늘 균형 있게 잘 먹었어. 최고야!"


def _detail(code: str, stage: str, goal: str, total: dict, goals: dict) -> str:
    wrap = stage == "wrap_up"
    cal, cal_goal = total["calories"], goals["calories"]
    pro, pro_goal = total["protein"], goals["protein"]
    if code == "calories_over":
        over = _num(cal - cal_goal)
        if goal in _GAIN_GOALS:
            return f"목표 {_num(cal_goal)}kcal 를 채우고 {over}kcal 더 드셨어요. 늘리는 중이니 잘하고 있어요."
        return (
            f"목표 {_num(cal_goal)}kcal 보다 {over}kcal 더 드셨어요. "
            "하루쯤은 괜찮아요. 다음 끼니를 조금 가볍게 해 보세요."
        )
    if code == "calories_low":
        if goal == "lose_weight":
            return (
                f"오늘 {_num(cal)}kcal 로 목표 {_num(cal_goal)}kcal 에 한참 못 미쳤어요. "
                "너무 적게 먹으면 오래 이어 가기 어려워요."
            )
        return (
            f"오늘 {_num(cal)}kcal 로 목표 {_num(cal_goal)}kcal 보다 "
            f"{_num(cal_goal - cal)}kcal 적었어요."
        )
    if code == "protein_low":
        left = _num(max(pro_goal - pro, 0))
        if wrap:
            return f"단백질을 목표 {_num(pro_goal)}g 중 {_num(pro)}g 드셨어요. {left}g 이 모자랐어요."
        return (
            f"단백질을 목표 {_num(pro_goal)}g 중 {_num(pro)}g 드셨어요. "
            f"남은 끼니에서 {left}g 을 더 챙겨 보세요."
        )
    if code in ("carbs_high", "fat_high"):
        key = "carbs" if code == "carbs_high" else "fat"
        return (
            f"{_LABEL[key]}을 목표 {_num(goals[key])}g 보다 "
            f"{_num(total[key] - goals[key])}g 더 드셨어요."
        )
    if code == "meals_skipped":
        return "오늘은 한 끼만 기록됐어요. 끼니를 거르면 다음 식사에서 많이 먹기 쉬워요."
    if not wrap:
        return (
            f"지금까지 {_num(cal)}kcal, 단백질 {_num(pro)}g 드셨어요. "
            f"오늘 목표는 {_num(cal_goal)}kcal 예요."
        )
    return f"오늘 {_num(cal)}kcal, 단백질 {_num(pro)}g 으로 목표에 맞게 드셨어요."


def build_daily_coach(
    total: dict,
    goals: dict,
    meal_count: int,
    *,
    primary_goal: str | None = None,
    meal_goal: str | None = None,
    focus_areas: list[str] | tuple[str, ...] = (),
    stage: str = "wrap_up",
    now: datetime | None = None,
) -> dict | None:
    """하루 코칭. 먹은 기록이 없으면 None (기록 유도는 펫의 기존 대사가 맡는다)."""
    if meal_count <= 0:
        return None
    now = now or now_utc()
    goal = resolve_primary_goal(primary_goal, meal_goal)
    expected = _expected_fraction(stage, now)
    ratios = {k: (total[k] / goals[k] if goals[k] else 1.0) for k in NUTRIENTS}

    detected = _detect(ratios, meal_count, stage, goal, expected)
    order = list(_PRIORITY[goal])
    # 늘리는 목표에서 목표 칼로리를 넘긴 것은 잘한 일이다 — 걸릴 게 없을 때만 칭찬으로 말한다
    if goal in _GAIN_GOALS:
        order.append("calories_over")
    focus_first = [c for area in focus_areas for c in _FOCUS_CODES.get(area, ()) if c in order]
    order = list(dict.fromkeys(focus_first + order))
    code = next((c for c in order if c in detected), "on_track")

    tone, message = _message(code, stage, goal)
    items = []
    for key in NUTRIENTS:
        ratio = ratios[key]
        high = _HIGH[key]
        if high is not None and ratio > high:
            status = "high"
        elif ratio < _low_threshold(key, goal, expected):
            status = "low"
        else:
            status = "ok"
        items.append({"nutrient": key, "status": status, "percent": round(ratio * 100)})
    nutrient = code.split("_")[0] if code.split("_")[0] in NUTRIENTS else None
    if code == "calories_over" and goal in _GAIN_GOALS:
        code = "calories_met"
    return {
        "code": code,
        "tone": tone,
        "nutrient": nutrient,
        "stage": stage,
        "message": message,
        "detail": _detail(
            "calories_over" if code == "calories_met" else code, stage, goal, total, goals
        ),
        "items": items,
    }
