"""구독(인앱 결제) API.

- GET  /v1/subscriptions/me                    현재 구독 상태 (필요 시 스토어 재조회)
- POST /v1/subscriptions/verify                구매 토큰 검증·등록 (결제 완료/복원)
- POST /v1/subscriptions/notifications/google  Play 실시간 개발자 알림(RTDN)
- POST /v1/subscriptions/notifications/apple   App Store Server Notifications V2

알림 엔드포인트는 스토어(Pub/Sub·Apple)가 호출하므로 사용자 인증이 없다.
대신 콘솔에 등록한 URL 의 쿼리 시크릿(?token=...)으로 호출자를 확인하고,
처리 실패와 무관하게 200 을 돌려준다 — 4xx/5xx 를 주면 스토어가 재시도를
반복하며 알림 큐가 밀린다. 실패는 로그로만 표면화한다.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, Request

from app.billing_client import BillingClient, get_billing_client
from app.core.config import settings
from app.core.deps import DB, CurrentUser
from app.models.billing import Subscription
from app.schemas.billing import SubscriptionResponse, SubscriptionVerifyRequest
from app.services import subscription as subscription_service

logger = logging.getLogger("eatlog.billing")

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def get_billing_client_factory() -> Callable[[str], BillingClient]:
    """플랫폼 → 스토어 클라이언트. 테스트는 dependency_overrides 로 목을 주입한다."""
    return get_billing_client


BillingFactory = Annotated[Callable[[str], BillingClient], Depends(get_billing_client_factory)]


def _to_response(sub: Subscription | None) -> SubscriptionResponse:
    if sub is None:
        return SubscriptionResponse(is_premium=False)
    return SubscriptionResponse(
        is_premium=subscription_service.is_entitled(sub),
        status=sub.status,
        platform=sub.platform,
        product_id=sub.product_id,
        is_auto_renewing=sub.is_auto_renewing,
        started_at=sub.started_at,
        expires_at=sub.expires_at,
        environment=sub.environment,
    )


@router.get("/me", response_model=SubscriptionResponse)
def get_my_subscription(
    user: CurrentUser, db: DB, billing: BillingFactory
) -> SubscriptionResponse:
    sub = subscription_service.latest_subscription(db, user.id)
    if sub is not None:
        sub = subscription_service.refresh_if_stale(db, billing(sub.platform), sub)
    return _to_response(sub)


@router.post("/verify", response_model=SubscriptionResponse)
def verify_subscription(
    body: SubscriptionVerifyRequest,
    user: CurrentUser,
    db: DB,
    billing: BillingFactory,
) -> SubscriptionResponse:
    """스토어 결제 완료(또는 구매 복원) 직후 FE 가 호출한다.

    성공 응답을 받은 뒤에만 FE 가 finishTransaction 으로 거래를 종료해야 한다 —
    검증 전에 종료하면 검증 실패 시 되살릴 방법이 없다.
    """
    sub = subscription_service.verify_purchase(
        db,
        billing(body.platform),
        user_id=user.id,
        platform=body.platform,
        product_id=body.product_id,
        purchase_token=body.purchase_token,
    )
    return _to_response(sub)


# --- 스토어 알림 -----------------------------------------------------------

def _secret_ok(request: Request, expected: str) -> bool:
    """콘솔에 등록한 URL 의 ?token= 값 대조. 미설정이면 검증 생략(개발 편의)."""
    if not expected:
        return True
    return request.query_params.get("token") == expected


def _decode_b64_json(value: str | None) -> dict:
    if not value:
        return {}
    padded = value + "=" * (-len(value) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError):
        try:
            return json.loads(base64.b64decode(padded))
        except (binascii.Error, ValueError):
            return {}


def _decode_jws_payload(token: str | None) -> dict:
    if not token or token.count(".") != 2:
        return {}
    return _decode_b64_json(token.split(".")[1])


@router.post("/notifications/google")
async def google_rtdn(request: Request, db: DB, billing: BillingFactory) -> dict[str, str]:
    """Play 실시간 개발자 알림 (Pub/Sub 푸시 구독).

    본문은 Pub/Sub 봉투 {"message": {"data": "<base64 DeveloperNotification>"}}.
    알림에는 사용자 정보가 없으므로 purchaseToken 으로 기존 행을 찾아
    스토어에 재조회한 결과로 상태만 되맞춘다.
    """
    if not _secret_ok(request, settings.google_play_rtdn_secret):
        logger.warning("RTDN 시크릿 불일치 — 무시")
        return {"status": "ignored"}

    envelope = await _safe_json(request)
    notification = _decode_b64_json((envelope.get("message") or {}).get("data"))
    sub_notification = notification.get("subscriptionNotification") or {}
    purchase_token = sub_notification.get("purchaseToken")
    if not purchase_token:
        # 테스트 알림(testNotification)·구독 외 알림은 정상적으로 무시한다
        return {"status": "ignored"}

    try:
        subscription_service.sync_by_purchase_key(
            db, billing("android"), "android", purchase_token
        )
    except Exception:  # noqa: BLE001 — 알림 처리 실패가 재시도 폭주로 번지면 안 된다
        logger.exception("RTDN 처리 실패 type=%s", sub_notification.get("notificationType"))
    return {"status": "ok"}


@router.post("/notifications/apple")
async def apple_server_notification(
    request: Request, db: DB, billing: BillingFactory
) -> dict[str, str]:
    """App Store Server Notifications V2.

    본문은 {"signedPayload": "<JWS>"}. 페이로드 안의 signedTransactionInfo 에서
    originalTransactionId 를 꺼내 해당 구독을 재조회한다.
    """
    if not _secret_ok(request, settings.app_store_notification_secret):
        logger.warning("App Store 알림 시크릿 불일치 — 무시")
        return {"status": "ignored"}

    body = await _safe_json(request)
    payload = _decode_jws_payload(body.get("signedPayload"))
    data = payload.get("data") or {}
    transaction = _decode_jws_payload(data.get("signedTransactionInfo"))
    original_transaction_id = transaction.get("originalTransactionId")
    if not original_transaction_id:
        return {"status": "ignored"}

    try:
        subscription_service.sync_by_purchase_key(
            db, billing("ios"), "ios", original_transaction_id
        )
    except Exception:  # noqa: BLE001
        logger.exception("App Store 알림 처리 실패 type=%s", payload.get("notificationType"))
    return {"status": "ok"}


async def _safe_json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
