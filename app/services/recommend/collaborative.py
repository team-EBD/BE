"""음식군 공동 섭취 기반 협업 후보. 충분한 다른 사용자 근거가 없으면 비워 둔다.

사용자×음식군의 이진 행렬에서 음식군 쌍의 코사인을 구하고 공동 사용자 수로 축소한다.
한 사람의 반복 기록·같은 끼니의 별칭 행은 표를 늘리지 않는다. 요청자 자신의 기록은
관심 음식군을 정하는 데만 쓰고, 협업 관계를 학습하는 표에서는 제외한다.
이는 전체 인기도나 음식 이름 유사도가 아닌 실제 다른 사용자의 섭취 관계다.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .candidates import Candidate, Companions, _eligible, _similarity_pool
from .groups import GroupIndex, load_group_index
from .signals import _eaten_rows

MIN_PROFILE_GROUPS = 2
MIN_CO_USERS = 3
SUPPORT_SHRINKAGE = 5.0
INTERACTION_WINDOW_DAYS = 90


def collaborative_candidates(
    db: Session,
    user_id: int,
    budget_kcal: int,
    *,
    now: datetime,
    index: GroupIndex | None = None,
    meal_type: str | None = None,
    companions: Companions | None = None,
    top: int = 15,
    exclude_keys: frozenset[str] = frozenset(),
    window_days: int = INTERACTION_WINDOW_DAYS,
) -> list[Candidate]:
    """신뢰할 수 있는 관심 군 2개와 군 쌍의 다른 공동 사용자 3명 이상일 때만 생성.

    score = 공동 사용자 / sqrt(각 군 사용자 수의 곱) × 공동 사용자 / (공동 사용자 + 5).
    후보마다 가장 강한 관계 하나를 쓰므로 한 사용자를 여러 관심 군을 통해 중복 가산하지 않는다.
    끼니별로 행렬을 쪼개지 않고 역할·끼니 및 밥 포함 열량 자격을 후보에 적용한다.
    이미 개인·인기 생성기가 찾은 메뉴도 기본적으로 남겨 merge가 독립적인 근거를 보존한다.
    """
    if top <= 0 or window_days <= 0:
        return []
    index = index or load_group_index(db)
    if not index.enabled:
        return []  # 이름 키만 있는 항목은 안정적인 음식군 행렬에 억지로 편입하지 않는다.
    choices = companions if companions is not None else {
        group.key: index.companion_of(group.key) for group in index.by_id.values()
    }
    # 과거 큰 메뉴도 관심의 기준이 될 수 있다. 현재 예산 상한은 실제 후보에만 적용한다.
    profiles = [candidate for candidate in _similarity_pool(db, index)
                if _eligible(candidate, 0, meal_type, choices)]
    valid_ids = {candidate.group_id for candidate in profiles}
    anchor_ids = {candidate.group_id for candidate in profiles if candidate.key not in exclude_keys}
    since = now - timedelta(days=window_days)
    own_rows = _eaten_rows(db, since=since, until=now, index=index, user_id=user_id)
    anchors = {row.group.id for row in own_rows if row.group and row.group.id in anchor_ids}
    if len(anchors) < MIN_PROFILE_GROUPS:
        return []

    item_users: dict[int, set[int]] = defaultdict(set)
    rows = _eaten_rows(
        db, since=since, until=now, index=index, exclude_test_users=True, exclude_user_id=user_id,
    )
    for row in rows:
        if row.group and row.group.id in valid_ids:
            item_users[row.group.id].add(row.user_id)

    candidates = []
    for candidate in profiles:
        if candidate.key in exclude_keys or not _eligible(candidate, budget_kcal, meal_type, choices):
            continue
        users = item_users.get(candidate.group_id, set())
        if len(users) < MIN_CO_USERS:
            continue
        best_score, best_support = 0.0, 0
        for anchor_id in sorted(anchors):
            if anchor_id == candidate.group_id:
                continue  # 같은 음식의 반복을 협업 관계로 보지 않는다.
            anchor_users = item_users.get(anchor_id, set())
            support = len(anchor_users & users)
            if support < MIN_CO_USERS:
                continue
            cosine = support / math.sqrt(len(anchor_users) * len(users))
            score = cosine * support / (support + SUPPORT_SHRINKAGE)
            if (score, support) > (best_score, best_support):
                best_score, best_support = score, support
        if best_support:
            candidates.append(replace(
                candidate, source="collaborative", collaborative=best_score,
                collaborative_support=best_support,
            ))
    candidates.sort(key=lambda candidate: (-candidate.collaborative, -candidate.collaborative_support, candidate.key))
    return candidates[:top]
