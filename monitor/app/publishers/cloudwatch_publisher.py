"""
CloudWatchMetricsPublisher — records custom metrics to AWS CloudWatch.

This is OPTIONAL.  Enable it by setting CLOUDWATCH_ENABLED=true.
When disabled, every method is a no-op so the rest of the monitor code
does not need to check a flag.

Metrics published:
  CoreServiceFailureCount  — incremented each time a failure event fires
  RecoveryActionCount      — incremented each time a recovery event fires
  (namespace: SelfHealingSystem)
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

    def _put(self, metric_name: str, value: float, dimensions: list[dict]) -> None:
        """Internal helper. Skips silently if CloudWatch is disabled."""
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
        except (ClientError, BotoCoreError) as exc:
            # Never crash the monitor over a metrics failure
            logger.warning("CloudWatchMetricsPublisher: put_metric_data failed — %s", exc)

    def record_failure(self, service_name: str) -> None:
        """Increment CoreServiceFailureCount for the given service."""
        logger.debug("CloudWatch: recording failure for %s", service_name)
        self._put(
            metric_name="CoreServiceFailureCount",
            value=1,
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )

    def record_recovery(self, service_name: str) -> None:
        """Increment RecoveryActionCount for the given service."""
        logger.debug("CloudWatch: recording recovery for %s", service_name)
        self._put(
            metric_name="RecoveryActionCount",
            value=1,
            dimensions=[{"Name": "ServiceName", "Value": service_name}],
        )
