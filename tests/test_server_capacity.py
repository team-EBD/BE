"""서버 용량 설정(2026-09-14) — 커넥션 풀 옵션·백그라운드 루프 담당(leader) 락·워커 수.

운영 Postgres 없이 검증 가능한 범위만 다룬다. advisory lock 의 실제 상호배제는
Postgres 전용이므로 여기서는 (1) SQLite 에서 항상 담당이 되는지 (2) Postgres 분기에서
락 SQL 을 호출하고 결과에 따라 커넥션을 보관/반납하는지 (3) 락 커넥션이 끊기면 자격을
내려놓는지 (4) main 의 감시 루프가 선출·자격 상실·종료를 올바르게 처리하는지를
가짜 엔진/함수로 확인한다.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app import main as app_main
from app.core import leader
from app.core.config import settings
from app.core.database import pool_kwargs


@pytest.fixture(autouse=True)
def _reset_leader_state():
    leader.release_leader()
    yield
    leader.release_leader()


# --- 커넥션 풀 옵션 -------------------------------------------------------------


def test_pool_kwargs_sqlite_is_empty():
    assert pool_kwargs("sqlite://") == {}
    assert pool_kwargs("sqlite:///./dev.db") == {}


def test_pool_kwargs_postgres_uses_settings():
    kwargs = pool_kwargs("postgresql+psycopg://u:p@h/db")
    assert kwargs == {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_seconds,
    }
    # 워커 2 × (size+overflow) + AI 서버 풀 10 이 RDS t4g.micro 상한(≈110) 아래
    assert 2 * (kwargs["pool_size"] + kwargs["max_overflow"]) + 10 < 110


# --- leader 락 -------------------------------------------------------------------


def _fake_pg_engine(lock_result: bool):
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    conn = MagicMock()
    conn.execute.return_value.scalar_one.return_value = lock_result
    engine.connect.return_value = conn
    return engine, conn


def test_leader_sqlite_always_true_without_lock_connection():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    assert leader.try_acquire_leader(engine) is True
    assert leader.is_leader() is False  # 비 Postgres 는 락 커넥션을 만들지 않는다
    assert leader.leader_alive() is True
    leader.release_leader()  # no-op


def test_leader_postgres_acquired_keeps_connection_and_releases():
    engine, conn = _fake_pg_engine(True)
    assert leader.try_acquire_leader(engine) is True
    assert leader.is_leader() is True
    assert "pg_try_advisory_lock" in str(conn.execute.call_args_list[0].args[0])
    conn.close.assert_not_called()  # 락 유지 = 커넥션 보관

    # 이미 담당이면 재시도 없이 True
    assert leader.try_acquire_leader(engine) is True
    assert engine.connect.call_count == 1

    leader.release_leader()
    assert leader.is_leader() is False
    conn.close.assert_called_once()
    assert "pg_advisory_unlock" in str(conn.execute.call_args_list[-1].args[0])


def test_leader_postgres_not_acquired_returns_connection():
    engine, conn = _fake_pg_engine(False)
    assert leader.try_acquire_leader(engine) is False
    assert leader.is_leader() is False
    conn.close.assert_called_once()


def test_leader_acquire_error_closes_connection_and_raises():
    engine, conn = _fake_pg_engine(True)
    conn.execute.side_effect = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        leader.try_acquire_leader(engine)
    assert leader.is_leader() is False
    conn.close.assert_called_once()


def test_leader_alive_true_while_connection_works():
    engine, conn = _fake_pg_engine(True)
    leader.try_acquire_leader(engine)
    assert leader.leader_alive() is True
    assert "SELECT 1" in str(conn.execute.call_args_list[-1].args[0])
    assert leader.is_leader() is True


def test_leader_alive_false_and_steps_down_when_connection_dead():
    engine, conn = _fake_pg_engine(True)
    leader.try_acquire_leader(engine)
    conn.execute.side_effect = RuntimeError("connection closed")
    assert leader.leader_alive() is False
    assert leader.is_leader() is False  # 담당 해제 → 다음 try_acquire 가 다시 락을 시도한다
    conn.close.assert_called_once()
    leader.release_leader()  # 이미 해제됐으므로 no-op (close 재호출 없음)
    conn.close.assert_called_once()


def test_release_leader_swallows_unlock_error():
    engine, conn = _fake_pg_engine(True)
    leader.try_acquire_leader(engine)
    conn.execute.side_effect = RuntimeError("connection closed")
    leader.release_leader()  # 예외가 밖으로 나오면 종료 경로가 깨진다
    assert leader.is_leader() is False
    conn.close.assert_called_once()


# --- main._leader_loop (선출·감시 루프) -------------------------------------------


class _LoopHarness:
    """`_leader_loop` 가 부르는 함수들을 스크립트대로 응답하는 가짜."""

    def __init__(self, acquire: list[bool], alive: list[bool]):
        self.acquire = list(acquire)
        self.alive = list(alive)
        self.started = 0
        self.running: list[asyncio.Task] = []
        self.cancelled = 0

    def try_acquire(self, _engine):
        return self.acquire.pop(0) if self.acquire else True

    def leader_alive(self):
        return self.alive.pop(0) if self.alive else True

    def start_loops(self):
        self.started += 1

        async def _sleep_forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

        self.running = [asyncio.create_task(_sleep_forever())]
        return list(self.running)


def _run_leader_loop(monkeypatch, harness: _LoopHarness, *, ticks: int) -> None:
    """감시 루프를 `ticks` 번의 sleep 만큼 돌린 뒤 취소하고, 종료가 깨끗한지 확인한다."""
    monkeypatch.setattr(app_main, "try_acquire_leader", harness.try_acquire)
    monkeypatch.setattr(app_main, "leader_alive", harness.leader_alive)
    monkeypatch.setattr(app_main, "_start_background_loops", harness.start_loops)
    monkeypatch.setattr(app_main, "_LEADER_RETRY_SECONDS", 0)

    async def scenario():
        task = asyncio.create_task(app_main._leader_loop())
        for _ in range(ticks):
            await asyncio.sleep(0)
        await app_main._cancel_all([task])
        assert task.cancelled() or task.done()

    asyncio.run(scenario())


def test_leader_loop_starts_loops_once_when_acquired(monkeypatch):
    h = _LoopHarness(acquire=[True], alive=[True, True, True])
    _run_leader_loop(monkeypatch, h, ticks=30)
    assert h.started == 1
    # 종료 시 루프 태스크도 함께 취소된다
    assert h.cancelled == 1
    assert all(t.cancelled() for t in h.running)


def test_leader_loop_waits_as_follower_until_lock_is_free(monkeypatch):
    h = _LoopHarness(acquire=[False, False, True], alive=[True])
    _run_leader_loop(monkeypatch, h, ticks=40)
    assert h.started == 1
    assert h.acquire == []  # False 두 번을 거쳐 세 번째 시도에서 담당이 됨


def test_leader_loop_stops_loops_and_reelects_when_lock_lost(monkeypatch):
    # 담당 → 자격 상실(alive False) → 재선출 성공 → 다시 담당
    h = _LoopHarness(acquire=[True, True], alive=[True, False, True, True])
    _run_leader_loop(monkeypatch, h, ticks=60)
    assert h.started == 2
    # 첫 루프 세트는 자격 상실 시점에, 둘째는 종료 시점에 취소됨
    assert h.cancelled == 2


def test_leader_loop_survives_acquire_exception(monkeypatch):
    h = _LoopHarness(acquire=[], alive=[True])
    calls = {"n": 0}

    def flaky(_engine):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return True

    h.try_acquire = flaky
    _run_leader_loop(monkeypatch, h, ticks=30)
    assert calls["n"] >= 2
    assert h.started == 1


# --- 기동 스크립트 ----------------------------------------------------------------


def test_start_script_runs_multiple_workers_with_override():
    script = Path(__file__).resolve().parents[1] / "scripts" / "start.sh"
    text = script.read_text(encoding="utf-8")
    assert "WEB_CONCURRENCY:-2" in text
    assert '--workers "${WORKERS}"' in text
