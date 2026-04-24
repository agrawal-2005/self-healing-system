"""
Dependency injection wiring for api-service.

FastAPI's Depends() system calls these functions to produce the objects
that route handlers need. By centralising construction here:
  - All wiring is in one file.
  - Swapping Settings or clients (e.g. for testing) means changing one place.
  - Route handlers stay unaware of how objects are created.

The three singletons are created once at import time.
"""

from app.clients.core_client import CoreClient
from app.clients.fallback_client import FallbackClient
from app.config.settings import settings
from app.services.api_service import ApiService

# ── Singletons ────────────────────────────────────────────────────────────────
# Created once when the module is first imported.

_core_client = CoreClient(
    base_url=settings.core_service_url,
    timeout=settings.request_timeout,
)

_fallback_client = FallbackClient(
    base_url=settings.fallback_service_url,
    timeout=settings.request_timeout,
)

_api_service = ApiService(
    core_client=_core_client,
    fallback_client=_fallback_client,
    service_name=settings.service_name,
)


# ── Provider functions (used with FastAPI Depends) ────────────────────────────

def get_api_service() -> ApiService:
    """Returns the shared ApiService instance."""
    return _api_service
