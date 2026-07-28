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

## 배포 / 마이그레이션

### 시작 시 마이그레이션 자동 적용

서버는 기동할 때 `alembic upgrade head` 를 스스로 적용한다 (`app/core/migrations.py`).
`scripts/start.sh` 를 시작 명령으로 쓰지 못하는 환경(cloudtype 대시보드가 자체 시작
명령으로 Dockerfile CMD 를 덮어쓰는 경우)에서도 코드와 DB 스키마가 어긋나지 않게 하기
위한 안전망이다.

- 여러 인스턴스가 동시에 떠도 Postgres advisory lock 으로 한 번만 실행된다.
- 이미 head 면 아무 일도 하지 않으므로 매 기동마다 돌아도 무해하다.
- **Postgres 가 아니거나 pytest 실행 중이면 건너뛴다** — 테스트(SQLite)는 `create_all`
  로 스키마를 만들고, `.env` 의 운영 DB 에 alembic 이 돌지 않도록 막는다.
- 끄려면 `RUN_MIGRATIONS_ON_STARTUP=false`.
- 마이그레이션이 실패해도 서버 기동은 계속한다(로그에 스택트레이스). 기동 자체가
  막히면 원인 파악이 더 어렵기 때문 — 배포 후 로그에서 `[startup] alembic upgrade head`
  와 완료 로그를 확인할 것.

### GitHub Actions → cloudtype

`.github/workflows/deploy.yml` 이 `dev` 푸시마다 pytest 를 돌리고, 저장소 변수
`CLOUDTYPE_PROJECT` 가 설정돼 있으면 cloudtype 에 배포한다.

필요한 설정 (Settings → Secrets and variables → Actions):

| 종류 | 이름 | 값 |
| --- | --- | --- |
| Secret | `CLOUDTYPE_TOKEN` | cloudtype API 키 (스페이스 설정 → 인증 → 새 API 키) |
| Secret | `GHP_TOKEN` | GitHub PAT(classic), 스코프 `repo`·`workflow`·`admin:public_key` |
| Variable | `CLOUDTYPE_PROJECT` | **`<스페이스>/<프로젝트>`** 형식 |
| Variable | `CLOUDTYPE_STAGE` | 스테이지 이름 (기본 스테이지면 생략 가능) |

`GHP_TOKEN` 은 `connect` 액션이 배포키를 등록하는 데 쓴다. 기본 `GITHUB_TOKEN` 은
`admin:public_key` 권한이 없어 대체할 수 없다. **PAT 만료일이 지나면 배포가 멈추므로**
만료 기간을 길게 잡거나 갱신 일정을 잡아둘 것.

`CLOUDTYPE_PROJECT` 를 비워 두면 배포 잡은 건너뛰고 테스트만 돈다. cloudtype 대시보드의
GitHub 자동배포를 이미 쓰고 있다면 그대로 두는 편이 낫다(중복 배포 방지).

**배포 스펙은 `cloudtype.yaml` 이며, 배포 시 앱 설정을 덮어쓴다.** 리포에 들어 있는 파일은
손으로 쓴 골격일 뿐이므로 그대로 쓰면 안 된다. **cloudtype 대시보드에서 해당 서비스를 열고
`CLI` 탭에 생성돼 있는 스펙을 복사해 교체할 것** — 현재 앱 설정이 그대로 반영된 값이라
환경변수 유실 없이 안전하다.

### 더 단순한 대안
cloudtype 대시보드에서 GitHub 저장소를 연결하고 배포 브랜치를 `dev` 로 지정하면
GitHub Actions 없이도 푸시할 때마다 자동 배포된다. 토큰·스펙 관리가 없어 더 간단하다.
GitHub Actions 를 쓰는 이유는 **배포 전에 pytest 를 게이트로 걸 수 있다**는 점 하나이며,
둘을 동시에 켜면 한 번의 푸시에 두 번 배포되니 하나만 쓸 것.
