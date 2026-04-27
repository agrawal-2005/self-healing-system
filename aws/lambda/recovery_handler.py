"""
AWS Lambda — Recovery Handler (Phase 3).

Triggered by EventBridge when monitor publishes a ServiceFailureDetected event.

Phase 3 improvements over Phase 2:
  - Retry logic: up to 3 attempts with exponential backoff (2s, 4s) on network errors
  - Duration measurement: logs how long the full recovery took
  - Structured response: includes request_id, duration_ms, attempt count
  - Better error distinction: network error vs HTTP error vs timeout
  - Timeout guard: 15s per HTTP attempt, total Lambda timeout is set in AWS console

Environment variables:
  RECOVERY_AGENT_URL  — tunnel/VPC URL of recovery-agent (e.g. https://abc.serveousercontent.com)
  TARGET_SERVICE      — default Docker container name (default: core-service)
  RECOVERY_TOKEN      — shared secret sent as X-Recovery-Token header
  MAX_RETRIES         — how many attempts before giving up (default: 3)
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Configuration ─────────────────────────────────────────────────────────────

RECOVERY_AGENT_URL = os.environ.get("RECOVERY_AGENT_URL", "http://localhost:8003")
TARGET_SERVICE     = os.environ.get("TARGET_SERVICE", "core-service")
RECOVERY_TOKEN     = os.environ.get("RECOVERY_TOKEN", "dev-token")
MAX_RETRIES        = int(os.environ.get("MAX_RETRIES", "3"))

# ── Action decision table ─────────────────────────────────────────────────────

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
    event   : Full EventBridge event envelope.
    context : Lambda context (provides request_id for structured logs).
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

    # ── Step 2: decide action ─────────────────────────────────────────────────
    action = _decide_action(failure_type)
    logger.info(
        "[%s] lambda_handler: decided action=%s for failure_type=%s",
        request_id, action, failure_type,
    )

    # ── Step 3: call recovery-agent (with retry) ──────────────────────────────
    reason = f"Lambda triggered by {failure_type} on {service_name} at {timestamp}"
    result, attempts = _call_recovery_agent_with_retry(
        action=action,
        target_service=service_name,
        reason=reason,
        request_id=request_id,
    )

    duration_ms = (time.monotonic() - t_start) * 1000
    result["request_id"]   = request_id
    result["duration_ms"]  = round(duration_ms, 1)
    result["attempts"]     = attempts

    logger.info(
        "[%s] lambda_handler: complete — success=%s  duration=%.0fms  attempts=%d",
        request_id, result.get("success"), duration_ms, attempts,
    )

    return _response(200 if result.get("success") else 500, result)


# ── Helper functions ──────────────────────────────────────────────────────────

def _decide_action(failure_type: str) -> str:
    action = _ACTION_MAP.get(failure_type, _DEFAULT_ACTION)
    if failure_type not in _ACTION_MAP:
        logger.warning(
            "_decide_action: unknown failure_type=%r — defaulting to %s",
            failure_type, _DEFAULT_ACTION,
        )
    return action


def _call_recovery_agent_with_retry(
    action: str,
    target_service: str,
    reason: str,
    request_id: str,
) -> tuple[dict, int]:
    """
    Calls recovery-agent with exponential backoff retry.

    Retry policy:
      Attempt 1 — immediate
      Attempt 2 — wait 2 seconds  (only on URLError / network failure)
      Attempt 3 — wait 4 seconds
      HTTP errors (4xx/5xx) are NOT retried — they indicate a logic problem,
      not a transient network issue.

    Returns (result_dict, number_of_attempts_made).
    """
    backoff_seconds = 2
    last_result: dict = {}

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("[%s] _call_recovery_agent: attempt %d/%d", request_id, attempt, MAX_RETRIES)
        result = _call_recovery_agent(action, target_service, reason, request_id)
        last_result = result

        if result.get("success"):
            return result, attempt

        # HTTP errors (4xx/5xx) — do not retry, it won't help
        error = result.get("error", "")
        if error.startswith("HTTP"):
            logger.error(
                "[%s] _call_recovery_agent: HTTP error %s — not retrying",
                request_id, error,
            )
            return result, attempt

        # Network error — worth retrying
        if attempt < MAX_RETRIES:
            wait = backoff_seconds * attempt
            logger.warning(
                "[%s] _call_recovery_agent: network error on attempt %d, retrying in %ds",
                request_id, attempt, wait,
            )
            time.sleep(wait)

    return last_result, MAX_RETRIES


def _call_recovery_agent(
    action: str,
    target_service: str,
    reason: str,
    request_id: str,
) -> dict:
    """Single HTTP call to recovery-agent POST /action."""
    url     = f"{RECOVERY_AGENT_URL}/action"
    payload = json.dumps({
        "action":         action,
        "target_service": target_service,
        "reason":         reason,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data    = payload,
        headers = {
            "Content-Type":      "application/json",
            "X-Recovery-Token":  RECOVERY_TOKEN,
            "X-Request-Id":      request_id,
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


def _response(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }
