"""프로모션 이미지 (앱 시작 팝업 · 튜토리얼 페이월).

내용은 전부 설정값(env/Parameter Store)에서 온다 — 이미지를 바꾸는 데 앱 재배포도
서버 코드 변경도 필요 없다 (docs/프로모션-이미지-교체.md).

보여줄 것이 없으면 None 을 돌려준다:
- promo_enabled 가 꺼짐
- promo_image_url 이 비어 있음
- 요청한 placement 가 promo_placements 에 없음
- 프리미엄 사용자인데 promo_show_to_premium 이 false
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import settings
from app.schemas.promotion import Promotion
from app.services.subscription import is_premium

logger = logging.getLogger("eatlog.promotion")

PLACEMENTS = ("launch", "tutorial")
ACTIONS = ("subscription", "url", "none")
_DEFAULT_IMAGE_SIZE = (1080, 1620)
_DEFAULT_TUTORIAL_IMAGE_SIZE = (1536, 1024)


def configured_placements() -> list[str]:
    """promo_placements(콤마 구분)를 목록으로. 공백·대소문자·중복은 정리한다."""
    seen: list[str] = []
    for raw in settings.promo_placements.split(","):
        value = raw.strip().lower()
        if value and value not in seen:
            seen.append(value)
    return seen


def _resolve_action() -> tuple[str, str | None]:
    """(action, action_url). 설정이 잘못됐으면 안전한 쪽(none)으로 떨어진다."""
    action = settings.promo_action.strip().lower()
    if action not in ACTIONS:
        return "none", None
    if action == "url":
        url = settings.promo_action_url.strip()
        # 열 주소가 없는 url 동작은 눌러도 아무 일이 없어야 한다
        return ("url", url) if url else ("none", None)
    return action, None


def _is_premium_safely(db: Session, user_id: int) -> bool:
    """프리미엄 판정에 실패해도 프로모션 조회가 깨지면 안 된다 — 실패하면 무료로 본다."""
    try:
        return is_premium(db, user_id)
    except Exception:  # noqa: BLE001 — 어떤 실패든 팝업 하나 때문에 500 을 내지 않는다
        logger.warning(
            "프로모션: 프리미엄 판정 실패(무료로 간주) user=%s", user_id, exc_info=True
        )
        return False


def active_promotion(db: Session, user_id: int, placement: str) -> Promotion | None:
    """이 사용자에게 placement 위치에서 보여줄 프로모션. 없으면 None."""
    if not settings.promo_enabled:
        return None
    image_url = settings.promo_image_url.strip()
    if not image_url:
        return None
    placements = configured_placements()
    if placement not in placements:
        return None
    if not settings.promo_show_to_premium and _is_premium_safely(db, user_id):
        return None

    width, height = settings.promo_image_width, settings.promo_image_height
    if placement == "tutorial":
        # 튜토리얼 페이월은 히어로 자리가 낮다 — 전용(가로형) 이미지가 있으면 그것을 쓴다
        tutorial_url = settings.promo_tutorial_image_url.strip()
        if tutorial_url:
            image_url = tutorial_url
            width = settings.promo_tutorial_image_width
            height = settings.promo_tutorial_image_height
            if width <= 0 or height <= 0:
                width, height = _DEFAULT_TUTORIAL_IMAGE_SIZE
    if width <= 0 or height <= 0:
        # FE 가 비율 계산에 쓰는 값 — 0 이하가 내려가면 레이아웃이 깨진다
        width, height = _DEFAULT_IMAGE_SIZE
    action, action_url = _resolve_action()
    return Promotion(
        id=settings.promo_id.strip(),
        image_url=image_url,
        image_width=width,
        image_height=height,
        alt_text=settings.promo_alt_text,
        action=action,
        action_url=action_url,
        placements=placements,
    )
