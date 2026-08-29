"""Firebase Storage 구현 (운영/실연동용).

Firebase Storage(내부적으로 GCS 버킷)에 업로드하고, 다운로드 토큰이 포함된
공개 URL(`https://firebasestorage.googleapis.com/v0/b/...`)을 반환한다.
반환된 URL 은 라우터가 `meal_images.image_url` 로 DB 에 저장한다.

자격증명은 다음 우선순위로 초기화한다:
  1) FIREBASE_CREDENTIALS_JSON — 서비스 계정 JSON 문자열(배포 환경변수용)
  2) FIREBASE_CREDENTIALS_FILE — 서비스 계정 JSON 파일 경로
  3) ADC (GOOGLE_APPLICATION_CREDENTIALS / GCP 메타데이터 서버)

firebase_admin 초기화는 첫 저장 시점까지 지연(lazy)한다. 앱 부팅 시
자격증명이 없어도 로컬 스토리지 모드로 뜰 수 있게 하기 위함이다.
"""
from __future__ import annotations

import json
import mimetypes
import threading
import uuid
from urllib.parse import quote

from app.storage.base import StoredObject

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class FirebaseStorage:
    def __init__(
        self,
        bucket_name: str,
        credentials_json: str | None = None,
        credentials_file: str | None = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("FIREBASE_STORAGE_BUCKET 가 설정되지 않았습니다.")
        self._bucket_name = bucket_name
        self._credentials_json = credentials_json
        self._credentials_file = credentials_file
        self._bucket = None
        self._lock = threading.Lock()

    def _get_bucket(self):
        """firebase_admin 앱/버킷을 지연 초기화(스레드 안전)."""
        if self._bucket is not None:
            return self._bucket
        with self._lock:
            if self._bucket is not None:
                return self._bucket

            import firebase_admin
            from firebase_admin import credentials as fb_credentials
            from firebase_admin import storage as fb_storage

            cred = self._build_credentials(fb_credentials)
            # 다른 firebase_admin 사용(FCM 등)과 충돌하지 않도록 전용 앱 이름 사용.
            try:
                app = firebase_admin.get_app(name="eatlog-storage")
            except ValueError:
                app = firebase_admin.initialize_app(
                    cred,
                    {"storageBucket": self._bucket_name},
                    name="eatlog-storage",
                )
            self._bucket = fb_storage.bucket(app=app)
            return self._bucket

    def _build_credentials(self, fb_credentials):
        if self._credentials_json:
            return fb_credentials.Certificate(json.loads(self._credentials_json))
        if self._credentials_file:
            return fb_credentials.Certificate(self._credentials_file)
        return fb_credentials.ApplicationDefault()

    def save(self, key: str, data: bytes) -> StoredObject:
        """key 경로에 업로드하고 다운로드 토큰이 포함된 공개 URL 을 반환한다."""
        bucket = self._get_bucket()
        blob = bucket.blob(key)

        content_type = mimetypes.guess_type(key)[0] or _DEFAULT_CONTENT_TYPE
        # firebaseStorageDownloadTokens 메타데이터가 있으면 토큰 기반 공개 URL 로 접근 가능.
        token = uuid.uuid4().hex
        blob.metadata = {"firebaseStorageDownloadTokens": token}
        blob.upload_from_string(data, content_type=content_type)

        return StoredObject(storage_key=key, url=self._download_url(key, token))

    def delete(self, key: str) -> None:
        """key 경로의 객체를 삭제한다. 이미 없으면(NotFound) 조용히 무시한다."""
        from google.api_core.exceptions import NotFound

        bucket = self._get_bucket()
        try:
            bucket.blob(key).delete()
        except NotFound:
            pass

    def _download_url(self, key: str, token: str) -> str:
        encoded = quote(key, safe="")
        return (
            f"https://firebasestorage.googleapis.com/v0/b/{self._bucket_name}"
            f"/o/{encoded}?alt=media&token={token}"
        )
