"""
GenericServiceClient — one HTTP client for every downstream service.

Instead of a separate client class per service (CoreClient, PaymentClient, …),
this single class can talk to any service.  The caller supplies the full URL
(base + endpoint) and timeout; everything else is identical.

Adding a new downstream service requires zero changes here.
"""

import logging

import httpx

logger = logging.getLogger(__name__)


class GenericServiceClient:
    def __init__(self, url: str, timeout: float) -> None:
        """
        Parameters
        ----------
        url     : Full URL to call, e.g. "http://core-service:8001/work".
                  Constructed in ServiceRegistry from gateway_url + gateway_endpoint.
        timeout : Seconds to wait before raising httpx.TimeoutException.
        """
        self.url     = url
        self.timeout = timeout

    async def call(self) -> dict:
        """
        GET {url}, return the parsed JSON body as a plain dict.

        Raises on any failure (4xx, 5xx, timeout, connection refused).
        The caller (GatewayService) owns the decision of what to do on failure.
        """
        logger.info("GenericServiceClient → GET %s (timeout=%.1fs)", self.url, self.timeout)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            return response.json()
