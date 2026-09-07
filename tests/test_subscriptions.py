"""인앱 결제(구독) — 검증·조회·권한 연동.

목 스토어 클라이언트(app/billing_client/mock.py)의 토큰 접두사 규약으로
정상/만료/해지/무효/스토어장애 경로를 모두 눌러 본다.
"""
from __future__ import annotations

import base64
import json

import pytest

from app.api.v1.subscriptions import get_billing_client_factory
from app.billing_client.mock import MockBillingClient
from app.core.config import settings
from app.main import app
from tests.conftest import login
from tests.test_images import upload

PRODUCT = "eatlog_premium_monthly"


@pytest.fixture(autouse=True)
def mock_billing():
    """스토어 호출을 목으로 고정 (billing_backend 설정과 무관하게)."""
    client = MockBillingClient()
    app.dependency_overrides[get_billing_client_factory] = lambda: (lambda platform: client)
    yield client
    app.dependency_overrides.pop(get_billing_client_factory, None)


@pytest.fixture()
def auth_headers2(client) -> dict[str, str]:
    """두 번째 사용자 — 같은 영수증을 다른 계정이 등록하는 시나리오용."""
    tokens = login(client, social_id="tester2")
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def verify(client, headers, token="tok-1", platform="android", product_id=PRODUCT):
    return client.post(
        "/v1/subscriptions/verify",
        headers=headers,
        json={"platform": platform, "product_id": product_id, "purchase_token": token},
    )


# --- 검증 -------------------------------------------------------------------

def test_verify_creates_active_subscription(client, auth_headers):
    res = verify(client, auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["is_premium"] is True
    assert body["status"] == "active"
    assert body["platform"] == "android"
    assert body["product_id"] == PRODUCT
    assert body["is_auto_renewing"] is True
    assert body["expires_at"].endswith("+09:00")  # 명세서 1.6 — KST 직렬화


def test_verify_is_idempotent(client, auth_headers):
    verify(client, auth_headers)
    res = verify(client, auth_headers)
    assert res.status_code == 200
    assert res.json()["is_premium"] is True


def test_expired_token_is_not_premium(client, auth_headers):
    res = verify(client, auth_headers, token="expired-1")
    assert res.status_code == 200
    assert res.json()["is_premium"] is False
    assert res.json()["status"] == "expired"


def test_canceled_keeps_entitlement_until_expiry(client, auth_headers):
    """해지 예약(갱신 꺼짐)이어도 만료 전까지는 프리미엄이다."""
    res = verify(client, auth_headers, token="canceled-1")
    assert res.json()["is_premium"] is True
    assert res.json()["is_auto_renewing"] is False


def test_invalid_token_rejected(client, auth_headers):
    res = verify(client, auth_headers, token="invalid-1")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_store_outage_returns_503(client, auth_headers):
    """스토어 장애는 '무효한 구매'가 아니다 — 재시도 가능한 503 으로 구분한다."""
    res = verify(client, auth_headers, token="down-1")
    assert res.status_code == 503


def test_unknown_product_rejected(client, auth_headers):
    res = verify(client, auth_headers, product_id="not_a_product")
    assert res.status_code == 400
    assert res.json()["error"]["details"][0]["reason"] == "unknown_product"


def test_same_purchase_cannot_be_linked_to_two_accounts(client, auth_headers, auth_headers2):
    assert verify(client, auth_headers, token="shared-token").status_code == 200
    res = verify(client, auth_headers2, token="shared-token")
    assert res.status_code == 409
    assert res.json()["error"]["details"][0]["reason"] == "already_linked"


def test_verify_requires_auth(client):
    res = client.post(
        "/v1/subscriptions/verify",
        json={"platform": "android", "product_id": PRODUCT, "purchase_token": "t"},
    )
    assert res.status_code == 401


# --- 조회 -------------------------------------------------------------------

def test_me_without_subscription(client, auth_headers):
    res = client.get("/v1/subscriptions/me", headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == {
        "is_premium": False,
        "status": None,
        "platform": None,
        "product_id": None,
        "is_auto_renewing": False,
        "started_at": None,
        "expires_at": None,
        "environment": None,
    }


def test_me_reflects_verified_subscription(client, auth_headers):
    verify(client, auth_headers)
    body = client.get("/v1/subscriptions/me", headers=auth_headers).json()
    assert body["is_premium"] is True
    assert body["product_id"] == PRODUCT


def test_me_refreshes_from_store_when_stale(client, auth_headers, monkeypatch, mock_billing):
    """캐시가 오래되면 스토어에 다시 물어본다 (해지·환불을 앱이 늦게 아는 것 방지)."""
    verify(client, auth_headers)
    monkeypatch.setattr(settings, "subscription_refresh_minutes", 0)  # 매번 재조회

    calls: list[str] = []
    original = mock_billing.verify_subscription

    def spy(platform, product_id, purchase_token):
        calls.append(purchase_token)
        return original(platform, product_id, purchase_token)

    monkeypatch.setattr(mock_billing, "verify_subscription", spy)
    client.get("/v1/subscriptions/me", headers=auth_headers)
    assert calls == ["tok-1"]


def test_me_keeps_cache_when_store_is_down(client, auth_headers, monkeypatch, mock_billing):
    from app.billing_client.base import BillingUnavailableError

    verify(client, auth_headers)
    monkeypatch.setattr(settings, "subscription_refresh_minutes", 0)

    def boom(*_args, **_kwargs):
        raise BillingUnavailableError("down")

    monkeypatch.setattr(mock_billing, "verify_subscription", boom)
    body = client.get("/v1/subscriptions/me", headers=auth_headers).json()
    assert body["is_premium"] is True  # 스토어 장애로 권한이 사라지면 안 된다


def test_me_requires_auth(client):
    assert client.get("/v1/subscriptions/me").status_code == 401


# --- 사용량 한도 연동 --------------------------------------------------------

def test_premium_lifts_daily_limit(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "recommend_daily_limit", 1)
    monkeypatch.setattr(settings, "recommend_daily_limit_premium", 0)  # 무제한

    body = {"date": "2026-06-27"}
    assert client.post("/v1/recommendations/next-meal", headers=auth_headers, json=body).status_code == 200
    assert client.post("/v1/recommendations/next-meal", headers=auth_headers, json=body).status_code == 429

    verify(client, auth_headers)
    assert client.post("/v1/recommendations/next-meal", headers=auth_headers, json=body).status_code == 200


def test_free_limit_error_suggests_upgrade(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "recommend_daily_limit", 1)
    body = {"date": "2026-06-27"}
    client.post("/v1/recommendations/next-meal", headers=auth_headers, json=body)
    res = client.post("/v1/recommendations/next-meal", headers=auth_headers, json=body)
    assert res.status_code == 429
    assert res.json()["error"]["details"][0]["upgradable"] is True


def test_daily_usage_marks_premium_unlimited(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "analyze_daily_limit", 3)
    monkeypatch.setattr(settings, "analyze_daily_limit_premium", 0)

    before = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert before["is_premium"] is False
    assert before["analyze"]["limit"] == 3

    verify(client, auth_headers)
    after = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert after["is_premium"] is True
    assert after["analyze"]["limit"] is None


def test_expired_subscription_does_not_lift_limit(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "recommend_daily_limit", 1)
    verify(client, auth_headers, token="expired-1")
    body = {"date": "2026-06-27"}
    client.post("/v1/recommendations/next-meal", headers=auth_headers, json=body)
    res = client.post("/v1/recommendations/next-meal", headers=auth_headers, json=body)
    assert res.status_code == 429


# --- 회원 탈퇴 ---------------------------------------------------------------

def test_account_deletion_removes_subscription(client, auth_headers, db_factory):
    from app.models.billing import Subscription

    verify(client, auth_headers)
    assert client.delete("/v1/users/me", headers=auth_headers).status_code == 204
    with db_factory() as db:
        assert db.query(Subscription).count() == 0


# --- 스토어 알림 -------------------------------------------------------------

def _rtdn_body(purchase_token: str) -> dict:
    payload = {
        "version": "1.0",
        "packageName": "com.eatlog",
        "subscriptionNotification": {
            "notificationType": 13,  # SUBSCRIPTION_EXPIRED
            "purchaseToken": purchase_token,
            "subscriptionId": PRODUCT,
        },
    }
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "projects/x/subscriptions/y"}


def test_google_rtdn_syncs_status(client, auth_headers, monkeypatch, mock_billing):
    verify(client, auth_headers, token="tok-rtdn")

    def now_expired(platform, product_id, purchase_token):
        # 스토어가 "이제 만료됨"이라고 답하는 상황 (키는 그대로)
        store = MockBillingClient().verify_subscription(platform, product_id, "expired-x")
        store.purchase_key = purchase_token
        return store

    monkeypatch.setattr(mock_billing, "verify_subscription", now_expired)
    res = client.post("/v1/subscriptions/notifications/google", json=_rtdn_body("tok-rtdn"))
    assert res.status_code == 200
    assert client.get("/v1/subscriptions/me", headers=auth_headers).json()["is_premium"] is False


def test_google_rtdn_ignores_unknown_token(client):
    res = client.post("/v1/subscriptions/notifications/google", json=_rtdn_body("never-seen"))
    assert res.status_code == 200


def test_google_rtdn_rejects_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "google_play_rtdn_secret", "s3cret")
    res = client.post("/v1/subscriptions/notifications/google", json=_rtdn_body("tok"))
    assert res.json()["status"] == "ignored"


def _jws(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def test_apple_notification_syncs_status(client, auth_headers, monkeypatch, mock_billing):
    verify(client, auth_headers, token="orig-tx-1", platform="ios")

    def now_expired(platform, product_id, purchase_token):
        # 스토어가 "이제 만료됨"이라고 답하는 상황 (키는 그대로)
        store = MockBillingClient().verify_subscription(platform, product_id, "expired-x")
        store.purchase_key = purchase_token
        return store

    monkeypatch.setattr(mock_billing, "verify_subscription", now_expired)
    signed = _jws(
        {
            "notificationType": "DID_CHANGE_RENEWAL_STATUS",
            "data": {"signedTransactionInfo": _jws({"originalTransactionId": "orig-tx-1"})},
        }
    )
    res = client.post("/v1/subscriptions/notifications/apple", json={"signedPayload": signed})
    assert res.status_code == 200
    assert client.get("/v1/subscriptions/me", headers=auth_headers).json()["is_premium"] is False


def test_apple_notification_ignores_malformed_body(client):
    res = client.post("/v1/subscriptions/notifications/apple", json={"signedPayload": "not-a-jws"})
    assert res.status_code == 200
    assert res.json()["status"] == "ignored"


# --- 사진 분석 경로도 프리미엄이면 무제한 -------------------------------------

def test_premium_lifts_analyze_limit(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "analyze_daily_limit", 1)
    monkeypatch.setattr(settings, "analyze_daily_limit_premium", 0)

    def analyze():
        image_id = upload(client, auth_headers).json()["meal_image_id"]
        return client.post(
            "/v1/meals/analyze", headers=auth_headers, json={"meal_image_id": image_id}
        )

    assert analyze().status_code == 200
    assert analyze().status_code == 429
    verify(client, auth_headers)
    assert analyze().status_code == 200


# --- 설정 점검 (GET /subscriptions/health) ------------------------------------

def test_health_requires_auth(client):
    assert client.get("/v1/subscriptions/health").status_code == 401


def test_health_reports_mock_backend(client, auth_headers):
    body = client.get("/v1/subscriptions/health", headers=auth_headers).json()
    assert body["android"]["reason"] == "mock_backend"
    assert body["android"]["store_access_ok"] is True


def test_health_reports_missing_config(client, auth_headers):
    """자격증명이 비어 있으면 not_configured 로 구분된다 (권한 문제와 헷갈리지 않게)."""
    from app.api.v1.subscriptions import get_billing_client_factory
    from app.billing_client.app_store import AppStoreBillingClient
    from app.billing_client.google_play import GooglePlayBillingClient

    clients = {
        "android": GooglePlayBillingClient(package_name="", credentials_json=""),
        "ios": AppStoreBillingClient(issuer_id="", key_id="", private_key="", bundle_id=""),
    }
    app.dependency_overrides[get_billing_client_factory] = lambda: (lambda p: clients[p])
    try:
        body = client.get("/v1/subscriptions/health", headers=auth_headers).json()
    finally:
        app.dependency_overrides.pop(get_billing_client_factory, None)

    for platform in ("android", "ios"):
        assert body[platform]["configured"] is False
        assert body[platform]["reason"] == "not_configured"


def test_store_outage_response_carries_reason(client, auth_headers):
    """503 응답만 보고도 원인을 가릴 수 있어야 한다 (서버 로그 없이 진단)."""
    res = verify(client, auth_headers, token="down-1")
    assert res.status_code == 503
    assert res.json()["error"]["details"][0]["reason"] == "store_unavailable"


def test_google_permission_denied_is_distinguishable(monkeypatch):
    """Play 가 401/403 이면 store_permission — 권한 반영 대기와 설정 누락을 구분한다."""
    import httpx

    from app.billing_client.base import BillingUnavailableError
    from app.billing_client.google_play import GooglePlayBillingClient

    gp = GooglePlayBillingClient(package_name="com.eatlog", credentials_json="{}")
    monkeypatch.setattr(gp, "_access_token", lambda: "fake-token")
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(403, text="insufficient permissions")
    )

    with pytest.raises(BillingUnavailableError) as exc:
        gp.verify_subscription("android", PRODUCT, "tok")
    assert exc.value.reason == "store_permission"

    check = gp.check_access()
    assert check.credentials_ok is True
    assert check.store_access_ok is False
    assert check.reason == "store_permission"
    assert check.status == 403


def test_google_probe_treats_unknown_token_as_healthy(monkeypatch):
    """존재하지 않는 토큰에 400/404 가 오면 권한은 정상이라는 뜻이다."""
    import httpx

    from app.billing_client.google_play import GooglePlayBillingClient

    gp = GooglePlayBillingClient(package_name="com.eatlog", credentials_json="{}")
    monkeypatch.setattr(gp, "_access_token", lambda: "fake-token")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404, text="not found"))

    check = gp.check_access()
    assert check.store_access_ok is True
    assert check.reason is None
