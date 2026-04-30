"""
CircuitBreaker — protects api-service from a repeatedly-failing core-service.

State machine:

  CLOSED   → Normal operation. All calls to core-service are allowed.
             On each failure, failure_count increments.
             When failure_count >= failure_threshold → transition to OPEN.

  OPEN     → core-service is blocked. Fallback is used directly.
             After recovery_timeout_seconds, transition to HALF_OPEN.

  HALF_OPEN → Probe mode. One call is allowed to test if core-service recovered.
               If success → CLOSED (recovery confirmed, reset counters).
               If failure → OPEN again (still broken, reset timer).

Why this matters:
  Without a circuit breaker, every /process request during a core-service
  outage waits the full request_timeout (3s) before falling back. Under load,
  100 req/s × 3s timeout = 300 requests stacking up. The circuit breaker
  short-circuits immediately after failure_threshold failures, protecting
  both api-service and the already-struggling core-service.
"""

import logging
import threading
import time
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: int = 30,
        half_open_max_calls: int = 1,
    ) -> None:
        """
        Parameters
        ----------
        failure_threshold         : consecutive failures needed to open the circuit
        recovery_timeout_seconds  : seconds to wait in OPEN before probing again
        half_open_max_calls       : probe calls allowed in HALF_OPEN state (usually 1)
        """
        self.failure_threshold        = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.half_open_max_calls      = half_open_max_calls

        self.state:             CircuitState = CircuitState.CLOSED
        self.failure_count:     int          = 0
        self.last_failure_time: float        = 0.0
        self._half_open_calls:  int          = 0

        # Reentrant lock — uvicorn workers can race on state mutations under load.
        # RLock (not Lock) so a future caller that holds the lock can re-enter
        # without deadlocking if helpers are added.
        self._lock = threading.RLock()

    # ── public API ─────────────────────────────────────────────────────────────

    def can_call_core(self) -> bool:
        """
        Returns True if api-service should call core-service right now.

        Call this BEFORE every core-service request:
          if self.circuit_breaker.can_call_core():
              # try core-service
          else:
              # skip straight to fallback
        """
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                elapsed   = time.time() - self.last_failure_time
                remaining = self.recovery_timeout_seconds - elapsed
                if elapsed >= self.recovery_timeout_seconds:
                    logger.warning(
                        "CircuitBreaker: OPEN → HALF_OPEN after %.0fs cooldown", elapsed
                    )
                    self.state            = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    return True
                logger.warning(
                    "CircuitBreaker: OPEN — blocking core-service call (%.0fs until probe)",
                    remaining,
                )
                return False

            if self.state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    logger.info(
                        "CircuitBreaker: HALF_OPEN — allowing probe call %d/%d",
                        self._half_open_calls, self.half_open_max_calls,
                    )
                    return True
                logger.warning(
                    "CircuitBreaker: HALF_OPEN — probe call limit reached, blocking"
                )
                return False

            return False

    def record_success(self) -> None:
        """
        Call this after a successful core-service response.
        Resets counters and closes the circuit if it was half-open.
        """
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                logger.info("CircuitBreaker: HALF_OPEN → CLOSED (core-service recovered)")
            elif self.state == CircuitState.CLOSED and self.failure_count > 0:
                logger.info(
                    "CircuitBreaker: CLOSED — failure count reset (was %d)",
                    self.failure_count,
                )
            self.failure_count    = 0
            self._half_open_calls = 0
            self.state            = CircuitState.CLOSED

    def record_failure(self) -> None:
        """
        Call this after a failed core-service response.
        Increments failure count and opens the circuit when threshold is hit.
        A failure in HALF_OPEN immediately re-opens the circuit.
        """
        with self._lock:
            self.failure_count    += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                logger.warning("CircuitBreaker: HALF_OPEN → OPEN (probe failed)")
                self.state = CircuitState.OPEN
                return

            if self.failure_count >= self.failure_threshold:
                logger.warning(
                    "CircuitBreaker: CLOSED → OPEN (failures=%d/%d threshold=%d)",
                    self.failure_count, self.failure_threshold, self.failure_threshold,
                )
                self.state = CircuitState.OPEN
            else:
                logger.warning(
                    "CircuitBreaker: failure recorded %d/%d",
                    self.failure_count, self.failure_threshold,
                )

    @property
    def current_state(self) -> str:
        """Returns the state as a plain string. Useful for health/status endpoints."""
        return self.state.value
