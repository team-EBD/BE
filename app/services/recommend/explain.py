"""추천 이유 문장 — 템플릿. LLM 을 쓰지 않아 지연 0·결정론."""
from __future__ import annotations

from .ranking import Ranked
from .signals import Budget, budget_label

MEAL_LABEL = {"breakfast": "아침", "lunch": "점심", "dinner": "저녁", "snack": "간식"}

_HEAD = {
    "personal": "{meal}에 자주 드시는 메뉴예요",
    "popular": "{meal}에 많이 기록되는 메뉴예요",
    "catalog": "{meal}에 무난한 기본 메뉴예요",
    "collaborative": "비슷한 메뉴를 기록한 다른 이용자들이 먹은 음식이에요",
    "similar": "자주 드시는 {anchor}와 음식 특성이 비슷해요",
    "similar_type": "자주 드시는 {anchor} 같은 {kind}예요",
}
# 끼니 예산은 우리가 추정한 값이라 숫자로 보여주지 않는다 (사용자가 정한 예산이 아니다).
# 예산 대비 가벼움/무거움만 말로 풀고, 칼로리·동반(밥) 합산은 카드의 kcal 줄이 보여준다.
_LABEL_TAIL = {
    "fit": "",
    "light": " · 가볍게 드실 수 있어요",
    "heavy": " · 양이 조금 많은 편이에요",
}
PROTEIN_MENTION_MIN = 0.5  # 단백질 부족분의 절반 이상 채우면 언급


def _kind_label(kind: str) -> str:
    # 계열 이름은 그대로("국·탕·찌개류"), 어미는 '류'를 붙인다("찌개" → "찌개류")
    return kind if kind.endswith("류") or "·" in kind else f"{kind}류"


def reason(ranked: Ranked, budget: Budget) -> str:
    cand = ranked.candidate
    meal = MEAL_LABEL.get(budget.meal_type, "이번 끼니")
    if cand.source == "similar" and cand.similar_type:
        head = _HEAD["similar_type"].format(anchor=cand.similar_to, kind=_kind_label(cand.similar_type))
    elif cand.source == "similar":
        head = _HEAD["similar"].format(anchor=cand.similar_to or "음식")
    else:
        head = _HEAD.get(cand.source, _HEAD["popular"]).format(meal=meal)

    tail = _LABEL_TAIL[budget_label(cand.total_calories, budget.meal_budget)]
    if ranked.parts.get("protein", 0.0) >= PROTEIN_MENTION_MIN:
        tail += " · 단백질 보충에 좋아요"
    return f"{head}{tail}"
