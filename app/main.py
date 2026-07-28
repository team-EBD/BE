"""FastAPI 애플리케이션 진입점.

- `/v1` 프리픽스 라우팅 (명세서 1.1)
- 공통 예외 핸들러 등록 (명세서 1.4/1.5)
- /static: 로컬 스토리지 이미지 서빙 (dev 전용 — 운영은 Object Storage URL)
- lifespan: 보존 기간(저번달 1일~) 지난 식사 이미지 매일 정리
  + 주간 리포트 도착 푸시 발송(매주 설정 요일·시각, KST)
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.v1 import api_router
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.errors import register_exception_handlers
from app.core.migrations import run_migrations_if_enabled
from app.core.timeutil import kst_date_of, now_utc
from app.push_client import get_push_client
from app.services.image_retention import purge_expired_images
from app.services.weekly_report_push import send_weekly_report_push, weekly_push_due
from app.storage import get_storage

logger = logging.getLogger("eatlog.main")

_PURGE_INTERVAL_SECONDS = 24 * 60 * 60
_WEEKLY_PUSH_CHECK_SECONDS = 60


def _run_image_purge() -> None:
    db = SessionLocal()
    try:
        purge_expired_images(db, get_storage())
    finally:
        db.close()


async def _image_purge_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_run_image_purge)
        except Exception:  # noqa: BLE001 — 정리 실패가 서비스를 죽여선 안 된다
            logger.exception("이미지 보존 기간 정리 실패 (다음 주기에 재시도)")
        await asyncio.sleep(_PURGE_INTERVAL_SECONDS)


def _run_weekly_report_push() -> None:
    db = SessionLocal()
    try:
        send_weekly_report_push(db, get_push_client())
    finally:
        db.close()


async def _weekly_report_push_loop() -> None:
    last_sent_date: str | None = None
    while True:
        try:
            now = now_utc()
            today = kst_date_of(now).isoformat()
            if weekly_push_due(
                now,
                settings.weekly_report_push_day,
                settings.weekly_report_push_time,
                already_sent_today=(last_sent_date == today),
            ):
                await asyncio.to_thread(_run_weekly_report_push)
                last_sent_date = today
        except Exception:  # noqa: BLE001 — 발송 실패가 서비스를 죽여선 안 된다
            logger.exception("주간 리포트 푸시 발송 실패 (다음 주기에 재시도)")
        await asyncio.sleep(_WEEKLY_PUSH_CHECK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 시작 명령이 scripts/start.sh 가 아니어도(cloudtype 대시보드 override)
    # 코드와 DB 스키마가 어긋나지 않도록 여기서 한 번 더 보장한다.
    await asyncio.to_thread(run_migrations_if_enabled)

    tasks = []
    if settings.image_retention_purge_enabled:
        tasks.append(asyncio.create_task(_image_purge_loop()))
    if settings.weekly_report_push_enabled:
        tasks.append(asyncio.create_task(_weekly_report_push_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()


app = FastAPI(
    title="Eatlog API",
    version="0.1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

register_exception_handlers(app)
app.include_router(api_router, prefix=settings.api_v1_prefix)

Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=settings.storage_dir), name="static")


@app.get("/healthz", tags=["health"])
def root() -> dict[str, str]:
    return {"service": "eatlog-api", "status": "ok", "docs": "/docs"}
