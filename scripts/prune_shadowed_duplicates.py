"""가려진 중복 행 정리 — 어떤 검색으로도 닿을 수 없는 비대표 행을 지운다.

사용법 (모든 적재 스크립트를 돌린 뒤 마지막에):
    python -m scripts.prune_shadowed_duplicates

왜 필요한가 (2026-08-04 PM 지적: "달걀찜·달걀조림이 3개씩 들어가 있다")
--------------------------------------------------------------------
음식편 조사 데이터는 같은 음식이 조사 유형(가정식·급식·외식 등)별로 행이 따로 있다.
큐레이션은 그중 1건만 대표로 승격하고 나머지는 비대표로 남기는데, 검색이 동명을
대표 1건으로 접기 때문에 **같은 이름의 대표가 있는 비대표 행은 화면에 나올 길이 없다**.

단, 브랜드가 있는 행은 지우면 안 된다 — 검색이 brand 컬럼도 매칭하므로
"피자헛"을 검색하면 (동명 대표에 가려져 있던) 피자헛 불고기피자가 그 질의에서는
파티션 최상위로 떠오른다. 즉:

    삭제 = 비대표 AND 브랜드 없음 AND 같은 이름의 대표 존재
           (이름 검색 → 대표에 접힘 / 브랜드 검색 → 매칭 자체가 안 됨 / AI 매칭 → 대표만)

원본은 CSV·JSONL 에 그대로 있으므로 재적재하면 언제든 복원된다.
"""
from __future__ import annotations

import argparse

from sqlalchemy import delete, or_, select

from app.core.database import SessionLocal
from app.models import NutritionItem


def run(session_factory=SessionLocal) -> int:
    with session_factory() as session:
        rep_names = select(NutritionItem.normalized_name).where(
            NutritionItem.is_representative.is_(True)
        )
        result = session.execute(
            delete(NutritionItem).where(
                NutritionItem.is_representative.is_(False),
                or_(NutritionItem.brand.is_(None), NutritionItem.brand == ""),
                NutritionItem.normalized_name.in_(rep_names),
            )
        )
        session.commit()
        return result.rowcount


def main() -> None:
    argparse.ArgumentParser(description="가려진 중복 비대표 행 정리").parse_args()
    deleted = run()
    print(f"[prune] 삭제 {deleted}건 (대표에 가려져 어떤 검색으로도 닿을 수 없던 비대표 행)")


if __name__ == "__main__":
    main()
