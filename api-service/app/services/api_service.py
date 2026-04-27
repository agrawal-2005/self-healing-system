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

Phase 4 addition:
  CloudWatch metrics are emitted on circuit state transitions and fallback use.
  The cloudwatch_publisher is a no-op when CLOUDWATCH_ENABLED=false.
"""

import logging

from app.clients.core_client import CoreClient
from app.clients.fallback_client import FallbackClient
from app.models.schemas import HealthResponse, ProcessResponse
from app.publishers.cloudwatch_publisher import CloudWatchMetricsPublisher
from app.services.circuit_breaker import CircuitBreaker, CircuitState

logger = logging.getLogger(__name__)

# Map CircuitState enum values to numeric gauge values for CloudWatch.
# CLOSED=0, HALF_OPEN=1, OPEN=2 — easy to plot as a time-series.
_STATE_GAUGE = {
    CircuitState.CLOSED:    0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN:      2,
}

# The downstream service the circuit breaker protects.
_CORE_TARGET = "core-service"


class ApiService:
    def __init__(
        self,
        core_client: CoreClient,
        fallback_client: FallbackClient,
        service_name: str,
        circuit_breaker: CircuitBreaker,
        cloudwatch_publisher: CloudWatchMetricsPublisher,
    ) -> None:
        """
        Dependencies are injected, not created here.

        This means ApiService works with any CoreClient / FallbackClient
        implementation — the real one, or a test double.
        """
        self.core_client          = core_client
        self.fallback_client      = fallback_client
        self.service_name         = service_name
        self.circuit_breaker      = circuit_breaker
        self.cloudwatch_publisher = cloudwatch_publisher

    async def process(self) -> ProcessResponse:
        """
        Core business logic for GET /process.

        Strategy:
          1. Check circuit breaker — if OPEN, skip core-service entirely.
          2. If allowed → try core-service.
          3. On success → record_success(), return core-service response.
          4. On failure → record_failure(), fall back to fallback-service.
          5. If core-service was skipped (OPEN) → use fallback directly.

        CloudWatch emissions:
          - FallbackUsedCount     every time a fallback response is returned
          - CircuitBreakerState   on every state transition (CLOSED/HALF_OPEN/OPEN)
          - CircuitBreakerOpenCount  when the circuit transitions INTO OPEN
        """

        # ── Step 1: check circuit, detect OPEN→HALF_OPEN transition ──────────
        state_before_check = self.circuit_breaker.state
        can_call           = self.circuit_breaker.can_call_core()
        state_after_check  = self.circuit_breaker.state

        if state_before_check != state_after_check:
            # OPEN → HALF_OPEN (timer expired, entering probe mode)
            self._emit_state_change(state_after_check)

        if not can_call:
            # Circuit is OPEN (or HALF_OPEN probe limit reached) — skip core-service
            logger.warning("Circuit OPEN — skipping core-service, using fallback directly.")
            fallback = await self.fallback_client.get_fallback()
            self.cloudwatch_publisher.record_fallback_used(_CORE_TARGET)
            return ProcessResponse(
                source="fallback-service",
                result=fallback.model_dump(),
                degraded=True,
            )

        # ── Step 2: try core-service ──────────────────────────────────────────
        try:
            work         = await self.core_client.get_work()
            state_before = self.circuit_breaker.state
            self.circuit_breaker.record_success()
            state_after  = self.circuit_breaker.state
            if state_before != state_after:
                # HALF_OPEN → CLOSED (probe succeeded, circuit closed)
                self._emit_state_change(state_after)
            logger.info("process(): core-service OK — source=core-service")
            return ProcessResponse(
                source="core-service",
                result=work.model_dump(),
                degraded=False,
            )
        except Exception as exc:
            # Covers: 5xx from core, timeout, connection refused, etc.
            logger.warning(
                "process(): core-service failed (%s). Switching to fallback.", exc
            )
            state_before = self.circuit_breaker.state
            self.circuit_breaker.record_failure()
            state_after  = self.circuit_breaker.state
            if state_before != state_after:
                # CLOSED→OPEN or HALF_OPEN→OPEN
                self._emit_state_change(state_after)

        # ── Step 3: try fallback-service ──────────────────────────────────────
        # If this raises, the exception propagates up to the route handler,
        # which converts it to an HTTP 503 response.
        fallback = await self.fallback_client.get_fallback()
        self.cloudwatch_publisher.record_fallback_used(_CORE_TARGET)
        logger.info("process(): fallback-service OK — source=fallback-service")
        return ProcessResponse(
            source="fallback-service",
            result=fallback.model_dump(),
            degraded=True,
        )

    def health(self) -> HealthResponse:
        """api-service is always healthy if this method is reachable."""
        return HealthResponse(status="healthy", service=self.service_name)

    # ── private ───────────────────────────────────────────────────────────────

    def _emit_state_change(self, new_state: CircuitState) -> None:
        """
        Emit CircuitBreakerState gauge + CircuitBreakerOpenCount on transition to OPEN.

        Called only when circuit state actually changes — not on every request.
        This keeps metric volume low and makes the gauge useful as a time-series.
        """
        gauge_value = _STATE_GAUGE[new_state]
        self.cloudwatch_publisher.record_circuit_state(_CORE_TARGET, gauge_value)

        if new_state == CircuitState.OPEN:
            # Count each time the circuit opens (useful for alerting)
            self.cloudwatch_publisher.record_circuit_open(_CORE_TARGET)
            logger.info(
                "CloudWatch: CircuitBreakerOpenCount+1 CircuitBreakerState=%d", gauge_value
            )
        else:
            logger.info(
                "CloudWatch: CircuitBreakerState=%d (%s)", gauge_value, new_state.value
            )
