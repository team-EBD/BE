"""음식군(food_groups) 생성·배정 — docs/음식군-DB-계약.md §3 A~G 를 한 번에.

    python -m scripts.build_food_groups --food ref/source/mfds_food.jsonl --processed ref/source/mfds_processed.jsonl
    python -m scripts.build_food_groups ... --dry-run          # 트랜잭션 실행 후 롤백
    python -m scripts.build_food_groups ... --skip-macros      # 대표값 계산 생략 (빠른 반복)

하는 일 (멱등 — 다시 돌려도 결과 같음):
  A  식약처 원본(JSONL)에서 대표식품명 → 군, 대분류 → 계열(18), 병합·재배치·role 규칙 적용 → food_groups upsert
  B  alias upsert: 동의어(taxonomy.SYNONYM_ALIASES) + 시드 46 + 수동 JSON(--aliases)
  C  nutrition_items.food_group_id — 식약처 코드(P/D) 행은 코드로, rep:/gen:/시드 행은 이름으로 배정
  F  serving_basis — 기존 감사값 보존, 환산 브랜드 원본 대조, 미판정은 NULL + 감사 CSV
  G  군 대표 영양값 — 같은 단위의 per_serving 구성원 밀도를 대표 중량으로 환산
  끝에 §7 V3·V5 에 해당하는 수치를 찍는다.

상품 ID·기록 영양 스냅샷은 보존한다. 군 병합은 FK를 이관하고, 사라진 자동 군은 추천에서 제외한다.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import FoodGroup, FoodGroupAlias, MealItem, NutritionItem, RecommendationItem
from scripts.food_group_taxonomy import (
    BROAD_GROUPS,
    DEFAULT_COMPANION,
    EXTRA_GROUPS,
    GROUP_ROLE_OVERRIDE,
    LOW_KCAL_MEAL_TO_EXCLUDE,
    ROLE_MEAL,
    SYNONYM_ALIASES,
    canonical_group,
    canonical_source_group,
    companion_for,
    family_for,
    norm,
    role_for,
)

_NA = {"", "해당없음"}
SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "nutrition_items_seed.json"
AUDIT_PATH = Path(__file__).resolve().parent.parent / "reports" / "serving_basis_audit.csv"


# ---------------------------------------------------------------------------
# A. 원본 스캔
# ---------------------------------------------------------------------------
class Taxonomy:
    """JSONL 한 번 스캔으로 만드는 색인."""

    def __init__(self) -> None:
        self.group_families: dict[str, Counter] = defaultdict(Counter)  # 군 → 계열 표본
        self.group_sources: dict[str, set] = defaultdict(set)  # 군 → 병합 전 대표식품명
        self.group_count: Counter = Counter()  # 군 → 원본 행 수
        self.code_group: dict[str, str] = {}  # 식품코드 → 군
        self.name_group: dict[str, Counter] = defaultdict(Counter)  # 정규화 식품명 → 군 표본
        self.code_serving: dict[str, tuple[float, str]] = {}
        self.dish_names: dict[str, set[str]] = defaultdict(set)
        self.refined_count = 0

    def scan(self, path: Path, kind: str) -> int:
        from scripts.import_public_nutrition import _parse_amount, display_name_from_raw

        n = 0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                raw_group = r.get("foodLv4Nm") or ""
                if raw_group in _NA:
                    continue
                group = canonical_source_group(kind, raw_group)
                name = r.get("foodNm") or ""
                if kind == "P" and group in BROAD_GROUPS:
                    matches = self.dish_names.get(norm(name), set())
                    if len(matches) == 1:
                        group = next(iter(matches))
                        self.refined_count += 1
                family = family_for(kind, r.get("foodLv3Nm") or "", group)
                if kind == "P" and canonical_source_group(kind, raw_group) in BROAD_GROUPS and group != canonical_source_group(kind, raw_group):
                    family = self.family_of(group)  # 음식편에서 확인된 구체 음식의 계열
                self.group_families[group][family] += 1
                self.group_sources[group].add(raw_group)
                self.group_count[group] += 1
                code = (r.get("foodCd") or "").strip()[:40]
                if code:
                    self.code_group[code] = group
                    serving = _parse_amount(r.get("servSize"))
                    if serving and 1 <= serving[0] <= 300:
                        self.code_serving[code] = serving
                self.name_group[norm(name)][group] += 1
                if kind == "D" and group not in BROAD_GROUPS:
                    self.dish_names[norm(name)].add(group)
                if kind == "D" and "_" in name:
                    # importer와 같은 표시명을 쓴다. 김치찌개_삼겹살에서 삼겹살만
                    # 떼면 구이를 찌개로 오분류하므로 음식 맥락을 유지한다.
                    display = norm(display_name_from_raw(name))
                    self.name_group[display][group] += 1
                    if group not in BROAD_GROUPS:
                        self.dish_names[display].add(group)
                n += 1
        return n

    def family_of(self, group: str) -> str:
        return self.group_families[group].most_common(1)[0][0]

    def group_by_name(self, normalized: str) -> str | None:
        c = self.name_group.get(normalized)
        if not c:
            return None
        group, votes = c.most_common(1)[0]
        return group if votes > sum(c.values()) / 2 else None


def upsert_groups(db: Session, tax: Taxonomy) -> dict[str, FoodGroup]:
    normalized_names: dict[str, str] = {}
    for name in set(tax.group_count) | set(EXTRA_GROUPS):
        key = norm(name)
        if key in normalized_names and normalized_names[key] != name:
            raise ValueError(f"음식군 정규화 충돌: {normalized_names[key]} / {name} — 병합 규칙 확인 필요")
        if not key or len(name) > 50:
            raise ValueError(f"음식군 이름은 공백을 제외한 1~50자여야 합니다: {name!r}")
        normalized_names[key] = name
    existing = {g.name: g for g in db.scalars(select(FoodGroup))}
    # 병합 규칙이 바뀌어도 가능한 한 군 ID를 유지한다. 양쪽이 이미 존재하면
    # 모든 FK를 먼저 옮기며, 기록에 저장된 이름/영양값 스냅샷은 수정하지 않는다.
    for old_name, old in list(existing.items()):
        name = canonical_group(old_name)
        if name == old_name or name not in tax.group_count:
            continue
        target = existing.get(name)
        if target is None:
            old.name = name
            existing[name] = old
        else:
            for model, column in (
                (NutritionItem, NutritionItem.food_group_id),
                (MealItem, MealItem.food_group_id),
                (RecommendationItem, RecommendationItem.food_group_id),
                (FoodGroupAlias, FoodGroupAlias.group_id),
                (FoodGroup, FoodGroup.companion_group_id),
            ):
                db.execute(update(model).where(column == old.id).values({column.key: target.id}))
            db.delete(old)
        del existing[old_name]
    db.flush()
    groups: dict[str, FoodGroup] = {}
    for name in sorted(tax.group_count):
        family = tax.family_of(name)
        role = role_for(family, name)
        g = existing.get(name)
        if g is None:
            g = FoodGroup(name=name[:50], family=family, role=role)
            db.add(g)
        else:
            # 규칙으로 다시 유도한다. 'manual:'(사람 조정) 만 남기고 'auto:'(저칼로리 강등) 는
            # 대표값이 바뀌었을 수 있으니 매번 초기화 → G 단계가 필요하면 다시 붙인다
            g.family = family
            if not g.note or g.note.startswith("auto:"):
                g.role, g.note = role, None
        g.source_names = "|".join(sorted(tax.group_sources[name]))
        if g.role != ROLE_MEAL:
            g.companion_group_id = None
        groups[name] = g
    # 식약처에 없는 우리 군 — 규칙표(EXTRA_GROUPS)가 정본. 대표값도 여기서 확정한다
    for name, spec in EXTRA_GROUPS.items():
        g = groups.get(name) or existing.get(name)
        if g is None:
            g = FoodGroup(name=name[:50])
            db.add(g)
        g.family, g.role, g.note = spec["family"], spec["role"], spec["note"]
        g.calories, g.carbs, g.protein, g.fat = spec["calories"], spec["carbs"], spec["protein"], spec["fat"]
        g.base_amount, g.base_unit = spec["base_amount"], spec["base_unit"]
        g.source_names = g.source_names or "manual"
        groups[name] = g
    # 사라진 자동 군을 남겨 두면 옛 대표값으로 계속 추천된다. FK/기록 ID는
    # 보존하면서 추천만 종료하고, 새 배정 후 구성원 수를 다시 센다.
    for name, g in existing.items():
        if name not in groups and not (g.note or "").startswith("manual:"):
            g.role, g.companion_group_id = "exclude", None
            g.calories = g.carbs = g.protein = g.fat = g.base_amount = None
            g.base_unit = None
            g.member_count = 0
            g.note = "auto: 현재 원본 분류에서 제외된 군 (기록 참조 보존)"
    db.flush()
    # 기본 동반 — 쌀밥 군이 있어야 한다
    rice = groups.get(DEFAULT_COMPANION)
    for name, g in groups.items():
        comp = companion_for(g.family, name, g.role)
        g.companion_group_id = rice.id if (comp and rice) else None
    db.flush()
    return groups


# ---------------------------------------------------------------------------
# B. alias
# ---------------------------------------------------------------------------
def upsert_aliases(
    db: Session, groups: dict[str, FoodGroup], tax: Taxonomy, manual_path: Path | None
) -> dict[str, int]:
    """alias(정규화) → group_id. 수동 지정은 항상 자동 생성보다 우선한다."""
    stats: Counter = Counter()
    existing = {a.alias: a for a in db.scalars(select(FoodGroupAlias))}
    generated: set[str] = set()

    def put(alias: str, group_name: str, kind: str, note: str | None = None) -> None:
        key = norm(alias)
        if len(key) > 100:
            raise ValueError(f"별칭이 100자를 초과합니다: {alias}")
        g = groups.get(group_name)
        if not key or g is None:
            stats[f"skip_{kind}"] += 1
            return
        generated.add(key)
        row = existing.get(key)
        if row is None:
            row = FoodGroupAlias(alias=key, group_id=g.id, kind=kind, note=note)
            db.add(row)
            existing[key] = row
        elif row.kind != "manual" or kind == "manual":
            row.group_id, row.kind, row.note = g.id, kind, note
        stats[kind] += 1

    for alias, group_name in SYNONYM_ALIASES.items():
        put(alias, group_name, "synonym")
    for group_name, sources in tax.group_sources.items():
        for raw_name in sources:
            if canonical_group(raw_name) == group_name and raw_name != group_name:
                put(raw_name, group_name, "synonym")

    syn_norm = {norm(k): v for k, v in SYNONYM_ALIASES.items()}
    group_by_norm = {norm(name): name for name in groups}
    if SEED_PATH.exists():
        for item in json.loads(SEED_PATH.read_text(encoding="utf-8"))["items"]:
            name = item["name"]
            key = norm(name)
            target = syn_norm.get(key) or group_by_norm.get(key) or tax.group_by_name(key)
            if target:
                put(name, target, "seed")
            else:
                stats["skip_seed_unresolved"] += 1
                print(f"  [alias] 시드 미매핑: {name}", file=sys.stderr)

    if manual_path and manual_path.exists():
        for alias, group_name in json.loads(manual_path.read_text(encoding="utf-8")).items():
            put(alias, group_name, "manual")

    for key, row in existing.items():
        if row.kind in ("synonym", "seed") and key not in generated:
            db.delete(row)

    db.flush()
    return dict(stats)


# ---------------------------------------------------------------------------
# C. nutrition_items 배정
# ---------------------------------------------------------------------------
def assign_items(db: Session, groups: dict[str, FoodGroup], tax: Taxonomy) -> Counter:
    stats: Counter = Counter()
    alias_map = {a.alias: a.group_id for a in db.scalars(select(FoodGroupAlias))}
    group_by_norm = {norm(name): name for name in groups}
    gid = {name: g.id for name, g in groups.items()}

    rows = db.execute(
        select(NutritionItem.id, NutritionItem.external_id, NutritionItem.normalized_name,
               NutritionItem.name, NutritionItem.is_representative)
    ).all()
    updates: list[dict] = []
    for item_id, ext, normalized, name, representative in rows:
        if ext and ext.startswith(("rep:", "gen:")) and not representative:
            updates.append({"id": item_id, "food_group_id": None})
            stats["inactive_generated"] += 1
            continue
        group_name: str | None = None
        how = None
        if ext and ext in tax.code_group:  # P/D — 식약처 코드
            group_name, how = tax.code_group[ext], "code"
        else:  # rep:/gen:/시드 — 이름
            key = norm(name) if name else normalized
            if key in alias_map:
                updates.append({"id": item_id, "food_group_id": alias_map[key]})
                stats["alias"] += 1
                continue
            if key in group_by_norm:
                group_name, how = group_by_norm[key], "group_name"
            else:
                group_name = tax.group_by_name(key)
                how = "name" if group_name else None
        if group_name and group_name in gid:
            updates.append({"id": item_id, "food_group_id": gid[group_name]})
            stats[how or "?"] += 1
        else:
            updates.append({"id": item_id, "food_group_id": None})
            stats["unassigned"] += 1

    for chunk_start in range(0, len(updates), 5000):
        db.execute(update(NutritionItem), updates[chunk_start:chunk_start + 5000])
    db.flush()
    return stats


# ---------------------------------------------------------------------------
# F. serving_basis
# ---------------------------------------------------------------------------
def fill_serving_basis(db: Session, audit_path: Path, tax: Taxonomy | None = None) -> Counter:
    stats: Counter = Counter()
    rows = db.execute(
        select(NutritionItem.id, NutritionItem.is_representative, NutritionItem.base_amount,
               NutritionItem.base_unit, NutritionItem.name, NutritionItem.calories,
               NutritionItem.total_weight, NutritionItem.external_id, NutritionItem.serving_basis,
               NutritionItem.category)
    ).all()
    updates, audit = [], []
    for item_id, rep, amount, unit, name, kcal, total_w, ext, current, category in rows:
        # 대표 행은 전부 1인분 환산본이다 — base_amount=100 인 대표 1,377행을 감사한 결과(2026-09-16)
        # 큐레이션 기준표(케이크·아이스크림 1인분=100g)와 1회섭취참고량 중앙값 100 인 상품이었다.
        # 잘못 승격된 100g 행이 아니므로 per_serving 으로 확정하고, 목록은 기록용 CSV 로만 남긴다.
        serving = tax.code_serving.get(ext) if tax else None
        serving_unit_matches = serving and (unit == serving[1] or (
            category == "음료" and unit in ("g", "ml") and serving[1] in ("g", "ml")))
        if not rep and serving and float(amount) == serving[0] and serving_unit_matches:
            basis = "per_serving"  # 이전 빌드가 모든 비대표에 찍은 per_100g도 교정
        elif current == "per_100g" and (float(amount) != 100 or unit not in ("g", "ml")):
            basis = None  # 원본 근거 없는 모순된 기준은 추정하지 않는다.
        elif current is not None:
            basis = current  # 감사로 확정/강등한 값은 재구축으로 덮어쓰지 않는다.
        elif rep:
            basis = "per_serving"  # 기존 큐레이션/시드/rep 생성 경로의 환산 계약
        elif float(amount) == 100 and unit in ("g", "ml"):
            basis = "per_100g"
        else:
            basis = None
        if rep and float(amount or 0) == 100 and unit in ("g", "ml"):
            audit.append((item_id, ext, name, kcal, total_w, basis))
            stats["per_serving_100"] += 1
        updates.append({"id": item_id, "serving_basis": basis})
        stats[basis] += 1
    for chunk_start in range(0, len(updates), 5000):
        db.execute(update(NutritionItem), updates[chunk_start:chunk_start + 5000])
    db.flush()
    if audit:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "external_id", "name", "calories", "total_weight", "decision(per_serving|per_100g)"])
            w.writerows(audit)
    return stats


# ---------------------------------------------------------------------------
# G. 군 대표 영양값
# ---------------------------------------------------------------------------
def _trimmed_mean(values: list[float]) -> float:
    if len(values) < 5:
        return statistics.median(values)
    k = max(1, len(values) // 10)
    s = sorted(values)[k:-k]
    return statistics.fmean(s)


def _member_tier(source: str | None, external_id: str | None) -> int:
    """대표값 재료 우선순위 — 시드·총칭(0) → 음식편 큐레이션(1) → 가공식품 동명 대표(2) → 그 외(3).

    가공식품 동명 대표(rep:)는 즉석 파우치라 1인분이 작다(즉석 김치찌개 150g ≈ 200kcal).
    음식편(식당·가정식 1인분)과 섞어 평균하면 김치찌개 군이 207kcal 로 내려갔다(로컬 실측).
    같은 군에 여러 계층이 있으면 **가장 앞 계층만** 쓴다.
    """
    ext = external_id or ""
    if source == "seed" or ext.startswith("gen:"):
        return 0
    if ext.startswith("D"):
        return 1
    if ext.startswith("rep:"):
        return 2
    return 3


def fill_group_macros(db: Session, groups: dict[str, FoodGroup]) -> Counter:
    stats: Counter = Counter()
    counts = dict(db.execute(select(NutritionItem.food_group_id, func.count())
                            .group_by(NutritionItem.food_group_id)).all())
    rows = db.execute(
        select(NutritionItem.food_group_id, NutritionItem.calories, NutritionItem.carbs,
               NutritionItem.protein, NutritionItem.fat, NutritionItem.base_amount,
               NutritionItem.base_unit, NutritionItem.source, NutritionItem.external_id)
        .where(NutritionItem.serving_basis == "per_serving", NutritionItem.food_group_id.isnot(None))
    ).all()
    by_group: dict[int, list] = defaultdict(list)
    for r in rows:
        by_group[r[0]].append(r)
    for g in groups.values():
        g.member_count = counts.get(g.id, 0)
        all_members = by_group.get(g.id, [])
        all_members = [m for m in all_members if m[5] and float(m[5]) > 0
                       and all(v is not None and float(v) >= 0 for v in m[1:5])]
        if all_members:
            best_tier = min(_member_tier(m[7], m[8]) for m in all_members)
            members = [m for m in all_members if _member_tier(m[7], m[8]) == best_tier]
        else:
            members = []
        if g.note and g.note.startswith("manual:"):  # EXTRA_GROUPS — 대표값은 규칙표가 정본
            stats["manual_kept"] += 1
            continue
        if not members:
            g.calories = g.carbs = g.protein = g.fat = g.base_amount = None
            g.base_unit = None
            stats["no_members"] += 1
            continue
        # g와 ml를 섞어 평균하지 않는다. 대표 중량에 밀도를 환산해 중량과 영양값을 맞춘다.
        g.base_unit = Counter(m[6] for m in members).most_common(1)[0][0]
        members = [m for m in members if m[6] == g.base_unit]
        g.base_amount = round(statistics.median([float(m[5]) for m in members]), 2)
        for column, index in (("calories", 1), ("carbs", 2), ("protein", 3), ("fat", 4)):
            setattr(g, column, round(_trimmed_mean([float(m[index]) / float(m[5]) for m in members])
                                     * g.base_amount, 2))
        stats["filled"] += 1

    # 모든 군의 대표값을 먼저 계산한다. 입력 순서에 따라 아직 계산되지 않은 쌀밥을
    # 읽으면 같은 음식이 재구축할 때마다 meal/exclude로 바뀔 수 있다.
    rice = groups.get(DEFAULT_COMPANION)
    for g in groups.values():
        comp = companion_for(g.family, g.name, g.role)
        companion = rice if comp and rice and rice.role == "companion" else None
        companion_calories = 0.0
        if companion and all(
            value is not None and math.isfinite(float(value)) and float(value) >= 0
            for value in (companion.calories, companion.carbs, companion.protein, companion.fat)
        ):
            companion_calories = float(companion.calories)
        total_calories = float(g.calories or 0) + companion_calories
        # 식사 후보와 같은 기준: 기본 밥을 포함해도 너무 작을 때만 자동 제외한다.
        # 명시 반찬/원재료의 exclude 역할은 밥으로 복구하지 않는다. 수동 판정도 유지한다.
        if (
            g.role == ROLE_MEAL and g.calories is not None and total_calories < LOW_KCAL_MEAL_TO_EXCLUDE
            and not g.note and g.name not in GROUP_ROLE_OVERRIDE  # 사람이 meal 로 못 박은 군은 유지
        ):
            g.role = "exclude"
            g.note = f"auto: 동반 포함 대표값 {total_calories:.0f}kcal < {LOW_KCAL_MEAL_TO_EXCLUDE:.0f} (소량)"
            stats["low_kcal_to_exclude"] += 1
        g.companion_group_id = companion.id if companion and g.role == ROLE_MEAL else None
    db.flush()
    return stats


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def report(db: Session) -> None:
    from sqlalchemy import func

    total_groups = db.scalar(select(func.count()).select_from(FoodGroup))
    roles = dict(db.execute(select(FoodGroup.role, func.count()).group_by(FoodGroup.role)).all())
    fams = db.execute(
        select(FoodGroup.family, func.count()).group_by(FoodGroup.family).order_by(func.count().desc())
    ).all()
    rep_total = db.scalar(select(func.count()).where(NutritionItem.is_representative.is_(True)))
    rep_null = db.scalar(
        select(func.count()).where(NutritionItem.is_representative.is_(True), NutritionItem.food_group_id.is_(None))
    )
    print(f"\n[군] {total_groups:,}개 · role {roles}")
    print("[계열] " + " · ".join(f"{f} {n}" for f, n in fams))
    print(f"[V3] 대표 행 미분류 {rep_null:,}/{rep_total:,} ({100 * rep_null / max(rep_total, 1):.1f}%) — 기준 < 5%")
    suspects = db.execute(
        select(FoodGroup.name, FoodGroup.role, FoodGroup.calories).where(
            ((FoodGroup.role == ROLE_MEAL) & (FoodGroup.calories < 100))
            | ((FoodGroup.role == "exclude") & (FoodGroup.calories > 300))
        ).order_by(FoodGroup.role, FoodGroup.calories)
    ).all()
    print(f"[V5] role 경계 의심 {len(suspects)}건: " + ", ".join(f"{n}({r},{c:.0f})" for n, r, c in suspects[:25]))


def main() -> None:
    ap = argparse.ArgumentParser(description="음식군 생성·배정 (docs/음식군-DB-계약.md §3)")
    ap.add_argument("--food", type=Path, required=True, help="음식편 JSONL")
    ap.add_argument("--processed", type=Path, required=True, help="가공식품편 JSONL")
    ap.add_argument("--aliases", type=Path, default=None, help="수동 alias JSON {alias: 군명}")
    ap.add_argument("--audit-out", type=Path, default=AUDIT_PATH)
    ap.add_argument("--skip-macros", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tax = Taxonomy()
    n_d = tax.scan(args.food, "D")
    n_p = tax.scan(args.processed, "P")
    print(f"[scan] 음식 {n_d:,} · 가공식품 {n_p:,} → 군 {len(tax.group_count):,} · 코드 {len(tax.code_group):,}"
          f" · 포괄 분류 세분 {tax.refined_count:,}")

    with SessionLocal() as db:
        groups = upsert_groups(db, tax)
        print(f"[A] food_groups upsert {len(groups):,}")
        print(f"[B] aliases {upsert_aliases(db, groups, tax, args.aliases)}")
        print(f"[C] nutrition_items 배정 {dict(assign_items(db, groups, tax))}")
        print(f"[F] serving_basis {dict(fill_serving_basis(db, args.audit_out, tax))} → 감사 CSV {args.audit_out}")
        if not args.skip_macros:
            print(f"[G] 군 대표값 {dict(fill_group_macros(db, groups))}")
        report(db)
        if args.dry_run:
            db.rollback()
            print("\n[dry-run] 롤백함 — DB 변경 없음")
        else:
            db.commit()
            print("\n[commit] 완료")


if __name__ == "__main__":
    main()
