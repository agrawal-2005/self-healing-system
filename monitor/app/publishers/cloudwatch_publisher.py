"""
CloudWatchMetricsPublisher — records custom metrics to AWS CloudWatch.

Enable with CLOUDWATCH_ENABLED=true in monitor/.env.
When disabled, every method is a no-op — no code changes needed in callers.

Metrics published (namespace: SelfHealingSystem):
  FailureDetectedCount   — each time monitor detects a service failure
  RecoverySuccessCount   — each time Lambda recovery succeeds
  RecoveryFailureCount   — each time Lambda recovery fails
  CircuitBreakerOpenCount — each time circuit breaker opens (from api-service logs, future)
  FallbackUsedCount      — each time api-service falls back to fallback-service

Why custom metrics?
  These let you build CloudWatch dashboards and alarms, e.g.:
  "Alert me if FailureDetectedCount > 5 in 10 minutes"
  "Alert me if RecoveryFailureCount > 0 in any period"
"""

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

_NAMESPACE = "SelfHealingSystem"


class CloudWatchMetricsPublisher:
    def __init__(self, region: str, enabled: bool = False) -> None:
        self.enabled = enabled
        if enabled:
            self._client = boto3.client("cloudwatch", region_name=region)
            logger.info("CloudWatchMetricsPublisher: enabled (namespace=%s)", _NAMESPACE)
        else:
            self._client = None
            logger.info("CloudWatchMetricsPublisher: disabled — metrics will not be sent")

    # ── public metrics ────────────────────────────────────────────────────────

    def record_failure_detected(self, service_name: str) -> None:
        """Call when monitor detects a service failure and publishes an event."""
        self._put(
            metric_name="FailureDetectedCount",
            value=1,
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_recovery_success(self, service_name: str) -> None:
        """Call when a Lambda-triggered recovery action succeeds."""
        self._put(
            metric_name="RecoverySuccessCount",
            value=1,
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_recovery_failure(self, service_name: str) -> None:
        """Call when a Lambda-triggered recovery action fails."""
        self._put(
            metric_name="RecoveryFailureCount",
            value=1,
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_circuit_open(self, service_name: str) -> None:
        """Call when the circuit breaker opens for a service."""
        self._put(
            metric_name="CircuitBreakerOpenCount",
            value=1,
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_fallback_used(self, service_name: str) -> None:
        """Call when api-service falls back to fallback-service."""
        self._put(
            metric_name="FallbackUsedCount",
            value=1,
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    # ── backward-compat aliases (used in Phase 2 code) ────────────────────────

    def record_failure(self, service_name: str) -> None:
        """Alias for record_failure_detected — keeps Phase 2 callers working."""
        self.record_failure_detected(service_name)

    def record_recovery(self, service_name: str) -> None:
        """Alias for record_recovery_success — keeps Phase 2 callers working."""
        self.record_recovery_success(service_name)

    # ── internal ──────────────────────────────────────────────────────────────

    def _put(self, metric_name: str, value: float, dimensions: list[dict]) -> None:
        """Send one metric data point. Skips silently if CloudWatch is disabled."""
        if not self.enabled:
            return
        try:
            self._client.put_metric_data(
                Namespace=_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": metric_name,
                        "Value": value,
                        "Unit": "Count",
                        "Dimensions": dimensions,
                    }
                ],
            )
            logger.debug("CloudWatch: %s +1 for %s", metric_name, dimensions)
        except (ClientError, BotoCoreError) as exc:
            # Never crash the monitor over a metrics failure
            logger.warning("CloudWatchMetricsPublisher: put_metric_data failed — %s", exc)
