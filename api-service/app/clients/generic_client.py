"""
GenericServiceClient — one HTTP client for every downstream service.

Instead of a separate client class per service (CoreClient, PaymentClient, …),
this single class can talk to any service.  The caller supplies the full URL
(base + endpoint) and timeout; everything else is identical.

Adding a new downstream service requires zero changes here.

Connection pooling:
  A single module-level httpx.AsyncClient is reused across calls so we
  benefit from keep-alive — creating a new client per call discards the
  TCP/TLS handshake every request and is measurably slower under load.
  Per-call timeout still works because we pass a `timeout=` to .get().
"""

import json
import logging

import httpx

logger = logging.getLogger(__name__)

# Shared async client. Limits chosen for a small demo; tune per workload.
# follow_redirects=False — downstream services are internal, redirects would
# usually indicate misconfiguration we don't want to silently follow.
_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _shared_client


async def aclose_shared_client() -> None:
    """Close the pooled client at app shutdown to release sockets cleanly."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


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

        Raises on any failure (4xx, 5xx, timeout, connection refused, malformed
        JSON). The caller (GatewayService) owns the decision of what to do on
        failure.
        """
        logger.info("GenericServiceClient → GET %s (timeout=%.1fs)", self.url, self.timeout)
        client = _get_client()
        response = await client.get(self.url, timeout=self.timeout)
        response.raise_for_status()
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            # A 200 with a non-JSON body should be treated as a service failure
            # so the circuit breaker / fallback path engages, not as a 500 leak.
            logger.warning(
                "GenericServiceClient: %s returned non-JSON body — %s",
                self.url, exc,
            )
            raise httpx.DecodingError(
                f"Invalid JSON from {self.url}: {exc}", request=response.request
            ) from exc
