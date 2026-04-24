"""
FallbackClient — HTTP client for fallback-service.

Mirrors the structure of CoreClient exactly.
Having two separate client classes means you can change how each service
is called without touching the other.
"""

import logging

import httpx

from app.models.schemas import FallbackResult

logger = logging.getLogger(__name__)


class FallbackClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        """
        Parameters
        ----------
        base_url : str
            Root URL of fallback-service, e.g. "http://fallback-service:8002".
        timeout : float
            Seconds before giving up and raising an exception.
        """
        self.base_url = base_url
        self.timeout = timeout

    async def get_fallback(self) -> FallbackResult:
        """
        Call GET /fallback on fallback-service.

        Returns a validated FallbackResult on success.
        Raises an exception on any failure (let the caller handle it).
        """
        url = f"{self.base_url}/fallback"
        logger.info("FallbackClient → GET %s (timeout=%.1fs)", url, self.timeout)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return FallbackResult(**response.json())

    async def health_check(self) -> dict:
        """Lightweight probe."""
        url = f"{self.base_url}/health"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url)
            return response.json()
