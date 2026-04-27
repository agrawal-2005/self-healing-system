"""
CloudWatchMetricsPublisher for recovery-agent.

Emits three metric types under the SelfHealingSystem namespace:

  RecoverySuccessCount   — incremented when a docker action completes with success=True.
                           Dimensions: ServiceName=recovery-agent, TargetService, Action

  RecoveryFailureCount   — incremented when a docker action completes with success=False.
                           Dimensions: ServiceName=recovery-agent, TargetService, Action

  RecoveryDurationMs     — duration of the docker command in milliseconds.
                           Unit: Milliseconds  (allows CloudWatch to compute p50/p99)
                           Dimensions: ServiceName=recovery-agent, TargetService, Action

Enable with:
  CLOUDWATCH_ENABLED=true
  CLOUDWATCH_NAMESPACE=SelfHealingSystem
  AWS_DEFAULT_REGION=us-east-1

When disabled (default), every method is a no-op.
If CloudWatch fails, recovery-agent NEVER crashes — error is logged and swallowed.
"""

import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class CloudWatchMetricsPublisher:
    """
    Thin wrapper around boto3 put_metric_data for recovery-agent metrics.

    All public methods silently no-op when enabled=False.
    All public methods silently swallow AWS errors so recovery never fails
    due to an observability side-effect.
    """

    def __init__(self, region: str, namespace: str, enabled: bool = False) -> None:
        self.enabled    = enabled
        self._namespace = namespace
        if enabled:
            self._client = boto3.client("cloudwatch", region_name=region)
            logger.info(
                "CloudWatchMetricsPublisher (recovery-agent): enabled (namespace=%s region=%s)",
                namespace, region,
            )
        else:
            self._client = None
            logger.info(
                "CloudWatchMetricsPublisher (recovery-agent): disabled — metrics will not be sent"
            )

    # ── public metrics ────────────────────────────────────────────────────────

    def record_recovery_success(self, target_service: str, action: str) -> None:
        """
        Call once when the docker command exits with returncode=0 (success=True).
        """
        self._put(
            metric_name="RecoverySuccessCount",
            value=1.0,
            unit="Count",
            dimensions=[
                {"Name": "ServiceName",  "Value": "recovery-agent"},
                {"Name": "TargetService", "Value": target_service},
                {"Name": "Action",        "Value": action},
            ],
        )

    def record_recovery_failure(self, target_service: str, action: str) -> None:
        """
        Call once when the docker command exits with a non-zero returncode (success=False).
        """
        self._put(
            metric_name="RecoveryFailureCount",
            value=1.0,
            unit="Count",
            dimensions=[
                {"Name": "ServiceName",  "Value": "recovery-agent"},
                {"Name": "TargetService", "Value": target_service},
                {"Name": "Action",        "Value": action},
            ],
        )

    def record_recovery_duration(
        self, target_service: str, action: str, duration_ms: float
    ) -> None:
        """
        Call after every action (success or failure) with the wall-clock duration.
        Using unit=Milliseconds lets CloudWatch compute avg/p50/p99 in dashboards.
        """
        self._put(
            metric_name="RecoveryDurationMs",
            value=duration_ms,
            unit="Milliseconds",
            dimensions=[
                {"Name": "ServiceName",  "Value": "recovery-agent"},
                {"Name": "TargetService", "Value": target_service},
                {"Name": "Action",        "Value": action},
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
                "CloudWatch (recovery-agent): %s=%.1f dims=%s",
                metric_name, value, dimensions,
            )
        except (ClientError, BotoCoreError) as exc:
            # A CloudWatch failure must never abort the recovery action.
            logger.warning(
                "CloudWatchMetricsPublisher (recovery-agent): put_metric_data failed — %s", exc
            )
