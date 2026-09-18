"""랭킹 — 선호 근거·끼니 적합·피드백으로 점수를 매기고 근접 후보의 다양성을 보완한다.

선호 근거는 개인 빈도, 음식 유사도, 협업, 전체 인기도 중 가장 강한 것을 쓴다. 빈도·인기도는
고정된 포화 함수로 0~1을 만들므로 후보 추가나 제거로 기존 후보의 점수가 바뀌지 않는다.
서로 겹친 생성기의 근거를 단순 합산하지 않고, 출처 이름은 점수에 사용하지 않는다.
예산 적합은 밥을 포함한 열량, 단백질 보완도 밥을 포함한 단백질로 계산한다. 예산에 크게
맞지 않는 후보가 빈도·단백질 보너스로 상위에 오르지 않도록 두 보너스에 적합도를 곱한다.
최근 섭취는 별도 감점이고, 비슷한 점수 사이에서만 계열·개인 출처 중복을 줄인다.

가중치는 검증 가능한 초기 휴리스틱이며 실제 채택·섭취율로 후속 보정해야 한다.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .candidates import Candidate
from .dish_type import dish_type

W_FREQ = 0.35
W_FIT = 0.30
W_PROTEIN = 0.15
W_ACCEPT = 0.15
W_RECENT = 0.30
FREQUENCY_HALF_SATURATION = 2.0
POPULARITY_HALF_SATURATION = 5.0
SIMILARITY_CONFIDENCE = 0.75  # 간접 선호는 충분히 반복된 개인 이력보다 약한 근거
POPULARITY_CONFIDENCE = 0.55  # 전체 인기의 개인 선호 근거는 더 약하다
DEFAULT_ACCEPTANCE = 0.5  # 채택 데이터 없을 때의 중립값
UNDER_BUDGET_PENALTY = 0.5  # 미달분 감점 배율
OVER_BUDGET_PENALTY = 2.0  # 초과분 감점 배율
TOP_K = 3
MAX_PERSONAL_IN_TOP = 2  # 강제 상한이 아닌, 점수가 가까울 때의 개인 후보 목표
DIVERSITY_PENALTY = 0.04
MAX_DIVERSITY_PENALTY = 0.08  # 이보다 점수가 낮은 후보를 다양성만으로 끌어올리지 않는다


@dataclass
class RankContext:
    budget: int  # 이번 끼니 예산 kcal
    protein_gap: float  # 오늘 단백질 부족분 g (양수면 부족)
    recency: Mapping[str, float] = field(default_factory=dict)  # key → 질림 감점 0~1
    acceptance: Mapping[str, float] = field(default_factory=dict)  # key → 섭취율 0~1


@dataclass
class Ranked:
    candidate: Candidate
    score: float
    parts: dict[str, float]  # 항목별 0~1 점수. diversity 만 실제 차감 점수


def fit_score(calories: float, budget: int) -> float:
    if budget <= 0:
        return 0.0
    if calories <= budget:
        return max(0.0, 1.0 - UNDER_BUDGET_PENALTY * (budget - calories) / budget)
    return max(0.0, 1.0 - OVER_BUDGET_PENALTY * (calories - budget) / budget)


def _unit(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _saturate(value: float, half: float) -> float:
    value = max(0.0, value)
    return value / (value + half)


def score(cand: Candidate, ctx: RankContext, max_freq: float | None = None) -> Ranked:
    # max_freq 인자는 기존 호출 호환용. 후보 풀 최대값은 더 이상 정규화 기준이 아니다.
    freq = _saturate(cand.freq, FREQUENCY_HALF_SATURATION)
    popularity = _saturate(cand.popularity, POPULARITY_HALF_SATURATION)
    similarity = _unit(cand.similarity)
    collaborative = _unit(cand.collaborative)
    relevance = max(freq, SIMILARITY_CONFIDENCE * similarity,
                    0.85 * collaborative, POPULARITY_CONFIDENCE * popularity)
    # 예산 적합은 메인 + 동반(밥) 합산으로 — 김치찌개 320 단독은 예산 630 에 '가벼움'이지만
    # 실제 식사(찌개+밥 620)는 '적정'이다 (docs/음식군-DB-계약.md §8)
    fit = fit_score(cand.total_calories, ctx.budget)
    protein = (
        _unit(cand.total_protein / ctx.protein_gap) if ctx.protein_gap > 0 else 0.0
    )
    accept = _unit(ctx.acceptance.get(cand.key, DEFAULT_ACCEPTANCE))
    recent = _unit(ctx.recency.get(cand.key, 0.0))
    total = (
        W_FREQ * relevance * fit + W_FIT * fit + W_PROTEIN * protein * fit
        + W_ACCEPT * accept - W_RECENT * recent
    )
    return Ranked(
        candidate=cand,
        score=round(total, 4),
        parts={
            "freq": round(freq, 3),
            "popularity": round(popularity, 3),
            "similarity": round(similarity, 3),
            "collaborative": round(collaborative, 3),
            "relevance": round(relevance, 3),
            "fit": round(fit, 3),
            "protein": round(protein, 3),
            "accept": round(accept, 3),
            "recent": round(recent, 3),
            "diversity": 0.0,
        },
    )


def after_selected(item: Ranked, selected: list[Ranked], *, max_personal: int = MAX_PERSONAL_IN_TOP) -> Ranked:
    """이미 고른 카드에 대한 다양성 감점. 규칙 순위와 밴딧에 같은 기준을 적용한다."""
    def category(cand: Candidate) -> str | None:
        return cand.family or dish_type(cand.group_name or cand.name)

    kind = category(item.candidate)
    repeats = sum(kind is not None and category(r.candidate) == kind for r in selected)
    if item.candidate.source == "personal" and sum(r.candidate.source == "personal" for r in selected) >= max(0, max_personal):
        repeats += 1
    penalty = min(MAX_DIVERSITY_PENALTY, DIVERSITY_PENALTY * repeats)
    return Ranked(item.candidate, round(item.score - penalty, 4),
                  {**item.parts, "diversity": round(penalty, 3)})


def rank(
    candidates: list[Candidate],
    ctx: RankContext,
    *,
    k: int = TOP_K,
    max_personal: int = MAX_PERSONAL_IN_TOP,
) -> list[Ranked]:
    """점수순 상위 k개. 계열·개인 출처 중복은 작은 감점으로만 조정한다.

    max_personal 은 강제 할당량이 아니다. 적합한 대안이 없으면 개인 후보나 같은 계열도
    유지한다. 순차 선택 시 감점을 score·parts 에 반영하므로 반환 점수와 표시 순서도 일치한다.
    """
    if not candidates or k <= 0:
        return []
    remaining = [score(c, ctx) for c in candidates]
    top: list[Ranked] = []
    while remaining and len(top) < k:
        best = min(
            (after_selected(item, top, max_personal=max_personal) for item in remaining),
            key=lambda item: (-item.score, item.candidate.key),
        )
        top.append(best)
        remaining = [item for item in remaining if item.candidate.key != best.candidate.key]
    return top
