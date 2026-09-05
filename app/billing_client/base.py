"""스토어 구독 검증 클라이언트 계약.

Google Play / App Store 의 서로 다른 응답을 하나의 `StoreSubscription` 으로
정규화한다. 상위(서비스/라우터)는 스토어 차이를 알 필요가 없다.

예외는 두 가지로만 구분한다.
- `BillingVerificationError` : 스토어가 "그런 구매 없음/무효"라고 답한 경우 → 400/404
- `BillingUnavailableError`  : 통신 실패·자격증명 미설정 등 우리 쪽 문제 → 502/503
  (사용자 구매는 유효할 수 있으므로 절대 '무효'로 단정하면 안 된다)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class BillingVerificationError(Exception):
    """스토어가 구매를 인정하지 않음 (위조/오타/이미 소멸된 토큰)."""


class BillingUnavailableError(Exception):
    """스토어 API 통신 실패 또는 서버 자격증명 미설정."""


@dataclass
class StoreSubscription:
    platform: str  # android/ios
    product_id: str
    purchase_key: str  # android: purchaseToken / ios: originalTransactionId
    status: str  # models.billing.SUBSCRIPTION_STATUSES
    is_auto_renewing: bool = False
    environment: str = "production"  # production/sandbox
    started_at: datetime | None = None
    expires_at: datetime | None = None
    canceled_at: datetime | None = None
    latest_order_id: str | None = None
    raw: dict = field(default_factory=dict)


class BillingClient(Protocol):
    def verify_subscription(
        self, platform: str, product_id: str, purchase_token: str
    ) -> StoreSubscription:
        """구매 토큰을 스토어에 조회해 정규화된 구독 상태를 돌려준다.

        purchase_token 은 플랫폼별로 의미가 다르다.
        - android: Google Play Billing 의 purchaseToken
        - ios: StoreKit2 트랜잭션의 transactionId (originalTransactionId 도 허용)
        """
        ...

    def acknowledge(self, platform: str, product_id: str, purchase_token: str) -> None:
        """구매 확인(Android 전용, 3일 내 미확인 시 자동 환불). 실패는 삼킨다."""
        ...
