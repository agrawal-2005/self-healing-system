"""
MonitorService — the coordinator.  Owns the main monitoring loop.

Responsibilities:
  1. Run a check cycle (call HealthChecker for each service).
  2. Pass results through LatencyChecker for classification.
  3. Decide whether to publish an event (cooldown logic lives here).
  4. Publish to EventBridge via EventBridgePublisher.
  5. Record metrics via CloudWatchMetricsPublisher.
  6. Print colour-coded logs to the terminal.

Phase 8 change — parallel health checks:
  run_check_cycle_async() fires all health checks simultaneously via asyncio.gather().
  Total cycle time = slowest single check, NOT sum of all checks.

  With 3s timeout:
    Old (sequential): N services × 3s  →  10 services = 30s cycle
    New (parallel):   max(individual)  →  10 services = 3s  cycle  (if all down)
"""

import asyncio
import logging
import time
from typing import Optional

from app.checkers.health_checker import HealthChecker
from app.checkers.latency_checker import LatencyChecker
from app.models.schemas import FailureEvent, HealthCheckResult, ServiceStatus
from app.publishers.cloudwatch_publisher import CloudWatchMetricsPublisher
from app.publishers.eventbridge_publisher import EventBridgePublisher
from app.services.event_cooldown import EventCooldown

logger = logging.getLogger(__name__)

_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_RESET  = "\033[0m"


class MonitorService:
    def __init__(
        self,
        services: dict[str, str],
        health_checker: HealthChecker,
        latency_checker: LatencyChecker,
        eventbridge_publisher: EventBridgePublisher,
        cloudwatch_publisher: CloudWatchMetricsPublisher,
        check_interval: int,
        cooldown_seconds: int,
    ) -> None:
        self.services              = services
        self.health_checker        = health_checker
        self.latency_checker       = latency_checker
        self.eventbridge_publisher = eventbridge_publisher
        self.cloudwatch_publisher  = cloudwatch_publisher
        self.check_interval        = check_interval
        self._cooldown             = EventCooldown(cooldown_seconds)

    # ── public ────────────────────────────────────────────────────────────────

    async def run_async(self) -> None:
        """
        Async main loop — entry point from monitor.py via asyncio.run().

        All health checks in each cycle fire in parallel.
        asyncio.sleep() yields the event loop so other coroutines can run
        (important once connection pooling is added).
        """
        logger.info(
            "MonitorService: starting — %d services, interval=%ds",
            len(self.services),
            self.check_interval,
        )
        while True:
            print()
            await self.run_check_cycle_async()
            await asyncio.sleep(self.check_interval)

    async def run_check_cycle_async(self) -> None:
        """
        Single async check cycle — all services checked simultaneously.

        asyncio.gather() fires all check_async() coroutines at once and waits
        for the slowest one.  If 10 services are down (3s timeout each), the
        total wait is 3s, not 30s.
        """
        names = list(self.services.keys())
        urls  = list(self.services.values())

        # Fire all health checks simultaneously
        raw_results: list[HealthCheckResult] = await asyncio.gather(
            *[self.health_checker.check_async(name, url) for name, url in zip(names, urls)]
        )

        results = []
        for raw in raw_results:
            result = self.latency_checker.classify(raw)
            results.append(result)
            self._process_result(result)
            self._log_result(result)

        self._print_summary(results)

    # Kept for unit tests that call the sync path directly.
    def run_check_cycle(self) -> None:
        asyncio.run(self.run_check_cycle_async())

    # ── private ───────────────────────────────────────────────────────────────

    def _process_result(self, result: HealthCheckResult) -> None:
        if self.latency_checker.is_failure(result):
            failure_type = self.latency_checker.failure_type(result)

            if not self._cooldown.should_send(result.service_name, failure_type):
                logger.info(
                    "Suppressing duplicate event service=%s failure=%s due to cooldown",
                    result.service_name,
                    failure_type,
                )
                return

            event = FailureEvent(
                service_name     = result.service_name,
                failure_type     = failure_type,
                latency_ms       = result.latency_ms,
                timestamp        = result.timestamp,
                health_endpoint  = result.url,
            )

            published = self.eventbridge_publisher.publish(event)

            if published:
                self.cloudwatch_publisher.record_failure_detected(
                    result.service_name, failure_type
                )
        else:
            self._cooldown.clear(result.service_name)

    def _log_result(self, result: HealthCheckResult) -> None:
        if result.status == ServiceStatus.UP:
            colour, tag = _GREEN, "OK   "
        elif result.status in (ServiceStatus.SLOW, ServiceStatus.VERY_SLOW):
            colour, tag = _YELLOW, result.status.value
        else:
            colour, tag = _RED, result.status.value

        logger.info(
            "%s[%s]%s  %-20s  status=%-10s  latency=%6.1fms",
            colour, tag, _RESET,
            result.service_name,
            result.status.value,
            result.latency_ms,
        )

    def _print_summary(self, results: list[HealthCheckResult]) -> None:
        up    = sum(1 for r in results if r.status == ServiceStatus.UP)
        total = len(results)
        if up == total:
            logger.info("%sAll %d/%d services healthy%s", _GREEN, up, total, _RESET)
        else:
            logger.warning(
                "%s%d/%d services healthy — check above%s",
                _RED, up, total, _RESET,
            )
