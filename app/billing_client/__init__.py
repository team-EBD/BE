"""인앱 결제(구독) 검증 클라이언트 추상화.

`billing_backend` 설정으로 구현을 전환한다: `mock`(스토어 없이 로컬/테스트) /
`store`(Google Play + App Store 실연동). 플랫폼별 구현은 라우팅 시점에 고른다.

푸시(push_client)·AI(ai_client)와 같은 팩토리 패턴이며, 테스트는
`app.api.v1.subscriptions.get_billing_client` 를 dependency_overrides 로 바꾼다.
"""
from __future__ import annotations

from app.billing_client.app_store import AppStoreBillingClient
from app.billing_client.base import (
    BillingClient,
    BillingUnavailableError,
    BillingVerificationError,
    StoreSubscription,
)
from app.billing_client.google_play import GooglePlayBillingClient
from app.billing_client.mock import MockBillingClient
from app.core.config import settings

__all__ = [
    "BillingClient",
    "BillingUnavailableError",
    "BillingVerificationError",
    "StoreSubscription",
    "AppStoreBillingClient",
    "GooglePlayBillingClient",
    "MockBillingClient",
    "get_billing_client",
    "reset_billing_client_cache",
]

_clients: dict[str, BillingClient] = {}


def reset_billing_client_cache() -> None:
    """설정을 바꾼 테스트가 이전 싱글턴을 물고 가지 않도록."""
    _clients.clear()


def get_billing_client(platform: str = "android") -> BillingClient:
    """플랫폼별 스토어 클라이언트(싱글턴). billing_backend != 'store' 면 목."""
    key = "mock" if settings.billing_backend != "store" else platform
    client = _clients.get(key)
    if client is not None:
        return client

    if key == "mock":
        client = MockBillingClient()
    elif key == "ios":
        client = AppStoreBillingClient(
            issuer_id=settings.app_store_issuer_id,
            key_id=settings.app_store_key_id,
            private_key=settings.app_store_private_key,
            bundle_id=settings.app_store_bundle_id or settings.apple_bundle_id,
        )
    else:
        client = GooglePlayBillingClient(
            package_name=settings.google_play_package_name,
            credentials_json=settings.google_play_credentials_json,
            credentials_file=settings.google_play_credentials_file,
        )
    _clients[key] = client
    return client
