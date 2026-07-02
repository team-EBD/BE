# Eatlog Backend

Eat로그 MVP 백엔드 (FastAPI + PostgreSQL).

> 설계 정본: `ref/설계/Eatlog_MVP_API_명세서_v0.1.md` (API 명세서가 SoT)
> 구현 순서: `ref/설계/Eatlog_BE_구현_계획서_v0.1.md` (Phase 0~11)

## 요구사항

- Python 3.12+
- Docker / Docker Compose (로컬 DB용)

## 빠른 시작 (Docker)

```bash
cp .env.example .env
docker compose up --build
```

- API: http://localhost:8000
- Swagger 문서: http://localhost:8000/docs
- 헬스체크: http://localhost:8000/v1/health

## 로컬 실행 (venv)

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# bash:                source .venv/bin/activate
pip install -r requirements.txt

# PostgreSQL 은 docker compose up db 로 띄우거나 별도 준비
cp .env.example .env
uvicorn app.main:app --reload
```

## 테스트

```bash
pytest
```

## 프로젝트 구조 (Phase 0 기준)

```
BE/
├── app/
│   ├── main.py            # 앱 진입점, 라우터/예외 핸들러 등록
│   ├── core/
│   │   ├── config.py      # 환경변수 (pydantic-settings)
│   │   ├── database.py    # 엔진/세션/Base/get_db
│   │   ├── errors.py      # 공통 에러 포맷·핸들러 (명세서 1.4/1.5)
│   │   └── pagination.py  # 공통 페이지네이션 (명세서 1.6)
│   └── api/v1/            # 라우터 (현재 health, 이후 auth/users/meals/…)
├── tests/
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

이후 `models/`, `schemas/`, `services/`, `repositories/`, `ai_client/`,
`social_client/`, `storage/`, `alembic/`, `scripts/` 가 Phase 1~ 에서 추가된다.

## 엔드포인트 규약

- 모든 API 는 `/v1` 프리픽스.
- 성공: 데이터 직접 반환 / 에러: `{ "error": { code, message, details } }`
- 날짜 `YYYY-MM-DD`, 일시 ISO8601(+09:00), ID `bigint`, 페이지네이션 `?page=&size=`.
