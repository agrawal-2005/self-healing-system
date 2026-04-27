"""
AWS Lambda — Recovery Handler (Phase 6).

Phase 6 additions over Phase 5 (EC2 deployment):
  - SmartRecoveryPolicy: replaces static _ACTION_MAP with severity-aware decisions
      LOW/MEDIUM/HIGH  → restart_service  (crash/timeout)
      CRITICAL         → enable_fallback  (crash/timeout, service too unstable)
      any severity     → enable_fallback  (slow — always force fallback)
  - Escalation: logs ESCALATION messages when severity ≥ HIGH
  - RollbackManager: recommends rollback on CRITICAL severity (dry-run)
  - CloudWatch metrics from Lambda:
      IncidentSeverityCount  — one per invocation, dimension=Severity
      EscalationCount        — only when is_escalated=True
      RollbackRecommendedCount — only when ROLLBACK_RECOMMENDED

Phase 3/4/5 behaviour preserved:
  - Retry logic: up to MAX_RETRIES attempts with exponential backoff
  - Duration measurement + structured response
  - Timeout guard: 15s per HTTP attempt

Environment variables:
  RECOVERY_AGENT_URL     — URL of recovery-agent (e.g. http://54.x.x.x:8003)
  TARGET_SERVICE         — default Docker container name (default: core-service)
  RECOVERY_TOKEN         — shared secret sent as X-Recovery-Token header
  MAX_RETRIES            — how many attempts before giving up (default: 3)
  IMAGE_TAG              — image tag for rollback baseline (default: latest)
  CLOUDWATCH_ENABLED     — "true" to publish severity metrics (default: false)
  CLOUDWATCH_NAMESPACE   — CloudWatch namespace (default: SelfHealingSystem)
  AWS_DEFAULT_REGION     — AWS region (default: us-east-1)
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from rollback_manager import RollbackManager
from smart_recovery_policy import IncidentSeverity, SmartRecoveryPolicy

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Configuration ─────────────────────────────────────────────────────────────

RECOVERY_AGENT_URL   = os.environ.get("RECOVERY_AGENT_URL",   "http://localhost:8003")
TARGET_SERVICE       = os.environ.get("TARGET_SERVICE",       "core-service")
RECOVERY_TOKEN       = os.environ.get("RECOVERY_TOKEN",       "dev-token")
MAX_RETRIES          = int(os.environ.get("MAX_RETRIES",      "3"))
CLOUDWATCH_ENABLED   = os.environ.get("CLOUDWATCH_ENABLED",   "false").lower() == "true"
CLOUDWATCH_NAMESPACE = os.environ.get("CLOUDWATCH_NAMESPACE", "SelfHealingSystem")
AWS_REGION           = os.environ.get("AWS_DEFAULT_REGION",   "us-east-1")

# ── Module-level singletons (survive warm invocations) ────────────────────────
_policy          = SmartRecoveryPolicy()
_rollback_manager = RollbackManager()
_cw_client: Optional[object] = None   # boto3 CloudWatch client, lazily initialised


def _get_cw_client():
    """Return a boto3 CloudWatch client, creating it once per container lifecycle."""
    global _cw_client
    if _cw_client is None and CLOUDWATCH_ENABLED:
        import boto3
        _cw_client = boto3.client("cloudwatch", region_name=AWS_REGION)
    return _cw_client


# ── Lambda handler ────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Entry point invoked by EventBridge.

    Flow:
      1. Parse FailureEvent detail
      2. SmartRecoveryPolicy.decide() → action + severity + escalation
      3. RollbackManager baseline + check
      4. Call recovery-agent (with retry)
      5. evaluate_recovery_outcome() — upgrade to CRITICAL if recovery failed
      6. Post-recovery rollback check (if newly CRITICAL)
      7. Emit CloudWatch metrics (IncidentSeverityCount, EscalationCount,
         RollbackRecommendedCount)
    """
    request_id = getattr(context, "aws_request_id", "local")
    t_start    = time.monotonic()

    logger.info("[%s] lambda_handler: received event — %s", request_id, json.dumps(event))

    # ── Step 1: parse FailureEvent detail ────────────────────────────────────
    detail = event.get("detail", {})
    if not detail:
        logger.error("[%s] lambda_handler: event has no 'detail' key — ignoring", request_id)
        return _response(400, {"success": False, "error": "Missing event detail"})

    service_name = detail.get("service_name", TARGET_SERVICE)
    failure_type = detail.get("failure_type", "crash")
    latency_ms   = detail.get("latency_ms", 0)
    timestamp    = detail.get("timestamp", "")

    logger.info(
        "[%s] lambda_handler: service=%s  failure=%s  latency=%.0fms  ts=%s",
        request_id, service_name, failure_type, latency_ms, timestamp,
    )

    # ── Step 2: SmartRecoveryPolicy decision ──────────────────────────────────
    decision = _policy.decide(service_name, failure_type)
    logger.info(
        "[%s] SmartRecoveryPolicy: action=%s  severity=%s  strategy=%s  "
        "count_5m=%d  count_10m=%d  escalated=%s",
        request_id,
        decision.action,
        decision.severity.value,
        decision.recovery_strategy,
        decision.failure_count_5min,
        decision.failure_count_10min,
        decision.is_escalated,
    )

    # ── Step 3: Record rollback baseline before recovery ──────────────────────
    _rollback_manager.record_baseline(service_name)

    # ── Step 4: Call recovery-agent (with retry) ──────────────────────────────
    reason = (
        f"Lambda Phase6 — {failure_type} on {service_name} at {timestamp} | "
        f"severity={decision.severity.value} strategy={decision.recovery_strategy} "
        f"count_5m={decision.failure_count_5min}"
    )
    if decision.escalation_reason:
        reason += f" | {decision.escalation_reason}"

    result, attempts = _call_recovery_agent_with_retry(
        action            = decision.action,
        target_service    = service_name,
        reason            = reason,
        severity          = decision.severity.value,
        recovery_strategy = decision.recovery_strategy,
        failure_count     = decision.failure_count_5min,
        escalation_reason = decision.escalation_reason,
        request_id        = request_id,
    )

    recovery_success = result.get("success", False)

    # ── Step 5: Evaluate recovery outcome ─────────────────────────────────────
    final_decision = _policy.evaluate_recovery_outcome(decision, recovery_success)
    if final_decision.severity != decision.severity:
        logger.error(
            "[%s] severity upgraded %s → %s after recovery failure",
            request_id, decision.severity.value, final_decision.severity.value,
        )

    # ── Step 6: Record successful recovery / rollback check ───────────────────
    if recovery_success:
        _rollback_manager.record_successful_recovery(service_name)

    rollback_image: Optional[str] = None
    if _rollback_manager.should_recommend(service_name, final_decision.severity):
        rollback_image = _rollback_manager.recommend_rollback(service_name)

    # ── Step 7: CloudWatch metrics ────────────────────────────────────────────
    _emit_metrics(
        service_name    = service_name,
        severity        = final_decision.severity,
        is_escalated    = final_decision.is_escalated,
        rollback_image  = rollback_image,
        request_id      = request_id,
    )

    # ── Finalise response ─────────────────────────────────────────────────────
    duration_ms = (time.monotonic() - t_start) * 1000
    result.update({
        "request_id":        request_id,
        "duration_ms":       round(duration_ms, 1),
        "attempts":          attempts,
        "severity":          final_decision.severity.value,
        "recovery_strategy": final_decision.recovery_strategy,
        "is_escalated":      final_decision.is_escalated,
        "rollback_recommended": rollback_image is not None,
    })

    logger.info(
        "[%s] lambda_handler: complete — success=%s  severity=%s  "
        "duration=%.0fms  attempts=%d  escalated=%s  rollback=%s",
        request_id,
        recovery_success,
        final_decision.severity.value,
        duration_ms,
        attempts,
        final_decision.is_escalated,
        rollback_image is not None,
    )

    return _response(200 if recovery_success else 500, result)


# ── CloudWatch emission ───────────────────────────────────────────────────────

def _emit_metrics(
    service_name: str,
    severity: IncidentSeverity,
    is_escalated: bool,
    rollback_image: Optional[str],
    request_id: str,
) -> None:
    """Emit Phase 6 CloudWatch metrics. Never raises — errors are logged only."""
    cw = _get_cw_client()
    if cw is None:
        return

    metric_data = [
        {
            "MetricName": "IncidentSeverityCount",
            "Value":      1.0,
            "Unit":       "Count",
            "Dimensions": [
                {"Name": "ServiceName", "Value": service_name},
                {"Name": "Severity",    "Value": severity.value},
            ],
        },
    ]

    if is_escalated:
        metric_data.append({
            "MetricName": "EscalationCount",
            "Value":      1.0,
            "Unit":       "Count",
            "Dimensions": [
                {"Name": "ServiceName", "Value": service_name},
                {"Name": "Severity",    "Value": severity.value},
            ],
        })

    if rollback_image is not None:
        metric_data.append({
            "MetricName": "RollbackRecommendedCount",
            "Value":      1.0,
            "Unit":       "Count",
            "Dimensions": [
                {"Name": "ServiceName", "Value": service_name},
            ],
        })

    try:
        cw.put_metric_data(Namespace=CLOUDWATCH_NAMESPACE, MetricData=metric_data)
        logger.info(
            "[%s] CloudWatch: published %d metrics (severity=%s escalated=%s rollback=%s)",
            request_id, len(metric_data), severity.value, is_escalated, rollback_image is not None,
        )
    except Exception as exc:
        logger.warning("[%s] CloudWatch publish failed (non-fatal) — %s", request_id, exc)


# ── Recovery-agent call ───────────────────────────────────────────────────────

def _call_recovery_agent_with_retry(
    action: str,
    target_service: str,
    reason: str,
    severity: str,
    recovery_strategy: str,
    failure_count: int,
    escalation_reason: str,
    request_id: str,
) -> tuple[dict, int]:
    """
    Calls recovery-agent with exponential backoff retry.

    Retry policy:
      Attempt 1 — immediate
      Attempt 2 — wait 2s  (only on network / URLError)
      Attempt 3 — wait 4s
      HTTP errors (4xx/5xx) NOT retried — logic problem, not transient.
    """
    backoff_seconds = 2
    last_result: dict = {}

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("[%s] _call_recovery_agent: attempt %d/%d", request_id, attempt, MAX_RETRIES)
        result = _call_recovery_agent(
            action            = action,
            target_service    = target_service,
            reason            = reason,
            severity          = severity,
            recovery_strategy = recovery_strategy,
            failure_count     = failure_count,
            escalation_reason = escalation_reason,
            request_id        = request_id,
        )
        last_result = result

        if result.get("success"):
            return result, attempt

        error = result.get("error", "")
        if error.startswith("HTTP"):
            logger.error(
                "[%s] HTTP error %s from recovery-agent — not retrying",
                request_id, error,
            )
            return result, attempt

        if attempt < MAX_RETRIES:
            wait = backoff_seconds * attempt
            logger.warning(
                "[%s] network error on attempt %d, retrying in %ds",
                request_id, attempt, wait,
            )
            time.sleep(wait)

    return last_result, MAX_RETRIES


def _call_recovery_agent(
    action: str,
    target_service: str,
    reason: str,
    severity: str,
    recovery_strategy: str,
    failure_count: int,
    escalation_reason: str,
    request_id: str,
) -> dict:
    """Single HTTP POST to recovery-agent /action with enriched Phase 6 payload."""
    url = f"{RECOVERY_AGENT_URL}/action"
    payload = json.dumps({
        "action":            action,
        "target_service":    target_service,
        "reason":            reason,
        "severity":          severity,
        "recovery_strategy": recovery_strategy,
        "failure_count":     failure_count,
        "escalation_reason": escalation_reason,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data    = payload,
        headers = {
            "Content-Type":     "application/json",
            "X-Recovery-Token": RECOVERY_TOKEN,
            "X-Request-Id":     request_id,
        },
        method = "POST",
    )

    logger.info("[%s] POST %s  payload=%s", request_id, url, payload.decode())

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            logger.info("[%s] response — %s", request_id, json.dumps(body))
            return body

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        logger.error("[%s] HTTP %d from recovery-agent — %s", request_id, exc.code, body)
        return {"success": False, "error": f"HTTP {exc.code}", "detail": body}

    except urllib.error.URLError as exc:
        logger.error("[%s] cannot reach recovery-agent — %s", request_id, exc.reason)
        return {"success": False, "error": str(exc.reason)}

    except TimeoutError:
        logger.error("[%s] recovery-agent call timed out after 15s", request_id)
        return {"success": False, "error": "timeout"}

    except Exception as exc:
        logger.exception("[%s] unexpected error — %s", request_id, exc)
        return {"success": False, "error": str(exc)}


# ── Response helper ───────────────────────────────────────────────────────────

def _response(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }
