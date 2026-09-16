# 음식군(food_groups) DB 계약 — 영양 DB 정리 × 추천 엔진 v2

이 문서는 **DB 정리(팀원)와 추천 엔진(feat/recommend-engine-v2)이 맞물리는 지점**을 고정한다.
테이블·컬럼·NULL 의미·채움 순서·검증 질의를 여기서 합의하고, 양쪽 구현은 이 문서를 따른다.
바뀌면 이 문서를 먼저 고친다.

근거 데이터 (2026-09-16 실측): 식약처 원본 음식편 19,495건(대분류 25 · 대표식품명 1,249),
가공식품편 590,542건(대분류 24 · 대표식품명 270), 둘 다 결측 0%.
우리 `nutrition_items` 42,036행 = 대표 15,680 (식약처 코드 있음 10,289 · `rep:` 5,367 · `gen:` 24) + 비대표 26,310 + 시드 46.

---

## 0. 원칙 — 기존 기록에 영향 0

| 원칙 | 이유 |
|---|---|
| 새 컬럼은 전부 **NULL 허용** | 채워지기 전·못 채운 행도 유효해야 한다. NULL = "미분류" |
| `nutrition_items.id` 는 **바뀌지 않는다** | `meal_items`·`favorite_foods`·`food_candidates` 가 FK 로 가리킨다 |
| `is_representative` 의 **의미를 바꾸지 않는다** | AI 매칭(`match_food_name`)·검색 순위가 이 플래그를 본다. 추천용 판단은 `food_groups.role` 이 맡는다 |
| 삭제는 **참조 재지정 → 아카이브 → 삭제** 순서로만 | 세 FK 모두 `ondelete=SET NULL` 이고 `meal_items` 는 스냅샷이라 삭제해도 화면·합계는 안 바뀌지만, 연결은 잃는다. 참조는 meal_items 56행·food_candidates 67행·favorites 0 (2026-09-16) |
| 군 대표 영양값은 **삭제 전에** 계산 | 구성원이 사라지면 재계산 근거가 DB 에 없다 (원본 JSONL 로만 가능) |

---

## 1. 3층 구조

```
계열 (family)   16개      food_groups.family  ← 우리가 정한 이름. 식약처 대분류 두 체계(음식 25·가공 24)를 §4 표로 통합
군   (group)    ~1,400개  food_groups         ← 식약처 대표식품명 기준, 중복 병합(버거=햄버거)
상품 (item)     42k → 33k nutrition_items     ← 기존 행. 순수 중복 8,581 삭제 (§6)
```

사용자에게 보이는 이름: **그 군에서 사용자가 기록한 상품명이 있으면 상품명, 없으면 군명**
(예: 이력에 "빅소불고기버거"가 있으면 그것, 없으면 "햄버거").

---

## 2. 테이블 (DDL)

```sql
-- 2.1 군
CREATE TABLE food_groups (
    id                  BIGSERIAL PRIMARY KEY,
    name                VARCHAR(50)  NOT NULL UNIQUE,        -- 표시명. 식약처 대표식품명 or 병합 후 대표명
    family              VARCHAR(30)  NOT NULL,               -- §4 계열 16개 중 하나
    role                VARCHAR(12)  NOT NULL,               -- meal | companion | snack | exclude  (§5 규칙으로 유도)
    companion_group_id  BIGINT REFERENCES food_groups(id),   -- 기본 동반 (찌개 → 쌀밥). NULL = 없음
    calories            NUMERIC(8,2), carbs NUMERIC(8,2), protein NUMERIC(8,2), fat NUMERIC(8,2),  -- 1인분 대표값 (구성원 절사평균)
    base_amount         NUMERIC(8,2), base_unit VARCHAR(20),                                        -- 대표값의 기준량
    member_count        INTEGER NOT NULL DEFAULT 0,          -- 채움 시점 구성원 수 (검증용)
    source_names        TEXT,                                -- 병합 전 식약처 대표식품명들 (예: "버거|햄버거")
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_food_groups_family ON food_groups(family);
CREATE INDEX ix_food_groups_role   ON food_groups(role);

-- 2.2 별칭 — 사용자 기록 이름·시드·동의어 → 군
CREATE TABLE food_group_aliases (
    alias           VARCHAR(100) PRIMARY KEY,                -- normalize_name() 적용된 키 (공백·온도·사이즈 제거)
    group_id        BIGINT NOT NULL REFERENCES food_groups(id) ON DELETE CASCADE,
    kind            VARCHAR(12)  NOT NULL,                   -- synonym | seed | manual | auto
    note            TEXT
);

-- 2.3 상품 — 기존 테이블에 컬럼 추가 (전부 NULL 허용)
ALTER TABLE nutrition_items
    ADD COLUMN food_group_id  BIGINT REFERENCES food_groups(id) ON DELETE SET NULL,
    ADD COLUMN serving_basis  VARCHAR(12);                   -- per_serving | per_100g  (NULL = 미판정)
CREATE INDEX ix_nutrition_items_food_group ON nutrition_items(food_group_id);

-- 2.4 사용자 기록 — 저장 시 채움 + 과거 backfill
ALTER TABLE meal_items
    ADD COLUMN food_group_id  BIGINT REFERENCES food_groups(id) ON DELETE SET NULL;
CREATE INDEX ix_meal_items_food_group ON meal_items(food_group_id);

-- 2.5 추천 항목별 행동 로그 (현재 recommendation_logs.recommended_items JSON 을 대체)
CREATE TABLE recommendation_items (
    id                    BIGSERIAL PRIMARY KEY,
    log_id                BIGINT NOT NULL REFERENCES recommendation_logs(id) ON DELETE CASCADE,
    food_group_id         BIGINT REFERENCES food_groups(id) ON DELETE SET NULL,
    name                  VARCHAR(100) NOT NULL,             -- 카드에 보인 이름
    source                VARCHAR(12)  NOT NULL,             -- personal | popular | similar
    rank                  SMALLINT     NOT NULL,             -- 1~3
    score                 NUMERIC(6,4),
    accepted_at           TIMESTAMPTZ,                       -- 카드 탭
    rejected_at           TIMESTAMPTZ,                       -- "이건 별로" (FE 미구현 — 컬럼만)
    eaten_at              TIMESTAMPTZ,                       -- 4시간 내 같은 군 기록 저장
    eaten_meal_record_id  BIGINT REFERENCES meal_records(id) ON DELETE SET NULL
);
CREATE INDEX ix_recommendation_items_log    ON recommendation_items(log_id);
CREATE INDEX ix_recommendation_items_group  ON recommendation_items(food_group_id);

-- 2.6 삭제 아카이브 (되돌리기용)
CREATE TABLE nutrition_items_pruned (LIKE nutrition_items INCLUDING ALL);
ALTER TABLE nutrition_items_pruned ADD COLUMN pruned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                                   ADD COLUMN survivor_id BIGINT;          -- 참조를 넘긴 대표 행
```

### NULL 의 의미 (엔진이 이렇게 해석한다)

| 컬럼 | NULL 이면 |
|---|---|
| `nutrition_items.food_group_id` | 미분류. **추천 후보에서 제외**, 매칭·검색은 정상 |
| `nutrition_items.serving_basis` | 미판정. 추천은 `per_serving` 만 쓰므로 제외 |
| `meal_items.food_group_id` | 미분류. 개인 빈도 집계에서 이름 키(`normalize_name(food_name)`)로 폴백 |
| `food_groups.companion_group_id` | 기본 동반 없음 (버거·면·김밥) |
| `food_groups.calories …` | 대표값 없음 → 그 군은 유사 후보 풀에서 제외 |

---

## 3. 채움 순서

| 단계 | 작업 | 입력 | 담당 | 완료 기준 |
|---|---|---|---|---|
| **A** | `food_groups` 생성 | 식약처 대표식품명 1,249 + 270 → §4 계열 매핑 → 병합 규칙(`source_names`) → §5 로 role | DB | 군 수 1,300~1,500, role 분포 리포트 |
| **B** | `food_group_aliases` 채움 | 동의어 6 + 시드 46 + 수동 ~25 (`잇로그-음식군-alias-초안.md` 참고) | DB + 나 | 사용자 기록 상위 200 이름 전부 매핑 |
| **C** | `nutrition_items.food_group_id` — 식약처 코드 행 | 재적재 **갱신 모드**: `external_id` 로 찾아 `food_group_id`·`serving_basis` 만 UPDATE. INSERT 는 새 코드만 | DB | P/D 행 미분류 < 1% |
| **D** | `nutrition_items.food_group_id` — `rep:` 행 5,367 | 큐레이션 스크립트(`curate_representative_foods`)가 구성원의 군을 **다수결**로 상속. 동률·소수는 NULL + 리포트 | DB | rep 행 미분류 < 5% |
| **E** | `gen:` 24 · 시드 46 | alias 표로 | DB | 전부 매핑 |
| **F** | `serving_basis` | 대표 = `per_serving`, 비대표 = `per_100g`. **단 대표 중 `base_amount=100` 1,365행은 감사** — `total_weight`·이름으로 1인분 확정, 못 하면 `per_100g` 로 강등(`is_representative` 는 유지) | DB | 감사 결과 표 |
| **G** | 군 대표 영양값 | `serving_basis=per_serving` 구성원의 절사평균(상하위 10%) — `build_generic_foods` 로직 재사용. **§6 삭제 전에 실행** | DB | 대표값 NULL 인 군 목록 |
| **H** | `meal_items.food_group_id` backfill | ① `nutrition_item_id` 있으면 조인 ② 없으면 `normalize_name(food_name)` → alias 정확일치 ③ 못 찾으면 **NULL 로 둔다** (어미 추정 금지 — 오탐이 개인 빈도를 오염) | 나 | 실사용자 기록 미분류 < 10% |
| **I** | 저장 시 채움 | `create_meal`/`update_meal` 에서 H 와 같은 규칙으로 `food_group_id` 기록 | 나 | 신규 기록 미분류 비율 모니터 |
| **J** | 피드백 이관 | `feedback.py` 를 JSON → `recommendation_items` 로. 과거 JSON 로그는 그대로 둔다 | 나 | 출처별 섭취율 질의 1줄 |

A → B → (C, D, E 병렬) → F → G → **§6 삭제** → H → I → J

---

## 4. 계열 16개 — 두 대분류 체계 통합표

| 계열 (우리 이름) | 음식편 대분류 | 가공식품 대분류 | 기본 role |
|---|---|---|---|
| 밥류 | 밥류 | 즉석식품류 中 밥류·주먹밥/김밥/초밥·도시락 | meal (쌀밥·잡곡밥 등 순수 밥은 companion) |
| 면류 | 면 및 만두류 (만두 제외) | 면류 | meal |
| 분식류 | — (군 재배치: 만두·떡볶이·순대·핫도그·어묵) | 즉석식품류 中 만두 | meal |
| 국·탕·찌개류 | 국 및 탕류 · 찌개 및 전골류 · 죽 및 스프류 | 즉석식품류 中 국/탕류 | meal |
| 구이·볶음·조림류 | 구이류 · 볶음류 · 조림류 · 찜류 · 전·적 및 부침류 | 식육가공품 中 양념육 | meal |
| 튀김류 | 튀김류 | 즉석식품류 中 튀김·닭튀김 | meal |
| 버거·피자·샌드위치 | 빵 및 과자류 中 햄버거·버거·피자·샌드위치·토스트 (군 재배치) | 즉석식품류 中 버거·샌드위치 | meal |
| 빵·과자·디저트 | 빵 및 과자류 (나머지) | 과자류·빵류 또는 떡류 · 코코아가공품류 · 빙과류 · 당류 · 잼류 | snack |
| 음료 | 음료 및 차류 | 음료류 | snack |
| 유제품 | 유제품류 및 빙과류 | 유가공품류 | snack |
| 샐러드·채소·나물 | 생채·무침류 · 나물·숙채류 · 채소류 | 농산가공식품류 中 채소 | meal (샐러드) / exclude (나물·무침) |
| 과일 | 과일류 | 농산가공식품류 中 과일 | snack |
| 육·수산 가공 | 수·조·어·육류 | 식육가공품 및 포장육 · 수산가공식품류 · 알가공품류 · 두부류 또는 묵류 | meal |
| 김치·절임 | 김치류 · 장아찌·절임류 · 젓갈류 | 절임류 또는 조림류 | **exclude** |
| 소스·양념 | 장류, 양념류 | 조미식품 · 장류 · 식용유지류 | **exclude** |
| 기타 | (해당 없음) | 특수영양식품 · 특수의료용도식품 · 주류 · 기타식품류 · 벌꿀 | exclude |

**군 단위 재배치 (예외 — 이 표가 전부)**

| 군 | 원본 대분류 | 우리 계열 | 이유 |
|---|---|---|---|
| 만두 | 면 및 만두류 | 분식류 | 대체 상황이 면이 아님 |
| 떡볶이 · 순대 · 핫도그 · 어묵 | 흩어짐 | 분식류 | 분식집 상황 |
| 햄버거(=버거) · 피자 · 샌드위치 · 토스트 | 빵 및 과자류 | 버거·피자·샌드위치 | 끼니 메뉴가 디저트와 섞임 |
| 김밥 · 삼각김밥 | 밥류 | 밥류 (유지) | role 만 companion 아닌 meal |

**병합 규칙**: 대표식품명이 같은 음식을 다르게 부르면 하나로 — `버거`+`햄버거` → 햄버거, `과ㆍ채주스`+`과·채주스` → 과채주스. 병합 전 이름은 `source_names` 에 남긴다.

---

## 5. role 유도 규칙 (사람이 라벨링하지 않는다)

순서대로 첫 번째 맞는 것:

```
1. family ∈ {김치·절임, 소스·양념, 기타}                                → exclude
2. family = 샐러드·채소·나물 이고 군명이 샐러드 로 끝나지 않음              → exclude   (나물·무침)
3. family = 밥류 이고 군명 ∈ {쌀밥, 잡곡밥, 현미밥, 흑미밥, 보리밥, 밥}     → companion
4. family ∈ {빵·과자·디저트, 음료, 유제품, 과일}                          → snack
5. 그 외                                                                → meal
```

`companion_group_id`: family ∈ {국·탕·찌개류, 구이·볶음·조림류, 튀김류(돈가스 제외)} → 쌀밥. 나머지 NULL.

예외는 `food_groups.role` 을 직접 UPDATE 하고 `note` 에 이유를 남긴다 (예: 계란찜·계란말이 → exclude, 반찬).
검수 범위: **사용자 기록에 등장한 군(~150개)** 만. 롱테일은 노출되지 않으므로 틀려도 영향 없음.

---

## 6. 삭제 (쳐내기)

| 대상 | 행 수 (실측) | 처리 |
|---|---|---|
| 비대표인데 같은 `normalized_name` 의 대표 행이 있음 | **8,581** | 삭제 |
| `family ∈ {기타}` 이고 `role=exclude` 인 비대표 (식용유지·당류·잼·특수영양·주류) | 수천 (A 후 집계) | 삭제 — 기록 가치 없음 |
| 비대표이고 대표 없음 (100g 유일 행) | 17,729 | **유지** — `serving_basis=per_100g`, 추천 제외, 검색 하위 노출 |
| 대표 행 | 15,680 | 유지 (F 감사 대상 1,365 포함) |

절차 (트랜잭션 하나):
```sql
-- 1) 참조 재지정: 지울 행 → 같은 normalized_name 의 대표 행(survivor)
UPDATE meal_items      SET nutrition_item_id = s.id FROM prune p JOIN survivor s ON … WHERE meal_items.nutrition_item_id = p.id;
UPDATE food_candidates SET nutrition_item_id = s.id … ;   UPDATE favorite_foods … ;
-- 2) 아카이브
INSERT INTO nutrition_items_pruned SELECT ni.*, now(), s.id FROM nutrition_items ni JOIN … ;
-- 3) 삭제
DELETE FROM nutrition_items WHERE id IN (SELECT id FROM prune);
```
**G(군 대표값 계산) 이후에만** 실행한다.

---

## 7. 검증 질의 — 각 단계 끝에 실행, 전부 통과해야 다음 단계

```sql
-- V1 행 수·id 보존: 삭제 단계 전까지 nutrition_items 행 수와 max(id) 가 시작값과 같다
SELECT count(*), max(id) FROM nutrition_items;

-- V2 FK 고아 0 (삭제 후)
SELECT
  (SELECT count(*) FROM meal_items      mi WHERE mi.nutrition_item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM nutrition_items n WHERE n.id = mi.nutrition_item_id)) AS meal_orphans,
  (SELECT count(*) FROM food_candidates fc WHERE fc.nutrition_item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM nutrition_items n WHERE n.id = fc.nutrition_item_id)) AS cand_orphans,
  (SELECT count(*) FROM favorite_foods  ff WHERE ff.nutrition_item_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM nutrition_items n WHERE n.id = ff.nutrition_item_id)) AS fav_orphans;

-- V3 미분류 비율 — 대표 행 기준 < 5%, 사용자 기록 기준 < 10%
SELECT
  round(100.0 * count(*) FILTER (WHERE food_group_id IS NULL) / count(*), 1) AS rep_unclassified_pct
FROM nutrition_items WHERE is_representative;
SELECT
  round(100.0 * count(*) FILTER (WHERE mi.food_group_id IS NULL) / count(*), 1) AS meal_unclassified_pct
FROM meal_items mi JOIN meal_records mr ON mr.id = mi.meal_record_id
WHERE mr.deleted_at IS NULL AND NOT mr.is_skipped;

-- V4 사용자 기록 상위 200 이름 중 미분류 0 (조용한 소실 방지)
SELECT mi.food_name, count(*) AS n
FROM meal_items mi JOIN meal_records mr ON mr.id = mi.meal_record_id
WHERE mr.deleted_at IS NULL AND mi.food_group_id IS NULL
GROUP BY mi.food_name ORDER BY n DESC LIMIT 200;      -- 결과가 비어야 한다 (또는 전부 exclude 대상 이름)

-- V5 role 경계 의심: meal 인데 1인분 < 100kcal / exclude 인데 > 300kcal
SELECT g.name, g.role, g.calories FROM food_groups g
WHERE (g.role = 'meal' AND g.calories < 100) OR (g.role = 'exclude' AND g.calories > 300)
ORDER BY g.role, g.calories;

-- V6 과거 기록 응답 불변: 임의 20건의 상세 응답(항목 이름·kcal·합계)이 변경 전 스냅샷과 같다
--    (SQL 이 아니라 API diff — scripts 에 before/after JSON 덤프 비교로 수행)
-- V7 매칭 회귀 0: 정확도평가 3차 세트(23장) 재실행 → top-1·MAPE 가 변경 전과 같다
```

---

## 8. 엔진 접점 (feat/recommend-engine-v2 에서 바뀌는 곳)

| 지점 | 지금 | 계약 후 |
|---|---|---|
| `signals.group_key` | `normalize_name(food_name)` | `meal_items.food_group_id` (NULL 이면 기존 키로 폴백) |
| `candidates._similarity_pool` | 시드 46 + `gen:*` 42 | `food_groups WHERE role IN ('meal','snack') AND calories IS NOT NULL` — 군 단위 1행 |
| `candidates.nutrient_similar` 종류군 | `dish_type` 어미 60개 | `food_groups.family` 일치 |
| `candidates.meal_worthy` | kcal 하한/상한 휴리스틱 | `role` (휴리스틱은 2차 안전망으로 유지) |
| 밥 동반 | 없음 | `companion_group_id` 기본 + 개인 동시기록(50%↑) 우선, 예산 적합은 메인+동반 합산 |
| `signals._reference_by_name` (오기록 대조) | 이름 → 대표 항목 | `food_groups` 대표값 |
| `feedback.py` | `recommendation_logs.recommended_items` JSON | `recommendation_items` 행 |
| 카드 표시명 | 후보 이름 | 사용자 이력의 상품명 있으면 그것, 없으면 `food_groups.name` |

---

## 9. 마이그레이션 순서 (alembic)

팀원의 `c4d8e21f7a95_add_meal_records_analytics_fields` 다음에 붙인다 (엔진 브랜치 rebase 선행).

```
xxxx_01_food_groups                 food_groups · food_group_aliases
xxxx_02_nutrition_items_group       nutrition_items.food_group_id · serving_basis (+ index)
xxxx_03_meal_items_group            meal_items.food_group_id (+ index)
xxxx_04_recommendation_items        recommendation_items
xxxx_05_nutrition_items_pruned      아카이브 테이블 (삭제 직전)
```
전부 nullable 컬럼·신규 테이블이라 다운타임 없음. 롤백은 역순 DROP.

---

## 10. 열린 결정

| 항목 | 기본값 (합의 없으면 이대로) |
|---|---|
| 순수 밥 군 목록 (companion) | 쌀밥·잡곡밥·현미밥·흑미밥·보리밥·밥 |
| 튀김류의 기본 동반 | 돈가스 → 쌀밥, 닭튀김·감자튀김 → 없음 |
| `base_amount=100` 대표 1,365행 감사 실패 시 | `per_100g` 로 강등, `is_representative` 유지 |
| 100g 유일 행 17,729 삭제 여부 | 유지 (검색 커버리지) |
| `rejected_at` FE 노출 | 컬럼만, UI 는 다음 릴리스 |

관련 문서: `~/Documents/Software maestro/잇로그-음식군-alias-초안.md` (alias 자동 매핑 93%, 검수 목록),
`scripts/report_mfds_taxonomy.py` (원본 분류 분포 리포트).
