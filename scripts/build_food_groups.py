"""음식군(food_groups) 생성·배정 — docs/음식군-DB-계약.md §3 A~G 를 한 번에.

    python -m scripts.build_food_groups --food ref/source/mfds_food.jsonl --processed ref/source/mfds_processed.jsonl
    python -m scripts.build_food_groups ... --dry-run          # DB 쓰기 없이 리포트만
    python -m scripts.build_food_groups ... --skip-macros      # 대표값 계산 생략 (빠른 반복)

하는 일 (멱등 — 다시 돌려도 결과 같음):
  A  식약처 원본(JSONL)에서 대표식품명 → 군, 대분류 → 계열(16), 병합·재배치·role 규칙 적용 → food_groups upsert
  B  alias upsert: 동의어(taxonomy.SYNONYM_ALIASES) + 시드 46 + 수동 JSON(--aliases)
  C  nutrition_items.food_group_id — 식약처 코드(P/D) 행은 코드로, rep:/gen:/시드 행은 이름으로 배정
  F  serving_basis — 대표=per_serving, 비대표=per_100g. 대표인데 base_amount=100 인 행은 NULL(미판정) + 감사 CSV
  G  군 대표 영양값 — per_serving 구성원의 절사평균(상하위 10%)
  끝에 §7 V3·V5 에 해당하는 수치를 찍는다.

DB 스키마·기존 행은 건드리지 않는다: INSERT 는 food_groups/aliases 만, nutrition_items 는 두 컬럼 UPDATE 만.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import FoodGroup, FoodGroupAlias, NutritionItem
from scripts.food_group_taxonomy import (
    DEFAULT_COMPANION,
    EXTRA_GROUPS,
    GROUP_ROLE_OVERRIDE,
    LOW_KCAL_MEAL_TO_EXCLUDE,
    ROLE_MEAL,
    SYNONYM_ALIASES,
    canonical_group,
    companion_for,
    family_for,
    norm,
    resolve_by_suffix,
    role_for,
)

_NA = {"", "해당없음"}
SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "nutrition_items_seed.json"
AUDIT_PATH = Path(__file__).resolve().parents[3] / "ref" / "source" / "serving_basis_audit.csv"


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

    def scan(self, path: Path, kind: str) -> int:
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
                group = canonical_group(raw_group)
                family = family_for(kind, r.get("foodLv3Nm") or "", group)
                self.group_families[group][family] += 1
                self.group_sources[group].add(raw_group)
                self.group_count[group] += 1
                code = (r.get("foodCd") or "").strip()[:40]
                if code:
                    self.code_group[code] = group
                name = r.get("foodNm") or ""
                self.name_group[norm(name)][group] += 1
                if kind == "D" and "_" in name:  # '대표_상세' → 상세명도 색인
                    self.name_group[norm(name.split("_", 1)[1])][group] += 1
                n += 1
        return n

    def family_of(self, group: str) -> str:
        return self.group_families[group].most_common(1)[0][0]

    def group_by_name(self, normalized: str) -> str | None:
        c = self.name_group.get(normalized)
        return c.most_common(1)[0][0] if c else None


def upsert_groups(db: Session, tax: Taxonomy) -> dict[str, FoodGroup]:
    existing = {g.name: g for g in db.scalars(select(FoodGroup))}
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
        g.member_count = tax.group_count[name]
        g.source_names = "|".join(sorted(tax.group_sources[name]))
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
    """alias(정규화) → group_id. 동의어 → 시드 → 수동 JSON 순, 먼저 온 것이 남는다."""
    stats: Counter = Counter()
    existing = {a.alias: a for a in db.scalars(select(FoodGroupAlias))}

    def put(alias: str, group_name: str, kind: str, note: str | None = None) -> None:
        key = norm(alias)[:100]
        g = groups.get(group_name)
        if not key or g is None:
            stats[f"skip_{kind}"] += 1
            return
        row = existing.get(key)
        if row is None:
            db.add(FoodGroupAlias(alias=key, group_id=g.id, kind=kind, note=note))
            existing[key] = FoodGroupAlias(alias=key, group_id=g.id, kind=kind)
        else:
            row.group_id, row.kind, row.note = g.id, kind, note
        stats[kind] += 1

    for alias, group_name in SYNONYM_ALIASES.items():
        put(alias, group_name, "synonym")

    syn_norm = {norm(k): v for k, v in SYNONYM_ALIASES.items()}
    if SEED_PATH.exists():
        for item in json.loads(SEED_PATH.read_text(encoding="utf-8"))["items"]:
            name = item["name"]
            key = norm(name)
            target = syn_norm.get(key) or (name if name in groups else tax.group_by_name(key))
            if target is None:
                target = resolve_by_suffix(key, sorted(groups, key=len, reverse=True))
            if target:
                put(name, target, "seed")
            else:
                stats["skip_seed_unresolved"] += 1
                print(f"  [alias] 시드 미매핑: {name}", file=sys.stderr)

    if manual_path and manual_path.exists():
        for alias, group_name in json.loads(manual_path.read_text(encoding="utf-8")).items():
            put(alias, group_name, "manual")

    db.flush()
    return dict(stats)


# ---------------------------------------------------------------------------
# C. nutrition_items 배정
# ---------------------------------------------------------------------------
def assign_items(db: Session, groups: dict[str, FoodGroup], tax: Taxonomy) -> Counter:
    stats: Counter = Counter()
    alias_map = {a.alias: a.group_id for a in db.scalars(select(FoodGroupAlias))}
    names_by_len = sorted(groups, key=len, reverse=True)
    gid = {name: g.id for name, g in groups.items()}

    rows = db.execute(
        select(NutritionItem.id, NutritionItem.external_id, NutritionItem.normalized_name,
               NutritionItem.name)
    ).all()
    updates: list[dict] = []
    for item_id, ext, normalized, name in rows:
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
            if key in groups:
                group_name, how = key, "group_name"
            else:
                group_name = tax.group_by_name(key)
                how = "name" if group_name else None
                if group_name is None:
                    group_name = resolve_by_suffix(key, names_by_len)
                    how = "suffix" if group_name else None
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
def fill_serving_basis(db: Session, audit_path: Path) -> Counter:
    stats: Counter = Counter()
    rows = db.execute(
        select(NutritionItem.id, NutritionItem.is_representative, NutritionItem.base_amount,
               NutritionItem.base_unit, NutritionItem.name, NutritionItem.calories,
               NutritionItem.total_weight, NutritionItem.external_id)
    ).all()
    updates, audit = [], []
    for item_id, rep, amount, unit, name, kcal, total_w, ext in rows:
        # 대표 행은 전부 1인분 환산본이다 — base_amount=100 인 대표 1,377행을 감사한 결과(2026-09-16)
        # 큐레이션 기준표(케이크·아이스크림 1인분=100g)와 1회섭취참고량 중앙값 100 인 상품이었다.
        # 잘못 승격된 100g 행이 아니므로 per_serving 으로 확정하고, 목록은 기록용 CSV 로만 남긴다.
        basis = "per_serving" if rep else "per_100g"
        if rep and float(amount or 0) == 100 and unit in ("g", "ml"):
            audit.append((item_id, ext, name, kcal, total_w))
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
            w.writerows([*a, ""] for a in audit)
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
        all_members = by_group.get(g.id, [])
        if all_members:
            best_tier = min(_member_tier(m[7], m[8]) for m in all_members)
            members = [m for m in all_members if _member_tier(m[7], m[8]) == best_tier]
        else:
            members = []
        if g.note and g.note.startswith("manual:"):  # EXTRA_GROUPS — 대표값은 규칙표가 정본
            g.member_count = len(all_members)
            stats["manual_kept"] += 1
            continue
        if not members:
            g.calories = g.carbs = g.protein = g.fat = g.base_amount = None
            g.base_unit = None
            stats["no_members"] += 1
            continue
        g.calories = round(_trimmed_mean([float(m[1]) for m in members]), 2)
        g.carbs = round(_trimmed_mean([float(m[2]) for m in members]), 2)
        g.protein = round(_trimmed_mean([float(m[3]) for m in members]), 2)
        g.fat = round(_trimmed_mean([float(m[4]) for m in members]), 2)
        g.base_amount = round(statistics.median([float(m[5]) for m in members]), 2)
        g.base_unit = Counter(m[6] for m in members).most_common(1)[0][0]
        g.member_count = len(all_members)
        stats["filled"] += 1
        # 대표값이 반찬 수준이면 meal 이 아니다 (게조림 10kcal · 무국물 12kcal). 수동 조정(note) 행은 유지
        if (
            g.role == ROLE_MEAL and g.calories < LOW_KCAL_MEAL_TO_EXCLUDE
            and not g.note and g.name not in GROUP_ROLE_OVERRIDE  # 사람이 meal 로 못 박은 군은 유지
        ):
            g.role = "exclude"
            g.note = f"auto: 대표값 {g.calories:.0f}kcal < {LOW_KCAL_MEAL_TO_EXCLUDE:.0f} (반찬)"
            stats["low_kcal_to_exclude"] += 1
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
    print(f"[scan] 음식 {n_d:,} · 가공식품 {n_p:,} → 군 {len(tax.group_count):,} · 코드 {len(tax.code_group):,}")

    with SessionLocal() as db:
        groups = upsert_groups(db, tax)
        print(f"[A] food_groups upsert {len(groups):,}")
        print(f"[B] aliases {upsert_aliases(db, groups, tax, args.aliases)}")
        print(f"[C] nutrition_items 배정 {dict(assign_items(db, groups, tax))}")
        print(f"[F] serving_basis {dict(fill_serving_basis(db, args.audit_out))} → 감사 CSV {args.audit_out}")
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
