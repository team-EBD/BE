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
    # Sign in with Apple — identityToken 의 aud 검증에 사용 (iOS 앱 번들 ID).
    # 비어 있으면 aud 검증을 생략한다 (운영에서는 반드시 설정할 것).
    apple_bundle_id: str = ""

    # --- AI 서버 연동 (Phase 8) ---
    # 구조: BE → AI 서버(/internal/analyze·/internal/recommend) → Gemini.
    # AI 서버가 기본 8000을 쓰므로 BE(8000)와 겹치지 않게 별도 포트로 띄운다.
    ai_server_base_url: str = "http://localhost:8001"
    ai_request_timeout_seconds: float = 20.0
    # mock: AI 서버 없이 고정 응답(개발/테스트) / real: 실제 AI 서버 호출
    ai_client_mode: str = "real"

    # --- 일일 AI 사용량 제한 (사용자당, KST 자정 리셋. 0 이하 = 무제한) ---
    analyze_daily_limit: int = 10
    recommend_daily_limit: int = 10

    # --- 시작 시 마이그레이션 ---
    # 서버 기동 시 alembic upgrade head 를 자동 적용한다. cloudtype 처럼 배포
    # 플랫폼이 Dockerfile CMD(scripts/start.sh)를 자체 시작 명령으로 덮어써
    # 마이그레이션이 실행되지 않는 환경을 위한 안전망 (app/core/migrations.py).
    # Postgres 가 아니거나 pytest 중이면 이 값과 무관하게 건너뛴다.
    run_migrations_on_startup: bool = True

    # --- 이미지 보존 기간 정리 ---
    # 저번달 1일(KST) 이전 업로드 이미지를 매일 스토리지·DB 에서 정리한다.
    # 테스트 등에서 백그라운드 태스크를 끄고 싶으면 false.
    image_retention_purge_enabled: bool = True

    # --- 푸시 발송 ---
    # push_backend: mock(로그만, dev/테스트) | fcm(Firebase Cloud Messaging, 운영)
    push_backend: str = "mock"
    # 주간 리포트 도착 푸시 — 매주 지정 요일(mon~sun)·시각(KST) 발송.
    # 기본 일요일: 리포트 주 단위(일~토)가 토요일 밤에 완결된 직후 아침.
    weekly_report_push_enabled: bool = True
    weekly_report_push_day: str = "sun"
    weekly_report_push_time: str = "09:00"  # HH:mm

    # --- 메일 발송 (비밀번호 재설정 인증코드) ---
    # email_backend: mock(로그만, dev/테스트) | smtp(Gmail 등 SMTP, 운영)
    # Gmail 사용 시 smtp_password 에는 계정 비밀번호가 아닌 "앱 비밀번호"를 넣는다.
    email_backend: str = "mock"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # 발신 주소 표시용 (비우면 smtp_user 사용)
    mail_from: str = ""

    # --- 비밀번호 재설정 인증코드 정책 ---
    password_reset_code_expire_minutes: int = 10
    password_reset_code_max_attempts: int = 5

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
