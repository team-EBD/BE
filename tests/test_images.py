"""Phase 4 DoD — 이미지 업로드."""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.meal import MealImage

JPEG = ("food.jpg", b"\xff\xd8\xff\xe0fakejpegdata", "image/jpeg")


def upload(client, headers, files=None, data=None):
    return client.post(
        "/v1/meals/images",
        headers=headers,
        files=files or {"image": JPEG},
        data=data or {"source": "camera"},
    )


def test_upload_success_201(client, auth_headers):
    res = upload(client, auth_headers)
    assert res.status_code == 201
    body = res.json()
    assert body["meal_image_id"] > 0
    assert body["storage_key"].startswith("meals/")
    assert body["image_url"].endswith(body["storage_key"].split("/")[-1])
    assert body["uploaded_at"].endswith("+09:00")


def test_upload_unsupported_format_415(client, auth_headers):
    res = upload(
        client, auth_headers, files={"image": ("doc.gif", b"GIF89a", "image/gif")}
    )
    assert res.status_code == 415
    assert res.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_upload_too_large_413(client, auth_headers):
    big = ("big.jpg", b"x" * (10 * 1024 * 1024 + 1), "image/jpeg")
    res = upload(client, auth_headers, files={"image": big})
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_upload_invalid_source_400(client, auth_headers):
    res = upload(client, auth_headers, data={"source": "screenshot"})
    assert res.status_code == 400


def _stored_taken_at(db_factory, meal_image_id):
    """저장된 taken_at 을 UTC aware 로 정규화해 반환.

    SQLite 는 DateTime(timezone=True) 값을 naive 로 돌려줄 수 있는데,
    저장 시점에 이미 to_utc 를 거쳤으므로 naive 면 UTC 로 간주한다.
    """
    with db_factory() as db:
        stored = db.get(MealImage, meal_image_id).taken_at
    if stored is not None and stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return stored


def test_upload_taken_at_z_format_stored_utc(client, auth_headers, db_factory):
    """FE 가 보내는 ISO 'Z' 포맷(EXIF 촬영 시각)이 UTC 로 저장되는 계약 회귀 테스트."""
    res = upload(
        client,
        auth_headers,
        data={"source": "gallery", "taken_at": "2026-07-20T10:30:00.000Z"},
    )
    assert res.status_code == 201
    stored = _stored_taken_at(db_factory, res.json()["meal_image_id"])
    assert stored == datetime(2026, 7, 20, 10, 30, tzinfo=timezone.utc)


def test_upload_taken_at_kst_offset_same_instant(client, auth_headers, db_factory):
    res = upload(
        client,
        auth_headers,
        data={"source": "gallery", "taken_at": "2026-07-20T19:30:00+09:00"},
    )
    assert res.status_code == 201
    stored = _stored_taken_at(db_factory, res.json()["meal_image_id"])
    assert stored == datetime(2026, 7, 20, 10, 30, tzinfo=timezone.utc)


def test_upload_without_taken_at_stores_null(client, auth_headers, db_factory):
    res = upload(client, auth_headers)
    assert res.status_code == 201
    assert _stored_taken_at(db_factory, res.json()["meal_image_id"]) is None
