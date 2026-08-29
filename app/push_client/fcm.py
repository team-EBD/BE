"""FCM 푸시 발송 구현 (운영/실연동용).

Firebase Storage(storage/firebase.py)와 같은 서비스 계정 자격증명을 재사용하되,
firebase_admin 앱은 전용 이름(eatlog-fcm)으로 분리 초기화한다.

자격증명 우선순위 (storage/firebase.py 와 동일):
  1) FIREBASE_CREDENTIALS_JSON — 서비스 계정 JSON 문자열(배포 환경변수용)
  2) FIREBASE_CREDENTIALS_FILE — 서비스 계정 JSON 파일 경로
  3) ADC (GOOGLE_APPLICATION_CREDENTIALS / GCP 메타데이터 서버)

초기화는 첫 발송 시점까지 지연(lazy)한다 — 자격증명이 없어도 앱은 뜬다.
"""
from __future__ import annotations

import json
import logging
import threading

from app.push_client.base import PushSendReport

logger = logging.getLogger("eatlog.push")

# FCM 멀티캐스트 1회 호출당 토큰 상한 (Firebase 제한 500)
_BATCH_SIZE = 500


class FcmPushClient:
    def __init__(
        self,
        credentials_json: str | None = None,
        credentials_file: str | None = None,
    ) -> None:
        self._credentials_json = credentials_json
        self._credentials_file = credentials_file
        self._app = None
        self._lock = threading.Lock()

    def _get_app(self):
        if self._app is not None:
            return self._app
        with self._lock:
            if self._app is not None:
                return self._app

            import firebase_admin
            from firebase_admin import credentials as fb_credentials

            if self._credentials_json:
                cred = fb_credentials.Certificate(json.loads(self._credentials_json))
            elif self._credentials_file:
                cred = fb_credentials.Certificate(self._credentials_file)
            else:
                cred = fb_credentials.ApplicationDefault()

            try:
                self._app = firebase_admin.get_app(name="eatlog-fcm")
            except ValueError:
                self._app = firebase_admin.initialize_app(cred, name="eatlog-fcm")
            return self._app

    def send(
        self, tokens: list[str], title: str, body: str, data: dict[str, str]
    ) -> PushSendReport:
        from firebase_admin import messaging

        app = self._get_app()
        report = PushSendReport()

        for start in range(0, len(tokens), _BATCH_SIZE):
            batch = tokens[start : start + _BATCH_SIZE]
            message = messaging.MulticastMessage(
                tokens=batch,
                notification=messaging.Notification(title=title, body=body),
                data=data,
            )
            batch_response = messaging.send_each_for_multicast(message, app=app)
            report.success_count += batch_response.success_count
            report.failure_count += batch_response.failure_count

            for token, response in zip(batch, batch_response.responses):
                if response.success:
                    continue
                # 등록 해제(앱 삭제 등)된 토큰은 호출측이 DB 에서 정리하도록 보고
                if isinstance(response.exception, messaging.UnregisteredError):
                    report.invalid_tokens.append(token)
                else:
                    logger.warning("FCM 발송 실패: %s", response.exception)

        return report
