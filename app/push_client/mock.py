"""Mock 푸시 클라이언트 (개발/테스트용) — 발송 대신 로그·기록만 남긴다."""
from __future__ import annotations

import logging

from app.push_client.base import PushSendReport

logger = logging.getLogger("eatlog.push")


class MockPushClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(
        self, tokens: list[str], title: str, body: str, data: dict[str, str]
    ) -> PushSendReport:
        self.sent.append({"tokens": list(tokens), "title": title, "body": body, "data": data})
        logger.info("[mock push] %d개 토큰에 발송: %s — %s", len(tokens), title, body)
        return PushSendReport(success_count=len(tokens))
