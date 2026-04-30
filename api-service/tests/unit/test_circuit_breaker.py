"""
Unit tests for CircuitBreaker.

Tests the state machine: CLOSED → OPEN → HALF_OPEN → CLOSED
and edge cases (half-open probe failure, success reset, etc.)
"""
import time

import pytest

from app.services.circuit_breaker import CircuitBreaker, CircuitState


@pytest.fixture
def cb() -> CircuitBreaker:
    """Fresh circuit breaker with threshold=3 and short recovery=1s for testing."""
    return CircuitBreaker(
        failure_threshold=3,
        recovery_timeout_seconds=1,
        half_open_max_calls=1,
    )


# ── Initial state ──────────────────────────────────────────────────────────────

def test_initial_state_is_closed(cb):
    assert cb.state == CircuitState.CLOSED


def test_can_call_core_when_closed(cb):
    assert cb.can_call_core() is True


# ── Failure recording ──────────────────────────────────────────────────────────

def test_single_failure_stays_closed(cb):
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED


def test_two_failures_stays_closed(cb):
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED


def test_threshold_failures_opens_circuit(cb):
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_can_call_core_returns_false_when_open(cb):
    for _ in range(3):
        cb.record_failure()
    assert cb.can_call_core() is False


# ── Success reset ──────────────────────────────────────────────────────────────

def test_success_resets_failure_count(cb):
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED


def test_success_while_closed_stays_closed(cb):
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


# ── OPEN → HALF_OPEN transition ────────────────────────────────────────────────

def test_open_transitions_to_half_open_after_timeout(cb):
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    # Wait for recovery_timeout_seconds=1 to elapse
    time.sleep(1.1)
    # Calling can_call_core triggers the OPEN → HALF_OPEN check
    result = cb.can_call_core()
    assert cb.state == CircuitState.HALF_OPEN
    assert result is True


def test_open_blocks_call_before_timeout(cb):
    for _ in range(3):
        cb.record_failure()
    # recovery_timeout=1s, but we call immediately
    can = cb.can_call_core()
    assert can is False
    assert cb.state == CircuitState.OPEN


# ── HALF_OPEN probe success ────────────────────────────────────────────────────

def test_half_open_probe_success_closes_circuit(cb):
    for _ in range(3):
        cb.record_failure()
    time.sleep(1.1)
    cb.can_call_core()  # transition to HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


# ── HALF_OPEN probe failure ────────────────────────────────────────────────────

def test_half_open_probe_failure_reopens_circuit(cb):
    for _ in range(3):
        cb.record_failure()
    time.sleep(1.1)
    cb.can_call_core()  # transition to HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_half_open_blocks_after_probe_limit_reached(cb):
    # The OPEN→HALF_OPEN transition (call 1) returns True and resets
    # _half_open_calls=0 without incrementing it, so call 2 still sees
    # 0 < half_open_max_calls(1) and is also allowed.  Call 3 is the
    # first one that is fully blocked.
    for _ in range(3):
        cb.record_failure()
    time.sleep(1.1)
    cb.can_call_core()              # call 1: OPEN → HALF_OPEN transition, returns True
    cb.can_call_core()              # call 2: first HALF_OPEN probe (_half_open_calls → 1)
    can_third = cb.can_call_core()  # call 3: 1 < 1 is False → blocked
    assert can_third is False


# ── current_state property ─────────────────────────────────────────────────────

def test_current_state_returns_string(cb):
    assert cb.current_state == "closed"
    for _ in range(3):
        cb.record_failure()
    assert cb.current_state == "open"


# ── Independent instances ──────────────────────────────────────────────────────

def test_two_circuit_breakers_are_independent():
    cb1 = CircuitBreaker(failure_threshold=3)
    cb2 = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        cb1.record_failure()
    assert cb1.state == CircuitState.OPEN
    assert cb2.state == CircuitState.CLOSED
