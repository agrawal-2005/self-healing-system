"""
Pydantic models for fallback-service.
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str


class FallbackResponse(BaseModel):
    message: str
    service: str
    degraded: bool    # always True — callers can use this flag to detect degraded mode
