"""
AWS Lambda — Recovery Handler.

Triggered by EventBridge when monitor publishes a ServiceFailureDetected event.

Flow:
  1. EventBridge invokes this Lambda with the full event envelope.
  2. lambda_handler() extracts event["detail"] — our FailureEvent payload.
  3. _decide_action() maps failure_type → ActionType.
  4. _call_recovery_agent() sends POST /action to the recovery-agent service.
  5. Returns a structured response dict (logged by Lambda, visible in CloudWatch).

Environment variables (set in Lambda console or via IaC):
  RECOVERY_AGENT_URL  — base URL of recovery-agent, e.g.:
                         https://abc123.ngrok.io   (local dev via ngrok)
                         http://10.0.1.5:8003       (VPC-peered EC2)
  TARGET_SERVICE      — Docker container name to act on (default: core-service)
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Configuration (from Lambda environment) ───────────────────────────────────

RECOVERY_AGENT_URL = os.environ.get("RECOVERY_AGENT_URL", "http://localhost:8003")
TARGET_SERVICE     = os.environ.get("TARGET_SERVICE", "core-service")

# ── Action decision table ─────────────────────────────────────────────────────
# Maps failure_type (from FailureEvent) → recovery action (for RecoveryService).
#
# crash   → restart the container immediately
# timeout → restart (container is probably deadlocked, not completely down)
# slow    → enable_fallback first (route traffic away), then restart is optional
#           For Phase 2 we keep it simple: enable_fallback stops the slow container.

_ACTION_MAP = {
    "crash":   "restart_service",
    "timeout": "restart_service",
    "slow":    "enable_fallback",
}

_DEFAULT_ACTION = "restart_service"


# ── Lambda handler ────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Entry point invoked by EventBridge.

    Parameters
    ----------
    event   : Full EventBridge event envelope (dict).
    context : Lambda context object (used for request ID in logs).
    """
    logger.info("lambda_handler: received event — %s", json.dumps(event))

    # ── Step 1: parse the FailureEvent detail ─────────────────────────────────
    detail = event.get("detail", {})
    if not detail:
        logger.error("lambda_handler: event has no 'detail' key — ignoring")
        return _response(400, "Missing event detail")

    service_name = detail.get("service_name", TARGET_SERVICE)
    failure_type = detail.get("failure_type", "crash")
    latency_ms   = detail.get("latency_ms", 0)
    timestamp    = detail.get("timestamp", "")

    logger.info(
        "lambda_handler: service=%s  failure=%s  latency=%.0fms  ts=%s",
        service_name, failure_type, latency_ms, timestamp,
    )

    # ── Step 2: decide action ─────────────────────────────────────────────────
    action = _decide_action(failure_type)
    logger.info("lambda_handler: decided action=%s for failure_type=%s", action, failure_type)

    # ── Step 3: call recovery-agent ───────────────────────────────────────────
    reason = f"Lambda triggered by {failure_type} on {service_name} at {timestamp}"
    result = _call_recovery_agent(
        action=action,
        target_service=service_name,
        reason=reason,
    )

    return _response(200 if result.get("success") else 500, result)


# ── Helper functions ──────────────────────────────────────────────────────────

def _decide_action(failure_type: str) -> str:
    """Look up the correct action for this failure type."""
    action = _ACTION_MAP.get(failure_type, _DEFAULT_ACTION)
    if failure_type not in _ACTION_MAP:
        logger.warning(
            "_decide_action: unknown failure_type=%r — defaulting to %s",
            failure_type, _DEFAULT_ACTION,
        )
    return action


def _call_recovery_agent(action: str, target_service: str, reason: str) -> dict:
    """
    POST /action to recovery-agent.

    Uses only Python standard library (urllib) so no extra packages need
    to be included in the Lambda deployment zip.

    Returns the parsed JSON response body, or an error dict on failure.
    """
    url     = f"{RECOVERY_AGENT_URL}/action"
    payload = json.dumps({
        "action":         action,
        "target_service": target_service,
        "reason":         reason,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )

    logger.info("_call_recovery_agent: POST %s  payload=%s", url, payload.decode())

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            logger.info("_call_recovery_agent: response — %s", json.dumps(body))
            return body

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else ""
        logger.error(
            "_call_recovery_agent: HTTP %d from recovery-agent — %s",
            exc.code, body,
        )
        return {"success": False, "error": f"HTTP {exc.code}", "detail": body}

    except urllib.error.URLError as exc:
        logger.error("_call_recovery_agent: cannot reach recovery-agent — %s", exc.reason)
        return {"success": False, "error": str(exc.reason)}

    except Exception as exc:
        logger.exception("_call_recovery_agent: unexpected error — %s", exc)
        return {"success": False, "error": str(exc)}


def _response(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }
