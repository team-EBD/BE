"""프로모션 이미지 스키마 (GET /v1/promotions/active)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Promotion(BaseModel):
    # 프로모션이 바뀌면 값이 바뀐다 — FE 는 '보지 않기' 기록을 id 별로 저장한다
    id: str
    image_url: str
    # 이미지를 받기 전에 자리를 잡기 위한 원본 크기(px)
    image_width: int
    image_height: int
    alt_text: str
    # subscription: 구독 화면 / url: action_url 을 웹뷰로 / none: 동작 없음
    action: Literal["subscription", "url", "none"]
    action_url: str | None = None
    placements: list[str]


class ActivePromotionResponse(BaseModel):
    """보여줄 것이 없으면 promotion 은 null (오류가 아니다)."""

    promotion: Promotion | None = None
