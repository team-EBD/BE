"""푸시 발송 클라이언트 추상화.

push_backend 설정으로 구현을 전환한다: `mock`(로그만, dev/테스트) /
`fcm`(Firebase Cloud Messaging, 운영). Firebase 자격증명은 이미지 스토리지와
같은 서비스 계정(firebase_credentials_*)을 재사용한다.
"""
from __future__ import annotations

from app.core.config import settings
from app.push_client.base import PushClient, PushSendReport
from app.push_client.fcm import FcmPushClient
from app.push_client.mock import MockPushClient

__all__ = [
    "PushClient",
    "PushSendReport",
    "MockPushClient",
    "FcmPushClient",
    "get_push_client",
]

_client: PushClient | None = None


def get_push_client() -> PushClient:
    """push_backend 설정에 따라 구현을 선택(싱글턴). 테스트는 인자 주입으로 대체."""
    global _client
    if _client is None:
        if settings.push_backend == "fcm":
            _client = FcmPushClient(
                credentials_json=settings.firebase_credentials_json or None,
                credentials_file=settings.firebase_credentials_file or None,
            )
        else:
            _client = MockPushClient()
    return _client
