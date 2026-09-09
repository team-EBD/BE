"""추천 이유 문장 — 템플릿. LLM 을 쓰지 않아 지연 0·결정론."""
from __future__ import annotations

from .ranking import Ranked
from .signals import Budget, budget_label

MEAL_LABEL = {"breakfast": "아침", "lunch": "점심", "dinner": "저녁", "snack": "간식"}

_HEAD = {
    "personal": "{meal}에 자주 드시는 메뉴예요",
    "popular": "{meal}에 많이 기록되는 메뉴예요",
    "similar": "자주 드시는 {anchor}와 영양 비율이 비슷해요",
    "similar_type": "자주 드시는 {anchor} 같은 {kind}류예요",
}
_LABEL_TAIL = {
    "fit": "",
    "light": " · 가볍게 드실 수 있어요",
    "heavy": " · 예산보다 조금 무거워요",
}
PROTEIN_MENTION_MIN = 0.5  # 단백질 부족분의 절반 이상 채우면 언급


def reason(ranked: Ranked, budget: Budget) -> str:
    cand = ranked.candidate
    meal = MEAL_LABEL.get(budget.meal_type, "이번 끼니")
    if cand.source == "similar" and cand.similar_type:
        head = _HEAD["similar_type"].format(anchor=cand.similar_to, kind=cand.similar_type)
    elif cand.source == "similar":
        head = _HEAD["similar"].format(anchor=cand.similar_to or "음식")
    else:
        head = _HEAD.get(cand.source, _HEAD["popular"]).format(meal=meal)
    kcal = f"{meal} 예산 {budget.meal_budget} 중 {round(cand.calories)}kcal"
    tail = _LABEL_TAIL[budget_label(cand.calories, budget.meal_budget)]
    if ranked.parts.get("protein", 0.0) >= PROTEIN_MENTION_MIN:
        tail += " · 단백질 보충에 좋아요"
    return f"{head} · {kcal}{tail}"
