"""API v1 라우터 집합.

이후 Phase 에서 auth, users, meals, foods, nutrition, recommendations, consent
라우터가 여기에 include 된다.
"""
from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router)
