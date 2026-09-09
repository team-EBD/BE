"""추천 엔진 v2 미리보기 — 특정 사용자의 후보·점수·최종 3개를 표로 출력한다.

사용법:
    python -m scripts.recommend_preview --user 12 --meal dinner [--mood light] [--now 2026-08-28T18:30]

DB 는 DATABASE_URL 을 따른다 (로컬 덤프 권장). 쓰기는 하지 않는다.
"""
from __future__ import annotations

import argparse
from datetime import datetime

from app.core.database import SessionLocal
from app.core.timeutil import KST, to_utc
from app.services.recommend import recommend
from app.services.recommend.ranking import RankContext, score
from app.services.recommend.signals import recency_penalties


def main() -> None:
    parser = argparse.ArgumentParser(description="추천 엔진 v2 미리보기")
    parser.add_argument("--user", type=int, required=True, help="users.id")
    parser.add_argument("--meal", choices=["breakfast", "lunch", "dinner", "snack"], default=None)
    parser.add_argument("--mood", choices=["any", "light", "hearty"], default="any")
    parser.add_argument("--now", default=None, help="KST, 예: 2026-08-28T18:30 (생략 시 현재)")
    args = parser.parse_args()

    now = to_utc(datetime.fromisoformat(args.now).replace(tzinfo=KST)) if args.now else None

    with SessionLocal() as db:
        result = recommend(db, args.user, meal_type=args.meal, mood=args.mood, now=now)
        b = result.budget
        print(
            f"\n[예산] {result.meal_type} · 목표 {b.goal_calories}kcal × 비율 {b.ratio:.2f}"
            f"({b.ratio_source}) = {b.ratio_budget} · 오늘 남은 {b.remaining_today}"
            f" → 예산 {b.meal_budget} (mood={b.mood}) · 단백질 부족 {b.protein_gap:+.0f}g"
        )
        print(f"[anchor] {', '.join(result.anchors) or '(없음 — 유사 생성기 미동작)'}")

        ctx = RankContext(
            budget=b.meal_budget,
            protein_gap=b.protein_gap,
            recency=recency_penalties(db, args.user, now=now) if now else {},
        )
        max_freq = max((c.freq for c in result.candidates), default=0.0)
        picked = {i.key for i in result.items}
        print(f"\n[후보 {len(result.candidates)}개]  ★ = 최종 선택  (recent = 질림 감점)")
        print(
            f"{'':2}{'출처':<9}{'이름':<18}{'kcal':>6}{'단백':>6}{'freq':>6}{'fit':>6}"
            f"{'prot':>6}{'recent':>7}{'점수':>7}"
        )
        rows = sorted(
            (score(c, ctx, max_freq) for c in result.candidates), key=lambda r: -r.score
        )
        for r in rows:
            c = r.candidate
            mark = "★ " if c.key in picked else "  "
            print(
                f"{mark}{c.source:<9}{c.name[:16]:<18}{c.calories:>6.0f}{c.protein:>6.1f}"
                f"{r.parts['freq']:>6.2f}{r.parts['fit']:>6.2f}{r.parts['protein']:>6.2f}"
                f"{r.parts['recent']:>7.2f}{r.score:>7.3f}"
            )

        print("\n[추천]")
        for i, item in enumerate(result.items, 1):
            print(f"{i}. {item.name} ({item.calories}kcal · {item.budget_label} · {item.source})")
            print(f"   {item.reason}")


if __name__ == "__main__":
    main()
