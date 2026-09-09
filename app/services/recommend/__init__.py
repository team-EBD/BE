"""추천 엔진 v2 — 후보 생성 → 랭킹 → 설명.

AI 서버 호출 없이 BE 안에서 결정론적으로 계산한다. 구조:

    signals    끼니별 개인 빈도(시간 감쇠) · 전체 인기 · 끼니 예산
    candidates 후보 생성기 3종(개인 빈도 / 전체 인기 / 영양 유사) + 합집합
    ranking    가중 점수 랭킹 + 다양성 규칙(개인 출신 최대 2개)
    explain    템플릿 이유 문장
    feedback   노출·채택·섭취 로그 (recommendation_logs JSON) → 랭킹의 채택률 입력
    engine     위를 조립하는 진입점 recommend()

DB 스키마 변경 없이 현재 테이블만 읽고 쓴다. 라우터 연결(API)은 별도 단계.
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
