"""nutrition_items 시드 로더 (JSON → DB).

`seed/nutrition_items_seed.json`(목업 40종)을 `nutrition_items` 테이블에 적재한다.

- **멱등(idempotent)**: `normalized_name` 을 자연키로 upsert 하므로 여러 번 실행해도
  중복 행이 생기지 않는다(id 는 DB auto-increment, 시드 JSON 의 id 는 참조용으로 무시).
- 실행: `python -m scripts.seed_nutrition_items`  (BE 루트에서)

DoD: 실행 후 nutrition_items 40행, 재실행해도 40행 유지.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import NutritionItem

SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "nutrition_items_seed.json"

# nutrition_items 컬럼에 그대로 매핑되는 필드 (JSON 의 id 는 제외 → DB auto-increment)
_FIELDS = (
    "name",
    "normalized_name",
    "base_amount",
    "base_unit",
    "calories",
    "carbs",
    "protein",
    "fat",
    "category",
    "source",
)


def load_items(path: Path = SEED_PATH) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["items"]


def seed(path: Path = SEED_PATH, session_factory=SessionLocal) -> dict[str, int]:
    """시드를 적재하고 {inserted, updated, total} 카운트를 반환한다.

    session_factory 를 주입하면 다른 DB(테스트용 SQLite 등)에도 적용할 수 있다.
    """
    items = load_items(path)
    inserted = updated = 0

    with session_factory() as session:
        for raw in items:
            values = {k: raw.get(k) for k in _FIELDS}
            values.setdefault("source", "seed")

            existing = session.scalar(
                select(NutritionItem).where(
                    NutritionItem.normalized_name == values["normalized_name"]
                )
            )
            if existing is None:
                session.add(NutritionItem(**values))
                inserted += 1
            else:
                for k, v in values.items():
                    setattr(existing, k, v)
                updated += 1

        session.commit()
        count = session.query(NutritionItem).count()

    return {"inserted": inserted, "updated": updated, "total": count}


def main() -> None:
    result = seed()
    print(
        f"[seed_nutrition_items] inserted={result['inserted']} "
        f"updated={result['updated']} total={result['total']}"
    )


if __name__ == "__main__":
    main()
