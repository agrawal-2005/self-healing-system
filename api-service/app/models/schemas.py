"""
Pydantic models for api-service.

These define the shape of data that flows:
  - INTO this service (request bodies, if any)
  - OUT OF this service (response bodies)
  - BETWEEN this service and downstream services (parsed from JSON responses)

Keeping models in one place makes it easy to change the API contract later.
"""

from pydantic import BaseModel


# ── Responses api-service sends to callers ────────────────────────────────────

class HealthResponse(BaseModel):
    """Returned by GET /health."""
    status: str
    service: str


class ProcessResponse(BaseModel):
    """
    Returned by GET /process.

    `source`  — which service actually produced the result.
    `result`  — the raw payload returned by that service.
    `degraded`— True when the result came from the fallback, not core.
    """
    source: str
    result: dict
    degraded: bool = False


class ErrorResponse(BaseModel):
    """Returned when both core and fallback are unavailable."""
    error: str
    detail: str


# ── Shapes api-service expects FROM downstream services ───────────────────────

class WorkResult(BaseModel):
    """
    Shape of the JSON body returned by core-service GET /work.
    Used inside CoreClient to parse and validate the response.
    """
    message: str
    service: str


class FallbackResult(BaseModel):
    """
    Shape of the JSON body returned by fallback-service GET /fallback.
    Used inside FallbackClient to parse and validate the response.
    """
    message: str
    service: str
    degraded: bool
