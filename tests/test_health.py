"""Phase 0 DoD 스모크 테스트.

DB 없이도 도는 것만 확인한다(/health, 에러 포맷, /v1 프리픽스).
DB 연결(/health/db)은 docker-compose 기동 후 수동/통합 테스트로 확인.
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_root_ok():
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_health_ok():
    res = client.get("/v1/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_v1_prefix_required():
    # 프리픽스 없이 접근하면 404 + 공통 에러 포맷
    res = client.get("/health")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"


def test_common_error_format():
    res = client.get("/v1/health/boom")
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"][0]["field"] == "demo"
