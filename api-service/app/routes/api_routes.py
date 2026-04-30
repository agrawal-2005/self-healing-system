"""
Route layer for api-service — Phase 8 (config-driven gateway).

Two routes only:
  GET /health            — liveness probe, always returns 200 while process is up
  GET /{service_name}    — generic proxy to any registered downstream service

Adding a new downstream service does NOT require a new route here.
It requires only a new entry in services_config.json.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_gateway_service
from app.models.schemas import ErrorResponse, HealthResponse, ProcessResponse
from app.services.gateway_service import GatewayService

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
)
async def health(gateway: GatewayService = Depends(get_gateway_service)):
    """Returns 200 if api-service itself is running."""
    return gateway.health()


@router.get(
    "/{service_name}",
    response_model=ProcessResponse,
    responses={
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Proxy a request to any registered downstream service",
)
async def proxy(
    service_name: str,
    gateway: GatewayService = Depends(get_gateway_service),
):
    """
    Routes the request to the named service via its circuit breaker.

    Strategy is configured per service in services_config.json:
      fallback  — on failure, returns a degraded response from fallback-service
      escalate  — on failure, returns HTTP 503 immediately (no fallback)

    Returns 404 if service_name is not registered in the gateway.
    Returns 503 if the service is unavailable and strategy=escalate,
              or if both the primary and fallback-service are unreachable.
    """
    try:
        return await gateway.call(service_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
