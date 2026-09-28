"""추천 v2: 개인·음식 유사·협업·인기 후보 → 공통 순위 → 마지막 카드 밴딧.

실제 카드 노출·기록 시작·명시 거절·연결된 식사 저장 결과를 다음 추천에 반영한다.
"""
from .engine import RecommendationResult, RecommendedItem, recommend
from .feedback import acceptance_rates, log_exposure, mark_accepted, mark_eaten, source_stats

__all__ = [
    "recommend",
    "RecommendationResult",
    "RecommendedItem",
    "log_exposure",
    "mark_accepted",
    "mark_eaten",
    "acceptance_rates",
    "source_stats",
]
