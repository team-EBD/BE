"""게이미피케이션 카탈로그 시드 (운영/로컬 공용).

`app.services.game_profile.ensure_catalog()` 가 요청 경로에서도 idempotent 하게
같은 일을 하므로 필수는 아니다. 배포 직후 상점을 미리 채워 두거나, 기획 JSON을
고친 뒤 즉시 반영하고 싶을 때 쓴다.

    .\\venv\\Scripts\\python.exe -m scripts.seed_gamification_catalog
"""
from __future__ import annotations

from app.core.database import SessionLocal
from app.services.game_profile import ensure_catalog


def seed(session_factory=SessionLocal) -> int:
    db = session_factory()
    try:
        rows = ensure_catalog(db)
        db.commit()
        return len(rows)
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    print(f"catalog_items 시드 완료: {seed()}종")
