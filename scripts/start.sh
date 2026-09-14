#!/bin/sh
# 컨테이너 기동 스크립트.
# 코드(모델)와 DB 스키마가 어긋나지 않도록 서버 시작 전에 alembic 마이그레이션을
# 반드시 적용한다. (미적용 시 예: users.password_hash UndefinedColumn 500 에러)
set -e

echo "[start] alembic upgrade head"
alembic upgrade head

# 워커(프로세스) 수. EC2 t4g.small 이 2 vCPU 라 기본 2. 환경변수 WEB_CONCURRENCY 로 조정
# (Parameter Store /eatlog/be/WEB_CONCURRENCY). 백그라운드 루프(이미지 정리·주간 푸시)는
# app/core/leader.py 의 advisory lock 으로 한 워커만 돌리므로 워커 수와 무관하게 1회 실행된다.
WORKERS="${WEB_CONCURRENCY:-2}"
echo "[start] uvicorn app.main:app --workers ${WORKERS}"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WORKERS}"
