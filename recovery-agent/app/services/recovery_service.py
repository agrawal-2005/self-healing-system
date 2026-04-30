"""
RecoveryService — business logic for recovery actions.

Action → Docker command mapping:
  RESTART_SERVICE  → docker restart {target}
    Use case: core-service crashed or is in a bad state. Restart it.

  ENABLE_FALLBACK  → docker stop {target}
    Use case: core-service is too slow or unreliable. Stop it to force
    api-service to route all traffic to fallback-service.

  DISABLE_FALLBACK → docker start {target}
    Use case: core-service has been fixed. Bring it back online.

Security: only services in allowed_services may be acted on (checked here).
History:  every action (success or failure) is appended to the JSONL log.
Timing:   recovery_duration_ms measures how long the docker command took.

Phase 4 addition:
  CloudWatch metrics are emitted after each action.
  The cloudwatch_publisher is a no-op when CLOUDWATCH_ENABLED=false.
"""

import logging
import os
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from app.models.schemas import ActionRequest, ActionResponse, ActionType, HealthResponse
from app.publishers.cloudwatch_publisher import CloudWatchMetricsPublisher
from app.services.docker_executor import DockerExecutor
from app.services.recovery_history import IncidentRecord, RecoveryHistoryRepository

logger = logging.getLogger(__name__)


class RecoveryService:
    def __init__(
        self,
        docker_executor: DockerExecutor,
        service_name: str,
        allowed_services: list[str],
        history_repository: RecoveryHistoryRepository,
        cloudwatch_publisher: CloudWatchMetricsPublisher,
        crash_reports_dir: str = "/app/data/crash_reports",
    ) -> None:
        self.docker_executor      = docker_executor
        self.service_name         = service_name
        self.allowed_services     = allowed_services
        self.history_repository   = history_repository
        self.cloudwatch_publisher = cloudwatch_publisher
        self.crash_reports_dir    = crash_reports_dir
        # Two subfolders so real incidents stay clean and discoverable:
        #   incidents/ — production CRITICAL escalations from Lambda
        #   tests/     — manual tests, marked with [TEST] in reason field
        os.makedirs(os.path.join(crash_reports_dir, "incidents"), exist_ok=True)
        os.makedirs(os.path.join(crash_reports_dir, "tests"), exist_ok=True)

    def health(self) -> HealthResponse:
        return HealthResponse(status="healthy", service=self.service_name)

    def execute_action(self, request: ActionRequest) -> ActionResponse:
        """
        Route the incoming action to the correct private handler,
        measure how long it takes, write a record to history, emit CloudWatch metrics.

        Order matters:
          1. Validate the target service against the allowlist.
          2. Execute the docker command (handler returns ActionResponse).
          3. THEN write history — response must exist before we record it.
          4. THEN emit CloudWatch metrics — after history, never block on metrics.
          5. Return the response.
        """
        logger.info(
            "RecoveryService: action=%s  target=%s  reason=%r",
            request.action, request.target_service, request.reason,
        )

        # ── Security: allowlist check ──────────────────────────────────────────
        if request.target_service not in self.allowed_services:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Service '{request.target_service}' is not in the allowed list. "
                    f"Allowed: {self.allowed_services}"
                ),
            )

        handlers = {
            ActionType.RESTART_SERVICE:  self._restart_service,
            ActionType.ENABLE_FALLBACK:  self._enable_fallback,
            ActionType.DISABLE_FALLBACK: self._disable_fallback,
        }

        handler = handlers[request.action]

        # ── Execute and measure duration ───────────────────────────────────────
        t_start  = time.monotonic()
        response = handler(request)                            # ← execute FIRST
        duration = (time.monotonic() - t_start) * 1000        # convert to ms

        # ── Write to history AFTER response exists ─────────────────────────────
        record = IncidentRecord(
            timestamp            = response.timestamp,
            service_name         = request.target_service,
            failure_type         = request.reason,
            action               = request.action.value,
            success              = response.success,
            message              = response.message,
            recovery_duration_ms = round(duration, 2),
            reason               = request.reason,
            stdout     = response.command_result.stdout     if response.command_result else None,
            stderr     = response.command_result.stderr     if response.command_result else None,
            returncode = response.command_result.returncode if response.command_result else None,
            # Phase 6 enrichment — None when request comes from older Lambda
            severity          = request.severity,
            failure_count     = request.failure_count,
            recovery_strategy = request.recovery_strategy,
            escalation_reason = request.escalation_reason,
        )
        self.history_repository.write_record(record)

        # ── Emit CloudWatch metrics AFTER history ──────────────────────────────
        action_str = request.action.value
        if response.success:
            self.cloudwatch_publisher.record_recovery_success(
                target_service=request.target_service,
                action=action_str,
            )
        else:
            self.cloudwatch_publisher.record_recovery_failure(
                target_service=request.target_service,
                action=action_str,
            )
        self.cloudwatch_publisher.record_recovery_duration(
            target_service=request.target_service,
            action=action_str,
            duration_ms=round(duration, 2),
        )

        # Phase 6 — severity and escalation metrics
        if request.severity:
            self.cloudwatch_publisher.record_incident_severity(
                target_service=request.target_service,
                severity=request.severity,
            )
            if request.escalation_reason:
                self.cloudwatch_publisher.record_escalation(
                    target_service=request.target_service,
                    severity=request.severity,
                )
                logger.warning(
                    "RecoveryService: ESCALATION severity=%s target=%s reason=%s",
                    request.severity, request.target_service, request.escalation_reason,
                )

        return response

    # ── action handlers ───────────────────────────────────────────────────────

    def _restart_service(self, request: ActionRequest) -> ActionResponse:
        cmd_result = self.docker_executor.restart(request.target_service)
        return ActionResponse(
            success=cmd_result.success,
            action=request.action,
            target_service=request.target_service,
            message=(
                f"Container '{request.target_service}' restarted successfully."
                if cmd_result.success
                else f"Failed to restart '{request.target_service}'. See command_result."
            ),
            command_result=cmd_result,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _enable_fallback(self, request: ActionRequest) -> ActionResponse:
        """Stop the target container to force api-service to use fallback-service.

        Before stopping, capture the container's last 200 log lines and write
        them to a crash report file so developers can diagnose the root cause.
        """
        # ── Step 1: capture logs BEFORE stopping (logs are gone after docker rm) ─
        report_path = self._write_crash_report(request)

        # ── Step 2: stop the container ────────────────────────────────────────────
        cmd_result = self.docker_executor.stop(request.target_service)
        return ActionResponse(
            success=cmd_result.success,
            action=request.action,
            target_service=request.target_service,
            message=(
                f"Fallback enabled: '{request.target_service}' stopped. "
                f"Crash report saved to {report_path}"
                if cmd_result.success
                else f"Failed to stop '{request.target_service}'."
            ),
            command_result=cmd_result,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _write_crash_report(self, request: ActionRequest) -> str:
        """Capture the last 200 lines of container logs and write a crash report file.

        Routing:
          reason starts with "[TEST]"  →  crash_reports/tests/
          otherwise (real Lambda call) →  crash_reports/incidents/

        This keeps the developer-facing `incidents/` directory clean and
        free of synthetic test artifacts. Alerting and dashboards should
        only watch `incidents/`.

        File name: <service>_<UTC-timestamp>.txt
        e.g.  core-service_2026-04-30T22-31-05Z.txt
        """
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        filename = f"{request.target_service}_{ts}.txt"

        is_test = bool(request.reason) and request.reason.strip().upper().startswith("[TEST]")
        subdir = "tests" if is_test else "incidents"
        filepath = os.path.join(self.crash_reports_dir, subdir, filename)

        log_result = self.docker_executor.capture_logs(request.target_service, tail=200)

        lines = []
        lines.append("=" * 72)
        lines.append(f"CRASH REPORT — {request.target_service}")
        lines.append(f"Captured  : {ts}")
        lines.append(f"Severity  : {request.severity or 'CRITICAL'}")
        lines.append(f"Failures  : {request.failure_count or 'N/A'} consecutive crashes")
        lines.append(f"Action    : {request.action.value} (enable_fallback triggered)")
        lines.append(f"Reason    : {request.reason}")
        if request.escalation_reason:
            lines.append(f"Escalation: {request.escalation_reason}")
        lines.append("=" * 72)
        lines.append("")
        lines.append("── CONTAINER LOGS (last 200 lines) ──")
        lines.append("")

        if log_result.success or log_result.stdout:
            lines.append(log_result.stdout or "(empty stdout)")
        if log_result.stderr:
            # docker logs writes to stderr by default — this is the real output
            lines.append(log_result.stderr)
        if not log_result.success and not log_result.stdout and not log_result.stderr:
            lines.append(f"(failed to capture logs: {log_result.error})")

        lines.append("")
        lines.append("── END OF REPORT ──")

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            logger.info(
                "RecoveryService: crash report saved → %s  severity=%s  failures=%s  bucket=%s",
                filepath, request.severity, request.failure_count, subdir,
            )
        except OSError as exc:
            logger.error("RecoveryService: failed to write crash report → %s", exc)
        return filepath

    def _disable_fallback(self, request: ActionRequest) -> ActionResponse:
        """Start the target container to restore normal routing."""
        cmd_result = self.docker_executor.start(request.target_service)
        return ActionResponse(
            success=cmd_result.success,
            action=request.action,
            target_service=request.target_service,
            message=(
                f"Fallback disabled: '{request.target_service}' started. "
                "Normal routing restored once healthcheck passes."
                if cmd_result.success
                else f"Failed to start '{request.target_service}'."
            ),
            command_result=cmd_result,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
