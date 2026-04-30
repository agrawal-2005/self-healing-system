"""
HealthChecker — performs a single HTTP GET /health call and returns a result.

Single responsibility:
  Given a service name and base URL, call /health, measure the latency,
  and return a HealthCheckResult.  It does NOT decide what to do with the result.

Two interfaces:
  check()       — synchronous (kept for backward-compat with tests)
  check_async() — async via httpx; used by MonitorService for parallel checks
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
import requests

from app.models.schemas import HealthCheckResult, ServiceStatus

logger = logging.getLogger(__name__)


class HealthChecker:
    def __init__(self, timeout_seconds: float = 3.0) -> None:
        self.timeout = timeout_seconds

    # ── async (parallel-capable) ──────────────────────────────────────────────

    async def check_async(self, service_name: str, base_url: str) -> HealthCheckResult:
        """
        Async version — call with asyncio.gather() to check all services at once.

        Total check-cycle time = slowest single check (not sum of all checks).
        Never raises — all exceptions are caught and returned as DOWN/TIMEOUT.
        """
        url       = f"{base_url}/health"
        timestamp = datetime.now(timezone.utc).isoformat()
        start     = time.monotonic()

        try:
            # follow_redirects=False — a healthy service returns 200 directly.
            # A redirect would indicate misconfiguration (load balancer, auth
            # gate) and should be treated as DOWN, not silently followed.
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                resp = await client.get(url)
            latency_ms = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                status = ServiceStatus.UP
            else:
                status = ServiceStatus.DOWN
                logger.warning(
                    "HealthChecker [%s]: HTTP %d (%.0fms)",
                    service_name, resp.status_code, latency_ms,
                )

            return HealthCheckResult(
                service_name     = service_name,
                url              = url,
                status           = status,
                latency_ms       = round(latency_ms, 1),
                timestamp        = timestamp,
                http_status_code = resp.status_code,
            )

        except httpx.TimeoutException:
            latency_ms = self.timeout * 1000
            logger.error("HealthChecker [%s]: TIMEOUT (>%.0fms)", service_name, latency_ms)
            return HealthCheckResult(
                service_name = service_name,
                url          = url,
                status       = ServiceStatus.TIMEOUT,
                latency_ms   = round(latency_ms, 1),
                timestamp    = timestamp,
                error        = "Request timed out",
            )

        except httpx.RequestError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.error("HealthChecker [%s]: DOWN — %s", service_name, exc)
            return HealthCheckResult(
                service_name = service_name,
                url          = url,
                status       = ServiceStatus.DOWN,
                latency_ms   = round(latency_ms, 1),
                timestamp    = timestamp,
                error        = str(exc),
            )

        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            logger.exception("HealthChecker [%s]: unexpected error — %s", service_name, exc)
            return HealthCheckResult(
                service_name = service_name,
                url          = url,
                status       = ServiceStatus.DOWN,
                latency_ms   = round(latency_ms, 1),
                timestamp    = timestamp,
                error        = str(exc),
            )

    # ── sync (kept for tests / backward compat) ───────────────────────────────

    def check(self, service_name: str, base_url: str) -> HealthCheckResult:
        """Synchronous wrapper around check_async — used in unit tests."""
        return asyncio.run(self.check_async(service_name, base_url))
