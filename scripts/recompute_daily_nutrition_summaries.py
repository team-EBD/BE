"""자정 경계로 저장된 일간 영양 캐시를 KST 06시 논리 날짜로 일회성 재계산한다.

사용법 (배포 후, 먼저 미리보기):
    python -m scripts.recompute_daily_nutrition_summaries
    python -m scripts.recompute_daily_nutrition_summaries --dry-run
    python -m scripts.recompute_daily_nutrition_summaries --apply
    python -m scripts.recompute_daily_nutrition_summaries --apply --since-user-id 1234

이전 날짜에 살아 있는 식단이 더는 없더라도 기존 캐시 행은 삭제하지 않고 0으로
갱신한다. 기존 (사용자, 날짜) 키를 유지하면서 실제 합계와 요약 문구를 바로잡고,
서비스의 recompute_daily_summary 경로만 사용하기 위해서다.

대상은 사용자별 기존 캐시 날짜와 살아 있는 식단의 새 논리 날짜의 합집합이다.
기본 모드는 읽기 전용 미리보기이며 --apply 때도 전체 변경 계획을 먼저 출력한다.
적용은 사용자마다 커밋하므로 중단 시 마지막 완료 사용자 다음 ID부터 재개할 수 있다.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from typing import TextIO

from sqlalchemy import select, union
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.timeutil import kst_date_of
from app.models import DailyNutritionSummary, MealRecord
from app.services.summary import (
    aggregate_day,
    build_summary_text,
    get_goals,
    recompute_daily_summary,
)


@dataclass
class Counts:
    users: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0

    def add(self, other: Counts) -> None:
        self.users += other.users
        self.created += other.created
        self.updated += other.updated
        self.unchanged += other.unchanged


def _user_batch(db: Session, after_id: int, batch_size: int) -> list[int]:
    """캐시 또는 살아 있는 식단이 있는 사용자만 ID 순으로 가져온다."""
    targets = union(
        select(DailyNutritionSummary.user_id.label("user_id")).where(
            DailyNutritionSummary.user_id > after_id
        ),
        select(MealRecord.user_id.label("user_id")).where(
            MealRecord.user_id > after_id,
            MealRecord.deleted_at.is_(None),
        ),
    ).subquery()
    return list(db.scalars(
        select(targets.c.user_id).order_by(targets.c.user_id).limit(batch_size)
    ))


def _planned_values(db: Session, user_id: int, day: date, goals: dict) -> dict:
    """운영 집계·문구 함수를 호출해 읽기 전용 변경 계획을 만든다."""
    total = aggregate_day(db, user_id, day)
    return {
        "total_calories": total["calories"],
        "total_carbs": total["carbs"],
        "total_protein": total["protein"],
        "total_fat": total["fat"],
        "meal_count": total["meal_count"],
        "summary_text": build_summary_text(total, goals, total["meal_count"]),
    }


def _changed(existing: DailyNutritionSummary, planned: dict) -> bool:
    return (
        any(float(getattr(existing, key)) != planned[key] for key in (
            "total_calories", "total_carbs", "total_protein", "total_fat"
        ))
        or existing.meal_count != planned["meal_count"]
        or existing.summary_text != planned["summary_text"]
    )


def _describe(day: date, action: str, existing: DailyNutritionSummary | None, planned: dict) -> str:
    old = "없음" if existing is None else (
        f"kcal={float(existing.total_calories):.2f}, 탄={float(existing.total_carbs):.2f}, "
        f"단={float(existing.total_protein):.2f}, 지={float(existing.total_fat):.2f}, "
        f"끼니={existing.meal_count}"
    )
    new = (
        f"kcal={planned['total_calories']:.2f}, 탄={planned['total_carbs']:.2f}, "
        f"단={planned['total_protein']:.2f}, 지={planned['total_fat']:.2f}, "
        f"끼니={planned['meal_count']}"
    )
    description = f"  {day} {action}: {old} -> {new}"
    old_text = existing.summary_text if existing is not None else None
    if old_text != planned["summary_text"]:
        description += f", 문구={old_text!r} -> {planned['summary_text']!r}"
    return description


def _process_user(
    db: Session, user_id: int, *, apply: bool, out: TextIO, show_details: bool
) -> Counts:
    existing = {
        row.summary_date: row for row in db.scalars(
            select(DailyNutritionSummary).where(DailyNutritionSummary.user_id == user_id)
        )
    }
    meal_days = {
        kst_date_of(eaten_at, settings.day_start_hour)
        for eaten_at in db.scalars(
            select(MealRecord.eaten_at).where(
                MealRecord.user_id == user_id,
                MealRecord.deleted_at.is_(None),
            ).execution_options(yield_per=1000)
        )
    }
    days = sorted(existing.keys() | meal_days)
    goals = get_goals(db, user_id)
    counts = Counts(users=1)
    for day in days:
        planned = _planned_values(db, user_id, day, goals)
        current = existing.get(day)
        if current is None:
            action = "CREATE"
            counts.created += 1
        elif _changed(current, planned):
            action = "UPDATE"
            counts.updated += 1
        else:
            counts.unchanged += 1
            continue
        if show_details:
            print(_describe(day, action, current, planned), file=out)
        if apply:
            recompute_daily_summary(db, user_id, day)
    return counts


def _scan(
    session_factory, *, apply: bool, since_user_id: int, batch_size: int,
    out: TextIO, show_details: bool,
) -> Counts:
    totals = Counts()
    after_id = since_user_id - 1
    batch_number = 0
    while True:
        with session_factory() as db:
            user_ids = _user_batch(db, after_id, batch_size)
        if not user_ids:
            break
        batch_number += 1
        print(f"배치 {batch_number}: 사용자 {user_ids[0]}~{user_ids[-1]} ({len(user_ids)}명)", file=out)
        for user_id in user_ids:
            with session_factory() as db:
                counts = _process_user(
                    db, user_id, apply=apply, out=out, show_details=show_details
                )
                if apply:
                    db.commit()
            totals.add(counts)
            print(
                f"  사용자 {user_id} 완료: 생성 {counts.created}, 수정 {counts.updated}, "
                f"동일 {counts.unchanged}" +
                (f" (재개: --since-user-id {user_id + 1})" if apply else ""),
                file=out,
            )
        after_id = user_ids[-1]
    print(
        f"합계: 사용자 {totals.users}, 생성 {totals.created}, "
        f"수정 {totals.updated}, 동일 {totals.unchanged}",
        file=out,
    )
    return totals


def run(
    session_factory=SessionLocal, *, apply: bool = False, since_user_id: int = 1,
    batch_size: int = 100, out: TextIO = sys.stdout,
) -> Counts:
    if settings.day_start_hour != 6:
        raise ValueError(f"이 스크립트는 KST 06시 경계 전용입니다: day_start_hour={settings.day_start_hour}")
    if since_user_id < 1 or batch_size < 1:
        raise ValueError("since_user_id와 batch_size는 양수여야 합니다")

    print(f"[미리보기] KST {settings.day_start_hour:02d}시 경계, DB 쓰기 없음", file=out)
    preview = _scan(
        session_factory, apply=False, since_user_id=since_user_id,
        batch_size=batch_size, out=out, show_details=True,
    )
    if not apply:
        print("실제 반영: --apply를 명시하세요.", file=out)
        return preview
    print("[적용] 사용자별 커밋 시작", file=out)
    return _scan(
        session_factory, apply=True, since_user_id=since_user_id,
        batch_size=batch_size, out=out, show_details=False,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="읽기 전용 미리보기 (기본값)")
    mode.add_argument("--apply", dest="apply", action="store_true", help="미리보기 후 사용자별 커밋")
    parser.set_defaults(apply=False)
    parser.add_argument("--since-user-id", type=int, default=1, help="재개할 첫 사용자 ID (포함)")
    parser.add_argument("--batch-size", type=int, default=100, help="사용자 ID 조회 배치 크기")
    args = parser.parse_args(argv)
    run(apply=args.apply, since_user_id=args.since_user_id, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
