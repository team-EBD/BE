"""Phase 3·10 DoD — 프로필/식습관/알림/동의/약관/푸시토큰."""
from __future__ import annotations


def test_get_me(client, auth_headers):
    res = client.get("/v1/users/me", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["nickname"] == "user-tester"
    assert body["created_at"].endswith("+09:00")  # 명세서 1.6


def test_patch_me_updates_profile(client, auth_headers):
    res = client.patch(
        "/v1/users/me",
        headers=auth_headers,
        json={"nickname": "현우", "household_type": "single", "daily_goal_calories": 1800},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["nickname"] == "현우"
    assert body["household_type"] == "single"
    assert body["daily_goal_calories"] == 1800


def test_me_body_fields_default_null(client, auth_headers):
    """소셜 가입 직후처럼 프로필이 없으면 신체 정보는 null 이어야 한다."""
    res = client.get("/v1/users/me", headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["gender"] is None
    assert body["height"] is None
    assert body["weight"] is None


def test_patch_me_body_fields(client, auth_headers):
    """FE 프로필 보완 화면이 보내는 신체 정보 저장 → 조회 시 반영."""
    res = client.patch(
        "/v1/users/me",
        headers=auth_headers,
        json={"gender": "female", "height": 165.5, "weight": 55.2},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["gender"] == "female"
    assert body["height"] == 165.5
    assert body["weight"] == 55.2

    res2 = client.get("/v1/users/me", headers=auth_headers)
    assert res2.json()["gender"] == "female"
    assert res2.json()["height"] == 165.5


def test_patch_me_body_fields_validation_400(client, auth_headers):
    res = client.patch("/v1/users/me", headers=auth_headers, json={"height": 20})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_me_invalid_nickname_400(client, auth_headers):
    res = client.patch("/v1/users/me", headers=auth_headers, json={"nickname": "a"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_eating_habits_default_then_update(client, auth_headers):
    res = client.get("/v1/users/eating-habits", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["default_portion"] == "normal"  # 미설정 기본값

    res2 = client.patch(
        "/v1/users/eating-habits",
        headers=auth_headers,
        json={"soup_preference": "leave", "default_portion": "small"},
    )
    assert res2.status_code == 200
    assert res2.json()["soup_preference"] == "leave"
    assert res2.json()["default_portion"] == "small"

    res3 = client.get("/v1/users/eating-habits", headers=auth_headers)
    assert res3.json()["soup_preference"] == "leave"


def test_notification_settings(client, auth_headers):
    res = client.patch(
        "/v1/users/notification-settings",
        headers=auth_headers,
        json={"is_enabled": True, "lunch_time": "12:00", "weekly_report_enabled": False},
    )
    assert res.status_code == 200
    assert res.json()["lunch_time"] == "12:00"
    assert res.json()["weekly_report_enabled"] is False


def test_notification_settings_bad_time_400(client, auth_headers):
    res = client.patch(
        "/v1/users/notification-settings", headers=auth_headers, json={"lunch_time": "25:99"}
    )
    assert res.status_code == 400


def test_location_consent_flow(client, auth_headers):
    res = client.post(
        "/v1/users/location-consent",
        headers=auth_headers,
        json={"consent_status": True, "consent_version": "1.0"},
    )
    assert res.status_code == 201
    assert res.json()["consent_status"] is True
    assert res.json()["revoked_at"] is None

    # 철회
    res2 = client.patch(
        "/v1/users/location-consent", headers=auth_headers, json={"consent_status": False}
    )
    assert res2.status_code == 200
    assert res2.json()["consent_status"] is False
    assert res2.json()["revoked_at"] is not None


def test_location_consent_patch_without_record_404(client, auth_headers):
    res = client.patch(
        "/v1/users/location-consent", headers=auth_headers, json={"consent_status": True}
    )
    assert res.status_code == 404


def test_terms_agreement(client, auth_headers):
    res = client.post(
        "/v1/users/terms-agreements",
        headers=auth_headers,
        json={"terms_type": "privacy", "version": "1.2"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["terms_type"] == "privacy"
    assert body["agreed_at"].endswith("+09:00")


def test_push_token_upsert(client, auth_headers):
    payload = {"device_id": "dev-1", "push_token": "tok-1", "platform": "android"}
    res = client.post("/v1/push-tokens", headers=auth_headers, json=payload)
    assert res.status_code == 201
    first_id = res.json()["push_token_id"]

    # 같은 디바이스 재등록 → 새 행 없이 갱신
    payload["push_token"] = "tok-2"
    res2 = client.post("/v1/push-tokens", headers=auth_headers, json=payload)
    assert res2.status_code == 201
    assert res2.json()["push_token_id"] == first_id
