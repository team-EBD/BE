"""모델 공통 요소 (타임스탬프 컬럼 팩토리).

ERD는 테이블마다 created_at/updated_at 유무가 다르므로 Mixin 대신
필요한 곳에서 골라 쓰는 컬럼 팩토리를 제공한다. 모든 시각은 timezone-aware
(명세서 1.6: ISO8601 +09:00). 저장은 UTC, 표현 시 +09:00 변환은 상위 계층 담당.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

# timezone 포함 datetime 타입 (Postgres timestamptz)
TZDateTime = DateTime(timezone=True)


def pk_column() -> Mapped[int]:
    """bigint PK. SQLite 에서는 INTEGER 로 변환해 autoincrement 가 동작하게 한다
    (SQLite 는 오직 INTEGER PRIMARY KEY 만 rowid autoincrement). Postgres 는 BIGINT 유지."""
    return mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )


def created_at_column() -> Mapped[datetime]:
    return mapped_column(TZDateTime, server_default=func.now(), nullable=False)


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        TZDateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
