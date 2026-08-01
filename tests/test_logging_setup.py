"""로깅 설정 회귀 테스트.

2026-08-01 사고: 기동 중 인-프로세스 alembic 실행이 `fileConfig` 로 uvicorn/앱
로거를 전부 비활성화해, 서버는 정상인데 로그가 마이그레이션 직후부터 끊겼다.
"""
from __future__ import annotations

import logging

from app.core import logging as app_logging
from app.core.migrations import _alembic_config


def test_inprocess_config_opts_out_of_ini_logging():
    """기동 훅이 쓰는 Config 는 env.py 의 fileConfig 를 건너뛰도록 표시한다."""
    assert _alembic_config().attributes["configure_logger"] is False


def test_ini_logging_skipped_does_not_disable_existing_loggers():
    """인-프로세스 실행에서는 기존 로거(uvicorn 등)가 살아 있어야 한다."""
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.disabled = False

    app_logging.apply_alembic_ini_logging(_alembic_config())

    assert uvicorn_logger.disabled is False


def test_ini_logging_applied_standalone_keeps_existing_loggers(monkeypatch):
    """CLI 단독 실행에서는 ini 를 적용하되, 기존 로거는 끄지 않는다."""
    calls: list[dict] = []
    monkeypatch.setattr(
        app_logging,
        "fileConfig",
        lambda path, **kwargs: calls.append({"path": path, **kwargs}),
    )
    config = _alembic_config()
    config.attributes.pop("configure_logger")  # alembic CLI 기본 상태

    app_logging.apply_alembic_ini_logging(config)

    assert len(calls) == 1
    assert calls[0]["disable_existing_loggers"] is False


def test_setup_logging_revives_disabled_logger_and_is_idempotent():
    app_logging.setup_logging()
    before = list(logging.getLogger().handlers)

    disabled = logging.getLogger("eatlog.main")
    disabled.disabled = True
    app_logging.setup_logging()

    assert disabled.disabled is False
    assert logging.getLogger().handlers == before  # 핸들러 중복 추가 없음
