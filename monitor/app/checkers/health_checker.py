"""
HealthChecker — performs a single HTTP GET /health call and returns a result.

Single responsibility:
  Given a service name and base URL, call /health, measure the latency,
  and return a HealthCheckResult.  It does NOT decide what to do with the result.

Why separate from MonitorService?
  - You can swap this with a TCP checker, gRPC checker, etc.
  - Easy to unit-test in isolation with a fake HTTP server.
"""

import logging
import time
from datetime import datetime, timezone

import requests

from app.models.schemas import HealthCheckResult, ServiceStatus

logger = logging.getLogger(__name__)


class HealthChecker:
    def __init__(self, timeout_seconds: float = 3.0) -> None:
        self.timeout = timeout_seconds

    def check(self, service_name: str, base_url: str) -> HealthCheckResult:
        """
        GET {base_url}/health, measure latency, return HealthCheckResult.

        Never raises — all exceptions are caught and returned as a DOWN/TIMEOUT result
        so the monitor loop never dies because one service is unreachable.
        """
        url = f"{base_url}/health"
        timestamp = datetime.now(timezone.utc).isoformat()
        start = time.monotonic()

        try:
            resp = requests.get(url, timeout=self.timeout)
            latency_ms = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                # Status is UP for now; LatencyChecker will downgrade to SLOW if needed.
                status = ServiceStatus.UP
            else:
                status = ServiceStatus.DOWN
                logger.warning(
                    "HealthChecker [%s]: HTTP %d (%.0fms)",
                    service_name, resp.status_code, latency_ms,
                )

            return HealthCheckResult(
                service_name=service_name,
                url=url,
                status=status,
                latency_ms=round(latency_ms, 1),
                timestamp=timestamp,
                http_status_code=resp.status_code,
            )

        except requests.exceptions.Timeout:
            latency_ms = self.timeout * 1000
            logger.error("HealthChecker [%s]: TIMEOUT (>%.0fms)", service_name, latency_ms)
            return HealthCheckResult(
                service_name=service_name,
                url=url,
                status=ServiceStatus.TIMEOUT,
                latency_ms=round(latency_ms, 1),
                timestamp=timestamp,
                error="Request timed out",
            )

        except requests.exceptions.ConnectionError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.error("HealthChecker [%s]: DOWN — %s", service_name, exc)
            return HealthCheckResult(
                service_name=service_name,
                url=url,
                status=ServiceStatus.DOWN,
                latency_ms=round(latency_ms, 1),
                timestamp=timestamp,
                error=str(exc),
            )

        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.exception("HealthChecker [%s]: unexpected error — %s", service_name, exc)
            return HealthCheckResult(
                service_name=service_name,
                url=url,
                status=ServiceStatus.DOWN,
                latency_ms=round(latency_ms, 1),
                timestamp=timestamp,
                error=str(exc),
            )
