"""API v1 라우터 집합 (명세서 2장 — 26개 엔드포인트)."""
from fastapi import APIRouter

from app.api.v1 import (
    ai_call_logs,
    auth,
    client_events,
    foods,
    health,
    meals,
    nutrition,
    push_tokens,
    recommendations,
    usage,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(push_tokens.router)
api_router.include_router(meals.router)
api_router.include_router(foods.router)
api_router.include_router(nutrition.router)
api_router.include_router(recommendations.router)
api_router.include_router(ai_call_logs.router)
api_router.include_router(usage.router)
api_router.include_router(client_events.router)
