"""
SmartRecoveryPolicy — Phase 6 intelligent recovery decision engine.

Replaces the static _ACTION_MAP in recovery_handler with a policy that
accounts for:
  - failure_type            (crash / timeout / slow)
  - recent failure count    (tracked in-memory across warm Lambda invocations)
  - recovery outcome        (whether the last recovery attempt succeeded)

Severity ladder:
  LOW      — first or isolated failure
  MEDIUM   — 2 failures within 5 minutes
  HIGH     — 3+ failures within 5 minutes   (escalation triggered)
  CRITICAL — 5+ failures within 10 minutes, OR recovery action itself failed

Recovery action override rules:
  crash/timeout + LOW/MEDIUM/HIGH  → restart_service
  crash/timeout + CRITICAL         → enable_fallback
    (restarting a critically unstable service just generates noise)
  slow  + any                      → enable_fallback
    (core is too slow — force fallback path regardless of severity)

Why module-level failure tracker?
  Lambda containers are reused across warm invocations. The failure history
  persists within a warm execution environment, giving a sliding window
  without any external state store (DynamoDB, SSM, etc.).
  On cold start the counter resets — severity defaults to LOW, which is
  acceptable graceful degradation.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── Severity enum ─────────────────────────────────────────────────────────────

class IncidentSeverity(str, Enum):
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


# ── Recovery decision ─────────────────────────────────────────────────────────

@dataclass
class RecoveryDecision:
    """
    All information SmartRecoveryPolicy returns after evaluating a failure.
    Lambda passes these fields to recovery-agent as enriched ActionRequest fields.
    """
    action:              str
    severity:            IncidentSeverity
    recovery_strategy:   str
    failure_count_5min:  int
    failure_count_10min: int
    service_name:        str  = ""
    escalation_reason:   str  = ""
    is_escalated:        bool = False


# ── In-memory failure tracker (warm Lambda invocation window) ─────────────────

# Maps service_name → list of monotonic timestamps (one per failure event)
_failure_history: dict[str, list[float]] = {}


def _clear_history(service_name: str) -> None:
    """
    Wipe the failure history for service_name.
    Called after a successful enable_fallback so that future failures on the
    same service start the severity ladder from scratch (LOW), preventing the
    loop where a post-recovery startup blip immediately re-triggers CRITICAL.
    """
    _failure_history.pop(service_name, None)
    logger.info("SmartRecoveryPolicy: cleared failure history for %s", service_name)


def _record_and_count(service_name: str) -> tuple[int, int]:
    """
    Record a new failure for service_name at the current time.
    Returns (count_in_last_5min, count_in_last_10min).
    Prunes entries older than 10 minutes to keep memory bounded.
    """
    now     = time.monotonic()
    history = _failure_history.setdefault(service_name, [])
    history.append(now)

    cutoff_10 = now - 600   # 10 minutes
    cutoff_5  = now - 300   #  5 minutes

    # Prune beyond the 10-minute window
    _failure_history[service_name] = [t for t in history if t > cutoff_10]

    count_5  = sum(1 for t in _failure_history[service_name] if t > cutoff_5)
    count_10 = len(_failure_history[service_name])
    return count_5, count_10


# ── SmartRecoveryPolicy ───────────────────────────────────────────────────────

class SmartRecoveryPolicy:
    """
    Stateless policy object — all mutable state lives in module-level
    _failure_history so it survives across warm Lambda invocations.

    Typical call sequence in Lambda handler:
        policy   = SmartRecoveryPolicy()                      # or module-level singleton
        decision = policy.decide("core-service", "crash")    # before calling recovery-agent
        ...call recovery-agent...
        final    = policy.evaluate_recovery_outcome(decision, recovery_success=True/False)
    """

    # Base action per failure_type (before severity overrides)
    _BASE_ACTION: dict[str, str] = {
        "crash":   "restart_service",
        "timeout": "restart_service",
        "slow":    "enable_fallback",
    }
    _DEFAULT_ACTION = "restart_service"

    def decide(self, service_name: str, failure_type: str) -> RecoveryDecision:
        """
        Evaluate this failure event and return a RecoveryDecision.
        Side-effect: records this failure in the in-memory tracker.
        """
        count_5, count_10 = _record_and_count(service_name)

        base_action = self._BASE_ACTION.get(failure_type, self._DEFAULT_ACTION)
        if failure_type not in self._BASE_ACTION:
            logger.warning(
                "SmartRecoveryPolicy: unknown failure_type=%r — defaulting to %s",
                failure_type, self._DEFAULT_ACTION,
            )

        # ── Determine severity ────────────────────────────────────────────────
        escalation_reason = ""
        is_escalated      = False

        if count_10 >= 5:
            severity = IncidentSeverity.CRITICAL
            escalation_reason = (
                f"ESCALATION: {service_name} failed {count_10}x in 10 min — "
                "service is critically unstable"
            )
            is_escalated = True
        elif count_5 >= 3:
            severity = IncidentSeverity.HIGH
            escalation_reason = (
                f"ESCALATION: {service_name} failed {count_5}x in 5 min — "
                "repeated failure pattern detected"
            )
            is_escalated = True
        elif count_5 >= 2:
            severity = IncidentSeverity.MEDIUM
        else:
            severity = IncidentSeverity.LOW

        # ── Severity overrides base action ────────────────────────────────────
        # CRITICAL crash/timeout → enable_fallback (stop hammering an unstable service)
        if severity == IncidentSeverity.CRITICAL and failure_type in ("crash", "timeout"):
            action            = "enable_fallback"
            recovery_strategy = "fallback_on_critical"
            if is_escalated:
                escalation_reason += " — switching to enable_fallback"
        else:
            action            = base_action
            recovery_strategy = f"{base_action}_on_{severity.value.lower()}"

        if is_escalated:
            logger.warning(escalation_reason)

        logger.info(
            "SmartRecoveryPolicy: service=%s failure=%s count_5m=%d count_10m=%d "
            "severity=%s action=%s",
            service_name, failure_type, count_5, count_10, severity.value, action,
        )

        return RecoveryDecision(
            action              = action,
            severity            = severity,
            recovery_strategy   = recovery_strategy,
            failure_count_5min  = count_5,
            failure_count_10min = count_10,
            service_name        = service_name,
            escalation_reason   = escalation_reason,
            is_escalated        = is_escalated,
        )

    def evaluate_recovery_outcome(
        self,
        decision: RecoveryDecision,
        recovery_success: bool,
    ) -> RecoveryDecision:
        """
        Called AFTER the recovery-agent call returns.
        If recovery FAILED, upgrades severity to CRITICAL regardless of current level.
        Returns a (possibly new) RecoveryDecision with updated fields.
        """
        if recovery_success:
            # After a successful enable_fallback, wipe the failure history for this
            # service so it starts back at LOW severity when it comes back up.
            # Without this clear, a brief DOWN during container startup immediately
            # re-triggers CRITICAL → enable_fallback → infinite restart loop.
            if decision.action == "enable_fallback" and decision.service_name:
                _clear_history(decision.service_name)
            return decision

        if decision.severity == IncidentSeverity.CRITICAL:
            return decision  # nothing to upgrade

        escalation_reason = (
            f"ESCALATION: Recovery action '{decision.action}' FAILED for service "
            f"(prior severity={decision.severity.value}) — "
            "manual intervention may be required"
        )
        logger.error(escalation_reason)

        return RecoveryDecision(
            action              = decision.action,
            severity            = IncidentSeverity.CRITICAL,
            recovery_strategy   = decision.recovery_strategy,
            failure_count_5min  = decision.failure_count_5min,
            failure_count_10min = decision.failure_count_10min,
            escalation_reason   = escalation_reason,
            is_escalated        = True,
        )
