"""
Unit tests for SmartRecoveryPolicy.

Each test resets the module-level _failure_history via the autouse fixture
so tests are fully isolated from each other.
"""
import pytest

import smart_recovery_policy as srp_module
from smart_recovery_policy import IncidentSeverity, SmartRecoveryPolicy


@pytest.fixture(autouse=True)
def reset_failure_history():
    """Clear module-level state before and after every test."""
    srp_module._failure_history.clear()
    yield
    srp_module._failure_history.clear()


@pytest.fixture
def policy() -> SmartRecoveryPolicy:
    return SmartRecoveryPolicy()


# ── Action mapping ─────────────────────────────────────────────────────────────

def test_crash_first_failure_action_is_restart(policy):
    decision = policy.decide("core-service", "crash")
    assert decision.action == "restart_service"


def test_timeout_first_failure_action_is_restart(policy):
    decision = policy.decide("core-service", "timeout")
    assert decision.action == "restart_service"


def test_slow_failure_action_is_enable_fallback(policy):
    decision = policy.decide("core-service", "slow")
    assert decision.action == "enable_fallback"


def test_unknown_failure_type_defaults_to_restart(policy):
    decision = policy.decide("core-service", "mystery_error")
    assert decision.action == "restart_service"


# ── Severity ladder ────────────────────────────────────────────────────────────

def test_first_failure_severity_is_low(policy):
    decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.LOW
    assert not decision.is_escalated


def test_second_failure_5min_severity_is_medium(policy):
    policy.decide("core-service", "crash")
    decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.MEDIUM
    assert decision.failure_count_5min == 2
    assert not decision.is_escalated


def test_three_failures_5min_severity_is_high(policy):
    for _ in range(3):
        decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.HIGH
    assert decision.failure_count_5min == 3
    assert decision.is_escalated
    assert "ESCALATION" in decision.escalation_reason


def test_five_failures_severity_is_critical(policy):
    for _ in range(5):
        decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.CRITICAL
    assert decision.failure_count_10min == 5
    assert decision.is_escalated


# ── Severity overrides action ──────────────────────────────────────────────────

def test_critical_crash_switches_to_enable_fallback(policy):
    """CRITICAL severity changes crash action from restart to enable_fallback."""
    for _ in range(5):
        decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.CRITICAL
    assert decision.action == "enable_fallback"


def test_critical_timeout_switches_to_enable_fallback(policy):
    for _ in range(5):
        decision = policy.decide("core-service", "timeout")
    assert decision.action == "enable_fallback"


def test_critical_slow_keeps_enable_fallback(policy):
    """Slow already uses enable_fallback — CRITICAL does not change it."""
    for _ in range(5):
        decision = policy.decide("core-service", "slow")
    assert decision.action == "enable_fallback"
    assert decision.severity == IncidentSeverity.CRITICAL


def test_high_crash_keeps_restart(policy):
    """HIGH severity crash still uses restart_service (only CRITICAL switches)."""
    for _ in range(3):
        decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.HIGH
    assert decision.action == "restart_service"


# ── Recovery outcome evaluation ────────────────────────────────────────────────

def test_recovery_failure_upgrades_severity_to_critical(policy):
    decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.LOW

    final = policy.evaluate_recovery_outcome(decision, recovery_success=False)
    assert final.severity == IncidentSeverity.CRITICAL
    assert final.is_escalated
    assert "FAILED" in final.escalation_reason


def test_recovery_success_does_not_change_severity(policy):
    decision = policy.decide("core-service", "crash")
    final = policy.evaluate_recovery_outcome(decision, recovery_success=True)
    assert final.severity == IncidentSeverity.LOW
    assert not final.is_escalated


def test_already_critical_not_double_upgraded(policy):
    """evaluate_recovery_outcome is a no-op when severity is already CRITICAL."""
    for _ in range(5):
        decision = policy.decide("core-service", "crash")
    assert decision.severity == IncidentSeverity.CRITICAL

    final = policy.evaluate_recovery_outcome(decision, recovery_success=False)
    assert final is decision  # same object returned — no change


# ── Service isolation ──────────────────────────────────────────────────────────

def test_different_services_tracked_independently(policy):
    """Failure count for one service does not affect another."""
    for _ in range(5):
        policy.decide("core-service", "crash")
    decision = policy.decide("other-service", "crash")
    assert decision.severity == IncidentSeverity.LOW
    assert decision.failure_count_5min == 1


# ── Recovery strategy field ────────────────────────────────────────────────────

def test_recovery_strategy_string_populated(policy):
    decision = policy.decide("core-service", "crash")
    assert decision.recovery_strategy != ""
    assert "restart_service" in decision.recovery_strategy


def test_recovery_strategy_reflects_severity(policy):
    decision = policy.decide("core-service", "crash")
    assert "low" in decision.recovery_strategy.lower()
