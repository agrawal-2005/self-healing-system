"""
AWS Lambda — Recovery Handler (Phase 7).

Phase 7 additions over Phase 6:
  - Multi-service support: service_name comes from the EventBridge event (not a hardcoded TARGET_SERVICE)
  - Service registry: loads services_config.json at module init — strategy per service
  - Strategy-aware recovery:
      restart   → current Phase 6 behavior (restart_service or enable_fallback on CRITICAL)
      fallback  → same as restart but logs FALLBACK_AVAILABLE with fallback service name
      escalate  → minimum severity = HIGH on first failure, logs CRITICAL_SERVICE_NO_FALLBACK
  - Backward compatible: if service not in config → default strategy=restart

Phase 6 behaviour preserved:
  - SmartRecoveryPolicy: LOW/MEDIUM/HIGH/CRITICAL severity ladder
  - Escalation: logs ESCALATION when severity ≥ HIGH
  - RollbackManager: dry-run rollback recommendation on CRITICAL
  - CloudWatch metrics: IncidentSeverityCount, EscalationCount, RollbackRecommendedCount

Environment variables:
  RECOVERY_AGENT_URL     — URL of recovery-agent (e.g. http://54.x.x.x:8003)
  TARGET_SERVICE         — fallback default if event has no service_name (default: core-service)
  RECOVERY_TOKEN         — shared secret sent as X-Recovery-Token header
  MAX_RETRIES            — how many attempts before giving up (default: 3)
  IMAGE_TAG              — image tag for rollback baseline (default: latest)
  CLOUDWATCH_ENABLED     — "true" to publish severity metrics (default: false)
  CLOUDWATCH_NAMESPACE   — CloudWatch namespace (default: SelfHealingSystem)
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
_policy           = SmartRecoveryPolicy()
_rollback_manager = RollbackManager()
_cw_client: Optional[object] = None


def _get_cw_client():
    global _cw_client
    if _cw_client is None and CLOUDWATCH_ENABLED:
        import boto3
        _cw_client = boto3.client("cloudwatch", region_name=AWS_REGION)
    return _cw_client


# ── Phase 7: Service registry ─────────────────────────────────────────────────

def _load_service_registry() -> dict[str, dict]:
    """
    Load services_config.json included in the Lambda zip.
    Returns a dict keyed by service_name → config entry.
    Falls back to empty dict (default strategy=restart for all services).
    """
    config_path = os.path.join(os.path.dirname(__file__), "services_config.json")
    if not os.path.exists(config_path):
        logger.warning("services_config.json not found — all services default to strategy=restart")
        return {}
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
        registry = {svc["service_name"]: svc for svc in config.get("services", [])}
        logger.info("Service registry loaded: %s", list(registry.keys()))
        return registry
    except Exception as exc:
        logger.error("Failed to load services_config.json: %s", exc)
        return {}


# Load once at module init (survives warm invocations)
_service_registry: dict[str, dict] = _load_service_registry()


def _get_service_config(service_name: str) -> dict:
    """Return config for a service, or safe defaults if not in registry."""
    return _service_registry.get(service_name, {
        "strategy": "restart",
        "fallback_service": None,
        "critical": False,
    })


# ── Lambda handler ────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Entry point invoked by EventBridge.

    Flow:
      1. Parse FailureEvent detail (service_name comes from event — not hardcoded)
      2. Look up service strategy from registry
      3. SmartRecoveryPolicy.decide() → action + severity + escalation
      4. Apply strategy overrides (escalate/fallback/restart)
      5. RollbackManager baseline + check
      6. Call recovery-agent (with retry)
      7. evaluate_recovery_outcome() — upgrade to CRITICAL if recovery failed
      8. Post-recovery rollback check
      9. Emit CloudWatch metrics
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

    # ── Step 2: Service registry lookup ──────────────────────────────────────
    svc_config       = _get_service_config(service_name)
    strategy         = svc_config.get("strategy", "restart")
    fallback_service = svc_config.get("fallback_service")
    is_critical      = svc_config.get("critical", False)

    logger.info(
        "[%s] STRATEGY: service=%s  strategy=%s  fallback=%s  critical=%s",
        request_id, service_name, strategy, fallback_service, is_critical,
    )

    # ── Step 3: SmartRecoveryPolicy decision ──────────────────────────────────
    decision = _policy.decide(service_name, failure_type)

    # ── Step 4: Apply strategy overrides ──────────────────────────────────────
    # escalate: minimum severity = HIGH even on first failure (no-fallback critical service)
    if strategy == "escalate":
        if decision.severity in (IncidentSeverity.LOW, IncidentSeverity.MEDIUM):
            logger.warning(
                "[%s] CRITICAL_SERVICE_NO_FALLBACK: %s has no fallback — "
                "upgrading severity %s → HIGH. Operator intervention may be required.",
                request_id, service_name, decision.severity.value,
            )
            decision.severity         = IncidentSeverity.HIGH
            decision.is_escalated     = True
            decision.escalation_reason = (
                f"ESCALATION: {service_name} is a critical service with no fallback — "
                f"severity forced to HIGH on every failure"
            )

    # fallback: when action would be enable_fallback, log the specific fallback service name
    if strategy == "fallback" and decision.action == "enable_fallback" and fallback_service:
        logger.info(
            "[%s] FALLBACK_AVAILABLE: %s is CRITICAL — traffic should route to %s",
            request_id, service_name, fallback_service,
        )

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

    # ── Step 5: Record rollback baseline before recovery ──────────────────────
    _rollback_manager.record_baseline(service_name)

    # ── Step 6: Call recovery-agent (with retry) ──────────────────────────────
    reason = (
        f"Lambda Phase7 — {failure_type} on {service_name} at {timestamp} | "
        f"strategy={strategy} severity={decision.severity.value} "
        f"strategy={decision.recovery_strategy} count_5m={decision.failure_count_5min}"
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

    # ── Step 7: Evaluate recovery outcome ─────────────────────────────────────
    final_decision = _policy.evaluate_recovery_outcome(decision, recovery_success)
    if final_decision.severity != decision.severity:
        logger.error(
            "[%s] severity upgraded %s → %s after recovery failure",
            request_id, decision.severity.value, final_decision.severity.value,
        )

    # ── Step 8: Record successful recovery / rollback check ───────────────────
    if recovery_success:
        _rollback_manager.record_successful_recovery(service_name)

    rollback_image: Optional[str] = None
    if _rollback_manager.should_recommend(service_name, final_decision.severity):
        rollback_image = _rollback_manager.recommend_rollback(service_name)

    # ── Step 9: CloudWatch metrics ────────────────────────────────────────────
    _emit_metrics(
        service_name   = service_name,
        severity       = final_decision.severity,
        is_escalated   = final_decision.is_escalated,
        rollback_image = rollback_image,
        request_id     = request_id,
    )

    # ── Finalise response ─────────────────────────────────────────────────────
    duration_ms = (time.monotonic() - t_start) * 1000
    result.update({
        "request_id":           request_id,
        "duration_ms":          round(duration_ms, 1),
        "attempts":             attempts,
        "service_name":         service_name,
        "strategy":             strategy,
        "severity":             final_decision.severity.value,
        "recovery_strategy":    final_decision.recovery_strategy,
        "is_escalated":         final_decision.is_escalated,
        "rollback_recommended": rollback_image is not None,
    })

    logger.info(
        "[%s] lambda_handler: complete — service=%s  strategy=%s  success=%s  "
        "severity=%s  duration=%.0fms  attempts=%d  escalated=%s  rollback=%s",
        request_id,
        service_name,
        strategy,
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
