"""식단 라우터 (Phase 4·6·8, 명세서 5·6.1·8장).

경로 선언 순서 주의: /meals/images·/meals/analyze·/meals/calendar 를
/meals/{meal_id} 보다 먼저 등록해야 정적 경로가 파라미터에 안 먹힌다.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select

from app.ai_client import get_ai_client
from app.ai_client.base import AIClient
from app.core.deps import DB, CurrentUser
from app.core.errors import APIError
from app.core.timeutil import kst_date_of, kst_day_bounds, kst_month_bounds, now_utc, to_utc
from app.models import MealImage, MealRecord
from app.schemas.meal import (
    AnalyzeFailedResponse,
    AnalyzeRequest,
    AnalyzeSuccessResponse,
    CalendarDay,
    CalendarResponse,
    MealCreateRequest,
    MealCreateResponse,
    MealDeleteResponse,
    MealDetailResponse,
    MealImageResponse,
    MealItemBrief,
    MealItemDetail,
    MealListEntry,
    MealListResponse,
    MealUpdateRequest,
)
from app.services.analyze import analyze_meal_image
from app.services.meals import (
    create_meal,
    delete_meal,
    get_owned_meal,
    meal_items_with_corrections,
    update_meal,
)
from app.storage import Storage, get_storage

router = APIRouter(prefix="/meals", tags=["meals"])

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB


# --- 5.1 이미지 업로드 ---

@router.post("/images", response_model=MealImageResponse, status_code=201)
async def upload_meal_image(
    user: CurrentUser,
    db: DB,
    image: UploadFile = File(...),
    source: str = Form(...),
    taken_at: datetime | None = Form(default=None),
    storage: Storage = Depends(get_storage),
) -> MealImageResponse:
    if source not in {"camera", "gallery"}:
        raise APIError(
            400, "VALIDATION_ERROR", "source 는 camera 또는 gallery 여야 합니다.",
            details=[{"field": "source", "reason": "invalid_value"}],
        )

    ext = (image.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS or image.content_type not in ALLOWED_CONTENT_TYPES:
        raise APIError(
            415, "UNSUPPORTED_MEDIA_TYPE", "지원하지 않는 이미지 포맷입니다. (jpg/png/webp)",
            details=[{"field": "image", "reason": "unsupported_format"}],
        )

    data = await image.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise APIError(
            413, "PAYLOAD_TOO_LARGE", "이미지 용량이 제한(10MB)을 초과했습니다.",
            details=[{"field": "image", "reason": "max_size_exceeded"}],
        )

    now = now_utc()
    key = f"meals/{now:%Y/%m}/{uuid.uuid4().hex}.{ext}"
    try:
        stored = storage.save(key, data)
    except OSError as exc:
        raise APIError(500, "INTERNAL_ERROR", "이미지 저장에 실패했습니다.") from exc

    record = MealImage(
        user_id=user.id,
        image_url=stored.url,
        storage_key=stored.storage_key,
        source=source,
        taken_at=to_utc(taken_at) if taken_at else None,
    )
    db.add(record)
    db.commit()
    return MealImageResponse(
        meal_image_id=record.id,
        image_url=record.image_url,
        storage_key=record.storage_key,
        uploaded_at=record.uploaded_at,
    )


# --- 6.1 AI 분석 (NFR-009: 사용자 명시 호출 시에만) ---

@router.post("/analyze", response_model=AnalyzeSuccessResponse | AnalyzeFailedResponse)
def analyze(
    body: AnalyzeRequest,
    user: CurrentUser,
    db: DB,
    ai: AIClient = Depends(get_ai_client),
) -> AnalyzeSuccessResponse | AnalyzeFailedResponse:
    return analyze_meal_image(db, user, body.meal_image_id, ai)


# --- 8.6 월별 캘린더 (정적 경로 — {meal_id} 보다 먼저) ---

@router.get("/calendar", response_model=CalendarResponse)
def get_calendar(
    user: CurrentUser,
    db: DB,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
) -> CalendarResponse:
    year, mon = int(month[:4]), int(month[5:7])
    start, end = kst_month_bounds(year, mon)
    rows = db.execute(
        select(MealRecord.eaten_at, MealRecord.total_calories).where(
            MealRecord.user_id == user.id,
            MealRecord.deleted_at.is_(None),
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
        )
    ).all()

    by_day: dict[str, dict] = {}
    for eaten_at, calories in rows:
        day = kst_date_of(eaten_at).isoformat()
        bucket = by_day.setdefault(day, {"meal_count": 0, "total_calories": 0.0})
        bucket["meal_count"] += 1
        bucket["total_calories"] += float(calories)

    days = [
        CalendarDay(
            date=day,
            meal_count=info["meal_count"],
            total_calories=round(info["total_calories"], 2),
        )
        for day, info in sorted(by_day.items(), reverse=True)
    ]
    return CalendarResponse(month=month, days=days)


# --- 8.5 날짜별 목록 ---

@router.get("", response_model=MealListResponse)
def list_meals(
    user: CurrentUser,
    db: DB,
    date_: date = Query(alias="date"),
) -> MealListResponse:
    start, end = kst_day_bounds(date_)
    meals = list(
        db.scalars(
            select(MealRecord)
            .where(
                MealRecord.user_id == user.id,
                MealRecord.deleted_at.is_(None),
                MealRecord.eaten_at >= start,
                MealRecord.eaten_at < end,
            )
            .order_by(MealRecord.eaten_at)
        )
    )
    return MealListResponse(
        date=date_.isoformat(),
        meals=[
            MealListEntry(
                meal_id=m.id,
                meal_type=m.meal_type,
                eaten_at=m.eaten_at,
                image_url=_image_url(db, m),
                total_calories=float(m.total_calories),
            )
            for m in meals
        ],
    )


def _image_url(db, meal: MealRecord) -> str | None:
    if meal.meal_image_id is None:
        return None
    image = db.get(MealImage, meal.meal_image_id)
    return image.image_url if image else None


# --- 8.1 식단 저장 ---

@router.post("", response_model=MealCreateResponse, status_code=201)
def create(body: MealCreateRequest, user: CurrentUser, db: DB) -> MealCreateResponse:
    meal = create_meal(db, user, body)
    items = [pair[0] for pair in meal_items_with_corrections(db, meal)]
    return MealCreateResponse(
        meal_id=meal.id,
        meal_type=meal.meal_type,
        eaten_at=meal.eaten_at,
        total_calories=float(meal.total_calories),
        total_carbs=float(meal.total_carbs),
        total_protein=float(meal.total_protein),
        total_fat=float(meal.total_fat),
        items=[
            MealItemBrief(meal_item_id=i.id, food_name=i.food_name, calories=float(i.calories))
            for i in items
        ],
    )


def _detail_response(db, meal: MealRecord) -> MealDetailResponse:
    pairs = meal_items_with_corrections(db, meal)
    return MealDetailResponse(
        meal_id=meal.id,
        meal_type=meal.meal_type,
        eaten_at=meal.eaten_at,
        memo=meal.memo,
        image_url=_image_url(db, meal),
        total_calories=float(meal.total_calories),
        total_carbs=float(meal.total_carbs),
        total_protein=float(meal.total_protein),
        total_fat=float(meal.total_fat),
        items=[
            MealItemDetail(
                meal_item_id=item.id,
                food_name=item.food_name,
                serving_amount=float(item.serving_amount),
                correction_type=correction,
                calories=float(item.calories),
                carbs=float(item.carbs),
                protein=float(item.protein),
                fat=float(item.fat),
            )
            for item, correction in pairs
        ],
    )


# --- 8.2 상세 / 8.3 수정 / 8.4 삭제 ---

@router.get("/{meal_id}", response_model=MealDetailResponse)
def get_detail(meal_id: int, user: CurrentUser, db: DB) -> MealDetailResponse:
    meal = get_owned_meal(db, user, meal_id)
    return _detail_response(db, meal)


@router.patch("/{meal_id}", response_model=MealDetailResponse)
def update(
    meal_id: int, body: MealUpdateRequest, user: CurrentUser, db: DB
) -> MealDetailResponse:
    meal = update_meal(db, user, meal_id, body)
    return _detail_response(db, meal)


@router.delete("/{meal_id}", response_model=MealDeleteResponse)
def delete(meal_id: int, user: CurrentUser, db: DB) -> MealDeleteResponse:
    delete_meal(db, user, meal_id)
    return MealDeleteResponse(deleted=True, meal_id=meal_id)
