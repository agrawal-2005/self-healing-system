"""
S3CrashReportPublisher — uploads crash report files to S3 for durable storage
that survives EC2 restarts/replacements and is accessible from the AWS console.

Design choices (mirrors CloudWatchMetricsPublisher):
  - Silent no-op when disabled (S3_CRASH_REPORTS_BUCKET unset)
  - Silently swallows AWS errors so recovery never fails because of S3
  - Only uploads REAL incidents — skips test reports

Object key layout:
  s3://<bucket>/<prefix>/<service>/<YYYY-MM-DD>/<filename>
  e.g.  s3://self-healing-crash-reports/incidents/core-service/2026-04-30/core-service_2026-04-30T22-31-05Z.txt

Why date-partitioned:
  - Easy to find "today's incidents" in the console
  - Plays well with S3 lifecycle rules (e.g. transition to Glacier after 90d)
  - Athena/S3 Select can query a date range cheaply

Where to view in AWS:
  S3 Console → <bucket-name> → incidents/<service>/<date>/
"""

import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class S3CrashReportPublisher:
    """Uploads crash reports to S3. Silent no-op when bucket is empty/unset."""

    def __init__(
        self,
        bucket: str,
        region: str,
        prefix: str = "incidents",
    ) -> None:
        self.bucket  = bucket.strip() if bucket else ""
        self.region  = region
        self.prefix  = prefix.strip("/") if prefix else "incidents"
        self.enabled = bool(self.bucket)

        if self.enabled:
            self._client = boto3.client("s3", region_name=region)
            logger.info(
                "S3CrashReportPublisher: enabled (bucket=%s prefix=%s region=%s)",
                self.bucket, self.prefix, region,
            )
        else:
            self._client = None
            logger.info(
                "S3CrashReportPublisher: disabled — set S3_CRASH_REPORTS_BUCKET to enable"
            )

    def upload(
        self,
        local_path: str,
        target_service: str,
        is_test: bool,
    ) -> str | None:
        """Upload a crash report file to S3.

        Returns the S3 URI on success, None on failure / disabled / test reports.
        Test reports are intentionally NOT uploaded — they would pollute the
        incidents bucket and trigger noisy alerts.
        """
        if not self.enabled:
            return None
        if is_test:
            logger.info("S3CrashReportPublisher: skipping upload of test report → %s", local_path)
            return None
        if not os.path.isfile(local_path):
            logger.warning("S3CrashReportPublisher: local file missing → %s", local_path)
            return None

        date_partition = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename       = os.path.basename(local_path)
        key            = f"{self.prefix}/{target_service}/{date_partition}/{filename}"

        try:
            self._client.upload_file(
                Filename=local_path,
                Bucket=self.bucket,
                Key=key,
                ExtraArgs={
                    "ContentType": "text/plain; charset=utf-8",
                    "Metadata": {
                        "service":   target_service,
                        "captured":  datetime.now(timezone.utc).isoformat(),
                        "source":    "recovery-agent",
                    },
                },
            )
            uri = f"s3://{self.bucket}/{key}"
            logger.info("S3CrashReportPublisher: uploaded crash report → %s", uri)
            return uri
        except (ClientError, BotoCoreError, S3UploadFailedError) as exc:
            # An S3 failure must never abort the recovery action.
            logger.warning(
                "S3CrashReportPublisher: upload failed (bucket=%s key=%s) — %s",
                self.bucket, key, exc,
            )
            return None
        except Exception as exc:
            # Defensive: anything else (auth errors, network) is also swallowed.
            logger.warning(
                "S3CrashReportPublisher: unexpected upload error (bucket=%s) — %s",
                self.bucket, exc,
            )
            return None
