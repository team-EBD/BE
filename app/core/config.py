"""애플리케이션 설정.

환경변수/.env 를 pydantic-settings 로 로딩한다. 명세서 1.6의 공통 규칙과
아키텍처 9장 기술스택을 기준으로 한다. (이후 Phase 에서 필드가 추가된다)
"""
from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 앱 ---
    app_env: str = "local"
    api_v1_prefix: str = "/v1"

    # --- 데이터베이스 ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "eatlog"
    postgres_password: str = "eatlog"
    postgres_db: str = "eatlog"
    # 직접 지정 시 우선 사용 (없으면 위 값들로 조합)
    database_url: str | None = None

    # --- 자체 JWT (Phase 2) ---
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # --- 소셜 로그인 (Phase 2) ---
    google_client_id: str = ""

    # --- AI 서버 연동 (Phase 8) ---
    # 구조: BE → AI 서버(/internal/analyze·/internal/recommend) → Gemini.
    # AI 서버가 기본 8000을 쓰므로 BE(8000)와 겹치지 않게 별도 포트로 띄운다.
    ai_server_base_url: str = "http://localhost:8001"
    ai_request_timeout_seconds: float = 20.0
    # mock: AI 서버 없이 고정 응답(개발/테스트) / real: 실제 AI 서버 호출
    ai_client_mode: str = "real"

    # --- 이미지 스토리지 (Phase 4) ---
    # storage_backend: local(디스크, main.py 가 /static 서빙) | firebase(Firebase Storage)
    storage_backend: str = "local"
    storage_dir: str = "./uploads"
    storage_base_url: str = "http://localhost:8000/static"

    # --- Firebase Storage (storage_backend=firebase 일 때 사용) ---
    # 버킷 이름은 보통 `<project-id>.appspot.com`.
    firebase_storage_bucket: str = ""
    # 자격증명: JSON 문자열(배포 환경변수) 또는 서비스 계정 파일 경로 중 하나.
    # 둘 다 비면 ADC(GOOGLE_APPLICATION_CREDENTIALS/메타데이터 서버)로 폴백.
    firebase_credentials_json: str = ""
    firebase_credentials_file: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_uri(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
