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

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self) -> None:
        # Both flags start as False — service begins healthy and fast.
        self._crashed: bool = False
        self._slow_mode: bool = False

    # ── Crash state ───────────────────────────────────────────────────────────

    def set_crashed(self) -> None:
        """Mark this service as crashed. /work and /health will return errors."""
        self._crashed = True
        logger.warning("StateManager: entered CRASHED state")

    def is_crashed(self) -> bool:
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
        self._slow_mode = False
        logger.info("StateManager: recovered — all flags cleared")
