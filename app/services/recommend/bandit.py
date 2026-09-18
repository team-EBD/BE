"""표시 화면에 맞는 한 카드에 적용하는 문맥 밴딧: 공유 보상 회귀 + epsilon-greedy.

홈은 대표 첫 카드, 추천 탭은 마지막 카드를 탐색하고 다른 위치는 규칙 순위로 정한다.
각 위치의 확률은 앞 선택들에 조건부다. 전체 슬레이트의 대안 정책을 평가하는
확률로 해석하지 않는다. 실제 노출 후 4시간이 지난 피드백을 요청 시 재학습하므로
식사 수정/삭제에 따른 보상 정정이 다음 요청에 반영된다. 외부 학습 작업은 필요 없다.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.food_taxonomy import FAMILIES
from app.models import RecommendationItem, RecommendationLog, User

from .candidates import Candidate
from .feedback import EATEN_WINDOW_HOURS, outcome_reward
from .ranking import RankContext, Ranked, after_selected, rank, score

POLICY_VERSION = "context-epsilon-v2"
FEATURE_VERSION = "food-context-v2"
REWARD_VERSION = "record1-start0.2-v1"
MEALS = ("breakfast", "lunch", "dinner", "snack")
MOODS = ("any", "light", "hearty")
PARTS = ("freq", "popularity", "similarity", "collaborative", "fit", "protein", "recent", "diversity")
FEATURE_NAMES = (
    ["baseline", "bias"] + list(PARTS) + [f"family:{f}" for f in FAMILIES]
    + [f"meal:{m}" for m in MEALS] + [f"mood:{m}" for m in MOODS] + ["position"]
    + [f"{p}*{m}" for p in ("fit", "protein") for m in MEALS]
    + ["surface:recommendation", "surface:home"]
)
OUTCOME_HOURS = EATEN_WINDOW_HOURS
MAX_TRAINING_ROWS = 1000
MAX_ROWS_PER_USER = 20
QUALITY_MARGIN = 0.12
MAX_ACTIONS = 10


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def features(item: Ranked, meal_type: str, mood: str, position: int, surface: str = "recommendation") -> list[float]:
    return (
        [_unit(item.score), 1.0] + [float(item.parts.get(p, 0)) for p in PARTS]
        + [float(item.candidate.family == f) for f in FAMILIES]
        + [float(meal_type == m) for m in MEALS] + [float(mood == m) for m in MOODS]
        + [min(position, 3) / 3]
        + [float(item.parts[p]) * float(meal_type == m) for p in ("fit", "protein") for m in MEALS]
        + [float(surface == "recommendation"), float(surface == "home")]
    )


@dataclass
class RewardModel:
    weights: list[float]
    samples: int = 0
    revision: str = "cold-start"
    users: int = 0

    def predict(self, values: list[float]) -> float:
        return _unit(values[0] + sum(w * x for w, x in zip(self.weights, values)))


def learn(db: Session, *, now: datetime) -> RewardModel:
    """노출·정책·특성 스냅샷이 있는 탐색 위치의 성숙한 결과만 학습한다.

무응답 0은 '기록 전환이 관측되지 않음'이며 음식 비선호로 저장하지 않는다.
선택 확률은 분석용으로 보존한다. 이 회귀는 IPS/DR 평가기가 아니다.
"""
    rows = db.execute(
        select(RecommendationItem, RecommendationLog.decision, RecommendationLog.user_id)
        .join(RecommendationLog, RecommendationLog.id == RecommendationItem.log_id)
        .join(User, User.id == RecommendationLog.user_id)
        .where(
            RecommendationItem.policy_version == POLICY_VERSION,
            RecommendationItem.shown_at >= now - timedelta(days=60),
            RecommendationItem.shown_at <= now - timedelta(hours=OUTCOME_HOURS),
            RecommendationLog.created_at <= now,
            RecommendationLog.decision["feature_version"].as_string() == FEATURE_VERSION,
            RecommendationItem.rank == RecommendationLog.decision["bandit_rank"].as_integer(),
            or_(User.email.is_(None), ~User.email.ilike("%@test.com")),
        )
        .order_by(RecommendationItem.shown_at.desc(), RecommendationItem.id.desc())
        .limit(MAX_TRAINING_ROWS)
    ).all()
    samples = []
    per_user: Counter = Counter()
    for row, decision, user_id in rows:
        if per_user[user_id] >= MAX_ROWS_PER_USER:
            continue
        values = row.features
        if (not isinstance(values, list) or len(values) != len(FEATURE_NAMES)
                or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values)
                or row.selection_probability is None or not 0 < row.selection_probability <= 1):
            continue
        samples.append((row.id, values, outcome_reward(row, now=now), user_id))
        per_user[user_id] += 1
    samples.reverse()
    weights = [0.0] * len(FEATURE_NAMES)
    # 작은 공유 선형 잔차 회귀. 강한 정규화와 표본 수 기반 혼합으로 초기 규칙을 보호한다.
    for _ in range(6):
        for _, values, reward, user_id in samples:
            error = reward - values[0] - sum(w * x for w, x in zip(weights, values))
            rate = 0.1 / (math.sqrt(per_user[user_id]) * (1 + sum(x * x for x in values)))
            weights = [w + rate * (error * x - 0.1 * w) for w, x in zip(weights, values)]
    revision = hashlib.sha256(json.dumps(samples, separators=(",", ":")).encode()).hexdigest()[:16] if samples else "cold-start"
    return RewardModel(weights, len(samples), revision, len(per_user))


def select_cards(
    candidates: list[Candidate], ctx: RankContext, model: RewardModel, *,
    meal_type: str, mood: str, k: int = 3, epsilon: float = 0.1,
    rng: random.Random | None = None,
    surface: str = "recommendation",
) -> tuple[list[Ranked], dict]:
    """홈은 첫 카드, 추천 탭은 마지막 카드를 탐색하고 나머지는 공통 순위로 결정한다."""
    epsilon = max(0.0, min(0.3, epsilon))
    unique = list({c.key: c for c in candidates}.values())
    count = min(max(0, k), len(unique))
    bandit_position = (1 if surface == "home" else count) if count else 0
    prefix = rank(unique, ctx, k=max(0, bandit_position - 1)) if count else []
    fixed = {r.candidate.key for r in prefix}
    remaining = sorted(
        (after_selected(score(c, ctx), prefix) for c in unique if c.key not in fixed),
        key=lambda r: (-r.score, r.candidate.key),
    ) if count else []
    eligible = [r for r in remaining if r.score >= remaining[0].score - QUALITY_MARGIN][:MAX_ACTIONS]
    # 같은 사람이 새로고침을 반복해도 공유 모델의 신뢰도를 크게 높이지 않는다.
    support = min(model.samples, model.users * 5)
    alpha = min(0.4, support / (support + 50))
    feature_map = {r.candidate.key: features(r, meal_type, mood, pos, surface) for pos, r in enumerate(prefix, 1)}
    feature_map.update({r.candidate.key: features(r, meal_type, mood, bandit_position, surface) for r in remaining})
    def policy_score(r):
        return (1 - alpha) * r.score + alpha * model.predict(feature_map[r.candidate.key])
    best = min(eligible, key=lambda r: (-policy_score(r), r.candidate.key)) if eligible else None
    probabilities = {
        r.candidate.key: epsilon / len(eligible) + ((1 - epsilon) if r is best else 0)
        for r in eligible
    }
    selected = list(prefix)
    if best is not None:
        draw = (rng or random.SystemRandom()).random()
        chosen = eligible[-1]
        cumulative = 0.0
        for item in eligible:
            cumulative += probabilities[item.candidate.key]
            if draw < cumulative:
                chosen = item
                break
        selected.append(Ranked(chosen.candidate, round(policy_score(chosen), 4), {
            **chosen.parts, "baseline": chosen.score,
            "reward_prediction": round(model.predict(feature_map[chosen.candidate.key]), 4),
            "learning_weight": round(alpha, 4),
        }))
    # 홈의 대표 카드가 정해진 뒤 전체 보기용 나머지 카드를 결정론적으로 채운다.
    # 각 후속 카드의 확률 1은 첫 선택에 조건부이며, 탐색 대상의 확률과 별도로 보존한다.
    while len(selected) < count:
        selected_keys = {r.candidate.key for r in selected}
        next_card = min(
            (after_selected(score(c, ctx), selected) for c in unique if c.key not in selected_keys),
            key=lambda r: (-r.score, r.candidate.key),
        )
        selected.append(next_card)
        fixed.add(next_card.candidate.key)
    selected_features = {
        r.candidate.key: (feature_map[r.candidate.key] if pos == bandit_position
                          else features(r, meal_type, mood, pos, surface))
        for pos, r in enumerate(selected, 1)
    }
    snapshots = []
    ranked_by_key = {r.candidate.key: r for r in prefix + remaining}
    for c in unique:
        r = ranked_by_key.get(c.key) or score(c, ctx)
        snapshots.append({
            "key": c.key, "name": c.name, "group_id": c.group_id, "family": c.family,
            "role": c.role, "source": c.source, "freq": c.freq, "popularity": c.popularity,
            "similarity": c.similarity, "similar_to": c.similar_to,
            "collaborative": c.collaborative, "collaborative_support": c.collaborative_support,
            "calories": c.calories, "protein": c.protein, "carbs": c.carbs, "fat": c.fat,
            "companion_key": c.companion_key, "total_calories": c.total_calories,
            "total_protein": c.total_protein, "baseline_score": r.score, "parts": r.parts,
            "features": feature_map.get(c.key, []),
            "action_probability": probabilities.get(c.key, 0.0),
        })
    return selected, {
        "policy_version": POLICY_VERSION, "feature_version": FEATURE_VERSION,
        "reward_version": REWARD_VERSION, "taxonomy_version": "food-family18-v1",
        "similarity_version": "food-attributes-v2", "cf_version": "item-cooccurrence-v1",
        "surface": surface, "bandit_rank": bandit_position or None,
        "epsilon": epsilon, "quality_margin": QUALITY_MARGIN,
        "fixed_keys": [r.candidate.key for r in selected if r.candidate.key in fixed],
        "fixed_prefix_keys": [r.candidate.key for r in prefix], "action_keys": list(probabilities),
        "greedy_key": best.candidate.key if best else None,
        "selected_keys": [r.candidate.key for r in selected],
        "probabilities": {**probabilities, **{key: 1.0 for key in fixed}},
        "action_probabilities": probabilities, "selected_features": selected_features,
        "candidates": snapshots, "feature_names": FEATURE_NAMES,
        "model": {"revision": model.revision, "samples": model.samples, "users": model.users, "weights": model.weights,
                  "learning_weight": alpha},
    }
