"""음식군 조회 계층 — food_groups · food_group_aliases 를 한 번 읽어 엔진 전체가 공유한다.

docs/음식군-DB-계약.md §8. 군이 아직 없는 DB(테스트 시드·마이그레이션 전)에서는 빈 색인이
돌아가고 엔진은 이름 키(normalize_name)로 폴백한다 — 그래서 군이 있든 없든 같은 코드가 돈다.

키 규칙: 군이 있으면 normalize_name(군명), 없으면 normalize_name(기록 이름).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FoodGroup, FoodGroupAlias
from app.services.matching import normalize_name

ROLE_MEAL, ROLE_COMPANION, ROLE_SNACK, ROLE_EXCLUDE = "meal", "companion", "snack", "exclude"
RECOMMENDABLE_ROLES = frozenset({ROLE_MEAL, ROLE_SNACK})


@dataclass(frozen=True)
class GroupInfo:
    id: int
    name: str
    key: str  # normalize_name(name) — 엔진 후보 키
    family: str
    role: str
    companion_id: int | None
    calories: float | None  # 1인분 대표값. None 이면 유사 후보 풀에서 제외
    carbs: float | None
    protein: float | None
    fat: float | None

    @property
    def has_macros(self) -> bool:
        return self.calories is not None


class GroupIndex:
    def __init__(self, groups: list[GroupInfo], aliases: dict[str, int]) -> None:
        self.by_id: dict[int, GroupInfo] = {g.id: g for g in groups}
        self.by_key: dict[str, GroupInfo] = {g.key: g for g in groups}
        self.alias: dict[str, int] = aliases

    @property
    def enabled(self) -> bool:
        return bool(self.by_id)

    def resolve(self, food_name: str | None, *group_ids: int | None) -> GroupInfo | None:
        """군 id 들(기록 스냅샷 → 상품 매칭) 중 처음 유효한 것, 없으면 이름 → alias → 군명 정확일치."""
        for gid in group_ids:
            if gid is not None and gid in self.by_id:
                return self.by_id[gid]
        if not food_name:
            return None
        key = normalize_name(food_name)
        gid = self.alias.get(key)
        if gid is not None and gid in self.by_id:
            return self.by_id[gid]
        return self.by_key.get(key)

    def key_for(self, food_name: str | None, *group_ids: int | None) -> str:
        g = self.resolve(food_name, *group_ids)
        return g.key if g else normalize_name(food_name or "")

    def info(self, key: str) -> GroupInfo | None:
        return self.by_key.get(key)

    def role_of(self, key: str) -> str | None:
        g = self.by_key.get(key)
        return g.role if g else None

    def companion_of(self, key: str) -> GroupInfo | None:
        g = self.by_key.get(key)
        if g is None or g.companion_id is None:
            return None
        return self.by_id.get(g.companion_id)


def load_group_index(db: Session) -> GroupIndex:
    groups = [
        GroupInfo(
            id=g.id,
            name=g.name,
            key=normalize_name(g.name),
            family=g.family,
            role=g.role,
            companion_id=g.companion_group_id,
            calories=float(g.calories) if g.calories is not None else None,
            carbs=float(g.carbs) if g.carbs is not None else None,
            protein=float(g.protein) if g.protein is not None else None,
            fat=float(g.fat) if g.fat is not None else None,
        )
        for g in db.scalars(select(FoodGroup))
    ]
    aliases = {a.alias: a.group_id for a in db.scalars(select(FoodGroupAlias))}
    return GroupIndex(groups, aliases)


EMPTY_INDEX = GroupIndex([], {})
