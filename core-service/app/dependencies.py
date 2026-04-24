"""
Dependency injection wiring for core-service.

StateManager is a shared singleton because it holds in-memory state
that must persist across requests. If each request created a new
StateManager, triggering /fail would never affect the next /work call.
"""

from app.config.settings import settings
from app.services.core_service import CoreService
from app.services.state_manager import StateManager

# ── Singletons ────────────────────────────────────────────────────────────────

_state_manager = StateManager()

_core_service = CoreService(
    state_manager=_state_manager,
    service_name=settings.service_name,
    slow_delay=settings.slow_delay_seconds,
)


# ── Provider functions ────────────────────────────────────────────────────────

def get_core_service() -> CoreService:
    return _core_service
