"""
LatencyChecker — upgrades a UP result to SLOW or VERY_SLOW if latency is high.

Why separate from HealthChecker?
  HealthChecker answers "is the service reachable?".
  LatencyChecker answers "is the service fast enough?".
  Keeping these two questions separate means you can tune thresholds without
  touching the HTTP logic.
"""

import logging

from app.models.schemas import HealthCheckResult, ServiceStatus

logger = logging.getLogger(__name__)


class LatencyChecker:
    def __init__(self, warn_ms: float = 500.0, slow_ms: float = 1000.0) -> None:
        """
        Parameters
        ----------
        warn_ms : float
            Latency above this is classified as SLOW.
        slow_ms : float
            Latency above this is classified as VERY_SLOW.
        """
        self.warn_ms = warn_ms
        self.slow_ms = slow_ms

    def classify(self, result: HealthCheckResult) -> HealthCheckResult:
        """
        Given a HealthCheckResult with status=UP, upgrade the status if
        the latency exceeds a threshold.

        If the status is already DOWN or TIMEOUT, return unchanged.
        """
        if result.status != ServiceStatus.UP:
            return result  # nothing to upgrade — already a failure

        if result.latency_ms > self.slow_ms:
            logger.warning(
                "LatencyChecker [%s]: VERY_SLOW — %.0fms > threshold %.0fms",
                result.service_name, result.latency_ms, self.slow_ms,
            )
            return result.model_copy(update={"status": ServiceStatus.VERY_SLOW})

        if result.latency_ms > self.warn_ms:
            logger.warning(
                "LatencyChecker [%s]: SLOW — %.0fms > threshold %.0fms",
                result.service_name, result.latency_ms, self.warn_ms,
            )
            return result.model_copy(update={"status": ServiceStatus.SLOW})

        return result  # all good

    def is_failure(self, result: HealthCheckResult) -> bool:
        """Returns True for any status that should trigger an EventBridge event."""
        return result.status in (
            ServiceStatus.DOWN,
            ServiceStatus.TIMEOUT,
            ServiceStatus.SLOW,
            ServiceStatus.VERY_SLOW,
        )

    def failure_type(self, result: HealthCheckResult) -> str:
        """
        Maps ServiceStatus to the string Lambda uses to decide which action to take.
        """
        mapping = {
            ServiceStatus.DOWN:      "crash",
            ServiceStatus.TIMEOUT:   "timeout",
            ServiceStatus.SLOW:      "slow",
            ServiceStatus.VERY_SLOW: "slow",
        }
        return mapping.get(result.status, "unknown")
