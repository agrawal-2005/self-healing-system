"""
Dependency injection wiring for api-service.

All construction is here so route handlers stay unaware of how objects are built.
Singletons are created once at import time.

Phase 8 (config-driven gateway):
  - ServiceRegistry reads services_config.json and creates one CircuitBreaker per service.
  - GatewayService replaces ApiService — it is generic and works for any registered service.
  - Adding a new downstream service requires only a services_config.json change; no code here.
"""

from app.config.settings import settings
from app.publishers.cloudwatch_publisher import CloudWatchMetricsPublisher
from app.services.gateway_service import GatewayService
from app.services.service_registry import ServiceRegistry

# ── Singletons ────────────────────────────────────────────────────────────────

_registry = ServiceRegistry.from_config_file(
    path                        = settings.services_config_path,
    cb_failure_threshold        = settings.circuit_failure_threshold,
    cb_recovery_timeout_seconds = settings.circuit_recovery_timeout_seconds,
    cb_half_open_max_calls      = settings.circuit_half_open_max_calls,
    default_timeout             = settings.request_timeout,
)

_cloudwatch_publisher = CloudWatchMetricsPublisher(
    region    = settings.aws_region,
    namespace = settings.cloudwatch_namespace,
    enabled   = settings.cloudwatch_enabled,
)

_gateway = GatewayService(
    registry             = _registry,
    fallback_url         = f"{settings.fallback_service_url}/fallback",
    fallback_timeout     = settings.request_timeout,
    service_name         = settings.service_name,
    cloudwatch_publisher = _cloudwatch_publisher,
)


# ── Provider functions (used with FastAPI Depends) ────────────────────────────

def get_gateway_service() -> GatewayService:
    """Returns the shared GatewayService instance."""
    return _gateway
