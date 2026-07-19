"""식사 이미지 보존 기간 관리.

정책: 이미지는 "오늘(KST) 기준 저번달 1일 00:00(KST)" 이후 업로드분만 보존한다.
그 이전 이미지는 스토리지에서 삭제하고 meal_images 행도 제거한다
(meal_records.meal_image_id 는 ON DELETE SET NULL 이라 기록 자체는 남는다 —
캘린더/상세에서 이미지만 아이콘 fallback 으로 바뀐다).

트리거: 앱 시작 시 + 24시간 주기 백그라운드 태스크 (main.py lifespan).
스토리지 삭제 실패는 행을 남겨 다음 주기에 재시도한다.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import KST, UTC, now_utc
from app.models import MealImage
from app.storage.base import Storage

logger = logging.getLogger("eatlog.image_retention")


def retention_cutoff_utc(now: datetime | None = None) -> datetime:
    """보존 시작 시점 — 오늘(KST) 기준 저번달 1일 00:00 KST 의 UTC 값."""
    now_kst = (now or now_utc()).astimezone(KST)
    if now_kst.month == 1:
        cutoff = datetime(now_kst.year - 1, 12, 1, tzinfo=KST)
    else:
        cutoff = datetime(now_kst.year, now_kst.month - 1, 1, tzinfo=KST)
    return cutoff.astimezone(UTC)


def purge_expired_images(
    db: Session, storage: Storage, now: datetime | None = None
) -> int:
    """보존 기간이 지난 이미지를 스토리지·DB 에서 제거하고 삭제 건수를 반환한다."""
    cutoff = retention_cutoff_utc(now)
    expired = list(db.scalars(select(MealImage).where(MealImage.uploaded_at < cutoff)))

    deleted = 0
    for image in expired:
        try:
            storage.delete(image.storage_key)
        except Exception:  # noqa: BLE001 — 실패 건은 남겨 다음 주기에 재시도
            logger.warning("스토리지 삭제 실패 (재시도 예정): %s", image.storage_key)
            continue
        db.delete(image)
        deleted += 1

    db.commit()
    if deleted:
        logger.info("보존 기간 경과 이미지 %d건 삭제 (cutoff=%s)", deleted, cutoff.isoformat())
    return deleted
