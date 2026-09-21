"""스토어 없이 도는 결제 검증 목 (로컬 개발/테스트용).

purchase_token 접두사로 분기해서, 실제 스토어 계정 없이도 만료·무효·유예까지
모든 경로를 눌러볼 수 있게 한다.

  invalid-*  -> BillingVerificationError (스토어가 부정한 구매)
  down-*     -> BillingUnavailableError  (스토어 통신 실패)
  expired-*  -> 어제 만료된 구독
  canceled-* -> 해지 예약(갱신 꺼짐, 만료 전까지 권한 유지)
  그 외      -> 30일 뒤 만료되는 정상 구독
"""
from __future__ import annotations

import logging
from datetime import timedelta

from app.billing_client.base import (
    BillingAccessCheck,
    BillingUnavailableError,
    BillingVerificationError,
    StoreSubscription,
)
from app.core.timeutil import now_utc

logger = logging.getLogger("eatlog.billing")

_PERIOD = timedelta(days=30)


class MockBillingClient:
    def verify_subscription(
        self, platform: str, product_id: str, purchase_token: str
    ) -> StoreSubscription:
        if purchase_token.startswith("invalid"):
            raise BillingVerificationError("스토어에서 확인되지 않는 구매입니다. (mock)")
        if purchase_token.startswith("down"):
            raise BillingUnavailableError("스토어 통신 실패 (mock)")

        now = now_utc()
        if purchase_token.startswith("expired"):
            status, expires_at, auto_renew = "expired", now - timedelta(days=1), False
        elif purchase_token.startswith("canceled"):
            status, expires_at, auto_renew = "canceled", now + _PERIOD, False
        else:
            status, expires_at, auto_renew = "active", now + _PERIOD, True

        logger.info("[mock billing] %s %s -> %s", platform, product_id, status)
        return StoreSubscription(
            platform=platform,
            product_id=product_id,
            purchase_key=purchase_token,
            status=status,
            is_auto_renewing=auto_renew,
            environment="sandbox",
            started_at=now - _PERIOD,
            expires_at=expires_at,
            latest_order_id=f"mock-order-{purchase_token[:16]}",
            raw={"mock": True, "token": purchase_token},
        )

    def acknowledge(self, platform: str, product_id: str, purchase_token: str) -> None:
        logger.info("[mock billing] acknowledge %s %s", platform, product_id)

    def check_access(self) -> BillingAccessCheck:
        """목은 항상 정상 — 대신 mock 임을 드러내 운영 오인을 막는다."""
        return BillingAccessCheck(
            platform="mock",
            configured=True,
            credentials_ok=True,
            store_access_ok=True,
            reason="mock_backend",
        )
