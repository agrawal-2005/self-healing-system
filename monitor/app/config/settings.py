"""
Settings for the class-based monitor.

Split into three logical groups:
  1. Which services to watch and how often
  2. Failure thresholds
  3. AWS integration config + feature flags
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Services to monitor ───────────────────────────────────────────────────
    api_service_url: str      = "http://localhost:8000"
    core_service_url: str     = "http://localhost:8001"
    fallback_service_url: str = "http://localhost:8002"

    # ── Timing ────────────────────────────────────────────────────────────────
    check_interval_seconds: int   = 5     # pause between check cycles
    request_timeout_seconds: float = 3.0  # per-request HTTP timeout

    # ── Failure thresholds ────────────────────────────────────────────────────
    latency_warn_ms: float  = 500.0   # above this → SLOW
    latency_slow_ms: float  = 1000.0  # above this → VERY_SLOW

    # How long (seconds) to wait before sending a second event for the SAME
    # service+failure_type combo.  Prevents flooding EventBridge.
    event_cooldown_seconds: int = 60

    # ── AWS / EventBridge ─────────────────────────────────────────────────────
    aws_region: str                = "us-east-1"
    eventbridge_event_bus: str     = "default"
    eventbridge_source: str        = "selfhealing.local"
    eventbridge_detail_type: str   = "ServiceFailureDetected"

    # ── CloudWatch ────────────────────────────────────────────────────────────
    # Metrics namespace (default matches other services)
    cloudwatch_namespace: str  = "SelfHealingSystem"

    # ── Feature flags ─────────────────────────────────────────────────────────
    # Set EVENTBRIDGE_ENABLED=false to run monitor without AWS credentials.
    eventbridge_enabled: bool  = True
    # Set CLOUDWATCH_ENABLED=true to publish metrics (optional).
    cloudwatch_enabled: bool   = False
    # Set DRY_RUN=true to log events instead of sending them (for local testing).
    dry_run: bool              = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
