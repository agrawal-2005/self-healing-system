"""
monitor.py — entry point for the self-healing monitor.

Phase 7 change: services are no longer hardcoded here.
They are loaded dynamically from services_config.json so adding a new
service to the registry automatically starts monitoring it — no code change needed.

This file:
  1. Reads settings.
  2. Loads service list from services_config.json (falls back to defaults if missing).
  3. Builds all class instances (wiring / DI).
  4. Starts the MonitorService main loop.
"""

import json
import logging
import os

from app.checkers.health_checker import HealthChecker
from app.checkers.latency_checker import LatencyChecker
from app.config.settings import settings
from app.publishers.cloudwatch_publisher import CloudWatchMetricsPublisher
from app.publishers.eventbridge_publisher import EventBridgePublisher
from app.services.monitor_service import MonitorService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [monitor] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

# ── Default services (Phase 5/6 fallback) ────────────────────────────────────
_DEFAULT_SERVICES = {
    "api-service":      "http://localhost:8000",
    "core-service":     "http://localhost:8001",
    "fallback-service": "http://localhost:8002",
}


def _load_services_from_config(config_path: str) -> dict[str, str]:
    """
    Load the service list from services_config.json.

    Each entry's health_url becomes the value in the returned dict.
    Falls back to _DEFAULT_SERVICES if the file doesn't exist or is malformed.
    """
    resolved = os.path.abspath(config_path)
    if not os.path.exists(resolved):
        logger.warning(
            "services_config.json not found at %s — using default services (Phase 5/6 mode)",
            resolved,
        )
        return _DEFAULT_SERVICES

    try:
        with open(resolved, "r") as f:
            config = json.load(f)

        services: dict[str, str] = {}
        for svc in config.get("services", []):
            name = svc["service_name"]
            # health_url is the Docker-internal URL (e.g. http://core-service:8001/health)
            # Strip /health suffix — MonitorService appends /health itself via health_checker
            url = svc["health_url"].removesuffix("/health")
            services[name] = url

        logger.info(
            "Loaded %d services from %s: %s",
            len(services),
            resolved,
            list(services.keys()),
        )
        return services

    except Exception as exc:
        logger.error(
            "Failed to parse %s (%s) — using default services", resolved, exc
        )
        return _DEFAULT_SERVICES


def build_monitor() -> MonitorService:
    """
    Construct and wire all monitor components.
    Reading constructor arguments here keeps the full dependency graph visible at a glance.
    """
    services = _load_services_from_config(settings.services_config_path)

    health_checker = HealthChecker(
        timeout_seconds=settings.request_timeout_seconds,
    )

    latency_checker = LatencyChecker(
        warn_ms=settings.latency_warn_ms,
        slow_ms=settings.latency_slow_ms,
    )

    eventbridge_publisher = EventBridgePublisher(
        region      = settings.aws_region,
        event_bus   = settings.eventbridge_event_bus,
        source      = settings.eventbridge_source,
        detail_type = settings.eventbridge_detail_type,
        dry_run     = settings.dry_run or not settings.eventbridge_enabled,
    )

    cloudwatch_publisher = CloudWatchMetricsPublisher(
        region    = settings.aws_region,
        enabled   = settings.cloudwatch_enabled,
        namespace = settings.cloudwatch_namespace,
    )

    return MonitorService(
        services              = services,
        health_checker        = health_checker,
        latency_checker       = latency_checker,
        eventbridge_publisher = eventbridge_publisher,
        cloudwatch_publisher  = cloudwatch_publisher,
        check_interval        = settings.check_interval_seconds,
        cooldown_seconds      = settings.event_cooldown_seconds,
    )


if __name__ == "__main__":
    logger.info("=== Self-Healing Monitor (Phase 7 — Multi-Service) ===")
    logger.info("EventBridge: %s", "ENABLED" if settings.eventbridge_enabled else "DISABLED")
    logger.info("CloudWatch:  %s", "ENABLED" if settings.cloudwatch_enabled else "DISABLED")
    logger.info("Config path: %s", settings.services_config_path)
    logger.info("Dry run:     %s", settings.dry_run)
    logger.info("=" * 40)

    monitor = build_monitor()
    monitor.run()
