"""
ApiService — business logic layer for api-service.

This class owns the DECISION:
  "Try core-service. If it fails or times out, try fallback-service."

It does NOT own HTTP details (those are in CoreClient / FallbackClient).
It does NOT own routing (that is in api_routes.py).

Why this separation?
  - You can unit-test the decision logic by injecting fake clients.
  - Changing the fallback strategy (e.g. adding retries, circuit breaker)
    means editing only this file.
  - The route handler stays a one-liner.
"""

import logging

from app.clients.core_client import CoreClient
from app.clients.fallback_client import FallbackClient
from app.models.schemas import HealthResponse, ProcessResponse
from app.services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

class ApiService:
    def __init__(
        self,
        core_client: CoreClient,
        fallback_client: FallbackClient,
        service_name: str,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        """
        Dependencies are injected, not created here.

        This means ApiService works with any CoreClient / FallbackClient
        implementation — the real one, or a test double.
        """
        self.core_client     = core_client
        self.fallback_client = fallback_client
        self.service_name    = service_name
        self.circuit_breaker = circuit_breaker

    async def process(self) -> ProcessResponse:
        """
        Core business logic for GET /process.

        Strategy:
          1. Ask core-service for work.
          2. If core-service succeeds → return the result, degraded=False.
          3. If core-service raises ANY exception → log it, try fallback.
          4. If fallback also fails → re-raise so the route layer returns 503.
        """

        if not self.circuit_breaker.can_call_core():
          logger.warning("Circuit OPEN — skipping core-service, using fallback directly.")
          fallback = await self.fallback_client.get_fallback()
          return ProcessResponse(
            source="fallback-service",
            result=fallback.model_dump(),
            degraded=True,
          )
        # ── Step 1: try core-service ─────────────────────────────────────────
        try:
            work = await self.core_client.get_work()
            self.circuit_breaker.record_success()
            logger.info("process(): core-service OK — source=core-service")
            return ProcessResponse(
                source="core-service",
                result=work.model_dump(),
                degraded=False,
            )
        except Exception as exc:
            # Covers: 5xx from core, timeout, connection refused, etc.
            logger.warning("process(): core-service failed (%s). Switching to fallback.", exc)
            self.circuit_breaker.record_failure()

        # ── Step 2: try fallback-service ─────────────────────────────────────
        # If this raises, the exception propagates up to the route handler,
        # which converts it to an HTTP 503 response.
        fallback = await self.fallback_client.get_fallback()
        logger.info("process(): fallback-service OK — source=fallback-service")
        return ProcessResponse(
            source="fallback-service",
            result=fallback.model_dump(),
            degraded=True,
        )

    def health(self) -> HealthResponse:
        """api-service is always healthy if this method is reachable."""
        return HealthResponse(status="healthy", service=self.service_name)
