"""푸시 발송 클라이언트 인터페이스."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class PushSendReport:
    """발송 결과 요약. invalid_tokens 는 등록 해제된 토큰(호출측이 DB 에서 정리)."""

    success_count: int = 0
    failure_count: int = 0
    invalid_tokens: list[str] = field(default_factory=list)


class PushClient(Protocol):
    def send(
        self, tokens: list[str], title: str, body: str, data: dict[str, str]
    ) -> PushSendReport:
        """토큰 목록에 알림을 발송한다. 통신 오류는 예외 대신 failure 로 집계한다."""
        ...
