"""추천 엔진 v2 미리보기 — 특정 사용자의 후보·점수·최종 3개를 표로 출력한다.

사용법:
    python -m scripts.recommend_preview --user 12 --meal dinner [--mood light] [--now 2026-08-28T18:30]

DB 는 DATABASE_URL 을 따른다 (로컬 덤프 권장). 쓰기는 하지 않는다.
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime

from app.core.database import SessionLocal
from app.core.timeutil import now_utc, to_utc
from app.services.recommend import recommend
from app.services.recommend.feedback import acceptance_rates
from app.services.recommend.ranking import RankContext, score
from app.services.recommend.signals import recency_penalties


def main() -> None:
    parser = argparse.ArgumentParser(description="추천 엔진 v2 미리보기")
    parser.add_argument("--user", type=int, required=True, help="users.id")
    parser.add_argument("--meal", choices=["breakfast", "lunch", "dinner", "snack"], default=None)
    parser.add_argument("--mood", choices=["any", "light", "hearty"], default="any")
    parser.add_argument("--now", default=None, help="KST, 예: 2026-08-28T18:30 (생략 시 현재)")
    parser.add_argument("--seed", type=int, default=0, help="미리보기 탐색 난수 (기본 0, 같은 입력 재현)")
    args = parser.parse_args()

    now = to_utc(datetime.fromisoformat(args.now)) if args.now else now_utc()

    with SessionLocal() as db:
        result = recommend(db, args.user, meal_type=args.meal, mood=args.mood, now=now, rng=random.Random(args.seed))
        b = result.budget
        print(
            f"\n[예산] {result.meal_type} · 목표 {b.goal_calories}kcal × 비율 {b.ratio:.2f}"
            f"({b.ratio_source}) = {b.ratio_budget} · 오늘 남은 {b.remaining_today}"
            f" → 예산 {b.meal_budget} (mood={b.mood}) · 단백질 부족 {b.protein_gap:+.0f}g"
        )
        print(f"[anchor] {', '.join(result.anchors) or '(없음 — 유사 생성기 미동작)'}")
        print(f"[음식군] {'사용 (군 키·음식 형태 우선 유사도·동반 합산)' if result.groups_enabled else '없음 (이름 키 폴백)'}")
        d = result.decision
        print(f"[생성기] {d['generator_counts']} · 거절 제외 {d['excluded_count']}")
        print(f"[밴딧] {d['policy_version']} · 학습 {d['model']['samples']}건 · 마지막 카드 탐색 {d['epsilon']:.0%}")

        ctx = RankContext(  # 엔진과 같은 문맥을 다시 만들어 후보 전체의 점수를 보여준다
            budget=b.meal_budget,
            protein_gap=b.protein_gap,
            recency=recency_penalties(db, args.user, now=now),
            acceptance={**acceptance_rates(db, now=now), **acceptance_rates(db, args.user, now=now)},
        )
        picked = {i.key: i for i in result.items}
        print(f"\n[후보 {len(result.candidates)}개]  ★ = 최종 선택 · 기본 점수순")
        print("  합산/합산P = 메인+동반 kcal/g · freq/pop/sim/cf/rel = 빈도/인기/유사도/협업/선호 근거")
        print("  recent = 질림 감점 · 기본 = 다양성 적용 전 · 차감/최종 = 선택된 메뉴의 다양성 감점/최종 점수")
        print(
            f"{'':2}{'출처':<9}{'이름':<18}{'계열':<12}{'동반':<8}{'kcal':>6}{'합산':>6}{'합산P':>6}"
            f"{'freq':>6}{'pop':>6}{'sim':>6}{'cf':>6}{'rel':>6}{'fit':>6}{'prot':>6}"
            f"{'accept':>7}{'recent':>7}{'기본':>7}{'차감':>7}{'최종':>7}"
        )
        rows = sorted(
            (score(c, ctx) for c in result.candidates), key=lambda r: (-r.score, r.candidate.key)
        )
        base_scores = {r.candidate.key: r.score for r in rows}
        for r in rows:
            c = r.candidate
            selected = picked.get(c.key)
            mark = "★ " if selected else "  "
            diversity = f"{selected.parts['diversity']:.3f}" if selected else "-"
            final = f"{selected.score:.3f}" if selected else "-"
            print(
                f"{mark}{c.source:<9}{c.name[:16]:<18}{(c.family or '-')[:10]:<12}"
                f"{(c.companion_name or '-')[:6]:<8}{c.calories:>6.0f}{c.total_calories:>6.0f}"
                f"{c.total_protein:>6.1f}{r.parts['freq']:>6.2f}{r.parts['popularity']:>6.2f}"
                f"{r.parts['similarity']:>6.2f}{r.parts['collaborative']:>6.2f}{r.parts['relevance']:>6.2f}{r.parts['fit']:>6.2f}"
                f"{r.parts['protein']:>6.2f}{r.parts.get('accept', 0.0):>7.2f}"
                f"{r.parts['recent']:>7.2f}{r.score:>7.3f}{diversity:>7}{final:>7}"
            )

        print("\n[추천]")
        for i, item in enumerate(result.items, 1):
            with_companion = f" + {item.companion_name} {item.companion_kcal} = {item.total_calories}" if item.companion_name else ""
            group = f" · 군 {item.group_name}/{item.family}" if item.group_name else ""
            print(f"{i}. {item.name} ({item.calories}kcal{with_companion} · {item.budget_label} · {item.source}{group})")
            print(f"   기본 {base_scores[item.key]:.3f} · 다양성 차감 {item.parts['diversity']:.3f} · 정책 점수 {item.score:.3f} · 조건부 선택 확률 {item.selection_probability:.4f}")
            print(f"   {item.reason}")


if __name__ == "__main__":
    main()
