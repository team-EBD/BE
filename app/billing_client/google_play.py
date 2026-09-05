"""Google Play 구독 검증 (Android Publisher API v3).

`purchases.subscriptionsv2.get` 로 purchaseToken 을 조회한다.
  GET https://androidpublisher.googleapis.com/androidpublisher/v3/applications/
      {packageName}/purchases/subscriptionsv2/tokens/{purchaseToken}

자격증명은 Google Cloud 서비스 계정(JSON). Play Console 에서 그 계정에
재무 데이터 보기(구독 조회) 권한을 부여해야 한다 — 콘솔 절차는
docs/인앱결제-서버-설정.md 참고.

의존성을 늘리지 않기 위해 OAuth2 액세스 토큰은 서비스 계정 키로 직접 서명한
JWT assertion(RS256)을 토큰 엔드포인트와 교환해 얻는다 (google-auth 의 동기
requests 트랜스포트를 끌어들이지 않고 httpx 로 통일 — ai_client/social_client 와 동일).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import httpx
from jose import jwt

from app.billing_client.base import (
    BillingUnavailableError,
    BillingVerificationError,
    StoreSubscription,
)

logger = logging.getLogger("eatlog.billing")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"
_TIMEOUT = 10.0
# 액세스 토큰 유효기간(3600s)보다 넉넉히 일찍 재발급한다
_TOKEN_SKEW_SECONDS = 300

# subscriptionState -> 서비스 공통 상태
_STATE_MAP = {
    "SUBSCRIPTION_STATE_ACTIVE": "active",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "grace",
    "SUBSCRIPTION_STATE_CANCELED": "canceled",
    "SUBSCRIPTION_STATE_ON_HOLD": "on_hold",
    "SUBSCRIPTION_STATE_PAUSED": "paused",
    "SUBSCRIPTION_STATE_EXPIRED": "expired",
    "SUBSCRIPTION_STATE_PENDING": "pending",
    "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED": "expired",
}


def _parse_rfc3339(value: str | None) -> datetime | None:
    """Google 은 "2026-09-05T12:00:00.123Z" (RFC3339) 형식으로 준다."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cancel_time(body: dict) -> datetime | None:
    context = body.get("canceledStateContext") or {}
    user_cancel = context.get("userInitiatedCancellation") or {}
    return _parse_rfc3339(user_cancel.get("cancelTime"))


class GooglePlayBillingClient:
    def __init__(
        self,
        package_name: str,
        credentials_json: str = "",
        credentials_file: str = "",
    ) -> None:
        self._package_name = package_name
        self._credentials_json = credentials_json
        self._credentials_file = credentials_file
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # --- 자격증명 / 액세스 토큰 -------------------------------------------
    def _service_account(self) -> dict:
        if self._credentials_json:
            raw = self._credentials_json
        elif self._credentials_file:
            try:
                with open(self._credentials_file, encoding="utf-8") as fp:
                    raw = fp.read()
            except OSError as exc:
                raise BillingUnavailableError(
                    f"Play 서비스 계정 파일을 읽을 수 없습니다: {exc}"
                ) from exc
        else:
            raise BillingUnavailableError("Play 서비스 계정 자격증명이 설정되지 않았습니다.")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise BillingUnavailableError(
                "Play 서비스 계정 JSON 형식이 올바르지 않습니다."
            ) from exc
        if not data.get("client_email") or not data.get("private_key"):
            raise BillingUnavailableError(
                "Play 서비스 계정 JSON 에 client_email/private_key 가 없습니다."
            )
        return data

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            account = self._service_account()
            issued = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": account["client_email"],
                    "scope": _SCOPE,
                    "aud": _TOKEN_URL,
                    "iat": issued,
                    "exp": issued + 3600,
                },
                account["private_key"],
                algorithm="RS256",
            )
            try:
                res = httpx.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                    timeout=_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise BillingUnavailableError(f"Google 토큰 발급 통신 실패: {exc}") from exc
            if res.status_code >= 400:
                logger.warning("Google 토큰 발급 실패 %d - %s", res.status_code, res.text[:200])
                raise BillingUnavailableError("Google 액세스 토큰 발급에 실패했습니다.")
            body = res.json()
            self._token = body["access_token"]
            self._token_expires_at = (
                time.time() + int(body.get("expires_in", 3600)) - _TOKEN_SKEW_SECONDS
            )
            return self._token

    # --- 구독 조회 ---------------------------------------------------------
    def verify_subscription(
        self, platform: str, product_id: str, purchase_token: str
    ) -> StoreSubscription:
        if not self._package_name:
            raise BillingUnavailableError("GOOGLE_PLAY_PACKAGE_NAME 이 설정되지 않았습니다.")
        url = (
            f"{_API_BASE}/applications/{self._package_name}"
            f"/purchases/subscriptionsv2/tokens/{purchase_token}"
        )
        try:
            res = httpx.get(
                url,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise BillingUnavailableError(f"Play 구독 조회 통신 실패: {exc}") from exc

        if res.status_code in (400, 404, 410):
            # Play 는 위조/소멸된 토큰을 400/404 로 답한다 — 무효한 구매
            logger.info("Play 구독 조회 무효 %d - %s", res.status_code, res.text[:200])
            raise BillingVerificationError("Google Play 에서 확인되지 않는 구매입니다.")
        if res.status_code >= 400:
            logger.warning("Play 구독 조회 실패 %d - %s", res.status_code, res.text[:200])
            raise BillingUnavailableError("Google Play 구독 조회에 실패했습니다.")

        return self._to_subscription(res.json(), product_id, purchase_token)

    def _to_subscription(
        self, body: dict, requested_product_id: str, purchase_token: str
    ) -> StoreSubscription:
        line_items = body.get("lineItems") or []
        # 만료가 가장 늦은 항목이 현재 유효한 플랜 (업그레이드 직후엔 두 개가 보인다)
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        line = max(
            line_items,
            key=lambda item: _parse_rfc3339(item.get("expiryTime")) or epoch,
            default={},
        )
        auto_renew = bool((line.get("autoRenewingPlan") or {}).get("autoRenewEnabled"))
        return StoreSubscription(
            platform="android",
            product_id=line.get("productId") or requested_product_id,
            purchase_key=purchase_token,
            status=_STATE_MAP.get(body.get("subscriptionState", ""), "expired"),
            is_auto_renewing=auto_renew,
            environment="sandbox" if "testPurchase" in body else "production",
            started_at=_parse_rfc3339(body.get("startTime")),
            expires_at=_parse_rfc3339(line.get("expiryTime")),
            canceled_at=_cancel_time(body),
            latest_order_id=body.get("latestOrderId"),
            raw=body,
        )

    # --- 구매 확인 ---------------------------------------------------------
    def acknowledge(self, platform: str, product_id: str, purchase_token: str) -> None:
        """미확인 구매는 3일 뒤 Google 이 자동 환불한다 — 검증 직후 서버가 확인한다.

        클라이언트(finishTransaction)도 확인을 시도하므로 이미 확인된 구매에 대한
        오류 응답은 정상 흐름이다. 어떤 실패도 구매를 무효로 만들지 않으므로 삼킨다.
        """
        if not self._package_name or not product_id:
            return
        url = (
            f"{_API_BASE}/applications/{self._package_name}"
            f"/purchases/subscriptions/{product_id}/tokens/{purchase_token}:acknowledge"
        )
        try:
            res = httpx.post(
                url,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                json={},
                timeout=_TIMEOUT,
            )
            if res.status_code >= 400:
                logger.info("Play 구매 확인 응답 %d - %s", res.status_code, res.text[:200])
        except (httpx.HTTPError, BillingUnavailableError) as exc:
            logger.info("Play 구매 확인 생략 - %s", exc)
