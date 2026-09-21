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
    """스토어 API 통신 실패 또는 서버 자격증명 미설정.

    `reason` 은 운영자가 원인을 구분하기 위한 코드다 (사용자 문구가 아니다).
    서버 로그를 못 보는 상황에서도 API 응답의 details 로 전달돼, "설정이 빠졌는지 /
    키가 틀렸는지 / 스토어 권한이 아직 반영 안 됐는지" 를 가릴 수 있게 한다.
    """

    def __init__(self, message: str, reason: str = "store_unavailable") -> None:
        super().__init__(message)
        self.reason = reason


# BillingUnavailableError.reason 값
REASON_NOT_CONFIGURED = "not_configured"        # 자격증명/패키지명 미설정
REASON_BAD_CREDENTIALS = "bad_credentials"      # JSON 형식 오류·키 서명 실패·토큰 발급 거부
REASON_PERMISSION = "store_permission"          # 스토어가 권한 없음(401/403) — 반영 대기 포함
REASON_UNREACHABLE = "store_unreachable"        # 네트워크/타임아웃/5xx


@dataclass
class BillingAccessCheck:
    """설정 점검 결과 (GET /v1/subscriptions/health).

    비밀값은 절대 담지 않는다 — 불리언과 코드, 상태코드만.
    """

    platform: str
    configured: bool = False       # 필요한 설정값이 채워져 있는가
    credentials_ok: bool | None = None  # 자격증명으로 인증 토큰을 얻을 수 있는가
    store_access_ok: bool | None = None  # 스토어가 우리 요청을 권한상 받아주는가
    reason: str | None = None
    status: int | None = None      # 스토어가 준 HTTP 상태코드 (참고용)


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

    def check_access(self) -> BillingAccessCheck:
        """실제 구매 없이 설정·자격증명·스토어 권한을 점검한다.

        구매 검증과 같은 경로를 쓰되 존재하지 않는 토큰으로 호출한다 —
        스토어가 "그런 구매 없음"(400/404)이라고 답하면 권한은 정상이라는 뜻이다.
        """
        ...
