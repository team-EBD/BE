"""메일 발송 클라이언트 인터페이스."""
from __future__ import annotations

from typing import Protocol


class MailSendError(Exception):
    """SMTP 접속/인증/발송 실패. 호출측이 표준 에러 응답으로 변환한다."""


class MailClient(Protocol):
    def send(self, to: str, subject: str, body: str) -> None:
        """단일 수신자에게 텍스트 메일을 발송한다. 실패 시 MailSendError."""
        ...
