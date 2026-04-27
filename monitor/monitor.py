"""
monitor.py — entry point for the class-based monitor.

This file:
  1. Reads settings.
  2. Builds all class instances (wiring / DI).
  3. Starts the MonitorService main loop.

No business logic lives here — only construction and startup.
"""

import logging

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


def build_monitor() -> MonitorService:
    """
    Construct and wire all monitor components.

    Reading the constructor arguments here (not inside each class) means
    you can see the full dependency graph at a glance.
    """
    services = {
        "api-service":      settings.api_service_url,
        "core-service":     settings.core_service_url,
        "fallback-service": settings.fallback_service_url,
    }

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
        region  = settings.aws_region,
        enabled = settings.cloudwatch_enabled,
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
    logger.info("=== Self-Healing Monitor (Phase 2) ===")
    logger.info("EventBridge: %s", "ENABLED" if settings.eventbridge_enabled else "DISABLED")
    logger.info("CloudWatch:  %s", "ENABLED" if settings.cloudwatch_enabled else "DISABLED")
    logger.info("Dry run:     %s", settings.dry_run)
    logger.info("=" * 40)

    monitor = build_monitor()
    monitor.run()
