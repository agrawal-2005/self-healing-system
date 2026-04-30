"""
Pydantic models for api-service.

Kept intentionally minimal — the gateway passes downstream responses through
as raw dicts, so there is no need for per-service response models.
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Returned by GET /health."""
    status: str
    service: str


class ProcessResponse(BaseModel):
    """
    Returned by GET /{service_name}.

    source   — which service produced the result ("core-service", "fallback-service", …)
    result   — raw JSON payload from that service
    degraded — True when the response came from fallback-service, not the primary
    """
    source:   str
    result:   dict
    degraded: bool = False


class ErrorResponse(BaseModel):
    """Returned on HTTP 503 / 404."""
    detail: str
