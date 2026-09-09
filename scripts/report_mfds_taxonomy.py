"""식약처 원본 분류(대분류·대표식품명·중분류)가 추천 '군'으로 쓸 만한지 확인하는 리포트.

DB 를 건드리지 않는다 — fetch_mfds_api 가 저장한 원본 JSONL 만 읽어 분포를 뽑는다.
재적재(컬럼 추가) 전에 "분류가 우리가 원하는 단위인가"를 데이터로 판단하는 용도.

사용법:
    python -m scripts.report_mfds_taxonomy ref/source/mfds_food.jsonl ref/source/mfds_processed.jsonl
    python -m scripts.report_mfds_taxonomy ref/source/mfds_food.jsonl --lookup 와퍼,김치찌개,공기밥,김치
    python -m scripts.report_mfds_taxonomy ... --wide 200 --top 30

보는 법 (출력 마지막에 같은 기준을 다시 찍는다):
  1. 대표식품명 총수가 300~500 근처인가 — 너무 많으면 군이 아니라 상품 수준, 너무 적으면 넓다
  2. --wide 이상 항목을 가진 대표식품명이 몇 개인가 — 그 군만 중분류로 한 단계 내리면 된다
  3. 가공식품(P)의 대표식품명 결측률 — 브랜드 상품에도 군이 붙는지
  4. --lookup 결과 — 와퍼가 '버거' 아래인지, 김치·칠리소스가 어느 대분류인지 (exclude 규칙 근거)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from scripts.import_mfds_api import API_TO_CSV

# 우리가 보는 컬럼 (표준데이터 CSV 컬럼명 기준). JSONL 은 API 필드명이라 API_TO_CSV 로 되돌린다.
COL_CODE, COL_NAME, COL_KIND = "식품코드", "식품명", "데이터구분코드"
COL_L3, COL_L4, COL_L5, COL_L6 = "식품대분류명", "대표식품명", "식품중분류명", "식품소분류명"
KIND_LABEL = {"D": "음식(D)", "P": "가공식품(P)"}
DEFAULT_LOOKUP = "와퍼,불고기버거,김치찌개,공기밥,잡곡밥,김치,칠리소스,크림치즈,아메리카노,새우깡"


def _normalize(row: dict) -> dict:
    """API 필드명(foodLv4Nm …) 또는 CSV 컬럼명(대표식품명 …) 어느 쪽이든 CSV 컬럼명으로."""
    if COL_NAME in row:  # 이미 CSV 컬럼명
        return row
    out = {csv: (row.get(api) or "") for csv, api in API_TO_CSV.items()}
    # 제조사 — API 필드명이 버전마다 달라 느슨하게 찾는다
    maker = next((v for k, v in row.items() if "mkr" in k.lower() or "제조사" in k), "")
    out["제조사명"] = maker or row.get("제조사명", "")
    return out


def _iter_rows(paths: list[Path]):
    for path in paths:
        if not path.exists():
            sys.exit(f"파일이 없습니다: {path}")
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield _normalize(json.loads(line))


def main() -> None:
    ap = argparse.ArgumentParser(description="식약처 분류 분포 리포트 (DB 무관)")
    ap.add_argument("paths", nargs="+", type=Path, help="fetch_mfds_api 가 만든 JSONL")
    ap.add_argument("--lookup", default=DEFAULT_LOOKUP, help="쉼표로 구분한 음식명 — 분류 표본 출력")
    ap.add_argument("--wide", type=int, default=200, help="이 개수 이상이면 '너무 넓은 군'으로 표시")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    total = 0
    by_kind = Counter()
    missing_l4 = Counter()  # kind → 대표식품명 결측 수
    l3_items = Counter()
    l3_groups: dict[str, set] = defaultdict(set)
    l4_items = Counter()
    l4_l3: dict[str, Counter] = defaultdict(Counter)  # 대표식품명이 여러 대분류에 걸치는지
    l4_l5: dict[str, set] = defaultdict(set)  # 대표식품명 아래 중분류 수 (넓은 군을 내릴 때 기준)
    lookups = [s.strip() for s in args.lookup.split(",") if s.strip()]
    samples: dict[str, list] = {q: [] for q in lookups}

    for row in _iter_rows(args.paths):
        total += 1
        kind = (row.get(COL_KIND) or "?")[:1]
        by_kind[kind] += 1
        l3, l4, l5 = row.get(COL_L3, ""), row.get(COL_L4, ""), row.get(COL_L5, "")
        if not l4:
            missing_l4[kind] += 1
        l3_items[l3] += 1
        if l4:
            l3_groups[l3].add(l4)
            l4_items[l4] += 1
            l4_l3[l4][l3] += 1
            if l5:
                l4_l5[l4].add(l5)
        name = row.get(COL_NAME, "")
        for q in lookups:
            if q in name and len(samples[q]) < 6:
                samples[q].append(row)

    print(f"\n총 {total:,}행 · " + " · ".join(f"{KIND_LABEL.get(k, k)} {n:,}" for k, n in by_kind.items()))

    print("\n[1] 데이터구분별 대표식품명 결측률  ← 브랜드 상품에도 군이 붙는가")
    for kind, n in by_kind.items():
        miss = missing_l4[kind]
        print(f"  {KIND_LABEL.get(kind, kind):<10} 결측 {miss:,}/{n:,} ({100 * miss / max(n, 1):.1f}%)")

    print(f"\n[2] 대분류(계열) {len(l3_items)}개 — 대표식품명 수 / 항목 수")
    for l3, n in l3_items.most_common():
        print(f"  {l3 or '(없음)':<22} 대표식품명 {len(l3_groups[l3]):>4}  항목 {n:>7,}")

    singles = sum(1 for c in l4_items.values() if c == 1)
    wide = [(g, c) for g, c in l4_items.items() if c >= args.wide]
    print(f"\n[3] 대표식품명(군) 총 {len(l4_items):,}개 · 항목 1개짜리 {singles:,}개 · {args.wide}개 이상 {len(wide)}개")
    print(f"  상위 {args.top}:")
    for g, c in l4_items.most_common(args.top):
        spread = "" if len(l4_l3[g]) == 1 else f"  ⚠ 대분류 {len(l4_l3[g])}곳에 걸침"
        print(f"    {g:<16} {c:>6,}  (중분류 {len(l4_l5[g])}개){spread}")
    if wide:
        print(f"  → {args.wide}개 이상인 군은 중분류로 한 단계 내릴 후보: "
              + ", ".join(f"{g}({c:,}→중분류 {len(l4_l5[g])})" for g, c in sorted(wide, key=lambda x: -x[1])))

    print("\n[4] 이름으로 찍어 본 분류 표본  ← 와퍼→버거? 김치·소스는 어느 대분류?")
    for q in lookups:
        rows = samples[q]
        print(f"  '{q}' ({len(rows)}건 표본)")
        if not rows:
            print("    (없음)")
        for r in rows:
            maker = r.get("제조사명") or ""
            print(f"    {KIND_LABEL.get((r.get(COL_KIND) or '?')[:1], '?'):<8} "
                  f"{r.get(COL_L3, ''):<14} > {r.get(COL_L4, ''):<10} > {r.get(COL_L5, ''):<12} | "
                  f"{r.get(COL_NAME, '')[:28]}{' · ' + maker if maker else ''}")

    print("\n[판단 기준]")
    print(f"  · 군(대표식품명) 300~500 이면 그대로 쓴다 — 현재 {len(l4_items):,}")
    print(f"  · {args.wide}개 이상 군 {len(wide)}개만 중분류로 내린다 (전체 기준을 바꾸지 않는다)")
    p_total, p_miss = by_kind.get("P", 0), missing_l4.get("P", 0)
    if p_total:
        print(f"  · 가공식품 결측 {100 * p_miss / p_total:.1f}% — 10% 넘으면 브랜드 상품은 어미 규칙 보완 필요")
    print("  · [4]에서 김치류·장류·양념류 같은 대분류가 확인되면 그 대분류가 exclude 규칙의 근거")


if __name__ == "__main__":
    main()
