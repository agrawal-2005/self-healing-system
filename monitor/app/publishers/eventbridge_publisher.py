"""
EventBridgePublisher — publishes failure events to AWS EventBridge.

Single responsibility: take a FailureEvent and deliver it to EventBridge.
Does NOT decide when to publish — that is MonitorService's job.

boto3 credential resolution order (automatic, nothing to configure manually):
  1. Environment variables: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
  2. ~/.aws/credentials file
  3. IAM instance role (when running on EC2)

If DRY_RUN=true in settings, this class logs the event instead of sending it.
This is useful for local testing without real AWS credentials.
"""

import json
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.models.schemas import FailureEvent

logger = logging.getLogger(__name__)


class EventBridgePublisher:
    def __init__(
        self,
        region: str,
        event_bus: str,
        source: str,
        detail_type: str,
        dry_run: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        region      : AWS region, e.g. "us-east-1"
        event_bus   : EventBridge bus name, "default" for the account default bus
        source      : Custom source string, e.g. "selfhealing.local"
        detail_type : Human-readable label shown in EventBridge console
        dry_run     : When True, log instead of publishing (for local dev)
        """
        self.event_bus   = event_bus
        self.source      = source
        self.detail_type = detail_type
        self.dry_run     = dry_run

        # boto3 client is created once and reused across calls
        self._client = boto3.client("events", region_name=region)

    def publish(self, event: FailureEvent) -> bool:
        """
        Publish a single FailureEvent to EventBridge.

        Returns True on success, False on failure (never raises).
        The monitor loop must continue even if AWS is temporarily unreachable.
        """
        entry = {
            "Source":       self.source,
            "DetailType":   self.detail_type,
            "Detail":       json.dumps(event.model_dump()),
            "EventBusName": self.event_bus,
        }

        if self.dry_run:
            logger.info(
                "EventBridgePublisher [DRY-RUN]: would publish → %s",
                json.dumps(entry, indent=2),
            )
            return True

        try:
            response = self._client.put_events(Entries=[entry])
            failed_count = response.get("FailedEntryCount", 0)

            if failed_count > 0:
                # EventBridge accepted the call but rejected one or more entries.
                # Each failed entry has its own ErrorCode and ErrorMessage.
                failed_entries = [
                    e for e in response.get("Entries", []) if "ErrorCode" in e
                ]
                logger.error(
                    "EventBridgePublisher: %d entry failed — %s",
                    failed_count,
                    failed_entries,
                )
                return False

            logger.info(
                "EventBridgePublisher: published event service=%s failure=%s latency=%.0fms",
                event.service_name,
                event.failure_type,
                event.latency_ms,
            )
            return True

        except (ClientError, BotoCoreError) as exc:
            logger.error("EventBridgePublisher: AWS error — %s", exc)
            return False

        except Exception as exc:
            logger.exception("EventBridgePublisher: unexpected error — %s", exc)
            return False
