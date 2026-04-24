"""
Route layer for api-service.

Rules for this file:
  - Each handler must be ≤ 5 lines of real logic.
  - No business logic here — only call service methods and return results.
  - No direct use of httpx, clients, or settings.

Why so thin?
  Routes are the HTTP boundary. Keeping them thin means the business logic
  (in ApiService) can be tested without a running HTTP server.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_api_service
from app.models.schemas import ErrorResponse, HealthResponse, ProcessResponse
from app.services.api_service import ApiService

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
)
async def health(service: ApiService = Depends(get_api_service)):
    """Returns 200 if api-service is running."""
    return service.health()


@router.get(
    "/process",
    response_model=ProcessResponse,
    responses={503: {"model": ErrorResponse}},
    summary="Process a request (with automatic fallback)",
)
async def process(service: ApiService = Depends(get_api_service)):
    """
    Calls core-service. Falls back to fallback-service automatically.
    Returns 503 only when BOTH downstream services are unreachable.
    """
    try:
        return await service.process()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Both core-service and fallback-service are unavailable: {exc}",
        )
