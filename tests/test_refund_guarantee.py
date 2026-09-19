"""월 구독 첫 결제 후 30일간 90회 기록 환불 보장 계약."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.timeutil import KST
from app.models import MealRecord, RefundGuaranteeClaim, Subscription, User
from app.services import refund_guarantee


START = datetime(2026, 9, 1, 6, tzinfo=KST)
NOW = START + timedelta(days=30) - timedelta(seconds=1)
URL = "/v1/subscriptions/refund-guarantee"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(refund_guarantee, "now_utc", lambda: NOW.astimezone(timezone.utc))


def _user_id(db):
    return db.scalar(select(User.id).where(User.social_id == "tester"))


def _subscription(db_factory, *, product="eatlog_premium_monthly", start=START, key="monthly"):
    with db_factory() as db:
        sub = Subscription(
            user_id=_user_id(db),
            platform="android",
            product_id=product,
            purchase_key=key,
            status="active",
            is_auto_renewing=True,
            started_at=start.astimezone(timezone.utc),
            expires_at=(start + timedelta(days=60)).astimezone(timezone.utc),
            verified_at=NOW.astimezone(timezone.utc),
        )
        db.add(sub)
        db.commit()
        return sub.id


def _meals(db_factory, times, *, skipped=False, deleted=False):
    with db_factory() as db:
        user_id = _user_id(db)
        db.add_all(
            MealRecord(
                user_id=user_id,
                meal_type="lunch",
                eaten_at=when.astimezone(timezone.utc),
                is_skipped=skipped,
                deleted_at=NOW.astimezone(timezone.utc) if deleted else None,
            )
            for when in times
        )
        db.commit()


def _thirty_days():
    return [START + timedelta(days=day, hours=6, minutes=minute) for day in range(30) for minute in range(3)]


def _reason(response):
    return response.json()["error"]["details"][0]["reason"]


def test_no_subscription_is_explicitly_ineligible(client, auth_headers):
    res = client.get(URL, headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == {
        "eligible_program": False,
        "recorded": None,
        "target": None,
        "daily_cap": None,
        "period_start": None,
        "period_end": None,
        "days_left": None,
        "achieved": None,
        "claim_status": None,
    }
    assert _reason(client.post(f"{URL}/claim", headers=auth_headers)) == "not_eligible_program"


def test_yearly_subscription_is_not_eligible(client, auth_headers, db_factory):
    _subscription(db_factory, product="eatlog_premium_yearly", key="yearly")
    assert client.get(URL, headers=auth_headers).json()["eligible_program"] is False


def test_ninety_records_in_thirty_days_are_achieved(client, auth_headers, db_factory):
    _subscription(db_factory)
    _meals(db_factory, _thirty_days())
    body = client.get(URL, headers=auth_headers).json()
    assert body["eligible_program"] is True
    assert body["recorded"] == body["target"] == 90
    assert body["daily_cap"] == 3
    assert body["achieved"] is True
    assert body["days_left"] == 1
    assert body["period_start"] == START.isoformat()
    assert body["period_end"] == (START + timedelta(days=30)).isoformat()


def test_first_monthly_payment_sets_one_time_period(client, auth_headers, db_factory):
    _subscription(db_factory, start=START, key="first-monthly")
    _subscription(db_factory, start=START + timedelta(days=15), key="renewed-monthly")
    _subscription(db_factory, product="eatlog_premium_yearly", start=START + timedelta(days=20), key="yearly")
    body = client.get(URL, headers=auth_headers).json()
    assert body["period_start"] == START.isoformat()
    assert body["period_end"] == (START + timedelta(days=30)).isoformat()


def test_period_bounds_exclude_earlier_and_future_meals(client, auth_headers, db_factory):
    _subscription(db_factory)
    _meals(db_factory, [START - timedelta(seconds=1), START, NOW + timedelta(seconds=1)])
    assert client.get(URL, headers=auth_headers).json()["recorded"] == 1


def test_ninety_records_on_one_logical_day_count_as_three(client, auth_headers, db_factory):
    _subscription(db_factory)
    _meals(db_factory, [START + timedelta(hours=6, minutes=minute) for minute in range(90)])
    body = client.get(URL, headers=auth_headers).json()
    assert body["recorded"] == 3
    assert body["achieved"] is False
    assert _reason(client.post(f"{URL}/claim", headers=auth_headers)) == "not_enough_records"


@pytest.mark.parametrize("excluded", ["skipped", "deleted"])
def test_skipped_or_deleted_records_are_excluded(client, auth_headers, db_factory, excluded):
    _subscription(db_factory)
    _meals(db_factory, _thirty_days()[:-1])
    _meals(db_factory, _thirty_days()[-1:], **{excluded: True})
    body = client.get(URL, headers=auth_headers).json()
    assert body["recorded"] == 89
    assert body["achieved"] is False


def test_three_am_belongs_to_previous_logical_day(client, auth_headers, db_factory):
    _subscription(db_factory)
    _meals(
        db_factory,
        [
            START + timedelta(hours=6),
            START + timedelta(hours=7),
            START + timedelta(days=1, hours=-3),  # 다음날 03:00 KST
            START + timedelta(days=1, hours=-1),  # 다음날 05:00 KST
            START + timedelta(days=1),  # 다음날 06:00 KST
        ],
    )
    assert client.get(URL, headers=auth_headers).json()["recorded"] == 4


def test_expired_period_cannot_claim(client, auth_headers, db_factory, monkeypatch):
    _subscription(db_factory)
    _meals(db_factory, _thirty_days())
    later = START + timedelta(days=30, seconds=1)
    monkeypatch.setattr(refund_guarantee, "now_utc", lambda: later.astimezone(timezone.utc))
    body = client.get(URL, headers=auth_headers).json()
    assert body["days_left"] == 0
    assert body["achieved"] is True
    assert _reason(client.post(f"{URL}/claim", headers=auth_headers)) == "period_expired"


def test_claim_is_unique_and_retains_snapshot(client, auth_headers, db_factory):
    sub_id = _subscription(db_factory)
    _meals(db_factory, _thirty_days())
    first = client.post(f"{URL}/claim", headers=auth_headers)
    assert first.status_code == 200
    assert first.json()["claim_status"] == "requested"
    second = client.post(f"{URL}/claim", headers=auth_headers)
    assert second.status_code == 409
    assert _reason(second) == "already_claimed"
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(RefundGuaranteeClaim)) == 1
        claim = db.scalar(select(RefundGuaranteeClaim))
        assert claim.subscription_id == sub_id
        assert claim.recorded_count == 90
        assert claim.status == "requested"
        assert claim.processed_at is None
        claim.status = "approved"
        claim.processed_at = NOW.astimezone(timezone.utc)
        db.commit()
    assert client.get(URL, headers=auth_headers).json()["claim_status"] == "approved"


def test_claim_status_survives_store_refund(client, auth_headers, db_factory):
    sub_id = _subscription(db_factory)
    _meals(db_factory, _thirty_days())
    assert client.post(f"{URL}/claim", headers=auth_headers).status_code == 200
    with db_factory() as db:
        db.get(Subscription, sub_id).status = "revoked"
        db.commit()
    body = client.get(URL, headers=auth_headers).json()
    assert body["eligible_program"] is True
    assert body["claim_status"] == "requested"


def test_refund_guarantee_requires_auth(client):
    assert client.get(URL).status_code == 401
    assert client.post(f"{URL}/claim").status_code == 401
