"""기동 시 alembic 자동 적용의 안전장치 검증.

실제 DB 연결 없이 실행 조건·실패 시 기동 중단·잠금 연결 재사용을 확인한다.
"""
from __future__ import annotations

import pytest
from contextlib import contextmanager
from types import SimpleNamespace

from app.core import migrations
from app.core.config import settings


@pytest.fixture()
def enabled(monkeypatch):
    """설정은 켜고 pytest 감지만 끈 상태 (운영 기동을 흉내낸다)."""
    monkeypatch.setattr(settings, "run_migrations_on_startup", True)
    monkeypatch.setattr(migrations, "_under_pytest", lambda: False)


def test_runs_on_postgres(enabled):
    assert migrations.should_run("postgresql+psycopg://u:p@host:5432/db") is True


def test_skipped_on_sqlite(enabled):
    """테스트/로컬 SQLite 는 create_all 로 스키마를 만든다 — alembic 대상이 아니다."""
    assert migrations.should_run("sqlite://") is False


def test_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "run_migrations_on_startup", False)
    monkeypatch.setattr(migrations, "_under_pytest", lambda: False)
    assert migrations.should_run("postgresql+psycopg://u:p@host:5432/db") is False


def test_skipped_under_pytest(monkeypatch):
    """pytest 중에는 .env 의 운영 DATABASE_URL 에 절대 적용하지 않는다."""
    monkeypatch.setattr(settings, "run_migrations_on_startup", True)
    assert migrations._under_pytest() is True  # 실제로 pytest 가 세팅한 환경변수
    assert migrations.should_run("postgresql+psycopg://u:p@host:5432/db") is False


def test_conftest_disables_startup_migrations():
    """테스트 스위트 전체에서 기본적으로 꺼져 있어야 한다 (conftest 보증)."""
    assert settings.run_migrations_on_startup is False


def test_failure_stops_startup(monkeypatch, caplog):
    """스키마가 실패한 서버가 정상 healthz를 반환하며 트래픽을 받으면 안 된다."""
    monkeypatch.setattr(migrations, "should_run", lambda *a, **k: True)

    def boom() -> None:
        raise RuntimeError("연결 실패")

    monkeypatch.setattr(migrations, "run_migrations", boom)
    with pytest.raises(RuntimeError, match="연결 실패"):
        migrations.run_migrations_if_enabled()
    assert "서버 기동 중단" in caplog.text


def test_alembic_config_points_at_project_root():
    """작업 디렉터리와 무관하게 alembic 스크립트 경로를 찾는다."""
    config = migrations._alembic_config()
    assert config.get_main_option("script_location").endswith("alembic")


def test_lifespan_does_not_serve_healthy_app_after_migration_failure(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    def boom():
        raise RuntimeError("schema upgrade failed")

    monkeypatch.setattr(main, "run_migrations_if_enabled", boom)
    with pytest.raises(RuntimeError, match="schema upgrade failed"):
        with TestClient(main.app):
            pytest.fail("마이그레이션 실패 후 요청을 받으면 안 됩니다")


@pytest.mark.parametrize("fails", [False, True])
def test_migration_uses_locked_connection_and_cleans_failed_transaction(monkeypatch, fails):
    events = []

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement, params):
            events.append("unlock" if "pg_advisory_unlock" in str(statement) else "lock")

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

    connection = Connection()

    @contextmanager
    def connect():
        yield connection

    def upgrade(config, revision):
        assert config.attributes["connection"] is connection
        assert revision == "head"
        events.append("upgrade")
        if fails:
            raise RuntimeError("DDL failed")

    monkeypatch.setattr(migrations, "engine", SimpleNamespace(connect=connect))
    monkeypatch.setattr(migrations.command, "upgrade", upgrade)
    if fails:
        with pytest.raises(RuntimeError, match="DDL failed"):
            migrations.run_migrations()
    else:
        migrations.run_migrations()
    assert events == ["lock", "commit", "upgrade", "rollback", "unlock", "commit"]
