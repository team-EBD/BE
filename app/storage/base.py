"""스토리지 인터페이스."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class StoredObject:
    storage_key: str
    url: str


class Storage(Protocol):
    def save(self, key: str, data: bytes) -> StoredObject:
        """key 경로에 저장하고 접근 URL 을 반환한다. 실패 시 예외."""
        ...
