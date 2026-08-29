"""이미지 스토리지 추상화 (Phase 4).

storage_backend 설정으로 구현을 전환한다: `local`(로컬 디스크, dev) /
`firebase`(Firebase Storage, 운영/실연동). 라우터는 get_storage 의존성만
바라보므로 구현 교체가 자유롭다.
"""
from __future__ import annotations

from app.core.config import settings
from app.storage.base import Storage, StoredObject
from app.storage.firebase import FirebaseStorage
from app.storage.local import LocalStorage

__all__ = [
    "Storage",
    "StoredObject",
    "LocalStorage",
    "FirebaseStorage",
    "get_storage",
]

_storage: Storage | None = None


def _build_storage() -> Storage:
    if settings.storage_backend == "firebase":
        return FirebaseStorage(
            bucket_name=settings.firebase_storage_bucket,
            credentials_json=settings.firebase_credentials_json or None,
            credentials_file=settings.firebase_credentials_file or None,
        )
    return LocalStorage(
        root_dir=settings.storage_dir, base_url=settings.storage_base_url
    )


def get_storage() -> Storage:
    """FastAPI 의존성. storage_backend 설정에 따라 구현을 선택(싱글턴)."""
    global _storage
    if _storage is None:
        _storage = _build_storage()
    return _storage
