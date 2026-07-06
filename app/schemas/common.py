"""스키마 공통 요소.

- KSTDateTime: 응답 일시를 ISO8601 +09:00 으로 직렬화 (명세서 1.6)
- Money/영양 수치는 float 로 통일 (DB Numeric → float 변환)
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import PlainSerializer

from app.core.timeutil import to_kst

KSTDateTime = Annotated[
    datetime, PlainSerializer(lambda v: to_kst(v).isoformat(), return_type=str)
]
