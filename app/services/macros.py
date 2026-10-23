"""탄단지 목표 산정 v2 (2026-10-21) — 비율 클램프 대신 '몸이 정하는 절대량 + 판정'.

원칙: 단백질과 지방은 체중·신장·목표가 정하는 절대량(g)이고 탄수는 나머지다. 목표 칼로리가 그 몸에
비해 무리하면 숫자를 자르지 않고 **status·reasons·min_calories** 로 알려 준다(사용자 결정 10-21).

    기준 체중 W_ref : BMI 30 까지 실체중, 그 위는 초과분의 25% 만 (조정체중 관행, BMI 30 에서 연속)
    단백질 P        : k × W_ref. k 는 목표별(감량·근육 1.6 / 체중증가 1.2 / 유지·건강식 1.0)에
                      활동량 연동 하한(유지·건강식·체중증가만)과 65세+ 하한 1.2 를 얹고 2.2 로 자른다.
                      65세+ 는 실체중 × 1.0 아래로 내려가지 않는다(ESPEN 고령자 최소).
    지방 F          : 목표 칼로리의 25% 고정
    탄수 C          : 나머지 (음수면 0 저장 + infeasible)
    판정            : 반올림 전 P·C 로 본다. 탄수 130 g RDA·AMDR(단백질 10~35%, 탄수 45~65%)·유지 열량
                      대비 적자/잉여를 절벽이 아닌 두 단계(info 표시 / review 재검토)로 본다. 19세 미만과
                      저체중 감량은 자동 산정 대상이 아니다(unsupported) — 숫자는 참고용으로 그대로 준다.

근거: Morton 2018(1.6 g/kg 플래토, 상한 2.2), ISSN/Helms 감량 1.6~2.4, PROT-AGE·ESPEN 65세+ ≥1.0~1.2,
AMDR(미국 DRI), 탄수 RDA 130 g. 정리: ref/설계/영양_목표_산정_근거_v2.md, 결정 과정 ref/기록/1021.md.
구현은 팀장·Codex 교차검증 후 Codex 안(반올림 전 값으로 판정, 각 g 은 마지막에 반올림)을 채택했다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.timeutil import KST, now_utc

K_PROTEIN = {
    "lose_weight": 1.6,
    "gain_muscle": 1.6,
    "gain_weight": 1.2,
    "maintain": 1.0,
    "eat_healthy": 1.0,
}
K_BY_ACTIVITY = {
    "sedentary": 1.0,
    "light": 1.0,
    "moderate": 1.2,
    "active": 1.4,
    "very_active": 1.6,
}
PROTEIN_MAX_PER_KG = 2.2
FAT_ENERGY_RATIO = 0.25
CARB_MIN_G = 130

_STATUS_RANK = {"ok": 0, "info": 1, "review": 2, "infeasible": 3, "unsupported": 4}


@dataclass(frozen=True)
class MacroPlan:
    carbs: int
    protein: int
    fat: int
    status: str
    reasons: list[str]
    min_calories: int | None
    reference_weight: float | None

    def targets(self) -> dict[str, int]:
        """저장·응답용 탄단지 g 만 (판정 제외)."""
        return {"carbs": self.carbs, "protein": self.protein, "fat": self.fat}


def reference_weight(weight: float, height: float | None) -> float:
    """BMI 30 초과분의 25%만 반영한다. 계산 중에는 반올림하지 않는다."""
    weight = float(weight)
    if height is None:
        return weight
    height_m2 = (float(height) / 100) ** 2
    w30 = 30 * height_m2
    return weight if weight / height_m2 <= 30 else w30 + 0.25 * (weight - w30)


def normalize_goal(goal: str | None) -> str:
    if goal is not None and goal in K_PROTEIN:
        return goal
    return {"diet": "lose_weight", "bulk": "gain_muscle"}.get(goal, "maintain")


def macro_plan(
    goal_calories: int,
    *,
    weight: float | None,
    height: float | None = None,
    birth_year: int | None = None,
    goal: str | None = None,
    activity_level: str | None = None,
    tdee: float | None = None,
) -> MacroPlan:
    """양의 목표 열량·신체정보에서 참고 목표를 계산한다 (입력 범위는 API에서 검증)."""
    if weight is None:
        return MacroPlan(
            carbs=round(goal_calories * 0.5 / 4),
            protein=round(goal_calories * 0.3 / 4),
            fat=round(goal_calories * 0.2 / 9),
            status="fallback",
            reasons=["no_body_info"],
            min_calories=None,
            reference_weight=None,
        )

    weight = float(weight)
    ref_weight = reference_weight(weight, height)
    goal = normalize_goal(goal)
    age = now_utc().astimezone(KST).year - birth_year if birth_year is not None else None
    k = K_PROTEIN[goal]
    if goal in ("maintain", "eat_healthy", "gain_weight"):
        k = max(k, K_BY_ACTIVITY.get(activity_level, 0))
    if age is not None and age >= 65:
        k = max(k, 1.2)
    k = min(k, PROTEIN_MAX_PER_KG)
    protein = k * ref_weight
    if age is not None and age >= 65:
        protein = max(protein, weight)
    fat = FAT_ENERGY_RATIO * goal_calories / 9
    carbs = (goal_calories - 4 * protein - 9 * fat) / 4
    protein_ratio = 4 * protein / goal_calories

    reasons = []
    status = "ok"

    def flag(condition: bool, code: str, severity: str) -> None:
        nonlocal status
        if condition:
            reasons.append(code)
            if _STATUS_RANK[severity] > _STATUS_RANK[status]:
                status = severity

    # 계약 표 순서를 유지하고 반올림 전 값으로 해당 사유를 모두 수집한다.
    flag(age is not None and age < 19, "under_19", "unsupported")
    flag(
        height is not None and weight / (float(height) / 100) ** 2 < 18.5
        and goal == "lose_weight",
        "underweight_lose_weight", "unsupported",
    )
    flag(carbs < 0, "protein_fat_exceed_calories", "infeasible")
    flag(0 <= carbs < 100, "carbs_under_100", "review")
    flag(100 <= carbs < CARB_MIN_G, "carbs_under_130", "info")
    flag(protein_ratio > 0.40, "protein_over_40pct", "review")
    flag(0.35 < protein_ratio <= 0.40, "protein_over_35pct", "info")
    flag(protein_ratio < 0.08, "protein_under_8pct", "review")
    flag(0.08 <= protein_ratio < 0.10, "protein_under_10pct", "info")
    flag(carbs >= 0 and 4 * carbs / goal_calories > 0.65, "carbs_over_65pct", "info")
    if tdee is not None:
        flag(
            goal == "lose_weight" and round((tdee - goal_calories) / tdee, 2) > 0.25,
            "deficit_over_25pct", "review",
        )
        flag(
            goal in ("gain_muscle", "gain_weight")
            and round((goal_calories - tdee) / tdee, 2) > 0.20,
            "surplus_over_20pct", "review",
        )
        flag(
            goal in ("maintain", "eat_healthy")
            and round(abs(goal_calories - tdee) / tdee, 2) > 0.20,
            "far_from_tdee", "info",
        )

    return MacroPlan(
        carbs=round(max(carbs, 0)),
        protein=round(protein),
        fat=round(fat),
        status=status,
        reasons=reasons,
        min_calories=math.ceil((4 * protein + 4 * CARB_MIN_G) / (1 - FAT_ENERGY_RATIO) / 10) * 10,
        reference_weight=round(ref_weight, 1),
    )
