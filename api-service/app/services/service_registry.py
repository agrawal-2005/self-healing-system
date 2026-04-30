"""
ServiceRegistry — loads service configs from services_config.json and creates
one CircuitBreaker per service.

Why this exists:
  Without this, every new service needs code changes in ApiService, dependencies.py,
  settings.py, and routes.  With this, you add one JSON block and redeploy — done.

What it stores per service:
  name       — service identifier, e.g. "core-service"
  url        — full call URL (gateway_url + gateway_endpoint)
  strategy   — "fallback" or "escalate" (what to do when circuit is OPEN or call fails)
  timeout    — per-service HTTP timeout in seconds
  circuit_breaker — independent CircuitBreaker instance

Only services that have both "gateway_url" and "gateway_endpoint" in the config
are registered here.  Services like fallback-service and recovery-agent are
monitored but not routed through the gateway, so they have no gateway fields.
"""

import json
import logging
import os
from dataclasses import dataclass

from app.services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

VALID_STRATEGIES = {"fallback", "escalate"}


@dataclass
class ServiceConfig:
    name:            str
    url:             str            # gateway_url + gateway_endpoint
    strategy:        str            # "fallback" | "escalate"
    timeout:         float
    circuit_breaker: CircuitBreaker


class ServiceRegistry:
    """
    Immutable registry of routable services, built once at startup.

    Usage:
        registry = ServiceRegistry.from_config_file(path, cb_threshold, cb_timeout, cb_half_open, default_timeout)
        config   = registry.get("core-service")   # raises KeyError if unknown
        names    = registry.names()               # list of registered service names
    """

    def __init__(self, configs: list[ServiceConfig]) -> None:
        self._map: dict[str, ServiceConfig] = {cfg.name: cfg for cfg in configs}

    def get(self, name: str) -> ServiceConfig:
        if name not in self._map:
            raise KeyError(
                f"Service {name!r} is not registered in the gateway. "
                f"Known services: {self.names()}"
            )
        return self._map[name]

    def names(self) -> list[str]:
        return list(self._map.keys())

    @classmethod
    def from_config_file(
        cls,
        path: str,
        cb_failure_threshold: int,
        cb_recovery_timeout_seconds: int,
        cb_half_open_max_calls: int,
        default_timeout: float,
    ) -> "ServiceRegistry":
        """
        Read services_config.json and build the registry.

        Only entries with both gateway_url and gateway_endpoint are included.
        Falls back to an empty registry (with a warning) if the file is missing.

        Parameters
        ----------
        path                        : path to services_config.json
        cb_failure_threshold        : circuit breaker failures before OPEN
        cb_recovery_timeout_seconds : seconds in OPEN before probing
        cb_half_open_max_calls      : probe calls allowed in HALF_OPEN
        default_timeout             : used when service entry has no "timeout" field
        """
        resolved = os.path.abspath(path)
        if not os.path.exists(resolved):
            logger.error(
                "ServiceRegistry: config not found at %s — no services registered", resolved
            )
            return cls([])

        try:
            with open(resolved, "r") as f:
                config = json.load(f)
        except Exception as exc:
            logger.error("ServiceRegistry: failed to parse %s — %s", resolved, exc)
            return cls([])

        configs: list[ServiceConfig] = []
        for entry in config.get("services", []):
            name              = entry.get("service_name", "")
            gateway_url       = entry.get("gateway_url")
            gateway_endpoint  = entry.get("gateway_endpoint")

            # Skip services not intended to be routed through the gateway
            if not gateway_url or not gateway_endpoint:
                continue

            if not name:
                logger.warning(
                    "ServiceRegistry: skipping entry with empty service_name — %r", entry
                )
                continue

            strategy = entry.get("strategy", "fallback")
            if strategy not in VALID_STRATEGIES:
                logger.warning(
                    "ServiceRegistry [%s]: unknown strategy %r — defaulting to 'fallback'",
                    name, strategy,
                )
                strategy = "fallback"

            # Validate timeout: a 0 or negative value would make every call
            # raise immediately, masking the underlying service as "always
            # failing" and thrashing the circuit breaker.
            try:
                timeout = float(entry.get("timeout", default_timeout))
            except (TypeError, ValueError):
                logger.warning(
                    "ServiceRegistry [%s]: invalid timeout %r — using default %.1fs",
                    name, entry.get("timeout"), default_timeout,
                )
                timeout = float(default_timeout)
            if timeout <= 0:
                logger.warning(
                    "ServiceRegistry [%s]: non-positive timeout %.2f — using default %.1fs",
                    name, timeout, default_timeout,
                )
                timeout = float(default_timeout)

            full_url = f"{gateway_url.rstrip('/')}{gateway_endpoint}"

            cb = CircuitBreaker(
                failure_threshold        = cb_failure_threshold,
                recovery_timeout_seconds = cb_recovery_timeout_seconds,
                half_open_max_calls      = cb_half_open_max_calls,
            )

            configs.append(ServiceConfig(
                name            = name,
                url             = full_url,
                strategy        = strategy,
                timeout         = timeout,
                circuit_breaker = cb,
            ))
            logger.info(
                "ServiceRegistry: registered %r  url=%s  strategy=%s  timeout=%.1fs",
                name, full_url, strategy, timeout,
            )

        logger.info("ServiceRegistry: %d service(s) ready — %s", len(configs), [c.name for c in configs])
        return cls(configs)
