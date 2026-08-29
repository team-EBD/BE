"""애플리케이션 로깅 설정.

왜 별도 모듈인가 — 2026-08-01 "cloudtype 로그 정지" 사고 때문이다.

기동 중(lifespan)에 alembic 을 인-프로세스로 실행하면 `alembic/env.py` 의
`fileConfig(alembic.ini)` 가 그 시점까지 만들어진 **모든 로거를 비활성화**한다
(`logging.config.fileConfig` 의 `disable_existing_loggers` 기본값이 True).
그래서 마이그레이션 직후부터 uvicorn(`uvicorn.error`/`uvicorn.access`)과 앱 로그
(`eatlog.*`)가 통째로 사라진다. 서버는 멀쩡히 요청을 처리하는데 터미널 출력만
`Will assume transactional DDL.` 에서 멈춰, 기동이 멎은 것처럼 보였다.

대책 두 가지:
1. `apply_alembic_ini_logging()` — alembic 을 CLI 로 단독 실행할 때만 ini 의 로깅
   설정을 적용하고, 인-프로세스 실행이면 건드리지 않는다. 적용할 때도
   `disable_existing_loggers=False` 로 기존 로거를 죽이지 않는다.
2. `setup_logging()` — 앱이 자기 로그 출력을 직접 보장한다. uvicorn 은 root 로거를
   설정하지 않아서, 이게 없으면 `eatlog.*`/alembic 의 INFO 로그가 아무 데도 안 찍힌다.
"""
from __future__ import annotations

import logging
import sys
from logging.config import fileConfig

_LOG_FORMAT = "%(levelname)-5.5s [%(name)s] %(message)s"

# 기동 시 마이그레이션 진행 상황("Running upgrade a -> b")을 보이게 한다.
_INFO_LOGGERS = ("eatlog", "alembic")
# 무엇이 껐든 되살려야 하는 로거 이름 접두사 (하위 로거 포함)
_KEEP_ENABLED_PREFIXES = ("eatlog", "alembic", "uvicorn")


def setup_logging(level: int = logging.INFO) -> None:
    """앱 로그가 stderr 로 나가도록 보장한다 (여러 번 불러도 안전).

    uvicorn 은 root 로거에 핸들러를 달지 않으므로, root 가 비어 있을 때만
    우리가 붙인다. gunicorn/커스텀 로깅 설정이 이미 있으면 존중한다.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
        root.setLevel(level)

    for name in _INFO_LOGGERS:
        logging.getLogger(name).setLevel(level)
    enable_app_loggers()


def enable_app_loggers() -> None:
    """앱/uvicorn 로거의 `disabled` 를 해제한다 (하위 로거까지).

    `fileConfig(disable_existing_loggers=True)` 는 로거를 **개별로** 끄기 때문에
    "eatlog" 뿐 아니라 "eatlog.main" 같은 하위 로거도 하나씩 되살려야 한다.
    """
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if not isinstance(logger, logging.Logger):
            continue  # PlaceHolder — 실제 로거가 아니다
        if name.startswith(_KEEP_ENABLED_PREFIXES):
            logger.disabled = False


def apply_alembic_ini_logging(config) -> None:
    """alembic.ini 의 [loggers] 설정을 적용한다 — 단독 실행일 때만.

    `config.attributes["configure_logger"] = False` 면 건너뛴다. 앱 기동 중
    인-프로세스 실행(app/core/migrations.py)이 이 값을 넣어, 이미 살아 있는
    uvicorn/앱 로거가 꺼지는 것을 막는다.
    """
    if not config.config_file_name:
        return
    if config.attributes.get("configure_logger", True) is False:
        return
    fileConfig(config.config_file_name, disable_existing_loggers=False)
