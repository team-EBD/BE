"""구독(인앱 결제) 스키마.

FE 는 스토어가 준 구매 토큰만 보내고, 상태·만료일은 전부 서버 응답을 따른다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import KSTDateTime


class SubscriptionVerifyRequest(BaseModel):
    platform: Literal["android", "ios"]
    product_id: str = Field(min_length=1, max_length=100)
    # android: Google Play Billing 의 purchaseToken
    # ios: StoreKit2 트랜잭션의 transactionId (originalTransactionId 도 가능)
    purchase_token: str = Field(min_length=1, max_length=4096)


class SubscriptionResponse(BaseModel):
    """현재 구독 상태. 구독 이력이 없으면 is_premium=false 에 나머지는 null."""

    is_premium: bool
    status: str | None = None  # active/grace/canceled/on_hold/paused/expired/revoked/pending
    platform: str | None = None
    product_id: str | None = None
    is_auto_renewing: bool = False
    started_at: KSTDateTime | None = None
    expires_at: KSTDateTime | None = None
    environment: str | None = None  # production/sandbox
