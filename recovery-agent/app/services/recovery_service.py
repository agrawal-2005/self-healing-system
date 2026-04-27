"""
RecoveryService — business logic for recovery actions.

This class owns the DECISION: given an ActionType, which Docker command runs?

Action → Docker command mapping:
  RESTART_SERVICE  → docker restart {target}
    Use case: core-service crashed or is in a bad state. Restart it.
    api-service will automatically resume using it once it is healthy.

  ENABLE_FALLBACK  → docker stop {target}
    Use case: core-service is too slow or unreliable and should be taken
    offline deliberately. Stopping the container forces api-service to
    route all requests to fallback-service.

  DISABLE_FALLBACK → docker start {target}
    Use case: core-service has been fixed/patched offline. Bring it back.
    api-service will detect it as healthy and resume normal routing.
"""

import logging
from datetime import datetime, timezone

from app.models.schemas import ActionRequest, ActionResponse, ActionType, HealthResponse
from app.services.docker_executor import DockerExecutor

logger = logging.getLogger(__name__)


class RecoveryService:
    def __init__(self, docker_executor: DockerExecutor, service_name: str) -> None:
        self.docker_executor = docker_executor
        self.service_name = service_name

    def health(self) -> HealthResponse:
        return HealthResponse(status="healthy", service=self.service_name)

    def execute_action(self, request: ActionRequest) -> ActionResponse:
        """
        Route the incoming action to the correct private handler.

        Using a dict of handlers (instead of if/elif) means adding a new
        action type only requires adding one entry here — no existing code
        changes needed.
        """
        logger.info(
            "RecoveryService: action=%s  target=%s  reason=%r",
            request.action,
            request.target_service,
            request.reason,
        )

        handlers = {
            ActionType.RESTART_SERVICE:  self._restart_service,
            ActionType.ENABLE_FALLBACK:  self._enable_fallback,
            ActionType.DISABLE_FALLBACK: self._disable_fallback,
        }

        handler = handlers[request.action]
        return handler(request)

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
                else f"Failed to restart '{request.target_service}'. See command_result for details."
            ),
            command_result=cmd_result,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _enable_fallback(self, request: ActionRequest) -> ActionResponse:
        """
        Stop the target container.
        api-service detects the connection refused on /work and automatically
        routes to fallback-service — no code changes needed in api-service.
        """
        cmd_result = self.docker_executor.stop(request.target_service)
        return ActionResponse(
            success=cmd_result.success,
            action=request.action,
            target_service=request.target_service,
            message=(
                f"Fallback enabled: '{request.target_service}' stopped. "
                "api-service will route all traffic to fallback-service."
                if cmd_result.success
                else f"Failed to stop '{request.target_service}'."
            ),
            command_result=cmd_result,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def _disable_fallback(self, request: ActionRequest) -> ActionResponse:
        """
        Start the target container to restore normal routing.
        """
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
