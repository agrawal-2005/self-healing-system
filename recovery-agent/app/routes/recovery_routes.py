"""
Route layer for recovery-agent.

Security:
  POST /action requires the X-Recovery-Token header to match RECOVERY_TOKEN.
  This prevents anyone who discovers the recovery-agent URL from triggering
  arbitrary docker commands. Lambda sends this token automatically.

  GET /health is intentionally NOT token-protected (load balancers need it).
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config.settings import settings
from app.dependencies import get_recovery_service
from app.models.schemas import ActionRequest, ActionResponse, HealthResponse
from app.services.recovery_service import RecoveryService

router = APIRouter()


def _verify_token(x_recovery_token: str = Header(default="")) -> None:
    """
    Validates the shared secret in the X-Recovery-Token header.

    Why a shared secret and not OAuth/JWT?
      For a local Docker dev environment, a shared secret is the simplest
      approach that adds real protection without requiring an auth server.
      In production, use IAM authentication instead.

    Configuration:
      Set RECOVERY_TOKEN env var on both recovery-agent and Lambda.
      If RECOVERY_TOKEN is empty, validation is skipped (useful for local testing
      without docker-compose env vars set — set it to a real value in production).
    """
    if not settings.recovery_token:
        return  # token check disabled (empty string = open access)
    if x_recovery_token != settings.recovery_token:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-Recovery-Token header.",
        )


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health(service: RecoveryService = Depends(get_recovery_service)):
    return service.health()


@router.post(
    "/action",
    response_model=ActionResponse,
    summary="Execute a recovery action",
    dependencies=[Depends(_verify_token)],
)
async def execute_action(
    request: ActionRequest,
    service: RecoveryService = Depends(get_recovery_service),
):
    """
    Called by AWS Lambda. Requires X-Recovery-Token header.

    Body example:
        {
          "action": "restart_service",
          "target_service": "core-service",
          "reason": "Lambda triggered by crash on core-service"
        }
    """
    return service.execute_action(request)
