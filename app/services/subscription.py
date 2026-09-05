"""구독(인앱 결제) 도메인 서비스.

원칙
- **클라이언트가 보낸 상태는 믿지 않는다.** FE 는 구매 토큰만 보내고, 권한 판정은
  서버가 스토어 API 로 조회한 결과로만 한다.
- 스토어 응답은 `subscriptions` 에 스냅샷으로 저장한다. 매 요청마다 스토어를
  때리면 느리고 쿼터도 소모되므로, 캐시가 오래됐거나 만료 시각이 지난 경우에만
  다시 조회한다(subscription_refresh_minutes).
- 하나의 구매(purchase_key)는 한 계정에만 귀속된다. 같은 영수증을 다른 계정이
  등록하려 하면 409 로 막는다 (계정 돌려쓰기 방지).
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing_client import (
    BillingClient,
    BillingUnavailableError,
    BillingVerificationError,
    StoreSubscription,
)
from app.core.config import settings
from app.core.errors import APIError
from app.core.timeutil import from_db, now_utc
from app.models.billing import ENTITLED_STATUSES, Subscription

logger = logging.getLogger("eatlog.billing")

PLATFORMS = ("android", "ios")
# raw 원문은 추적용이라 통째로 보관할 필요가 없다 — 컬럼/로그 비대화 방지
_RAW_MAX_CHARS = 4000


def _normalize_platform(platform: str) -> str:
    value = (platform or "").lower()
    if value not in PLATFORMS:
        raise APIError(
            400,
            "VALIDATION_ERROR",
            "지원하지 않는 결제 플랫폼입니다.",
            details=[{"field": "platform", "reason": "unsupported"}],
        )
    return value


def _check_product_id(product_id: str) -> None:
    allowed = settings.subscription_product_id_list
    if allowed and product_id not in allowed:
        raise APIError(
            400,
            "VALIDATION_ERROR",
            "등록되지 않은 구독 상품입니다.",
            details=[{"field": "product_id", "reason": "unknown_product"}],
        )


def is_entitled(sub: Subscription | None) -> bool:
    """이 구독이 지금 프리미엄 권한을 주는가.

    해지 예약(canceled)·결제 유예(grace)도 만료 시각 전까지는 권한을 유지한다.
    expires_at 이 없으면(평생 이용권 등) 상태만으로 판단한다.
    """
    if sub is None or sub.status not in ENTITLED_STATUSES:
        return False
    if sub.expires_at is None:
        return True
    return from_db(sub.expires_at) > now_utc()


def latest_subscription(db: Session, user_id: int) -> Subscription | None:
    """사용자의 가장 최근 구독 행 (만료 시각이 가장 늦은 것)."""
    rows = db.scalars(
        select(Subscription).where(Subscription.user_id == user_id)
    ).all()
    if not rows:
        return None
    return max(
        rows,
        key=lambda s: (
            is_entitled(s),
            from_db(s.expires_at) if s.expires_at else from_db(s.created_at),
        ),
    )


def is_premium(db: Session, user_id: int) -> bool:
    """권한 판정. 캐시만 본다 — 스토어 재조회는 구독 조회 API 가 담당한다.

    (만료 시각이 지나면 캐시만으로도 자연히 false 가 되므로 안전하다.)
    """
    return is_entitled(latest_subscription(db, user_id))


def _apply(sub: Subscription, store: StoreSubscription) -> None:
    sub.product_id = store.product_id
    sub.status = store.status
    sub.is_auto_renewing = store.is_auto_renewing
    sub.environment = store.environment
    sub.started_at = store.started_at
    sub.expires_at = store.expires_at
    sub.canceled_at = store.canceled_at
    sub.latest_order_id = store.latest_order_id
    sub.raw_payload = json.dumps(store.raw, ensure_ascii=False, default=str)[:_RAW_MAX_CHARS]
    sub.verified_at = now_utc()


def upsert_from_store(db: Session, user_id: int, store: StoreSubscription) -> Subscription:
    """스토어 조회 결과를 저장. 다른 계정에 귀속된 구매면 409."""
    existing = db.scalar(
        select(Subscription).where(
            Subscription.platform == store.platform,
            Subscription.purchase_key == store.purchase_key,
        )
    )
    if existing is not None and existing.user_id != user_id:
        raise APIError(
            409,
            "CONFLICT",
            "이미 다른 계정에 연결된 구독입니다. 구매하신 계정으로 로그인해 주세요.",
            details=[{"field": "purchase_token", "reason": "already_linked"}],
        )

    sub = existing or Subscription(
        user_id=user_id,
        platform=store.platform,
        purchase_key=store.purchase_key,
        product_id=store.product_id,
        status=store.status,
        verified_at=now_utc(),
    )
    _apply(sub, store)
    if existing is None:
        db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _store_error_to_api_error(exc: Exception) -> APIError:
    if isinstance(exc, BillingVerificationError):
        return APIError(
            400,
            "VALIDATION_ERROR",
            "스토어에서 확인되지 않는 구매입니다. 결제 내역을 확인해 주세요.",
            details=[{"field": "purchase_token", "reason": "not_found"}],
        )
    return APIError(
        503,
        "SERVICE_UNAVAILABLE",
        "결제 확인 서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    )


def verify_purchase(
    db: Session,
    client: BillingClient,
    user_id: int,
    platform: str,
    product_id: str,
    purchase_token: str,
) -> Subscription:
    """구매 토큰을 스토어에 검증하고 저장한다 (FE 결제 완료/복원 시 호출)."""
    platform = _normalize_platform(platform)
    _check_product_id(product_id)
    try:
        store = client.verify_subscription(platform, product_id, purchase_token)
    except (BillingVerificationError, BillingUnavailableError) as exc:
        logger.info("구독 검증 실패 user=%s platform=%s - %s", user_id, platform, exc)
        raise _store_error_to_api_error(exc) from exc

    sub = upsert_from_store(db, user_id, store)
    # Android 미확인 구매는 3일 뒤 자동 환불된다 — 검증 성공 직후 확인해 둔다
    if platform == "android":
        client.acknowledge(platform, product_id, purchase_token)
    return sub


def _needs_refresh(sub: Subscription) -> bool:
    if sub.expires_at is not None and from_db(sub.expires_at) <= now_utc():
        return True  # 만료로 보이면 갱신됐는지 반드시 재확인한다
    interval = settings.subscription_refresh_minutes
    if interval <= 0:
        return True
    return from_db(sub.verified_at) + timedelta(minutes=interval) <= now_utc()


def refresh_if_stale(
    db: Session, client: BillingClient, sub: Subscription
) -> Subscription:
    """캐시가 오래됐으면 스토어에 다시 물어본다. 실패해도 캐시를 그대로 돌려준다.

    (스토어 장애로 이미 결제한 사용자의 권한이 사라지면 안 된다.)
    """
    if not _needs_refresh(sub):
        return sub
    try:
        store = client.verify_subscription(sub.platform, sub.product_id, sub.purchase_key)
    except BillingVerificationError:
        # 스토어가 더 이상 모르는 구매 — 만료로 확정한다
        sub.status = "expired"
        sub.is_auto_renewing = False
        sub.verified_at = now_utc()
        db.commit()
        db.refresh(sub)
        return sub
    except BillingUnavailableError as exc:
        logger.warning("구독 상태 갱신 실패(캐시 유지) sub=%s - %s", sub.id, exc)
        return sub
    return upsert_from_store(db, sub.user_id, store)


def sync_by_purchase_key(
    db: Session, client: BillingClient, platform: str, purchase_key: str
) -> Subscription | None:
    """스토어 알림(RTDN / App Store Server Notifications)으로 상태만 되맞춘다.

    우리가 모르는 구매(다른 앱·이미 삭제된 계정)면 아무것도 하지 않는다 —
    알림에는 어느 사용자인지가 없으므로 새 행을 만들 수 없다.
    """
    sub = db.scalar(
        select(Subscription).where(
            Subscription.platform == platform,
            Subscription.purchase_key == purchase_key,
        )
    )
    if sub is None:
        logger.info("알 수 없는 구매 알림 무시 platform=%s", platform)
        return None
    try:
        store = client.verify_subscription(platform, sub.product_id, purchase_key)
    except BillingVerificationError:
        sub.status = "expired"
        sub.is_auto_renewing = False
        sub.verified_at = now_utc()
        db.commit()
        db.refresh(sub)
        return sub
    except BillingUnavailableError as exc:
        logger.warning("구독 알림 처리 중 스토어 조회 실패 - %s", exc)
        return sub
    return upsert_from_store(db, sub.user_id, store)
