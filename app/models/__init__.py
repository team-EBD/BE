"""ORM 모델 패키지.

모든 모델을 여기서 import 해 `Base.metadata` 에 등록한다.
(Alembic autogenerate 가 전체 테이블을 인식하려면 이 import 가 필요하다.)

ERD(ref/설계/eatlog_mvp_erd.mmd) 17개 테이블 1:1 매핑.
"""
from app.core.database import Base
from app.models.ai import AiCallLog, FoodCandidate, RecommendationLog
from app.models.meal import CorrectionLog, MealImage, MealItem, MealRecord
from app.models.nutrition import DailyNutritionSummary, NutritionItem
from app.models.user import (
    EatingHabit,
    LocationConsent,
    NotificationSetting,
    PasswordResetCode,
    PushToken,
    RefreshToken,
    TermsAgreement,
    User,
    UserProfile,
)

__all__ = [
    "Base",
    # user domain
    "User",
    "RefreshToken",
    "PasswordResetCode",
    "UserProfile",
    "EatingHabit",
    "NotificationSetting",
    "PushToken",
    "LocationConsent",
    "TermsAgreement",
    # nutrition domain
    "NutritionItem",
    "DailyNutritionSummary",
    # meal domain
    "MealImage",
    "MealRecord",
    "MealItem",
    "CorrectionLog",
    # ai domain
    "AiCallLog",
    "FoodCandidate",
    "RecommendationLog",
]
