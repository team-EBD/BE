#!/bin/sh
# 컨테이너 기동 스크립트.
# 코드(모델)와 DB 스키마가 어긋나지 않도록 서버 시작 전에 alembic 마이그레이션을
# 반드시 적용한다. (미적용 시 예: users.password_hash UndefinedColumn 500 에러)
set -e

echo "[start] alembic upgrade head"
alembic upgrade head

echo "[start] uvicorn app.main:app"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
