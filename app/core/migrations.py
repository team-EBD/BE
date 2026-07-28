"""서버 기동 시 alembic 마이그레이션 자동 적용.

왜 필요한가: 컨테이너 기동 스크립트(scripts/start.sh)가 이미 `alembic upgrade head`
를 돌리지만, cloudtype 은 대시보드의 자체 시작 명령(uvicorn 직접 실행)으로
Dockerfile CMD 를 덮어써서 그 스크립트가 실행되지 않는다. 그러면 코드와 DB 스키마가
어긋나 배포 직후 500 이 난다(예: 2026-07-15 meal_records.is_skipped).
그래서 앱 자신이 lifespan 에서 한 번 더 보장한다 — 시작 명령이 무엇이든 적용된다.

안전장치:
- Postgres 가 아니면 건너뛴다 (테스트의 SQLite 는 create_all 로 스키마를 만든다).
- pytest 실행 중이면 건너뛴다 (.env 의 운영 DATABASE_URL 을 건드리지 않도록).
- 여러 인스턴스가 동시에 기동해도 advisory lock 으로 한 번만 실행된다.
- 이미 head 면 alembic 이 아무것도 하지 않으므로 매 기동마다 돌아도 무해하다.
"""
from __future__ import annotations

import logging
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine

logger = logging.getLogger("eatlog.migrations")

# 동시 기동 시 마이그레이션을 직렬화하는 Postgres advisory lock 키 (임의 상수)
_MIGRATION_LOCK_KEY = 8_240_727_001


def _under_pytest() -> bool:
    """pytest 실행 중인지. (pytest 가 테스트마다 세팅하는 환경변수로 판단)"""
    return bool(os.getenv("PYTEST_CURRENT_TEST"))


def should_run(uri: str | None = None) -> bool:
    """시작 시 마이그레이션을 돌려야 하는 상황인지."""
    if not settings.run_migrations_on_startup:
        return False
    if _under_pytest():  # 테스트가 운영 DB 를 건드리지 않게
        return False
    return (uri or settings.sqlalchemy_database_uri).startswith("postgresql")


def _alembic_config() -> Config:
    """alembic.ini 를 프로젝트 루트 기준으로 로드한다 (작업 디렉터리 무관)."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "alembic"))
    return config


def run_migrations() -> None:
    """`alembic upgrade head` 를 advisory lock 아래에서 실행한다."""
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        try:
            command.upgrade(_alembic_config(), "head")
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
            connection.commit()


def run_migrations_if_enabled() -> None:
    """기동 훅. 실패해도 예외를 밖으로 내보내지 않는다.

    마이그레이션 실패로 서버가 아예 안 뜨면 원인 파악이 더 어려워지므로,
    로그로 크게 남기고 기동은 계속한다 (스키마 불일치는 해당 API 에서 드러난다).
    """
    if not should_run():
        return
    try:
        logger.info("[startup] alembic upgrade head")
        run_migrations()
        logger.info("[startup] 마이그레이션 적용 완료")
    except Exception:  # noqa: BLE001 — 기동 자체를 막지 않는다
        logger.exception("[startup] 마이그레이션 실패 — 수동 확인 필요")
