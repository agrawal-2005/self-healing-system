"""
Settings for api-service.

Reads configuration from environment variables.
Pydantic-settings automatically maps env var names to field names
(case-insensitive). Defaults work fine for local Docker Compose use.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Identity
    service_name: str = "api-service"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Downstream service base URLs (overridden by docker-compose env block)
    core_service_url: str = "http://core-service:8001"
    fallback_service_url: str = "http://fallback-service:8002"

    # How long (seconds) to wait for a downstream response before giving up
    request_timeout: float = 3.0

    circuit_failure_threshold: int = 3
    circuit_recovery_timeout_seconds: int = 30
    circuit_half_open_max_calls: int = 1

    # ── CloudWatch metrics ────────────────────────────────────────────────────
    # Set CLOUDWATCH_ENABLED=true (+ AWS credentials in env) to publish metrics.
    cloudwatch_enabled: bool   = False
    cloudwatch_namespace: str  = "SelfHealingSystem"
    aws_region: str            = "us-east-1"

    # extra="ignore" so that AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY loaded
    # from env_file don't cause a validation error (boto3 reads them directly).
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Module-level singleton — import this everywhere instead of re-creating it
settings = Settings()
