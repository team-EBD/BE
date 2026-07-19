"""이미지 보존 기간(저번달 1일~) — cutoff 계산·정리·응답 필터."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.timeutil import KST, UTC
from app.models import MealImage
from app.services.image_retention import purge_expired_images, retention_cutoff_utc
from tests.test_images import upload
from tests.test_meals import MEAL_PAYLOAD


def test_retention_cutoff_is_first_day_of_last_month_kst():
    now = datetime(2026, 7, 19, 12, 0, tzinfo=KST)
    assert retention_cutoff_utc(now) == datetime(2026, 6, 1, tzinfo=KST).astimezone(UTC)


def test_retention_cutoff_crosses_year_boundary():
    now = datetime(2026, 1, 15, tzinfo=KST)
    assert retention_cutoff_utc(now) == datetime(2025, 12, 1, tzinfo=KST).astimezone(UTC)


class RecordingStorage:
    """delete 호출을 기록하는 스토리지 대역."""

    def __init__(self, fail_keys: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self.fail_keys = fail_keys or set()

    def save(self, key: str, data: bytes):  # pragma: no cover — 미사용
        raise AssertionError("not used")

    def delete(self, key: str) -> None:
        if key in self.fail_keys:
            raise RuntimeError("storage down")
        self.deleted.append(key)


def _make_image(db, user_id: int, key: str, uploaded_at: datetime) -> MealImage:
    image = MealImage(
        user_id=user_id,
        image_url=f"http://img/{key}",
        storage_key=key,
        source="camera",
        uploaded_at=uploaded_at,
    )
    db.add(image)
    db.commit()
    return image


def _user_id(client, auth_headers) -> int:
    return client.get("/v1/users/me", headers=auth_headers).json()["id"]


def test_purge_deletes_only_expired_images(client, auth_headers, db_factory):
    db = db_factory()
    uid = _user_id(client, auth_headers)
    cutoff = retention_cutoff_utc()

    old = _make_image(db, uid, "meals/old.jpg", cutoff - timedelta(seconds=1))
    kept = _make_image(db, uid, "meals/new.jpg", cutoff + timedelta(seconds=1))

    storage = RecordingStorage()
    assert purge_expired_images(db, storage) == 1
    assert storage.deleted == ["meals/old.jpg"]
    assert db.get(MealImage, old.id) is None
    assert db.get(MealImage, kept.id) is not None
    db.close()


def test_purge_keeps_row_when_storage_delete_fails(client, auth_headers, db_factory):
    """스토리지 삭제 실패 건은 행을 남겨 다음 주기에 재시도한다."""
    db = db_factory()
    uid = _user_id(client, auth_headers)
    cutoff = retention_cutoff_utc()

    _make_image(db, uid, "meals/fail.jpg", cutoff - timedelta(days=1))
    _make_image(db, uid, "meals/ok.jpg", cutoff - timedelta(days=1))

    storage = RecordingStorage(fail_keys={"meals/fail.jpg"})
    assert purge_expired_images(db, storage) == 1
    remaining = [i.storage_key for i in db.query(MealImage).all()]
    assert "meals/fail.jpg" in remaining
    assert "meals/ok.jpg" not in remaining
    db.close()


def test_list_and_detail_hide_expired_image_url(client, auth_headers, db_factory):
    """정리 전이라도 보존 기간 밖 이미지 URL 은 응답에서 숨긴다."""
    # 정상 업로드 후 uploaded_at 을 보존 기간 밖으로 되돌린다
    image_id = upload(client, auth_headers).json()["meal_image_id"]
    db = db_factory()
    image = db.get(MealImage, image_id)
    image.uploaded_at = retention_cutoff_utc() - timedelta(days=40)
    db.commit()
    db.close()

    payload = {**MEAL_PAYLOAD, "meal_image_id": image_id}
    meal_id = client.post("/v1/meals", headers=auth_headers, json=payload).json()["meal_id"]

    detail = client.get(f"/v1/meals/{meal_id}", headers=auth_headers).json()
    assert detail["image_url"] is None

    date_key = payload["eaten_at"][:10]
    listed = client.get(
        "/v1/meals", headers=auth_headers, params={"date": date_key}
    ).json()["meals"]
    assert listed[0]["image_url"] is None


def test_list_returns_image_url_within_retention(client, auth_headers):
    image_id = upload(client, auth_headers).json()["meal_image_id"]
    payload = {**MEAL_PAYLOAD, "meal_image_id": image_id}
    meal_id = client.post("/v1/meals", headers=auth_headers, json=payload).json()["meal_id"]
    detail = client.get(f"/v1/meals/{meal_id}", headers=auth_headers).json()
    assert detail["image_url"]
