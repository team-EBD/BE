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

## DB 마이그레이션 & 시드 (Phase 1)

```bash
# 1) 스키마 생성 (17개 테이블)
alembic upgrade head

# 2) 음식 영양 시드 40종 적재 (멱등 — 여러 번 실행해도 40행)
python -m scripts.seed_nutrition_items
```

- 모델은 `app/models/`(ERD 1:1, 17개 테이블), 마이그레이션은 `alembic/versions/`.
- 새 모델/컬럼 추가 후 마이그레이션 생성: `alembic revision --autogenerate -m "메시지"`
- 시드 원본: `seed/nutrition_items_seed.json` (목업 40종, 정식 공공 영양DB로 교체 예정)

## 테스트

```bash
pytest
```

## 이미지 스토리지 (Phase 4)

- FE 가 `POST /v1/meals/images` 로 이미지를 전송하면 BE 가 스토리지에 저장하고
  접근 URL 을 `meal_images.image_url` 로 DB 에 기록한 뒤 응답한다.
- `.env` 의 `STORAGE_BACKEND` 로 전환한다: `local`(디스크, `main.py` 가 `/static` 서빙, dev) /
  `firebase`(Firebase Storage, 운영/실연동).
- `firebase` 사용 시 `.env` 에 아래를 채운다:
  - `FIREBASE_STORAGE_BUCKET` — 버킷 이름(보통 `<project-id>.appspot.com`)
  - 자격증명 — `FIREBASE_CREDENTIALS_JSON`(서비스 계정 JSON 문자열, 배포 환경변수용) 또는
    `FIREBASE_CREDENTIALS_FILE`(파일 경로) 중 하나. 둘 다 비우면 ADC
    (`GOOGLE_APPLICATION_CREDENTIALS`/GCP 메타데이터 서버)로 폴백.
- 반환 URL 은 다운로드 토큰이 포함된 `https://firebasestorage.googleapis.com/v0/b/...`
  형식이라 FE 가 별도 인증 없이 바로 로드할 수 있다.

## AI 서버 연동 (Phase 8)

- `.env` 의 `AI_CLIENT_MODE` 로 전환한다: `mock`(AI 서버 없이 고정 응답) / `real`(AI 서버 호출).
- `real` 이면 `AI_SERVER_BASE_URL`(기본 `http://localhost:8001`)의
  `/internal/analyze`·`/internal/recommend` 를 호출한다. AI 서버는 8000이 기본이므로
  BE 와 같은 머신이면 `uvicorn app.main:app --port 8001` 로 분리해 띄운다.

## 프로젝트 구조 (Phase 0~11 구현 완료)

```
BE/
├── app/
│   ├── main.py            # 앱 진입점, 라우터·예외 핸들러·/static 서빙 등록
│   ├── core/
│   │   ├── config.py      # 환경변수 (pydantic-settings)
│   │   ├── database.py    # 엔진/세션/Base/get_db
│   │   ├── security.py    # 자체 JWT 발급/검증, refresh 해시
│   │   ├── deps.py        # get_current_user (401 구분)
│   │   ├── errors.py      # 공통 에러 포맷·핸들러 (명세서 1.4/1.5)
│   │   ├── pagination.py  # 공통 페이지네이션 (명세서 1.6)
│   │   └── timeutil.py    # 저장 UTC / 응답 +09:00 / 하루 경계 KST
│   ├── models/            # SQLAlchemy 모델 (ERD 1:1, 17개 테이블)
│   ├── schemas/           # Pydantic 요청/응답 (API 명세서 1:1)
│   ├── api/v1/            # 라우터 9개 (26개 엔드포인트)
│   ├── services/          # correction·matching·summary·meals·analyze
│   ├── ai_client/         # AI 서버 클라이언트 (mock/real, 실패 계약 변환)
│   ├── social_client/     # 구글 토큰 검증 (provider 확장 스위치)
│   └── storage/           # 이미지 스토리지 추상화 (로컬 → S3 교체 예정)
├── alembic/               # 마이그레이션
├── scripts/               # 시드 로더
├── tests/                 # pytest 62개 (E2E 핵심 루프 포함)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

> 계획서의 `repositories/` 계층은 MVP 규모에서 생략 — services 가 쿼리를 직접 수행한다.

## 엔드포인트 규약

- 모든 API 는 `/v1` 프리픽스.
- 성공: 데이터 직접 반환 / 에러: `{ "error": { code, message, details } }`
- 날짜 `YYYY-MM-DD`, 일시 ISO8601(+09:00), ID `bigint`, 페이지네이션 `?page=&size=`.
