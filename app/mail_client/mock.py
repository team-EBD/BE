"""Mock 메일 클라이언트 (개발/테스트용) — 발송 대신 로그·기록만 남긴다.

개발 중에는 서버 로그에 찍힌 본문(인증코드 포함)으로 플로우를 확인한다.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("eatlog.mail")


class MockMailClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        logger.info("[mock mail] to=%s subject=%s\n%s", to, subject, body)
