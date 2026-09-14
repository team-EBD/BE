"""백그라운드 루프 담당(leader) 워커 선출 — Postgres advisory lock.

왜 필요한가: uvicorn `--workers N` 이면 같은 앱이 프로세스 N 개로 뜨고 lifespan 도
N 번 실행된다. 이미지 정리·주간 리포트 푸시 루프가 프로세스마다 돌면 푸시가
N 번 나간다(발송 중복 방지가 프로세스 메모리 기반이라 워커 간엔 공유되지 않음).

동작:
- 각 워커가 세션 수준 advisory lock(`pg_try_advisory_lock`)을 시도한다. 먼저 잡은
  워커만 True 를 받아 루프를 띄우고, 나머지는 False 를 받아 API 만 처리한다.
- 락은 잡은 커넥션이 살아 있는 동안 유지된다(모듈 전역으로 보관, 풀에 반납하지 않음 —
  풀 커넥션 하나를 상시 점유한다). 담당 워커가 죽으면 커넥션이 끊겨 락이 풀리고,
  다른 워커가 다음 재시도에 넘겨받는다.
- 락 커넥션이 DB 재시작 등으로 끊기면 락도 사라진다. 그대로 두면 옛 담당과 새 담당이
  동시에 루프를 돌리므로, 담당 워커는 `leader_alive()` 로 주기 점검해 끊겼으면 자격을
  내려놓고 재선출에 참여한다(app/main.py `_leader_loop`).
- Postgres 가 아니면(테스트 SQLite, 로컬 파일 DB) 항상 True — 단일 프로세스 가정.

migrations.py 의 마이그레이션 락과 키를 다르게 둔다(용도가 다르므로 서로 막지 않게).
이 모듈의 함수는 모두 블로킹 DB 호출이다 — 이벤트 루프에서는 `asyncio.to_thread` 로 부른다.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger("eatlog.leader")

# 백그라운드 루프 담당 락 키 (임의 상수. 마이그레이션 락 8_240_727_001 과 다름)
_LEADER_LOCK_KEY = 8_240_914_002

# 락을 쥔 커넥션. None 이면 이 프로세스는 담당이 아니다.
_lock_conn: Connection | None = None


def _is_postgres(engine: Engine) -> bool:
    return engine.dialect.name.startswith("postgres")


def _close_quietly(conn: Connection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 — 이미 끊긴 커넥션을 닫다 나는 예외는 무시
        logger.debug("leader 커넥션 close 실패 (무시)", exc_info=True)


def try_acquire_leader(engine: Engine) -> bool:
    """루프 담당 락을 시도한다. 잡으면 True. 이미 잡고 있으면 그대로 True.

    락을 쥔 커넥션은 모듈 전역에 보관한다 — `with` 로 닫으면 풀에 반납되면서
    세션 락도 풀리기 때문이다. 실패(락을 남이 쥠)면 커넥션을 바로 반납한다.
    """
    global _lock_conn
    if _lock_conn is not None:
        return True
    if not _is_postgres(engine):
        return True
    conn = engine.connect()
    try:
        got = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _LEADER_LOCK_KEY}
            ).scalar_one()
        )
        conn.commit()  # 세션 락은 트랜잭션과 무관하게 유지된다
    except Exception:
        _close_quietly(conn)
        raise
    if not got:
        conn.close()
        return False
    _lock_conn = conn
    return True


def leader_alive() -> bool:
    """담당 자격이 유효한지 — 락 커넥션이 살아 있는지 확인한다.

    끊겨 있으면(DB 재시작·네트워크 단절) 락도 이미 사라진 것이므로 담당 상태를
    해제하고 False 를 돌려준다. 비 Postgres(락 커넥션 없음)는 항상 True.
    """
    global _lock_conn
    if _lock_conn is None:
        return True
    try:
        _lock_conn.execute(text("SELECT 1"))
        _lock_conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — 어떤 오류든 '자격 상실'로 본다
        # 끊긴 커넥션의 traceback 은 길고 정보가 없다 — 한 줄로 남기고 상세는 debug
        logger.warning(
            "leader 락 커넥션 끊김 (%s) — 담당 해제 후 재선출에 참여", type(exc).__name__
        )
        logger.debug("leader_alive 상세", exc_info=True)
        conn, _lock_conn = _lock_conn, None
        _close_quietly(conn)
        return False


def release_leader() -> None:
    """담당을 내려놓는다 — 락 해제 후 커넥션을 풀에 반납한다. 담당이 아니면 no-op."""
    global _lock_conn
    if _lock_conn is None:
        return
    conn, _lock_conn = _lock_conn, None
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LEADER_LOCK_KEY})
        conn.commit()
    except Exception:  # noqa: BLE001 — 커넥션이 이미 끊겼으면 락도 함께 사라졌다
        logger.debug("leader unlock 실패 (커넥션 종료로 해제됨)", exc_info=True)
    finally:
        _close_quietly(conn)


def is_leader() -> bool:
    """이 프로세스가 Postgres 락을 쥔 담당인지 (비 Postgres 는 락이 없으므로 False)."""
    return _lock_conn is not None
