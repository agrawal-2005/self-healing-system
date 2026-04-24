"""
Dependency injection wiring for fallback-service.
"""

from app.config.settings import settings
from app.services.fallback_service import FallbackService

_fallback_service = FallbackService(service_name=settings.service_name)


def get_fallback_service() -> FallbackService:
    return _fallback_service
