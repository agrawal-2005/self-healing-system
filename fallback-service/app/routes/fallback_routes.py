"""
Route layer for fallback-service.
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_fallback_service
from app.models.schemas import FallbackResponse, HealthResponse
from app.services.fallback_service import FallbackService

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(service: FallbackService = Depends(get_fallback_service)):
    return service.health()


@router.get("/fallback", response_model=FallbackResponse, summary="Return fallback response")
async def fallback(service: FallbackService = Depends(get_fallback_service)):
    """Called by api-service when core-service is unavailable."""
    return service.get_fallback()
