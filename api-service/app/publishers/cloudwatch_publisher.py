"""
CloudWatchMetricsPublisher for api-service.

Emits three metric types under the SelfHealingSystem namespace:

  FallbackUsedCount      — incremented every time api-service returns a
                           fallback-service response instead of core-service.
                           Dimensions: ServiceName=api-service, TargetService=core-service

  CircuitBreakerOpenCount — incremented every time the circuit transitions
                            to the OPEN state (either CLOSED→OPEN or HALF_OPEN→OPEN).
                            Dimensions: ServiceName=api-service, TargetService=core-service

  CircuitBreakerState     — gauge written on every state transition.
                            CLOSED=0, HALF_OPEN=1, OPEN=2
                            Lets you graph circuit state over time in CloudWatch.
                            Dimensions: ServiceName=api-service, TargetService=core-service

Enable with:
  CLOUDWATCH_ENABLED=true      (env var or docker-compose)
  CLOUDWATCH_NAMESPACE=SelfHealingSystem
  AWS_DEFAULT_REGION=us-east-1

When disabled (default), every method is a no-op — no code changes needed in callers.
If CloudWatch fails, the app NEVER crashes — the exception is logged and swallowed.
"""

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# Numeric encoding for the CircuitBreakerState gauge
CIRCUIT_STATE_VALUES = {
    "closed":    0,
    "half_open": 1,
    "open":      2,
}


class CloudWatchMetricsPublisher:
    """
    Thin wrapper around boto3 put_metric_data for api-service metrics.

    All public methods silently no-op when enabled=False.
    All public methods silently swallow AWS errors so api-service stays up.
    """

    def __init__(self, region: str, namespace: str, enabled: bool = False) -> None:
        self.enabled    = enabled
        self._namespace = namespace
        if enabled:
            self._client = boto3.client("cloudwatch", region_name=region)
            logger.info(
                "CloudWatchMetricsPublisher (api-service): enabled (namespace=%s region=%s)",
                namespace, region,
            )
        else:
            self._client = None
            logger.info("CloudWatchMetricsPublisher (api-service): disabled — metrics will not be sent")

    # ── public metrics ────────────────────────────────────────────────────────

    def record_fallback_used(self, target_service: str = "core-service") -> None:
        """
        Call once per /process request that returns a fallback response.
        This fires whether the circuit is OPEN (skipped core-service directly)
        OR core-service threw an exception.
        """
        self._put(
            metric_name="FallbackUsedCount",
            value=1.0,
            unit="Count",
            dimensions=[
                {"Name": "ServiceName",  "Value": "api-service"},
                {"Name": "TargetService", "Value": target_service},
            ],
        )

    def record_circuit_open(self, target_service: str = "core-service") -> None:
        """
        Call once per CLOSED→OPEN or HALF_OPEN→OPEN transition.
        Do NOT call on every /process request — only on state change.
        """
        self._put(
            metric_name="CircuitBreakerOpenCount",
            value=1.0,
            unit="Count",
            dimensions=[
                {"Name": "ServiceName",  "Value": "api-service"},
                {"Name": "TargetService", "Value": target_service},
            ],
        )

    def record_circuit_state(self, target_service: str, state_value: int) -> None:
        """
        Call on every circuit state transition.
        state_value: 0=CLOSED, 1=HALF_OPEN, 2=OPEN

        Lets you plot circuit state as a time-series in CloudWatch.
        """
        self._put(
            metric_name="CircuitBreakerState",
            value=float(state_value),
            unit="None",
            dimensions=[
                {"Name": "ServiceName",  "Value": "api-service"},
                {"Name": "TargetService", "Value": target_service},
            ],
        )

    # ── internal ──────────────────────────────────────────────────────────────

    def _put(
        self,
        metric_name: str,
        value: float,
        unit: str,
        dimensions: list[dict],
    ) -> None:
        """Send one data point. Silently skipped when disabled or if AWS errors."""
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
                "CloudWatch (api-service): %s=%.1f dims=%s",
                metric_name, value, dimensions,
            )
        except (ClientError, BotoCoreError) as exc:
            # A CloudWatch failure must never bring down api-service.
            logger.warning(
                "CloudWatchMetricsPublisher (api-service): put_metric_data failed — %s", exc
            )
