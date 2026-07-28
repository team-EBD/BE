"""테스트 공통 픽스처.

- SQLite in-memory(StaticPool) + create_all 로 실제 DB 없이 전 API 를 돌린다.
- 소셜 검증·AI 클라이언트·스토리지는 dependency_overrides 로 교체.
- auth_headers: 소셜 로그인 플로우를 통과해 실제 토큰을 획득한다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai_client import get_ai_client
from app.ai_client.mock import MockAIClient
from app.api.v1.auth import get_social_verifier
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app

# 테스트에서는 백그라운드 태스크(이미지 정리·주간 리포트 푸시)를 끈다
# (TestClient lifespan 이 실제 SessionLocal/스토리지를 건드리지 않도록).
settings.image_retention_purge_enabled = False
settings.weekly_report_push_enabled = False
# 테스트는 SQLite(create_all)로 스키마를 만든다 — .env 의 운영 DB 에
# alembic 을 돌리지 않도록 시작 시 마이그레이션도 끈다.
settings.run_migrations_on_startup = False
# 요청 타이밍 미들웨어는 실제 SessionLocal(.env DB)에 기록하므로 테스트에서 끈다
settings.request_log_enabled = False
from app.models import NutritionItem  # noqa: F401 — 모델 로딩 보장
from app.social_client import SocialIdentity
from app.storage import get_storage
from app.storage.local import LocalStorage
from scripts.seed_nutrition_items import seed


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite 는 기본으로 FK 를 강제하지 않는다 — 운영 Postgres 와 동일하게
    # ondelete CASCADE/SET NULL 이 동작하도록 켠다 (회원 탈퇴 등 검증에 필요)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    seed(session_factory=factory)  # 음식 40종
    yield factory
    engine.dispose()


@pytest.fixture()
def client(db_factory, tmp_path):
    def override_get_db():
        db = db_factory()
        try:
            yield db
        finally:
            db.close()

    def fake_verifier():
        def verify(provider: str, token: str) -> SocialIdentity:
            # 테스트 규약: token 문자열이 곧 social_id (사용자 구분용)
            return SocialIdentity(
                provider=provider,
                social_id=token,
                email=f"{token}@example.com",
                nickname=f"user-{token}",
                profile_image_url=None,
            )

        return verify

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_social_verifier] = fake_verifier
    app.dependency_overrides[get_ai_client] = lambda: MockAIClient()
    app.dependency_overrides[get_storage] = lambda: LocalStorage(
        root_dir=str(tmp_path / "uploads"), base_url="http://testserver/static"
    )

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, social_id: str = "tester") -> dict:
    """소셜 로그인 → 토큰/유저 반환."""
    res = client.post(
        "/v1/auth/social/login", json={"provider": "google", "token": social_id}
    )
    assert res.status_code in (200, 201), res.text
    return res.json()


@pytest.fixture()
def auth_headers(client) -> dict[str, str]:
    tokens = login(client)
    return {"Authorization": f"Bearer {tokens['access_token']}"}
