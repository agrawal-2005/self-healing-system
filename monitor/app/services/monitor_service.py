"""
MonitorService — the coordinator.  Owns the main monitoring loop.

Responsibilities:
  1. Run a check cycle (call HealthChecker for each service).
  2. Pass results through LatencyChecker for classification.
  3. Decide whether to publish an event (cooldown logic lives here).
  4. Publish to EventBridge via EventBridgePublisher.
  5. Record metrics via CloudWatchMetricsPublisher.
  6. Print colour-coded logs to the terminal.

Why cooldown logic belongs here (not in publishers):
  EventBridgePublisher's job is "publish this event".
  MonitorService's job is "decide WHEN to publish".
  Mixing them would make EventBridgePublisher harder to test and reuse.
"""

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

# ANSI colour codes for terminal output
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_RESET  = "\033[0m"


# class _EventCooldown:
#     """
#     Prevents duplicate events for the same service+failure_type within a window.

#     Example: if core-service crashes at 10:00:00 and is still down at 10:00:05,
#     we should NOT send a second "crash" event.  We wait until cooldown expires,
#     then send one more.  This keeps Lambda invocation count low.
#     """

#     def __init__(self, cooldown_seconds: int) -> None:
#         self.cooldown_seconds = cooldown_seconds
#         # key: "service_name:failure_type"  value: monotonic time of last send
#         self._last_sent: dict[str, float] = {}

#     def should_send(self, service_name: str, failure_type: str) -> bool:
#         key = f"{service_name}:{failure_type}"
#         now = time.monotonic()
#         last = self._last_sent.get(key)
#         if last is None or (now - last) >= self.cooldown_seconds:
#             self._last_sent[key] = now
#             return True
#         return False

#     def clear(self, service_name: str) -> None:
#         """Called when a service recovers — allows a fresh event on next failure."""
#         keys = [k for k in self._last_sent if k.startswith(f"{service_name}:")]
#         for k in keys:
#             del self._last_sent[k]


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
        """
        Parameters
        ----------
        services               : dict mapping service_name → base_url
        health_checker         : performs the HTTP /health call
        latency_checker        : upgrades UP → SLOW/VERY_SLOW if needed
        eventbridge_publisher  : sends events to AWS EventBridge
        cloudwatch_publisher   : records CloudWatch metrics (may be no-op)
        check_interval         : seconds between check cycles
        cooldown_seconds       : minimum seconds between duplicate events
        """
        self.services              = services
        self.health_checker        = health_checker
        self.latency_checker       = latency_checker
        self.eventbridge_publisher = eventbridge_publisher
        self.cloudwatch_publisher  = cloudwatch_publisher
        self.check_interval        = check_interval
        self._cooldown             = EventCooldown(cooldown_seconds)

    # ── public ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Blocking main loop.  Call this from the entry point (monitor.py).
        Runs forever, sleeping check_interval seconds between cycles.
        """
        logger.info(
            "MonitorService: starting — %d services, interval=%ds",
            len(self.services),
            self.check_interval,
        )
        while True:
            print()  # blank line between rounds for readability
            self.run_check_cycle()
            time.sleep(self.check_interval)

    def run_check_cycle(self) -> None:
        """
        Single check cycle.  Called once per interval.
        Public so it can be called directly in tests.
        """
        results = []
        for name, url in self.services.items():
            result = self.health_checker.check(name, url)
            result = self.latency_checker.classify(result)
            results.append(result)
            self._process_result(result)
            self._log_result(result)

        self._print_summary(results)

    # ── private ───────────────────────────────────────────────────────────────

    def _process_result(self, result: HealthCheckResult) -> None:
        """
        Decide whether this result should produce an EventBridge event.

        Decision rules:
          - If failure detected AND cooldown has expired → publish event
          - If service is healthy (UP) → clear cooldown so next failure fires fresh
        """
        if self.latency_checker.is_failure(result):
            failure_type = self.latency_checker.failure_type(result)

            if not self._cooldown.should_send(result.service_name, failure_type):
                logger.info(
                    "Suppressing duplicate event service=%s failure=%s due to cooldown",
                    result.service_name,
                    failure_type,
                )
                return

            # If we reach here → event should be sent
            event = FailureEvent(
                service_name=result.service_name,
                failure_type=failure_type,
                latency_ms=result.latency_ms,
                timestamp=result.timestamp,
                health_endpoint=result.url,
            )

            published = self.eventbridge_publisher.publish(event)

            if published:
                self.cloudwatch_publisher.record_failure(result.service_name)
        else:
            # Service is healthy — clear cooldown so next failure creates a fresh event
            self._cooldown.clear(result.service_name)

    def _log_result(self, result: HealthCheckResult) -> None:
        """Colour-coded terminal line for each service check."""
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
        up   = sum(1 for r in results if r.status == ServiceStatus.UP)
        total = len(results)
        if up == total:
            logger.info("%sAll %d/%d services healthy%s", _GREEN, up, total, _RESET)
        else:
            logger.warning(
                "%s%d/%d services healthy — check above%s",
                _RED, up, total, _RESET,
            )
