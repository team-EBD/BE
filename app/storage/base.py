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

    def delete(self, key: str) -> None:
        """key 경로의 객체를 삭제한다. 이미 없으면 조용히 무시한다."""
        ...
