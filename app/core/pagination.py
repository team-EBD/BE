"""공통 페이지네이션 유틸 (명세서 1.6).

    ?page=1&size=20   (기본 size 20, 최대 100)

응답의 `pagination` 객체:
    { "page": 1, "size": 20, "total": 87, "total_pages": 5 }
"""
from __future__ import annotations

import math
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

DEFAULT_SIZE = 20
MAX_SIZE = 100


class PageParams(BaseModel):
    page: int = 1
    size: int = DEFAULT_SIZE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size


def page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_SIZE)] = DEFAULT_SIZE,
) -> PageParams:
    """FastAPI 의존성으로 쓰는 페이지 파라미터 파서."""
    return PageParams(page=page, size=size)


class Pagination(BaseModel):
    page: int
    size: int
    total: int
    total_pages: int

    @classmethod
    def build(cls, params: PageParams, total: int) -> "Pagination":
        total_pages = math.ceil(total / params.size) if params.size else 0
        return cls(
            page=params.page,
            size=params.size,
            total=total,
            total_pages=total_pages,
        )
