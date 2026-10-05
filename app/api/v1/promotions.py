"""프로모션 이미지 조회.

GET /v1/promotions/active?placement=launch|tutorial — 지금 보여줄 프로모션 1건.
보여줄 것이 없으면 200 {"promotion": null} (오류가 아니다).
내용은 설정값(env/Parameter Store)으로 바꾼다 — docs/프로모션-이미지-교체.md.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from app.core.deps import DB, CurrentUser
from app.schemas.promotion import ActivePromotionResponse
from app.services import promotion as promotion_service

router = APIRouter(prefix="/promotions", tags=["promotions"])


@router.get("/active", response_model=ActivePromotionResponse)
def get_active_promotion(
    user: CurrentUser,
    db: DB,
    placement: Literal["launch", "tutorial"] = Query(default="launch"),
) -> ActivePromotionResponse:
    return ActivePromotionResponse(
        promotion=promotion_service.active_promotion(db, user.id, placement)
    )
