"""
StateManager — owns the mutable runtime state of core-service.

Why a separate class for this?
  - CoreService is about BEHAVIOUR (do work, be slow, report health).
  - StateManager is about STATE (am I crashed right now? am I in slow mode?).
  - Splitting them means you can change how state is stored (e.g. move to
    Redis in Phase 2) without touching any business logic in CoreService.

In Phase 1 state is stored in plain Python instance variables (in-memory).
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self) -> None:
        # Both flags start as False — service begins healthy and fast.
        self._crashed: bool = False
        self._crashed_until: Optional[float] = None  # monotonic deadline for auto-recovery
        self._slow_mode: bool = False

    # ── Crash state ───────────────────────────────────────────────────────────

    def set_crashed(self, duration_seconds: float = 20.0) -> None:
        """Mark this service as crashed for `duration_seconds`, then auto-recover."""
        self._crashed = True
        self._crashed_until = time.monotonic() + duration_seconds
        logger.warning(
            "StateManager: entered CRASHED state (auto-recover in %.0fs)", duration_seconds
        )

    def is_crashed(self) -> bool:
        if self._crashed and self._crashed_until is not None:
            if time.monotonic() >= self._crashed_until:
                self._crashed = False
                self._crashed_until = None
                logger.info("StateManager: CRASHED timer expired — auto-recovering")
        return self._crashed

    # ── Slow mode ─────────────────────────────────────────────────────────────

    def set_slow(self) -> None:
        """
        Activate slow mode. When enabled, /work adds a delay before responding.
        Use this to trigger timeout-based fallback in api-service.
        """
        self._slow_mode = True
        logger.warning("StateManager: entered SLOW MODE")

    def is_slow(self) -> bool:
        return self._slow_mode

    # ── Recovery ──────────────────────────────────────────────────────────────

    def recover(self) -> None:
        """Clear all failure flags. Service returns to normal operation."""
        self._crashed = False
        self._crashed_until = None
        self._slow_mode = False
        logger.info("StateManager: recovered — all flags cleared")
