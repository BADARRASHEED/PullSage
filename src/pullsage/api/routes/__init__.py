"""HTTP route aggregation."""

from fastapi import APIRouter

from pullsage.api.routes.capabilities import router as capabilities_router
from pullsage.api.routes.reviews import router as reviews_router

api_router = APIRouter()
api_router.include_router(reviews_router)
api_router.include_router(capabilities_router)

__all__ = ["api_router"]
