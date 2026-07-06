"""로컬 디스크 스토리지 (dev 용).

main.py 가 root_dir 를 /static 으로 서빙해 image_url 이 실제로 열리게 한다.
"""
from __future__ import annotations

from pathlib import Path

from app.storage.base import StoredObject


class LocalStorage:
    def __init__(self, root_dir: str, base_url: str) -> None:
        self.root = Path(root_dir)
        self.base_url = base_url.rstrip("/")

    def save(self, key: str, data: bytes) -> StoredObject:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(storage_key=key, url=f"{self.base_url}/{key}")
