"""서버 기동 시 alembic 마이그레이션 자동 적용.

왜 필요한가: 컨테이너 기동 스크립트(scripts/start.sh)가 이미 이 모듈을 실행하지만,
cloudtype 은 대시보드의 자체 시작 명령(uvicorn 직접 실행)으로
Dockerfile CMD 를 덮어써서 그 스크립트가 실행되지 않는다. 그러면 코드와 DB 스키마가
어긋나 배포 직후 500 이 난다(예: 2026-07-15 meal_records.is_skipped).
그래서 앱 자신이 lifespan 에서 한 번 더 보장한다 — 시작 명령이 무엇이든 적용된다.

안전장치:
- Postgres 가 아니면 건너뛴다 (테스트의 SQLite 는 create_all 로 스키마를 만든다).
- pytest 실행 중이면 건너뛴다 (.env 의 운영 DATABASE_URL 을 건드리지 않도록).
- CLI·시작 스크립트·lifespan 모두 같은 advisory lock 으로 직렬화한다.
- 이미 head 면 alembic 이 아무것도 하지 않으므로 매 기동마다 돌아도 무해하다.
- 실패하면 기동도 실패한다. 잘못된 스키마로 healthz=200 을 반환하지 않는다.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

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
    # 인-프로세스 실행이므로 env.py 가 ini 의 로깅 설정을 적용하면 안 된다.
    # (fileConfig 가 uvicorn/앱 로거를 전부 비활성화한다 — app/core/logging.py)
    config.attributes["configure_logger"] = False
    return config


@contextmanager
def migration_connection(connectable):
    """이 연결에서 DDL과 advisory lock을 함께 실행한다 (CLI와 앱 공용)."""
    with connectable.connect() as connection:
        if connection.dialect.name != "postgresql":
            yield connection
            return
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
        connection.commit()  # 세션 락은 유지하고 Alembic이 별도 DDL 트랜잭션을 시작하게 한다.
        try:
            yield connection
        finally:
            # 실패한 DDL 트랜잭션을 먼저 정리해야 unlock SQL도 실행할 수 있다.
            connection.rollback()
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
            connection.commit()


def run_migrations() -> None:
    """앱이 사용하는 DB 연결 하나로 잠금과 `alembic upgrade head`를 실행한다."""
    with migration_connection(engine) as connection:
        config = _alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def run_migrations_if_enabled() -> None:
    """기동 훅. 스키마 적용 실패 시 서버를 기동하지 않는다."""
    if not should_run():
        return
    try:
        logger.info("[startup] alembic upgrade head")
        run_migrations()
        logger.info("[startup] 마이그레이션 적용 완료")
    except Exception:
        logger.exception("[startup] 마이그레이션 실패 — 서버 기동 중단")
        raise


if __name__ == "__main__":
    run_migrations_if_enabled()
