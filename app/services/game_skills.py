"""사진 분석 경로에서 발동하는 지원 스킬 — '발견 돋보기'(`food_clarifier`).

기록 보상(`game_rewards`)이나 미션(`game_missions`)이 아니라 **AI 분석 요청**을
바꾸는 유일한 스킬이라 여기 따로 둔다.

발동 조건 (셋 다 참일 때만):
  ① feature flag `settings.game_food_clarifier`
  ② `game_profiles.equipped_skill_code == "food_clarifier"` 이고 해금돼 있다
  ③ 충전이 남아 있다 — **마지막 사용 논리 날짜로부터 7일**이 지났다

충전 소모는 `skill_usage_ledger` 의 `food-clarifier:<logical-date>` 키로 멱등이다
(핸드오프 §4). 기록일 기준으로 충전되는 `streak_pause` 와 달리 달력 기준이라
`user_skills.charge_count` 를 깎지 않는다 — `_recharge_skills()` 가 기록일로만
되돌려 주기 때문에 깎으면 영영 0 으로 남는다. **원장이 단일 원천**이고,
표시용 잔여 충전도 `remaining_charges()` 로 원장에서 계산한다.

충전을 쓰는 시점은 **요청을 보내기 직전**이다. 추가 AI 비용은 응답이 실패해도
이미 발생했고, 원장은 지우지 않는다(불변 조건).
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import GameProfile, SkillUsageLedger, UserSkill
from app.services import game_catalog as catalog
from app.services.game_ledger import skill_usage
from app.services.game_profile import logical_today

logger = logging.getLogger("eatlog.game_skills")

CLARIFIER_SKILL_CODE = "food_clarifier"
# 시드(`gamification_growth_v3.json`)의 `recharge_days` 가 단일 원천. 비어 있으면 7일.
_DEFAULT_RECHARGE_DAYS = 7

# AI 서버 `/internal/analyze` 의 candidate_depth 계약값
DEPTH_STANDARD = "standard"
DEPTH_CLARIFIER = "clarifier"


def recharge_days() -> int:
    return catalog.skill_calendar_recharge_days(CLARIFIER_SKILL_CODE) or _DEFAULT_RECHARGE_DAYS


def last_used_date(db: Session, user_id: int) -> date | None:
    """'발견 돋보기'를 마지막으로 쓴 논리 날짜."""
    return db.scalar(
        select(func.max(SkillUsageLedger.logical_date)).where(
            SkillUsageLedger.user_id == user_id,
            SkillUsageLedger.skill_code == CLARIFIER_SKILL_CODE,
        )
    )


def charge_available(db: Session, user_id: int, day: date) -> bool:
    """마지막 사용일로부터 재충전 주기가 지났는가 (한 번도 안 썼으면 True)."""
    last = last_used_date(db, user_id)
    return last is None or (day - last).days >= recharge_days()


def remaining_charges(db: Session, user_id: int, fallback: int) -> int:
    """표시용 잔여 충전. 원장 기준이라 `user_skills.charge_count` 를 쓰지 않는다."""
    try:
        return 1 if charge_available(db, user_id, logical_today()) else 0
    except Exception:  # pragma: no cover — 표시 실패가 조회 API 를 깨면 안 된다
        logger.warning("발견 돋보기 충전 조회 실패", exc_info=True)
        return fallback


def _equipped_and_unlocked(db: Session, user_id: int) -> bool:
    profile = db.scalar(select(GameProfile).where(GameProfile.user_id == user_id))
    if profile is None or profile.equipped_skill_code != CLARIFIER_SKILL_CODE:
        return False
    return db.scalar(
        select(UserSkill.id).where(
            UserSkill.user_id == user_id,
            UserSkill.skill_code == CLARIFIER_SKILL_CODE,
        )
    ) is not None


def candidate_depth(db: Session, user_id: int, day: date | None = None) -> str:
    """이번 분석 요청의 후보 깊이. `"clarifier"` 면 충전 1회를 소모한 것이다.

    **판정이 터져도 기록 흐름을 막지 않는다** — 어떤 예외든 삼키고 standard 로
    되돌린다. 저장 시도는 savepoint 안에서 하므로 실패해도 세션은 살아 있다.
    """
    if not settings.game_food_clarifier:
        return DEPTH_STANDARD
    day = day or logical_today()
    used = False
    try:
        with db.begin_nested():
            if not _equipped_and_unlocked(db, user_id):
                return DEPTH_STANDARD
            if not charge_available(db, user_id, day):
                return DEPTH_STANDARD
            used = skill_usage(
                db,
                user_id,
                key=f"food-clarifier:{day.isoformat()}",
                skill_code=CLARIFIER_SKILL_CODE,
                day=day,
                reason="extra_candidate",
            )
    except Exception:
        logger.warning("발견 돋보기 판정 실패 — standard 로 진행", exc_info=True)
        return DEPTH_STANDARD
    return DEPTH_CLARIFIER if used else DEPTH_STANDARD
