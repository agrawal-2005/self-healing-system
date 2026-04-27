"""
Pydantic models shared across monitor classes.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ServiceStatus(str, Enum):
    """
    What a single health check found.

    UP        → responded 200 within timeout
    DOWN      → connection refused / non-200
    TIMEOUT   → no response before request_timeout expired
    SLOW      → responded 200 but latency > latency_warn_ms
    VERY_SLOW → responded 200 but latency > latency_slow_ms
    """
    UP        = "UP"
    DOWN      = "DOWN"
    TIMEOUT   = "TIMEOUT"
    SLOW      = "SLOW"
    VERY_SLOW = "VERY_SLOW"


class HealthCheckResult(BaseModel):
    """
    Output of HealthChecker.check().
    Passed to LatencyChecker and eventually to MonitorService for decisions.
    """
    service_name: str
    url: str
    status: ServiceStatus
    latency_ms: float
    timestamp: str                  # ISO-8601
    http_status_code: Optional[int] = None
    error: Optional[str] = None


class FailureEvent(BaseModel):
    """
    The payload published to EventBridge when a failure is detected.

    Lambda receives this inside event["detail"].
    """
    service_name: str
    failure_type: str    # "crash" | "timeout" | "slow" | "very_slow"
    latency_ms: float
    timestamp: str
    health_endpoint: str
