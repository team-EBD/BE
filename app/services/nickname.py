"""닉네임 태그 할당 — 닉네임 중복 허용(표시형식 "닉네임#0001").

닉네임을 전역 유일로 강제하지 않는 대신 (닉네임, 태그) 쌍을 유일하게 유지한다.
같은 닉네임이 여러 명이어도 태그로 구분되므로 닉네임 선점(스쿼팅)이 성립하지 않는다.
"""
from __future__ import annotations

import secrets

from sqlalchemy import select

from app.core.errors import APIError
from app.models import User

TAG_MAX = 9999  # 0001~9999
_MAX_ATTEMPTS = 20


def _is_tag_taken(db, nickname: str, tag: str, exclude_user_id: int | None = None) -> bool:
    query = select(User.id).where(User.nickname == nickname, User.nickname_tag == tag)
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    return db.scalar(query) is not None


def allocate_nickname_tag(
    db, nickname: str, *, keep_tag: str | None = None, exclude_user_id: int | None = None
) -> str:
    """닉네임에 대해 사용 가능한 4자리 태그를 반환한다.

    keep_tag 가 주어지면(닉네임 변경 시) 기존 태그를 우선 유지하고,
    새 닉네임에서 이미 쓰이고 있을 때만 새로 뽑는다.
    """
    if keep_tag is not None and not _is_tag_taken(db, nickname, keep_tag, exclude_user_id):
        return keep_tag

    for _ in range(_MAX_ATTEMPTS):
        tag = f"{secrets.randbelow(TAG_MAX) + 1:04d}"
        if not _is_tag_taken(db, nickname, tag, exclude_user_id):
            return tag

    # 9999개가 사실상 소진된 인기 닉네임 — MVP 에서는 다른 닉네임을 안내한다.
    raise APIError(
        409, "CONFLICT", "해당 닉네임은 더 이상 사용할 수 없습니다. 다른 닉네임을 사용해 주세요."
    )
