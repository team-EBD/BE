"""DB 엔진 / 세션 / 선언적 베이스.

모든 모델은 Base 를 상속한다(Phase 1). 라우터는 get_db 의존성으로 세션을 받는다.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    future=True,
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
