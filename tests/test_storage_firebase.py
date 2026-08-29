"""FirebaseStorage 단위 테스트.

네트워크/자격증명 없이 버킷을 스텁으로 주입해 save() 계약을 검증한다:
  - blob 에 content_type + firebaseStorageDownloadTokens 메타데이터로 업로드
  - 토큰이 포함된 firebasestorage.googleapis.com 다운로드 URL 반환
  - storage_backend 설정에 따른 get_storage() 백엔드 선택
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.storage.firebase import FirebaseStorage


class _FakeBlob:
    def __init__(self) -> None:
        self.metadata: dict | None = None
        self.uploaded: tuple[bytes, str] | None = None

    def upload_from_string(self, data: bytes, content_type: str) -> None:
        self.uploaded = (data, content_type)


class _FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, _FakeBlob] = {}

    def blob(self, key: str) -> _FakeBlob:
        self.blobs.setdefault(key, _FakeBlob())
        return self.blobs[key]


def test_save_uploads_and_returns_token_url() -> None:
    fake_bucket = _FakeBucket()
    store = FirebaseStorage(bucket_name="eatlog.appspot.com")
    store._bucket = fake_bucket  # 지연 초기화 우회 (네트워크/자격증명 불필요)

    key = "meals/2026/07/abc123.jpg"
    stored = store.save(key, b"\xff\xd8\xffdata")

    blob = fake_bucket.blobs[key]
    assert blob.uploaded is not None
    data, content_type = blob.uploaded
    assert data == b"\xff\xd8\xffdata"
    assert content_type == "image/jpeg"

    token = blob.metadata["firebaseStorageDownloadTokens"]
    assert token

    assert stored.storage_key == key
    parsed = urlparse(stored.url)
    assert parsed.netloc == "firebasestorage.googleapis.com"
    assert "eatlog.appspot.com" in parsed.path
    # key 는 URL 인코딩되어 경로에 들어간다 ("/" -> %2F)
    assert "meals%2F2026%2F07%2Fabc123.jpg" in parsed.path
    qs = parse_qs(parsed.query)
    assert qs["alt"] == ["media"]
    assert qs["token"] == [token]


def test_empty_bucket_name_rejected() -> None:
    with pytest.raises(ValueError):
        FirebaseStorage(bucket_name="")


def test_get_storage_selects_backend(monkeypatch) -> None:
    import app.storage as storage_module
    from app.storage import FirebaseStorage as FS
    from app.storage import LocalStorage as LS

    monkeypatch.setattr(storage_module.settings, "storage_backend", "local", raising=False)
    monkeypatch.setattr(storage_module, "_storage", None, raising=False)
    assert isinstance(storage_module.get_storage(), LS)

    monkeypatch.setattr(storage_module.settings, "storage_backend", "firebase", raising=False)
    monkeypatch.setattr(
        storage_module.settings, "firebase_storage_bucket", "eatlog.appspot.com", raising=False
    )
    monkeypatch.setattr(storage_module, "_storage", None, raising=False)
    assert isinstance(storage_module.get_storage(), FS)

    # 다른 테스트에 영향 없도록 싱글턴 초기화
    monkeypatch.setattr(storage_module, "_storage", None, raising=False)
