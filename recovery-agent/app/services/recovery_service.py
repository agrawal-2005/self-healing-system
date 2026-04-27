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
    ) -> None:
        self.docker_executor      = docker_executor
        self.service_name         = service_name
        self.allowed_services     = allowed_services
        self.history_repository   = history_repository
        self.cloudwatch_publisher = cloudwatch_publisher

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
        """Stop the target container to force api-service to use fallback-service."""
        cmd_result = self.docker_executor.stop(request.target_service)
        return ActionResponse(
            success=cmd_result.success,
            action=request.action,
            target_service=request.target_service,
            message=(
                f"Fallback enabled: '{request.target_service}' stopped."
                if cmd_result.success
                else f"Failed to stop '{request.target_service}'."
            ),
            command_result=cmd_result,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

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
