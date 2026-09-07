"""App Store 구독 검증 (App Store Server API v1).

`GET /inApps/v1/subscriptions/{transactionId}` 로 구독 그룹의 최신 상태를 받는다.
  - 운영:   https://api.storekit.itunes.apple.com
  - 샌드박스: https://api.storekit-sandbox.itunes.apple.com

TestFlight/시뮬레이터 구매는 샌드박스에만 존재하므로, 운영에서 404 가 나면
샌드박스로 한 번 더 조회한다 (Apple 권장 순서 — 운영 우선).

인증: App Store Connect API 키(.p8)로 서명한 ES256 JWT.
  header  {alg: ES256, kid: <Key ID>, typ: JWT}
  payload {iss: <Issuer ID>, iat, exp, aud: "appstoreconnect-v1", bid: <Bundle ID>}

응답 안의 signedTransactionInfo/signedRenewalInfo 는 Apple 이 서명한 JWS 다.
여기서는 **페이로드만 디코드**한다 — 값 자체를 Apple 서버에서 TLS 로 직접 받았기
때문에(클라이언트를 거치지 않는다) x5c 인증서 체인 검증이 추가 보증을 주지 않는다.
클라이언트가 보낸 JWS 를 그대로 믿는 구조였다면 반드시 체인 검증이 필요하다.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from datetime import datetime, timezone

import httpx
from jose import jwt

from app.billing_client.base import (
    REASON_BAD_CREDENTIALS,
    REASON_NOT_CONFIGURED,
    REASON_PERMISSION,
    REASON_UNREACHABLE,
    BillingAccessCheck,
    BillingUnavailableError,
    BillingVerificationError,
    StoreSubscription,
)

logger = logging.getLogger("eatlog.billing")

_PROD_BASE = "https://api.storekit.itunes.apple.com"
_SANDBOX_BASE = "https://api.storekit-sandbox.itunes.apple.com"
_AUDIENCE = "appstoreconnect-v1"
_TIMEOUT = 10.0
# Apple 상한은 60분. 짧게 잡고 매 요청 새로 서명한다(상태 보관 불필요).
_JWT_TTL_SECONDS = 20 * 60

# App Store Server API 의 구독 status 코드
_STATUS_MAP = {
    1: "active",  # autoRenewStatus 가 꺼져 있으면 아래에서 canceled 로 내린다
    2: "expired",
    3: "on_hold",  # 결제 재시도 중 — 권한 없음
    4: "grace",
    5: "revoked",
}


def _from_ms(value: int | float | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _decode_jws_payload(token: str | None) -> dict:
    """JWS 의 페이로드 세그먼트만 디코드 (서명 검증은 상단 주석 참고)."""
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError):
        logger.warning("App Store JWS 페이로드 디코드 실패")
        return {}


class AppStoreBillingClient:
    def __init__(self, issuer_id: str, key_id: str, private_key: str, bundle_id: str) -> None:
        self._issuer_id = issuer_id
        self._key_id = key_id
        # 배포 환경변수에서는 개행이 \n 문자열로 들어오는 경우가 많다
        self._private_key = (private_key or "").replace("\\n", "\n").strip()
        self._bundle_id = bundle_id

    def _auth_token(self) -> str:
        if not (self._issuer_id and self._key_id and self._private_key and self._bundle_id):
            raise BillingUnavailableError(
                "App Store Server API 자격증명이 설정되지 않았습니다.", REASON_NOT_CONFIGURED
            )
        issued = int(time.time())
        try:
            return jwt.encode(
                {
                    "iss": self._issuer_id,
                    "iat": issued,
                    "exp": issued + _JWT_TTL_SECONDS,
                    "aud": _AUDIENCE,
                    "bid": self._bundle_id,
                },
                self._private_key,
                algorithm="ES256",
                headers={"kid": self._key_id, "typ": "JWT"},
            )
        except Exception as exc:  # noqa: BLE001 — 키 형식 오류를 설정 문제로 표면화
            raise BillingUnavailableError(
                f"App Store API 키 서명 실패: {exc}", REASON_BAD_CREDENTIALS
            ) from exc

    def _get(self, base: str, transaction_id: str) -> httpx.Response:
        url = f"{base}/inApps/v1/subscriptions/{transaction_id}"
        try:
            return httpx.get(
                url,
                headers={"Authorization": f"Bearer {self._auth_token()}"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise BillingUnavailableError(
                f"App Store 구독 조회 통신 실패: {exc}", REASON_UNREACHABLE
            ) from exc

    def verify_subscription(
        self, platform: str, product_id: str, purchase_token: str
    ) -> StoreSubscription:
        res = self._get(_PROD_BASE, purchase_token)
        if res.status_code == 404:
            # 운영에 없으면 샌드박스(TestFlight/시뮬레이터) 구매일 수 있다
            res = self._get(_SANDBOX_BASE, purchase_token)

        if res.status_code == 404:
            raise BillingVerificationError("App Store 에서 확인되지 않는 구매입니다.")
        if res.status_code in (401, 403):
            logger.warning("App Store 인증 거부 %d - %s", res.status_code, res.text[:300])
            raise BillingUnavailableError(
                "App Store 가 API 키를 인정하지 않습니다.", REASON_PERMISSION
            )
        if res.status_code >= 400:
            logger.warning("App Store 구독 조회 실패 %d - %s", res.status_code, res.text[:200])
            raise BillingUnavailableError(
                "App Store 구독 조회에 실패했습니다.", REASON_UNREACHABLE
            )

        return self._to_subscription(res.json(), product_id, purchase_token)

    def _to_subscription(
        self, body: dict, requested_product_id: str, transaction_id: str
    ) -> StoreSubscription:
        groups = body.get("data") or []
        transactions = [t for group in groups for t in (group.get("lastTransactions") or [])]
        if not transactions:
            raise BillingVerificationError("App Store 응답에 구독 트랜잭션이 없습니다.")

        # 요청한 트랜잭션과 같은 구독을 우선 고른다 (구독 그룹에 여러 상품이 있을 수 있다)
        latest = next(
            (t for t in transactions if t.get("originalTransactionId") == transaction_id),
            None,
        )
        if latest is None:
            latest = max(transactions, key=lambda t: t.get("status") == 1)

        info = _decode_jws_payload(latest.get("signedTransactionInfo"))
        renewal = _decode_jws_payload(latest.get("signedRenewalInfo"))

        auto_renew = renewal.get("autoRenewStatus") == 1
        status = _STATUS_MAP.get(latest.get("status"), "expired")
        if status == "active" and not auto_renew:
            # 사용자가 갱신을 껐다 — 만료 시각까지는 계속 권한을 준다
            status = "canceled"

        environment = (info.get("environment") or body.get("environment") or "Production").lower()
        expires_at = _from_ms(info.get("expiresDate"))
        if status == "grace":
            # 유예 기간에는 갱신 예정일이 아니라 유예 종료 시각까지 권한을 준다
            expires_at = _from_ms(renewal.get("gracePeriodExpiresDate")) or expires_at

        return StoreSubscription(
            platform="ios",
            product_id=info.get("productId") or requested_product_id,
            # 갱신·재구독에도 안 바뀌는 값을 키로 쓴다
            purchase_key=info.get("originalTransactionId")
            or latest.get("originalTransactionId")
            or transaction_id,
            status=status,
            is_auto_renewing=auto_renew,
            environment="sandbox" if environment == "sandbox" else "production",
            started_at=_from_ms(info.get("originalPurchaseDate")),
            expires_at=expires_at,
            canceled_at=_from_ms(info.get("revocationDate")),
            latest_order_id=info.get("transactionId"),
            raw={"transaction": info, "renewal": renewal, "status": latest.get("status")},
        )

    def acknowledge(self, platform: str, product_id: str, purchase_token: str) -> None:
        """iOS 는 서버측 확인 절차가 없다 (StoreKit 의 finishTransaction 이 담당)."""
        return None

    # --- 설정 점검 --------------------------------------------------------
    def check_access(self) -> BillingAccessCheck:
        """존재할 수 없는 트랜잭션 ID 로 조회해 키와 권한만 확인한다.

        - 400/404 → 키·서명은 통과했고 트랜잭션 ID 만 거부됨 = **설정 완료**
          (Apple 은 형식이 맞지 않는 transactionId 에 404 가 아니라 400 을 준다.
           400 을 받았다는 것 자체가 JWT 인증을 통과했다는 뜻이다.)
        - 401/403 → Issuer ID / Key ID / .p8 불일치
        """
        out = BillingAccessCheck(platform="ios")
        out.configured = bool(
            self._issuer_id and self._key_id and self._private_key and self._bundle_id
        )
        if not out.configured:
            out.reason = REASON_NOT_CONFIGURED
            return out

        try:
            self._auth_token()
        except BillingUnavailableError as exc:
            out.credentials_ok = False
            out.reason = getattr(exc, "reason", REASON_BAD_CREDENTIALS)
            return out
        out.credentials_ok = True

        try:
            res = self._get(_PROD_BASE, "0")
        except BillingUnavailableError as exc:
            out.reason = getattr(exc, "reason", REASON_UNREACHABLE)
            return out

        out.status = res.status_code
        if res.status_code in (400, 404):
            out.store_access_ok = True
        elif res.status_code in (401, 403):
            out.store_access_ok = False
            out.reason = REASON_PERMISSION
        else:
            out.store_access_ok = False
            out.reason = REASON_UNREACHABLE
        return out
