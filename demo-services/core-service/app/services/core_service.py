"""
CoreService — business logic layer for core-service.

This class owns the BEHAVIOUR of core-service:
  - What does "doing work" mean?
  - What happens during a simulated crash?
  - What does a slow response look like?
  - How is health determined?

It delegates ALL state questions to StateManager.
It does NOT know about HTTP, FastAPI, or routing.
"""

import asyncio
import logging

from fastapi import HTTPException

from app.models.schemas import (
    FailResponse,
    HealthResponse,
    RecoverResponse,
    SlowResponse,
    WorkResponse,
)
from app.services.state_manager import StateManager

logger = logging.getLogger(__name__)


class CoreService:
    def __init__(self, state_manager: StateManager, service_name: str, slow_delay: float) -> None:
        """
        Parameters
        ----------
        state_manager : StateManager
            Injected — owns the crashed/slow flags.
        service_name : str
            Used in response bodies so callers know which service replied.
        slow_delay : float
            Seconds to sleep in slow_work() and in work() when slow mode is on.
        """
        self.state_manager = state_manager
        self.service_name = service_name
        self.slow_delay = slow_delay

    def health(self) -> HealthResponse:
        """
        Returns healthy only when no failure flags are set.
        The monitor (and docker-compose healthcheck) calls this endpoint.
        """
        if self.state_manager.is_crashed():
            return HealthResponse(status="unhealthy", service=self.service_name)
        return HealthResponse(status="healthy", service=self.service_name)

    async def do_work(self) -> WorkResponse:
        """
        Main work endpoint, called by api-service via CoreClient.

        Behaviour matrix:
          crashed=True  → raises 500   → api-service falls back immediately
          slow=True     → sleeps N sec → api-service times out → falls back
          normal        → returns result immediately
        """
        if self.state_manager.is_crashed():
            logger.error("do_work(): service is in CRASHED state, raising 500")
            raise HTTPException(
                status_code=500,
                detail="core-service is in a simulated crashed state. POST /recover to reset.",
            )

        if self.state_manager.is_slow():
            logger.warning("do_work(): SLOW MODE active — sleeping %.1fs", self.slow_delay)
            await asyncio.sleep(self.slow_delay)

        logger.info("do_work(): returning successful result")
        return WorkResponse(message="Work completed successfully.", service=self.service_name)

    async def slow_work(self) -> SlowResponse:
        """
        Standalone slow endpoint — always sleeps, regardless of slow_mode flag.
        Useful to directly observe the latency behaviour without affecting /work.
        """
        logger.warning("slow_work(): sleeping %.1fs (simulated latency)", self.slow_delay)
        await asyncio.sleep(self.slow_delay)
        return SlowResponse(
            message="Slow response returned after simulated latency.",
            service=self.service_name,
            latency_simulated_seconds=self.slow_delay,
        )

    def trigger_fail(self) -> FailResponse:
        """
        Activates the crashed state.
        After this call, /work returns 500 and /health returns 503.
        """
        self.state_manager.set_crashed()
        return FailResponse(
            message="core-service is now simulating a crash. POST /recover to reset.",
            crashed=True,
        )

    def trigger_slow(self) -> dict:
        """
        Activates slow mode so that /work adds a delay.
        This lets api-service's timeout trigger the fallback path.
        """
        self.state_manager.set_slow()
        return {
            "message": f"Slow mode enabled. /work will now sleep {self.slow_delay}s. POST /recover to reset.",
            "slow_mode": True,
        }

    def recover(self) -> RecoverResponse:
        """Clears all failure flags. Service returns to normal operation."""
        self.state_manager.recover()
        return RecoverResponse(
            message="core-service has recovered. All failure flags cleared.",
            crashed=False,
        )
