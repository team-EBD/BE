"""SMTP 메일 클라이언트 (stdlib smtplib, 외부 의존성 없음).

Gmail 기준 기본값: smtp.gmail.com:587 + STARTTLS + 앱 비밀번호.
(Gmail 은 계정 비밀번호가 아닌 "앱 비밀번호" 발급이 필요하다)
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from app.mail_client.base import MailSendError


class SmtpMailClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        from_name: str = "eatlog",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._from_name = from_name
        self._timeout = timeout_seconds

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = formataddr((self._from_name, self._from_address))
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                smtp.starttls()
                smtp.login(self._username, self._password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailSendError(str(exc)) from exc
