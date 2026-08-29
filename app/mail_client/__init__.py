"""메일 발송 클라이언트 추상화.

email_backend 설정으로 구현을 전환한다: `mock`(로그만, dev/테스트) /
`smtp`(Gmail 등 SMTP 서버, 운영). push_client 와 동일한 구조.
"""
from __future__ import annotations

from app.core.config import settings
from app.mail_client.base import MailClient, MailSendError
from app.mail_client.mock import MockMailClient
from app.mail_client.smtp import SmtpMailClient

__all__ = [
    "MailClient",
    "MailSendError",
    "MockMailClient",
    "SmtpMailClient",
    "get_mail_client",
]

_client: MailClient | None = None


def get_mail_client() -> MailClient:
    """email_backend 설정에 따라 구현을 선택(싱글턴). 테스트는 dependency override 로 대체."""
    global _client
    if _client is None:
        if settings.email_backend == "smtp":
            _client = SmtpMailClient(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user,
                password=settings.smtp_password,
                from_address=settings.mail_from or settings.smtp_user,
            )
        else:
            _client = MockMailClient()
    return _client
