"""헬스체크 라우터.

- GET /v1/health  : 앱 생존 확인 (항상 200)
- GET /v1/health/db : DB 연결까지 확인
- GET /v1/health/boom : 공통 에러 포맷 검증용 (의도적 에러) — Phase 0 DoD 확인용, 이후 제거 가능
"""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import APIError

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": "ok"}


@router.get("/health/boom")
def health_boom() -> None:
    # 공통 에러 핸들러가 명세서 1.4 포맷으로 변환하는지 확인하는 용도.
    raise APIError(
        status_code=400,
        code="VALIDATION_ERROR",
        message="의도적으로 발생시킨 검증 에러입니다.",
        details=[{"field": "demo", "reason": "intentional"}],
    )
