"""비밀번호 재설정 인증코드 발급·검증 서비스.

- 코드: 6자리 숫자, 유효기간·시도 상한은 설정값(password_reset_code_*).
- 원문은 저장하지 않고 SHA-256 해시만 저장한다 (refresh_tokens 와 동일 원칙).
- 사용자당 유효한 코드는 항상 최신 1개 — 재발급 시 이전 코드를 폐기한다.
"""
from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token
from app.core.timeutil import from_db, now_utc
from app.models import PasswordResetCode, User

CODE_LENGTH = 6

VerifyResult = Literal["ok", "expired", "invalid"]


def generate_code() -> str:
    """000000~999999 균등 분포의 6자리 코드 (CSPRNG)."""
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def issue_code(db: Session, user: User) -> str:
    """새 코드를 발급하고 원문을 반환한다(메일 본문용). 이전 미사용 코드는 폐기."""
    now = now_utc()
    stale = db.scalars(
        select(PasswordResetCode).where(
            PasswordResetCode.user_id == user.id,
            PasswordResetCode.used_at.is_(None),
        )
    )
    for row in stale:
        row.used_at = now

    code = generate_code()
    db.add(
        PasswordResetCode(
            user_id=user.id,
            code_hash=hash_token(code),
            expires_at=now
            + timedelta(minutes=settings.password_reset_code_expire_minutes),
        )
    )
    return code


def consume_code(db: Session, user: User, code: str) -> VerifyResult:
    """코드 검증. 성공 시 사용 처리("ok"), 실패 시 사유를 반환한다.

    불일치는 attempt_count 를 올리고, 상한 초과 코드는 무효로 취급한다.
    호출측은 결과와 무관하게 commit 해야 시도 횟수가 보존된다.
    """
    row = db.scalar(
        select(PasswordResetCode)
        .where(
            PasswordResetCode.user_id == user.id,
            PasswordResetCode.used_at.is_(None),
        )
        .order_by(PasswordResetCode.id.desc())
        .limit(1)
    )
    if row is None:
        return "invalid"

    now = now_utc()
    if from_db(row.expires_at) <= now:
        return "expired"
    if row.attempt_count >= settings.password_reset_code_max_attempts:
        return "invalid"
    if not hmac.compare_digest(row.code_hash, hash_token(code)):
        row.attempt_count += 1
        return "invalid"

    row.used_at = now
    return "ok"
