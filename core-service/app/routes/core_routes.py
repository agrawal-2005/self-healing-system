"""
Route layer for core-service.

Every handler delegates immediately to CoreService.
No logic lives here — just HTTP plumbing.
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_core_service
from app.models.schemas import (
    FailResponse,
    HealthResponse,
    RecoverResponse,
    SlowResponse,
    WorkResponse,
)
from app.services.core_service import CoreService

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(service: CoreService = Depends(get_core_service)):
    """Returns 200 when healthy, 503 would require custom exception handling (left for Phase 2)."""
    return service.health()


@router.get("/work", response_model=WorkResponse, summary="Main work endpoint")
async def work(service: CoreService = Depends(get_core_service)):
    """
    Called by api-service. Returns 500 when crashed, delays when in slow mode.
    HTTPException is raised inside CoreService and propagates through FastAPI automatically.
    """
    return await service.do_work()


@router.get("/slow", response_model=SlowResponse, summary="Simulate high latency")
async def slow(service: CoreService = Depends(get_core_service)):
    """Always sleeps slow_delay_seconds before responding. Independent of slow-mode flag."""
    return await service.slow_work()


@router.post("/fail", response_model=FailResponse, summary="Simulate a crash")
async def fail(service: CoreService = Depends(get_core_service)):
    """Sets crashed=True. Subsequent /work and /health calls reflect the failure."""
    return service.trigger_fail()


@router.post("/slow-mode", summary="Enable slow mode on /work")
async def slow_mode(service: CoreService = Depends(get_core_service)):
    """Activates slow mode so /work sleeps before responding (triggers api-service timeout)."""
    return service.trigger_slow()


@router.post("/recover", response_model=RecoverResponse, summary="Reset all failure flags")
async def recover(service: CoreService = Depends(get_core_service)):
    """Clears crashed and slow_mode flags. Service returns to normal operation."""
    return service.recover()
