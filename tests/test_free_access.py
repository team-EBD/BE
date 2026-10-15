"""전면 무료 기간 (FREE_ACCESS_UNTIL) — 기간 안에는 무료 사용권·일일 한도 대신 남용 방지 상한만 본다."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.config import Settings, settings
from app.core.timeutil import KST, kst_date_of, now_utc
from app.services import free_access
from tests.test_ai_premium_gate import _post, seed_ai_logs  # noqa: F401 (fixture 재사용)

ANALYZE = "/v1/meals/analyze"


def _today_plus(days: int) -> str:
    return (kst_date_of(now_utc()) + timedelta(days=days)).isoformat()


@pytest.fixture
def free_period(monkeypatch):
    monkeypatch.setattr(settings, "free_access_until", _today_plus(7))


@pytest.fixture
def gate_on_exhausted(monkeypatch, seed_ai_logs):
    """게이트가 켜져 있고 무료 사용권을 이미 다 쓴 사용자."""
    monkeypatch.setattr(settings, "ai_premium_gate", True)
    monkeypatch.setattr(settings, "free_credit_limit", 3)
    seed_ai_logs("analyze", 3, days_ago=2)


def test_code_default_runs_through_december():
    assert Settings.model_fields["free_access_until"].default == "2026-12-31"
    assert Settings.model_fields["free_access_daily_limit"].default == 50


def test_last_day_is_inclusive_in_kst(monkeypatch):
    monkeypatch.setattr(settings, "free_access_until", "2026-11-30")
    assert free_access.is_free_access_active(datetime(2026, 11, 30, 23, 59, tzinfo=KST))
    assert not free_access.is_free_access_active(datetime(2026, 12, 1, 0, 0, tzinfo=KST))


@pytest.mark.parametrize("raw", ["", "   ", "11월 30일", "2026-13-40"])
def test_blank_or_malformed_date_means_no_free_period(monkeypatch, raw):
    monkeypatch.setattr(settings, "free_access_until", raw)
    assert free_access.free_access_until() is None
    assert not free_access.is_free_access_active()


def test_exhausted_free_user_is_blocked_without_free_period(
    client, auth_headers, gate_on_exhausted
):
    res = _post(client, auth_headers, ANALYZE)
    assert res.status_code == 429
    assert res.json()["error"]["details"][0]["reason"] == "free_credit_exhausted"


def test_free_period_lifts_free_credit_limit(
    client, auth_headers, gate_on_exhausted, free_period
):
    assert _post(client, auth_headers, ANALYZE).status_code == 200


def test_free_period_lifts_daily_limits_when_gate_is_off(
    client, auth_headers, seed_ai_logs, monkeypatch, free_period
):
    monkeypatch.setattr(settings, "ai_premium_gate", False)
    monkeypatch.setattr(settings, "analyze_daily_limit", 1)
    seed_ai_logs("analyze", 1)
    assert _post(client, auth_headers, ANALYZE).status_code == 200


def test_expired_free_period_restores_the_limit(
    client, auth_headers, gate_on_exhausted, monkeypatch
):
    monkeypatch.setattr(settings, "free_access_until", _today_plus(-1))
    assert _post(client, auth_headers, ANALYZE).status_code == 429


def test_abuse_cap_blocks_without_upsell(
    client, auth_headers, seed_ai_logs, monkeypatch, free_period
):
    monkeypatch.setattr(settings, "free_access_daily_limit", 2)
    seed_ai_logs("analyze", 2)
    res = _post(client, auth_headers, ANALYZE)
    assert res.status_code == 429
    error = res.json()["error"]
    detail = error["details"][0]
    assert detail["reason"] == "daily_limit_exceeded"
    assert detail["limit"] == 2
    # 무료 기간에는 구독을 권하지 않는다
    assert detail["upgradable"] is False
    assert "프리미엄" not in error["message"]
    assert "업그레이드" not in error["message"]


def test_abuse_cap_zero_means_unlimited(
    client, auth_headers, seed_ai_logs, monkeypatch, free_period
):
    monkeypatch.setattr(settings, "free_access_daily_limit", 0)
    seed_ai_logs("analyze", 60)
    assert _post(client, auth_headers, ANALYZE).status_code == 200


def test_usage_reports_unlimited_and_the_end_date(
    client, auth_headers, gate_on_exhausted, free_period
):
    body = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert body["free_access_until"] == _today_plus(7)
    assert body["free_credits"] == {"limit": None, "used": 3, "remaining": None}
    assert body["analyze"]["limit"] is None
    assert body["recommend"]["limit"] is None
    # 구독 상태는 사실대로 둔다
    assert body["is_premium"] is False


def test_usage_has_no_end_date_outside_the_period(client, auth_headers, gate_on_exhausted):
    body = client.get("/v1/usage/daily", headers=auth_headers).json()
    assert body["free_access_until"] is None
    assert body["free_credits"]["limit"] == 3
    assert body["free_credits"]["remaining"] == 0
