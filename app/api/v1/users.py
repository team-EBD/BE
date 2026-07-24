"""사용자 라우터 (Phase 3·10, 명세서 4·11·12.1·13장).

/users/me, /users/eating-habits, /users/notification-settings,
/users/location-consent, /users/terms-agreements
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import DB, CurrentUser
from app.core.errors import APIError
from app.core.timeutil import now_utc, to_utc
from app.models import (
    EatingHabit,
    LocationConsent,
    NotificationSetting,
    TermsAgreement,
    UserProfile,
)
from app.schemas.user import (
    EatingHabitsResponse,
    EatingHabitsUpdateRequest,
    LocationConsentCreateRequest,
    LocationConsentResponse,
    LocationConsentUpdateRequest,
    MeDetailResponse,
    NotificationSettingsResponse,
    NotificationSettingsUpdateRequest,
    TermsAgreementRequest,
    TermsAgreementResponse,
    UpdateMeRequest,
)
from app.services.goals import personalized_goals
from app.services.nickname import allocate_nickname_tag
from app.services.summary import DEFAULT_GOALS, derive_macro_goals

router = APIRouter(prefix="/users", tags=["users"])

# 식습관 기본값 (명세서 11.2 — 미설정 시 기본값 반환)
HABIT_DEFAULTS = {
    "default_portion": "normal",
    "soup_preference": "eat",
    "sauce_preference": "eat",
    "leftover_frequency": "never",
    "meal_goal": "maintain",
}


def _me_response(db: DB, user) -> MeDetailResponse:
    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
    return MeDetailResponse(
        id=user.id,
        email=user.email,
        nickname=user.nickname,
        nickname_tag=user.nickname_tag,
        household_type=profile.household_type if profile else None,
        daily_goal_calories=profile.goal_calories if profile else None,
        gender=profile.gender if profile else None,
        height=float(profile.height) if profile and profile.height is not None else None,
        weight=float(profile.weight) if profile and profile.weight is not None else None,
        birth_year=profile.birth_year if profile else None,
        goal_source=profile.goal_source if profile else None,
        created_at=user.created_at,
    )


@router.get("/me", response_model=MeDetailResponse)
def get_me(user: CurrentUser, db: DB) -> MeDetailResponse:
    return _me_response(db, user)


@router.patch("/me", response_model=MeDetailResponse)
def update_me(body: UpdateMeRequest, user: CurrentUser, db: DB) -> MeDetailResponse:
    if body.nickname is not None and body.nickname != user.nickname:
        # 새 닉네임에서 기존 태그가 비어 있으면 유지, 쓰이고 있으면 새로 할당
        user.nickname_tag = allocate_nickname_tag(
            db, body.nickname, keep_tag=user.nickname_tag, exclude_user_id=user.id
        )
        user.nickname = body.nickname

    profile_fields = (
        body.household_type,
        body.daily_goal_calories,
        body.gender,
        body.height,
        body.weight,
        body.birth_year,
        body.goal_source,
    )
    if any(value is not None for value in profile_fields):
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
        if profile is None:
            goals = derive_macro_goals(body.daily_goal_calories or DEFAULT_GOALS["calories"])
            profile = UserProfile(
                user_id=user.id,
                goal_calories=body.daily_goal_calories or DEFAULT_GOALS["calories"],
                goal_carbs=goals["carbs"],
                goal_protein=goals["protein"],
                goal_fat=goals["fat"],
                # 컬럼 default 는 INSERT(flush) 시점에야 적용되므로 여기서 명시해야
                # 아래 goal_source == "auto" 분기(BMR 자동 산정)가 첫 저장에서 동작한다
                # (소셜 가입자의 온보딩 PATCH 가 정확히 이 경로)
                goal_source="auto",
            )
            db.add(profile)
        if body.household_type is not None:
            profile.household_type = body.household_type
        if body.gender is not None:
            profile.gender = body.gender
        if body.height is not None:
            profile.height = body.height
        if body.weight is not None:
            profile.weight = body.weight
        if body.birth_year is not None:
            profile.birth_year = body.birth_year

        if body.daily_goal_calories is not None:
            # 사용자가 직접 설정한 목표 — 이후 신체정보가 바뀌어도 자동 재계산으로
            # 덮어쓰지 않는다
            profile.goal_source = "manual"
            _apply_goal_calories(profile, body.daily_goal_calories)
        elif body.goal_source == "auto" or (
            profile.goal_source == "auto"
            and any(
                value is not None
                for value in (body.gender, body.height, body.weight, body.birth_year)
            )
        ):
            # 자동 산정 사용자의 신체정보 변경, 또는 직접 설정 목표의 자동 되돌리기
            # — BMR/TDEE 목표를 재계산한다
            profile.goal_source = "auto"
            habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
            goals = personalized_goals(
                profile.gender,
                profile.birth_year,
                profile.height,
                profile.weight,
                habit.meal_goal if habit else None,
            )
            if goals is not None:
                _apply_goal_calories(profile, goals["calories"])

    db.commit()
    return _me_response(db, user)


def _apply_goal_calories(profile: UserProfile, calories: int) -> None:
    """목표 칼로리 변경 시 탄단지 목표(50:30:20)도 함께 갱신한다."""
    profile.goal_calories = calories
    macros = derive_macro_goals(calories)
    profile.goal_carbs = macros["carbs"]
    profile.goal_protein = macros["protein"]
    profile.goal_fat = macros["fat"]


# --- 식습관 (명세서 11장) ---

def _habits_response(habit: EatingHabit | None) -> EatingHabitsResponse:
    if habit is None:
        return EatingHabitsResponse(**HABIT_DEFAULTS)
    return EatingHabitsResponse(
        default_portion=habit.default_portion or HABIT_DEFAULTS["default_portion"],
        soup_preference=habit.soup_preference or HABIT_DEFAULTS["soup_preference"],
        sauce_preference=habit.sauce_preference or HABIT_DEFAULTS["sauce_preference"],
        leftover_frequency=habit.leftover_frequency or HABIT_DEFAULTS["leftover_frequency"],
        meal_goal=habit.meal_goal or HABIT_DEFAULTS["meal_goal"],
    )


@router.get("/eating-habits", response_model=EatingHabitsResponse)
def get_eating_habits(user: CurrentUser, db: DB) -> EatingHabitsResponse:
    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
    return _habits_response(habit)


@router.patch("/eating-habits", response_model=EatingHabitsResponse)
def update_eating_habits(
    body: EatingHabitsUpdateRequest, user: CurrentUser, db: DB
) -> EatingHabitsResponse:
    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
    if habit is None:
        habit = EatingHabit(user_id=user.id)
        db.add(habit)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(habit, field, value)

    # 목표 유형(감량/유지/증량) 변경은 자동 산정 목표 칼로리에 반영한다
    if body.meal_goal is not None:
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
        if profile is not None and profile.goal_source == "auto":
            goals = personalized_goals(
                profile.gender,
                profile.birth_year,
                profile.height,
                profile.weight,
                body.meal_goal,
            )
            if goals is not None:
                _apply_goal_calories(profile, goals["calories"])

    db.commit()
    return _habits_response(habit)


# --- 알림 설정 (명세서 12.1) ---

def _notification_response(setting: NotificationSetting | None) -> NotificationSettingsResponse:
    """미설정 사용자는 모델 기본값(알림 on, 시간 미지정)을 반환한다."""
    if setting is None:
        return NotificationSettingsResponse(
            is_enabled=True, lunch_time=None, dinner_time=None, weekly_report_enabled=True
        )
    return NotificationSettingsResponse(
        is_enabled=setting.is_enabled,
        lunch_time=setting.lunch_time,
        dinner_time=setting.dinner_time,
        weekly_report_enabled=setting.weekly_report_enabled,
    )


@router.get("/notification-settings", response_model=NotificationSettingsResponse)
def get_notification_settings(user: CurrentUser, db: DB) -> NotificationSettingsResponse:
    setting = db.scalar(
        select(NotificationSetting).where(NotificationSetting.user_id == user.id)
    )
    return _notification_response(setting)


@router.patch("/notification-settings", response_model=NotificationSettingsResponse)
def update_notification_settings(
    body: NotificationSettingsUpdateRequest, user: CurrentUser, db: DB
) -> NotificationSettingsResponse:
    setting = db.scalar(
        select(NotificationSetting).where(NotificationSetting.user_id == user.id)
    )
    if setting is None:
        setting = NotificationSetting(user_id=user.id)
        db.add(setting)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(setting, field, value)
    db.commit()
    return _notification_response(setting)


# --- 위치 동의 (명세서 13.1~13.2) ---

def _consent_response(consent: LocationConsent) -> LocationConsentResponse:
    return LocationConsentResponse(
        consent_status=consent.consent_status,
        consent_version=consent.consent_version,
        agreed_at=consent.agreed_at,
        revoked_at=consent.revoked_at,
    )


@router.post("/location-consent", response_model=LocationConsentResponse, status_code=201)
def create_location_consent(
    body: LocationConsentCreateRequest, user: CurrentUser, db: DB
) -> LocationConsentResponse:
    now = now_utc()
    consent = db.scalar(select(LocationConsent).where(LocationConsent.user_id == user.id))
    if consent is None:
        consent = LocationConsent(
            user_id=user.id,
            consent_status=body.consent_status,
            consent_version=body.consent_version,
            agreed_at=now,
            revoked_at=None if body.consent_status else now,
        )
        db.add(consent)
    else:
        # ERD 는 사용자당 1행(현재 상태) — 재동의는 갱신으로 처리
        consent.consent_status = body.consent_status
        consent.consent_version = body.consent_version
        consent.agreed_at = now
        consent.revoked_at = None if body.consent_status else now
    db.commit()
    return _consent_response(consent)


@router.patch("/location-consent", response_model=LocationConsentResponse)
def update_location_consent(
    body: LocationConsentUpdateRequest, user: CurrentUser, db: DB
) -> LocationConsentResponse:
    consent = db.scalar(select(LocationConsent).where(LocationConsent.user_id == user.id))
    if consent is None:
        raise APIError(404, "NOT_FOUND", "저장된 위치 동의가 없습니다.")
    now = now_utc()
    consent.consent_status = body.consent_status
    if body.consent_status:
        consent.agreed_at = now
        consent.revoked_at = None
    else:
        consent.revoked_at = now
    db.commit()
    return _consent_response(consent)


# --- 약관 동의 (명세서 13.3) ---

@router.post("/terms-agreements", response_model=TermsAgreementResponse, status_code=201)
def create_terms_agreement(
    body: TermsAgreementRequest, user: CurrentUser, db: DB
) -> TermsAgreementResponse:
    agreed_at = to_utc(body.agreed_at) if body.agreed_at else now_utc()
    agreement = TermsAgreement(
        user_id=user.id,
        terms_type=body.terms_type,
        version=body.version,
        agreed_at=agreed_at,
    )
    db.add(agreement)
    db.commit()
    return TermsAgreementResponse(
        terms_agreement_id=agreement.id,
        terms_type=agreement.terms_type,
        version=agreement.version,
        agreed_at=agreement.agreed_at,
    )
