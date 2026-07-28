"""클라이언트 계측 이벤트 수집 (POST /client-events) + total_ms 기록 검증."""
from __future__ import annotations

from sqlalchemy import select

from app.models import AiCallLog, ClientEvent


def test_create_event_stores_row(client, auth_headers, db_factory):
    res = client.post(
        "/v1/client-events",
        json={
            "event_type": "analyze_flow",
            "duration_ms": 9200,
            "meta": {"upload_ms": 3100, "analyze_ms": 5800, "status": "success"},
        },
        headers=auth_headers,
    )
    assert res.status_code == 201, res.text
    event_id = res.json()["client_event_id"]

    with db_factory() as db:
        event = db.get(ClientEvent, event_id)
        assert event.event_type == "analyze_flow"
        assert event.duration_ms == 9200
        assert event.meta["upload_ms"] == 3100
        assert event.user_id is not None


def test_create_event_requires_auth(client):
    res = client.post("/v1/client-events", json={"event_type": "analyze_flow"})
    assert res.status_code == 401


def test_meta_keys_capped_at_limit(client, auth_headers, db_factory):
    res = client.post(
        "/v1/client-events",
        json={"event_type": "spam", "meta": {f"k{i}": i for i in range(50)}},
        headers=auth_headers,
    )
    assert res.status_code == 201
    with db_factory() as db:
        event = db.get(ClientEvent, res.json()["client_event_id"])
        assert len(event.meta) == 20


def test_invalid_duration_rejected(client, auth_headers):
    res = client.post(
        "/v1/client-events",
        json={"event_type": "analyze_flow", "duration_ms": -1},
        headers=auth_headers,
    )
    assert res.status_code == 400 or res.status_code == 422


def test_analyze_records_total_ms(client, auth_headers, db_factory):
    """분석 호출 시 ai_call_logs.total_ms(BE 전체 시간)가 함께 기록된다."""
    upload = client.post(
        "/v1/meals/images",
        files={"image": ("meal.jpg", b"fake-image-bytes", "image/jpeg")},
        data={"source": "camera"},
        headers=auth_headers,
    )
    assert upload.status_code == 201, upload.text
    meal_image_id = upload.json()["meal_image_id"]

    res = client.post(
        "/v1/meals/analyze",
        json={"meal_image_id": meal_image_id},
        headers=auth_headers,
    )
    assert res.status_code == 200, res.text

    with db_factory() as db:
        log = db.scalar(
            select(AiCallLog).where(AiCallLog.meal_image_id == meal_image_id)
        )
        assert log is not None
        assert log.total_ms is not None
        assert log.total_ms >= 0
