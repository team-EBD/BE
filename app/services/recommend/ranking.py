"""랭킹 — 후보에 점수를 매겨 정렬하고 상위 k개를 고른다 (정밀도 담당).

score = 0.35·습관강도(정규화) + 0.30·예산적합 + 0.15·단백질보완 + 0.15·채택률 − 0.30·질림

- 습관강도: 후보 중 최대 감쇠 점수(반감기 45일)로 나눠 0~1. popular/similar 출신은 0
- 예산적합: 예산에 가까울수록 1. 미달분은 절반만 감점(단품은 끼니 예산보다 작은 게
  정상 — 찌개+밥처럼 조합해 먹는다), 초과분은 2배로 감점(과식 방지)
- 단백질보완: 오늘 단백질이 목표에 못 미칠 때만, 부족분 대비 후보 단백질 비율(상한 1)
- 채택률: 과거 추천 대비 실제 섭취 비율. 피드백 로그가 붙기 전엔 전부 0.5 → 순위에 영향 없음
- 질림: 재섭취 주기 대비 최근 섭취(signals.recency_penalties, 0~1). 어제 먹은 습관 음식
  (0.35 − 0.30·0.8 = 0.11)이 일주일 만에 먹을 때가 된 습관 음식(0.35·0.8 = 0.28)에 밀린다 —
  "최근에 먹은 건 오히려 안 먹는다"를 빈도보다 세게 반영

가중치는 초기값이다 — 출처별 섭취율이 쌓이면 조정하고, 채택 로그가 충분해지면 이 함수
전체를 밴딧(Thompson Sampling)으로 교체한다. 후보 생성은 그대로 둔다.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .candidates import Candidate

W_FREQ = 0.35
W_FIT = 0.30
W_PROTEIN = 0.15
W_ACCEPT = 0.15
W_RECENT = 0.30
DEFAULT_ACCEPTANCE = 0.5  # 채택 데이터 없을 때의 중립값
UNDER_BUDGET_PENALTY = 0.5  # 미달분 감점 배율
OVER_BUDGET_PENALTY = 2.0  # 초과분 감점 배율
TOP_K = 3
MAX_PERSONAL_IN_TOP = 2  # "익숙한 것 2 + 새로운 것 1" 보장


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
    parts: dict[str, float]  # 항목별 기여(가중치 적용 전) — 미리보기·디버깅용


def fit_score(calories: float, budget: int) -> float:
    if budget <= 0:
        return 0.0
    if calories <= budget:
        return max(0.0, 1.0 - UNDER_BUDGET_PENALTY * (budget - calories) / budget)
    return max(0.0, 1.0 - OVER_BUDGET_PENALTY * (calories - budget) / budget)


def score(cand: Candidate, ctx: RankContext, max_freq: float) -> Ranked:
    freq = cand.freq / max_freq if max_freq > 0 else 0.0
    fit = fit_score(cand.calories, ctx.budget)
    protein = (
        min(cand.protein / ctx.protein_gap, 1.0) if ctx.protein_gap > 0 and cand.protein > 0 else 0.0
    )
    accept = float(ctx.acceptance.get(cand.key, DEFAULT_ACCEPTANCE))
    recent = float(ctx.recency.get(cand.key, 0.0))
    total = W_FREQ * freq + W_FIT * fit + W_PROTEIN * protein + W_ACCEPT * accept - W_RECENT * recent
    return Ranked(
        candidate=cand,
        score=round(total, 4),
        parts={
            "freq": round(freq, 3),
            "fit": round(fit, 3),
            "protein": round(protein, 3),
            "accept": round(accept, 3),
            "recent": round(recent, 3),
        },
    )


def rank(
    candidates: list[Candidate],
    ctx: RankContext,
    *,
    k: int = TOP_K,
    max_personal: int = MAX_PERSONAL_IN_TOP,
) -> list[Ranked]:
    """점수순 상위 k개. 개인 출신이 max_personal 을 넘으면 가장 낮은 개인 후보를
    다음 순위의 비(非)개인 후보로 교체한다. 같은 입력이면 항상 같은 결과."""
    if not candidates:
        return []
    max_freq = max(c.freq for c in candidates)
    ranked = sorted(
        (score(c, ctx, max_freq) for c in candidates),
        key=lambda r: (-r.score, r.candidate.key),
    )
    top, rest = ranked[:k], ranked[k:]

    def personal_count() -> int:
        return sum(1 for r in top if r.candidate.source == "personal")

    while personal_count() > max_personal:
        replacement = next((r for r in rest if r.candidate.source != "personal"), None)
        if replacement is None:
            break  # 비개인 후보가 없으면 그대로 둔다 (신규 사용자 반대 케이스)
        lowest_personal = max(i for i, r in enumerate(top) if r.candidate.source == "personal")
        top[lowest_personal] = replacement
        rest.remove(replacement)
        top.sort(key=lambda r: (-r.score, r.candidate.key))
    return top
