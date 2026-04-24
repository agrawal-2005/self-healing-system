"""
FallbackService — business logic for fallback-service.

In Phase 1 the fallback is a simple static response.
In Phase 2 this is where you would add:
  - Cache lookup (return last known good response)
  - Default/safe value logic
  - Alerting that fallback was triggered
"""

import logging

from app.models.schemas import FallbackResponse, HealthResponse

logger = logging.getLogger(__name__)


class FallbackService:
    def __init__(self, service_name: str) -> None:
        self.service_name = service_name

    def health(self) -> HealthResponse:
        """
        Fallback-service is stateless and has no failure modes in Phase 1,
        so health is always healthy.
        """
        return HealthResponse(status="healthy", service=self.service_name)

    def get_fallback(self) -> FallbackResponse:
        """
        Returns a safe degraded response.
        The `degraded=True` flag signals to the caller (api-service)
        that this is not the primary result.
        """
        logger.info("get_fallback(): serving fallback response")
        return FallbackResponse(
            message="core-service is unavailable. Serving safe fallback response.",
            service=self.service_name,
            degraded=True,
        )
