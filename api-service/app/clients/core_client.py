"""
CoreClient — HTTP client for core-service.

Single Responsibility: knows HOW to talk to core-service.
It does NOT know WHEN to call core-service or WHAT to do if it fails.
That decision lives in ApiService (the business-logic layer).

Why a class?
  - Groups all core-service URLs in one place.
  - Changing the base URL or timeout affects only this file.
  - Easy to mock in tests: replace CoreClient with a fake.
"""

import logging

import httpx

from app.models.schemas import WorkResult

logger = logging.getLogger(__name__)


class CoreClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        """
        Parameters
        ----------
        base_url : str
            Root URL of core-service, e.g. "http://core-service:8001".
        timeout : float
            Seconds to wait before raising httpx.TimeoutException.
        """
        self.base_url = base_url
        self.timeout = timeout

    async def get_work(self) -> WorkResult:
        """
        Call GET /work on core-service.

        Returns a validated WorkResult on success.
        Raises an exception (httpx.HTTPStatusError, httpx.TimeoutException,
        httpx.RequestError) on any failure — the caller decides what to do.
        """
        url = f"{self.base_url}/work"
        logger.info("CoreClient → GET %s (timeout=%.1fs)", url, self.timeout)

        # httpx.AsyncClient is created per-call.
        # For Phase 1 this is fine; Phase 2 can promote it to a shared
        # session stored in app.state for connection-pool reuse.
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()          # raises on 4xx / 5xx
            return WorkResult(**response.json())

    async def health_check(self) -> dict:
        """Lightweight probe — used by monitoring scripts, not the main flow."""
        url = f"{self.base_url}/health"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            return response.json()
