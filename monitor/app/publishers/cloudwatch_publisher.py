"""
CloudWatchMetricsPublisher — records custom metrics to AWS CloudWatch.

Enable with CLOUDWATCH_ENABLED=true in monitor/.env.
When disabled, every method is a no-op — no code changes needed in callers.

Metrics published (namespace: SelfHealingSystem):
  FailureDetectedCount   — each time monitor detects a service failure
                           Dimensions: ServiceName, FailureType
  RecoverySuccessCount   — each time Lambda recovery succeeds (future)
  RecoveryFailureCount   — each time Lambda recovery fails (future)
  CircuitBreakerOpenCount — each time circuit breaker opens (from api-service)
  FallbackUsedCount      — each time api-service falls back to fallback-service

Generic methods for any metric:
  put_count(name, dims, value=1)     — unit=Count
  put_duration(name, dims, ms)       — unit=Milliseconds
  put_gauge(name, dims, value)       — unit=None (dimensionless gauge)

Why custom metrics?
  These let you build CloudWatch dashboards and alarms, e.g.:
  "Alert me if FailureDetectedCount > 5 in 10 minutes"
  "Alert me if RecoveryFailureCount > 0 in any period"
"""

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class CloudWatchMetricsPublisher:
    def __init__(
        self,
        region: str,
        enabled: bool = False,
        namespace: str = "SelfHealingSystem",
    ) -> None:
        self.enabled    = enabled
        self._namespace = namespace
        if enabled:
            self._client = boto3.client("cloudwatch", region_name=region)
            logger.info(
                "CloudWatchMetricsPublisher: enabled (namespace=%s region=%s)",
                namespace, region,
            )
        else:
            self._client = None
            logger.info("CloudWatchMetricsPublisher: disabled — metrics will not be sent")

    # ── named metrics (most callers use these) ────────────────────────────────

    def record_failure_detected(
        self, service_name: str, failure_type: str = "unknown"
    ) -> None:
        """
        Call when monitor detects a service failure and publishes an EventBridge event.
        failure_type: "crash", "timeout", "slow", "very_slow" — matches LatencyChecker output.
        """
        self._put(
            metric_name="FailureDetectedCount",
            value=1.0,
            unit="Count",
            dimensions=[
                {"Name": "ServiceName", "Value": service_name},
                {"Name": "FailureType", "Value": failure_type},
            ],
        )

    def record_recovery_success(self, service_name: str) -> None:
        """Call when a Lambda-triggered recovery action succeeds."""
        self._put(
            metric_name="RecoverySuccessCount",
            value=1.0,
            unit="Count",
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_recovery_failure(self, service_name: str) -> None:
        """Call when a Lambda-triggered recovery action fails."""
        self._put(
            metric_name="RecoveryFailureCount",
            value=1.0,
            unit="Count",
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_circuit_open(self, service_name: str) -> None:
        """Call when the circuit breaker opens for a service."""
        self._put(
            metric_name="CircuitBreakerOpenCount",
            value=1.0,
            unit="Count",
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_fallback_used(self, service_name: str) -> None:
        """Call when api-service falls back to fallback-service."""
        self._put(
            metric_name="FallbackUsedCount",
            value=1.0,
            unit="Count",
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    # ── generic helpers (for callers that want to emit arbitrary metrics) ─────

    def put_count(
        self,
        metric_name: str,
        dimensions: list[dict],
        value: float = 1.0,
    ) -> None:
        """Emit any Count metric. value defaults to 1."""
        self._put(metric_name, value, "Count", dimensions)

    def put_duration(
        self,
        metric_name: str,
        dimensions: list[dict],
        duration_ms: float,
    ) -> None:
        """
        Emit a duration metric in milliseconds.
        Using unit=Milliseconds lets CloudWatch compute avg/p50/p99.
        """
        self._put(metric_name, duration_ms, "Milliseconds", dimensions)

    def put_gauge(
        self,
        metric_name: str,
        dimensions: list[dict],
        value: float,
    ) -> None:
        """
        Emit a dimensionless gauge (e.g. CircuitBreakerState 0/1/2).
        Uses CloudWatch unit=None.
        """
        self._put(metric_name, value, "None", dimensions)

    # ── backward-compat aliases (kept for Phase 2/3 callers) ─────────────────

    def record_failure(self, service_name: str) -> None:
        """Alias for record_failure_detected without failure_type dimension."""
        self.record_failure_detected(service_name, failure_type="unknown")

    def record_recovery(self, service_name: str) -> None:
        """Alias for record_recovery_success."""
        self.record_recovery_success(service_name)

    # ── internal ──────────────────────────────────────────────────────────────

    def _put(
        self,
        metric_name: str,
        value: float,
        unit: str,
        dimensions: list[dict],
    ) -> None:
        """Send one metric data point. Skips silently if CloudWatch is disabled."""
        if not self.enabled:
            return
        try:
            self._client.put_metric_data(
                Namespace=self._namespace,
                MetricData=[
                    {
                        "MetricName": metric_name,
                        "Value":      value,
                        "Unit":       unit,
                        "Dimensions": dimensions,
                    }
                ],
            )
            logger.debug(
                "CloudWatch (monitor): %s=%.1f unit=%s dims=%s",
                metric_name, value, unit, dimensions,
            )
        except (ClientError, BotoCoreError) as exc:
            # Never crash the monitor over a metrics failure
            logger.warning(
                "CloudWatchMetricsPublisher: put_metric_data failed — %s", exc
            )
