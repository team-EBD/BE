"""ORM 모델 패키지.

모든 모델을 여기서 import 해 `Base.metadata` 에 등록한다.
(Alembic autogenerate 가 전체 테이블을 인식하려면 이 import 가 필요하다.)

ERD(ref/설계/eatlog_mvp_erd.mmd) 17개 테이블 1:1 매핑.
"""
from app.core.database import Base
from app.models.ai import AiCallLog, FoodCandidate, RecommendationItem, RecommendationLog
from app.models.billing import Subscription
from app.models.game import (
    CatalogItem,
    GameEvent,
    GameMission,
    GameProfile,
    PetBond,
    RewardLedger,
    SkillUsageLedger,
    StagePlacement,
    UnlockProgress,
    UserEvent,
    UserItem,
    UserMission,
    UserSkill,
)
from app.models.meal import CorrectionLog, MealImage, MealItem, MealRecord
from app.models.nutrition import (
    DailyNutritionSummary,
    FavoriteFood,
    FoodGroup,
    FoodGroupAlias,
    NutritionItem,
    NutritionItemPruned,
)
from app.models.telemetry import ClientEvent, RequestLog
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
    "FoodGroup",
    "FoodGroupAlias",
    "NutritionItemPruned",
    "FavoriteFood",
    "DailyNutritionSummary",
    # meal domain
    "MealImage",
    "MealRecord",
    "MealItem",
    "CorrectionLog",
    # ai domain
    "AiCallLog",
    "ClientEvent",
    "RequestLog",
    "FoodCandidate",
    "RecommendationLog",
    "RecommendationItem",
    # billing domain
    "Subscription",
    # gamification domain
    "GameProfile",
    "CatalogItem",
    "UserItem",
    "StagePlacement",
    "UnlockProgress",
    "RewardLedger",
    "PetBond",
    "UserSkill",
    "SkillUsageLedger",
    "GameMission",
    "UserMission",
    "GameEvent",
    "UserEvent",
]
