"""
Pydantic models for recovery-agent.

ActionRequest  — what Lambda (or any caller) sends to POST /action
ActionResponse — what recovery-agent sends back
CommandResult  — structured result from DockerExecutor
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ActionType(str, Enum):
    """
    The three recovery actions recovery-agent supports.

    RESTART_SERVICE  → docker restart {target_service}
    ENABLE_FALLBACK  → docker stop {target_service}   (forces fallback in api-service)
    DISABLE_FALLBACK → docker start {target_service}  (restores normal routing)
    """
    RESTART_SERVICE  = "restart_service"
    ENABLE_FALLBACK  = "enable_fallback"
    DISABLE_FALLBACK = "disable_fallback"


class ActionRequest(BaseModel):
    """
    Sent by Lambda to POST /action.

    action         — which recovery action to perform
    target_service — Docker container name to act on (default: core-service)
    reason         — human-readable reason (from Lambda, useful in logs)
    """
    action: ActionType
    target_service: str = "core-service"
    reason: str = ""


class CommandResult(BaseModel):
    """
    Structured output from a docker CLI command.
    Returned inside ActionResponse so callers can see exactly what happened.
    """
    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    error: Optional[str] = None   # set only when an exception was raised


class ActionResponse(BaseModel):
    """Complete result of executing a recovery action."""
    success: bool
    action: str
    target_service: str
    message: str
    command_result: Optional[CommandResult] = None
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    service: str
