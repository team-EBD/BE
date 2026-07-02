"""FastAPI 애플리케이션 진입점.

- `/v1` 프리픽스 라우팅 (명세서 1.1)
- 공통 예외 핸들러 등록 (명세서 1.4/1.5)
이후 모든 도메인 라우터는 app.api.v1.api_router 에 붙는다.
"""
from fastapi import FastAPI

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


@app.get("/", tags=["health"])
def root() -> dict[str, str]:
    return {"service": "eatlog-api", "status": "ok", "docs": "/docs"}
