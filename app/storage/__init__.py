"""이미지 스토리지 추상화 (Phase 4).

dev 는 로컬 디스크, 운영은 S3 호환 구현으로 교체한다. 라우터는
get_storage 의존성만 바라보므로 구현 교체가 자유롭다.
"""
from __future__ import annotations

from app.core.config import settings
from app.storage.base import Storage, StoredObject
from app.storage.local import LocalStorage

__all__ = ["Storage", "StoredObject", "LocalStorage", "get_storage"]

_storage: Storage | None = None


def get_storage() -> Storage:
    """FastAPI 의존성. MVP 는 로컬 디스크 구현."""
    global _storage
    if _storage is None:
        _storage = LocalStorage(
            root_dir=settings.storage_dir, base_url=settings.storage_base_url
        )
    return _storage
