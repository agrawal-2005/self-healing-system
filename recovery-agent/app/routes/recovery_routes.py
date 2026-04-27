"""
Route layer for recovery-agent.  Thin — delegates everything to RecoveryService.
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_recovery_service
from app.models.schemas import ActionRequest, ActionResponse, HealthResponse
from app.services.recovery_service import RecoveryService

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(service: RecoveryService = Depends(get_recovery_service)):
    return service.health()


@router.post(
    "/action",
    response_model=ActionResponse,
    summary="Execute a recovery action",
)
async def execute_action(
    request: ActionRequest,
    service: RecoveryService = Depends(get_recovery_service),
):
    """
    Called by AWS Lambda.

    Body example:
        {"action": "restart_service", "target_service": "core-service",
         "reason": "crash detected by monitor"}
    """
    return service.execute_action(request)
