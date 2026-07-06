"""FastAPI 애플리케이션 진입점.

- `/v1` 프리픽스 라우팅 (명세서 1.1)
- 공통 예외 핸들러 등록 (명세서 1.4/1.5)
- /static: 로컬 스토리지 이미지 서빙 (dev 전용 — 운영은 Object Storage URL)
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1 import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers

app = FastAPI(
    title="Eatlog API",
    version="0.1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.api_v1_prefix)

Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=settings.storage_dir), name="static")


@app.get("/", tags=["health"])
def root() -> dict[str, str]:
    return {"service": "eatlog-api", "status": "ok", "docs": "/docs"}
