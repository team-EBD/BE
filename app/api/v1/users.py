"""사용자 라우터 (Phase 3·10, 명세서 4·11·12.1·13장).

/users/me, /users/eating-habits, /users/notification-settings,
/users/location-consent, /users/terms-agreements
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Response
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
    GoalPlan,
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
from app.services.goals import (
    MEAL_GOAL_BY_PRIMARY,
    PRIMARY_BY_MEAL_GOAL,
    goal_plan,
    personalized_goals,
)
from app.services.nickname import allocate_nickname_tag
from app.services.summary import DEFAULT_GOALS, derive_macro_goals

logger = logging.getLogger("eatlog.users")

router = APIRouter(prefix="/users", tags=["users"])

# 식습관 기본값 (명세서 11.2 — 미설정 시 기본값 반환)
HABIT_DEFAULTS = {
    "default_portion": "normal",
    "soup_preference": "eat",
    "sauce_preference": "eat",
    "leftover_frequency": "never",
    "meal_goal": "maintain",
}


def _focus_areas(profile: UserProfile | None) -> list[str]:
    return [a for a in (profile.focus_areas or "").split(",") if a] if profile else []


def _goal_plan(profile: UserProfile | None, meal_goal: str | None) -> GoalPlan | None:
    """자동 산정 목표의 근거. 저장된 목표와 지금 계산이 어긋나면(직접 설정, 또는 예전 공식으로
    계산해 둔 값) 숫자가 맞지 않는 설명을 보여 주지 않도록 null 을 준다."""
    if profile is None or profile.goal_source != "auto":
        return None
    plan = goal_plan(
        profile.gender,
        profile.birth_year,
        profile.height,
        profile.weight,
        meal_goal,
        primary_goal=profile.primary_goal,
        activity_level=profile.activity_level,
        goal_pace=profile.goal_pace,
        target_weight=profile.target_weight,
    )
    if plan is None or plan["calories"] != profile.goal_calories:
        return None
    return GoalPlan(**{k: v for k, v in plan.items() if k != "calories"})


def _me_response(db: DB, user) -> MeDetailResponse:
    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
    habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
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
        primary_goal=profile.primary_goal if profile else None,
        activity_level=profile.activity_level if profile else None,
        target_weight=(
            float(profile.target_weight)
            if profile and profile.target_weight is not None
            else None
        ),
        goal_pace=profile.goal_pace if profile else None,
        focus_areas=_focus_areas(profile),
        daily_goal_carbs=profile.goal_carbs if profile else None,
        daily_goal_protein=profile.goal_protein if profile else None,
        daily_goal_fat=profile.goal_fat if profile else None,
        goal_plan=_goal_plan(profile, habit.meal_goal if habit else None),
        tutorial_completed_at=user.tutorial_completed_at,
        created_at=user.created_at,
    )


@router.get("/me", response_model=MeDetailResponse)
def get_me(user: CurrentUser, db: DB) -> MeDetailResponse:
    return _me_response(db, user)


@router.delete("/me", status_code=204, response_class=Response)
def delete_me(user: CurrentUser, db: DB) -> Response:
    """회원 탈퇴 — 계정과 모든 데이터를 즉시 영구 삭제한다 (하드 삭제).

    - 사용자 행 삭제 시 FK CASCADE 로 프로필/식단/식습관/토큰 등이 함께 삭제된다
      (ai_call_logs 는 user_id SET NULL — 통계용 익명 기록만 남음).
    - 업로드된 식사 사진은 스토리지에서 best-effort 로 삭제한다
      (스토리지 오류가 탈퇴 자체를 막으면 안 되므로 실패는 로깅만).
    """
    from app.models import MealImage
    from app.storage import get_storage

    storage = get_storage()
    images = db.scalars(select(MealImage).where(MealImage.user_id == user.id)).all()
    for image in images:
        try:
            storage.delete(image.storage_key)
        except Exception:  # noqa: BLE001
            logger.warning("탈퇴 스토리지 삭제 실패 (고아 파일): %s", image.storage_key)

    db.delete(user)
    db.commit()
    return Response(status_code=204)


@router.patch("/me", response_model=MeDetailResponse)
def update_me(body: UpdateMeRequest, user: CurrentUser, db: DB) -> MeDetailResponse:
    if "tutorial_completed_at" in body.model_fields_set:
        user.tutorial_completed_at = (
            to_utc(body.tutorial_completed_at) if body.tutorial_completed_at else None
        )

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
        body.primary_goal,
        body.activity_level,
        body.goal_pace,
        body.focus_areas,
    )
    if any(value is not None for value in profile_fields) or (
        "target_weight" in body.model_fields_set
    ):
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
        if body.activity_level is not None:
            profile.activity_level = body.activity_level
        if body.goal_pace is not None:
            profile.goal_pace = body.goal_pace
        if "target_weight" in body.model_fields_set:
            profile.target_weight = body.target_weight
        if body.focus_areas is not None:
            # 중복 없이, 받은 순서대로
            profile.focus_areas = ",".join(dict.fromkeys(body.focus_areas)) or None

        # 탄단지는 목표 유형(감량/유지/증량)에 따라 단백질 계수가 달라지므로 함께 읽는다
        habit = db.scalar(select(EatingHabit).where(EatingHabit.user_id == user.id))
        if body.primary_goal is not None:
            profile.primary_goal = body.primary_goal
            # 추천·요약과 이전 앱이 읽는 예전 목표 유형도 함께 맞춘다
            if habit is None:
                habit = EatingHabit(user_id=user.id)
                db.add(habit)
            habit.meal_goal = MEAL_GOAL_BY_PRIMARY[body.primary_goal]
        meal_goal = profile.primary_goal or (habit.meal_goal if habit else None)
        if body.daily_goal_calories is not None:
            # 사용자가 직접 설정한 목표 — 이후 신체정보가 바뀌어도 자동 재계산으로
            # 덮어쓰지 않는다 (칼로리는 수동, 탄단지는 체중·목표유형 기준 유지)
            profile.goal_source = "manual"
            _apply_goal_calories(profile, body.daily_goal_calories, meal_goal)
        elif body.goal_source == "auto" or (
            profile.goal_source == "auto"
            and any(
                value is not None
                for value in (
                    body.gender,
                    body.height,
                    body.weight,
                    body.birth_year,
                    body.primary_goal,
                    body.activity_level,
                    body.goal_pace,
                )
            )
        ):
            # 자동 산정 사용자의 신체정보·목표 변경, 또는 직접 설정 목표의 자동 되돌리기
            # — BMR/TDEE 목표를 재계산한다
            profile.goal_source = "auto"
            _recalculate_auto_goals(profile, meal_goal)
        elif body.primary_goal is not None:
            # 칼로리를 직접 정한 사용자 — 칼로리는 그대로 두고 탄단지만 새 목표에 맞춘다
            _apply_goal_calories(profile, profile.goal_calories, meal_goal)

    db.commit()
    return _me_response(db, user)


def _recalculate_auto_goals(profile: UserProfile, meal_goal: str | None) -> None:
    """신체정보·목표·활동량·속도로 목표 칼로리와 탄단지를 다시 계산한다 (신체정보 부족 시 유지).

    meal_goal 자리에는 세분화한 목표(primary_goal)가 있으면 그 값을, 없으면 예전 목표 유형을 넘긴다.
    """
    goals = personalized_goals(
        profile.gender,
        profile.birth_year,
        profile.height,
        profile.weight,
        meal_goal,
        activity_level=profile.activity_level,
        goal_pace=profile.goal_pace,
    )
    if goals is not None:
        _apply_goal_calories(profile, goals["calories"], meal_goal)


def _apply_goal_calories(
    profile: UserProfile, calories: int, meal_goal: str | None = None
) -> None:
    """목표 칼로리 변경 시 탄단지 목표도 함께 갱신한다.

    체중·목표유형이 있으면 단백질(체중당 g) 우선 산정, 없으면 비율 폴백.
    """
    profile.goal_calories = calories
    macros = derive_macro_goals(calories, profile.weight, meal_goal)
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
        if profile is not None:
            # 세분화한 목표(primary_goal)가 이 유형과 어긋날 때만 맞춘다 — 같은 유형 안의 구분
            # (유지 ↔ 건강한 식습관, 근육 ↔ 체중 증량)은 이전 앱의 저장으로 지워지지 않게 둔다
            if (
                profile.primary_goal is not None
                and MEAL_GOAL_BY_PRIMARY.get(profile.primary_goal) != body.meal_goal
            ):
                profile.primary_goal = PRIMARY_BY_MEAL_GOAL[body.meal_goal]
            goal_key = profile.primary_goal or body.meal_goal
            if profile.goal_source == "auto":
                _recalculate_auto_goals(profile, goal_key)

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
