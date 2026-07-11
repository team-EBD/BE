"""Phase 4 DoD — 이미지 업로드."""
from __future__ import annotations

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
