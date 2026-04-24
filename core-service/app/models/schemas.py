"""
Pydantic models for core-service.

Each model maps to exactly one endpoint's response body.
This makes the API contract explicit and machine-readable (OpenAPI docs).
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str          # "healthy" | "unhealthy"
    service: str


class WorkResponse(BaseModel):
    message: str
    service: str


class SlowResponse(BaseModel):
    message: str
    service: str
    latency_simulated_seconds: float


class FailResponse(BaseModel):
    message: str
    crashed: bool        # confirms the new state


class RecoverResponse(BaseModel):
    message: str
    crashed: bool        # confirms the new state
