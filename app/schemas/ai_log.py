"""AI 호출 로그 조회 스키마 (명세서 6.2, 운영용)."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.pagination import Pagination
from app.schemas.common import KSTDateTime


class AiCallLogItem(BaseModel):
    id: int
    provider: str
    model_name: str
    task_type: str
    status: str
    latency_ms: int
    token: int | None
    cost: float | None
    created_at: KSTDateTime


class AiCallLogListResponse(BaseModel):
    items: list[AiCallLogItem]
    pagination: Pagination
