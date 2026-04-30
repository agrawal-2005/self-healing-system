"""
GatewayService — the config-driven business logic layer for api-service.

Replaces the old ApiService.  Key difference:
  Old: one hard-coded method per service (process, process_payment, process_movie …)
  New: one generic method — call(service_name) — works for any service in the registry.

Adding a new downstream service requires:
  1. Add an entry to services_config.json  ← only change needed
  2. Redeploy (docker compose up --build)

Per-service strategy behaviour:
  "fallback"  — circuit OPEN or call fails → route to fallback-service, degraded=True
  "escalate"  — circuit OPEN or call fails → raise RuntimeError → HTTP 503 (no fallback)

Circuit breakers are independent per service.  A trip on movie-service does NOT
affect payment-service or core-service.

CloudWatch emissions:
  - FallbackUsedCount     : every time a fallback response is returned
  - CircuitBreakerState   : gauge (0=CLOSED, 1=HALF_OPEN, 2=OPEN) on every transition
  - CircuitBreakerOpenCount : incremented each time a circuit trips to OPEN
"""

import logging

from app.clients.generic_client import GenericServiceClient
from app.models.schemas import HealthResponse, ProcessResponse
from app.publishers.cloudwatch_publisher import CloudWatchMetricsPublisher
from app.services.circuit_breaker import CircuitState
from app.services.service_registry import ServiceConfig, ServiceRegistry

logger = logging.getLogger(__name__)

_STATE_GAUGE = {
    CircuitState.CLOSED:    0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN:      2,
}


class GatewayService:
    def __init__(
        self,
        registry:             ServiceRegistry,
        fallback_url:         str,
        fallback_timeout:     float,
        service_name:         str,
        cloudwatch_publisher: CloudWatchMetricsPublisher,
    ) -> None:
        """
        Parameters
        ----------
        registry             : all registered downstream services + their circuit breakers
        fallback_url         : full URL of fallback-service endpoint, e.g.
                               "http://fallback-service:8002/fallback"
        fallback_timeout     : timeout for fallback-service calls
        service_name         : identity string used in HealthResponse
        cloudwatch_publisher : may be a no-op when CLOUDWATCH_ENABLED=false
        """
        self.registry             = registry
        self.fallback_url         = fallback_url
        self.fallback_timeout     = fallback_timeout
        self.service_name         = service_name
        self.cloudwatch_publisher = cloudwatch_publisher

    # ── public ────────────────────────────────────────────────────────────────

    async def call(self, service_name: str) -> ProcessResponse:
        """
        Generic request handler — works for every service in the registry.

        Flow:
          1. Look up ServiceConfig (raises KeyError for unknown names).
          2. Check this service's circuit breaker.
          3. If OPEN → apply strategy (fallback or escalate).
          4. Try the primary service.
          5. On success → record_success(), return response.
          6. On failure → record_failure(), apply strategy.
        """
        config = self.registry.get(service_name)   # KeyError if not registered
        cb     = config.circuit_breaker
        client = GenericServiceClient(config.url, config.timeout)

        # ── Step 1: check circuit ─────────────────────────────────────────────
        state_before = cb.state
        can_call     = cb.can_call_core()
        state_after  = cb.state

        if state_before != state_after:
            self._emit_state_change(state_after, service_name)

        if not can_call:
            logger.warning(
                "GATEWAY [%s]: circuit OPEN — applying strategy=%s",
                service_name, config.strategy,
            )
            return await self._apply_strategy(config, reason="circuit OPEN")

        # ── Step 2: try the primary service ───────────────────────────────────
        try:
            data         = await client.call()
            state_before = cb.state
            cb.record_success()
            state_after  = cb.state
            if state_before != state_after:
                self._emit_state_change(state_after, service_name)
            logger.info("GATEWAY [%s]: OK — source=%s", service_name, service_name)
            return ProcessResponse(source=service_name, result=data, degraded=False)

        except Exception as exc:
            logger.warning("GATEWAY [%s]: call failed (%s)", service_name, exc)
            state_before = cb.state
            cb.record_failure()
            state_after  = cb.state
            if state_before != state_after:
                self._emit_state_change(state_after, service_name)

        # ── Step 3: primary failed — apply strategy ───────────────────────────
        return await self._apply_strategy(config, reason="call failed")

    def health(self) -> HealthResponse:
        """api-service is always healthy if this method is reachable."""
        return HealthResponse(status="healthy", service=self.service_name)

    # ── private ───────────────────────────────────────────────────────────────

    async def _apply_strategy(self, config: ServiceConfig, reason: str) -> ProcessResponse:
        """
        Decide what to do when the primary service is unavailable.

        fallback  → call fallback-service, return degraded response.
        escalate  → raise RuntimeError (route handler converts to HTTP 503).
        """
        if config.strategy == "fallback":
            logger.warning(
                "FALLBACK_TRIGGERED [%s]: %s — routing to fallback-service",
                config.name, reason,
            )
            data = await self._call_fallback()
            self.cloudwatch_publisher.record_fallback_used(config.name)
            return ProcessResponse(source="fallback-service", result=data, degraded=True)

        # strategy == "escalate"
        logger.warning(
            "ESCALATE [%s]: %s — no fallback available, returning 503",
            config.name, reason,
        )
        raise RuntimeError(
            f"{config.name} unavailable ({reason}) — strategy=escalate, no fallback"
        )

    async def _call_fallback(self) -> dict:
        """Call fallback-service and return the raw JSON dict."""
        client = GenericServiceClient(self.fallback_url, self.fallback_timeout)
        return await client.call()

    def _emit_state_change(self, new_state: CircuitState, target: str) -> None:
        gauge = _STATE_GAUGE[new_state]
        self.cloudwatch_publisher.record_circuit_state(target, gauge)
        if new_state == CircuitState.OPEN:
            self.cloudwatch_publisher.record_circuit_open(target)
            logger.info("CloudWatch [%s]: CircuitBreakerOpenCount+1 state=%d", target, gauge)
        else:
            logger.info("CloudWatch [%s]: CircuitBreakerState=%d (%s)", target, gauge, new_state.value)
