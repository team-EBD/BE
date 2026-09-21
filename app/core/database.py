"""DB 엔진 / 세션 / 선언적 베이스.

모든 모델은 Base 를 상속한다(Phase 1). 라우터는 get_db 의존성으로 세션을 받는다.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


def pool_kwargs(uri: str) -> dict:
    """URI 에 맞는 커넥션 풀 옵션. SQLite(테스트·로컬 파일)는 큐 풀을 쓰지 않아
    pool_size 류 인자를 받지 않으므로 비워 둔다."""
    if uri.startswith("sqlite"):
        return {}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_seconds,
    }


engine = create_engine(
    settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    future=True,
    **pool_kwargs(settings.sqlalchemy_database_uri),
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """모든 ORM 모델의 공통 베이스."""


def get_db() -> Generator[Session, None, None]:
    """요청 스코프 DB 세션 의존성."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
