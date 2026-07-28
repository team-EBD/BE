"""기동 시 alembic 자동 적용의 안전장치 검증.

실제 마이그레이션을 돌리지 않고, "언제 돌아야 하는지"와 "실패해도 기동을 막지
않는지"만 확인한다 (운영 DB 를 건드리면 안 되므로).
"""
from __future__ import annotations

import pytest

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


def test_failure_does_not_break_startup(monkeypatch, caplog):
    """마이그레이션이 실패해도 예외가 밖으로 나가지 않는다 (기동 계속)."""
    monkeypatch.setattr(migrations, "should_run", lambda *a, **k: True)

    def boom() -> None:
        raise RuntimeError("연결 실패")

    monkeypatch.setattr(migrations, "run_migrations", boom)
    migrations.run_migrations_if_enabled()  # 예외 없이 반환해야 한다
    assert "마이그레이션 실패" in caplog.text


def test_alembic_config_points_at_project_root():
    """작업 디렉터리와 무관하게 alembic 스크립트 경로를 찾는다."""
    config = migrations._alembic_config()
    assert config.get_main_option("script_location").endswith("alembic")
