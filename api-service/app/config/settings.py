"""
Settings for api-service.

Reads configuration from environment variables.
Pydantic-settings automatically maps env var names to field names
(case-insensitive). Defaults work fine for local Docker Compose use.

Phase 8 (config-driven gateway):
  Per-service URLs are no longer listed here — they live in services_config.json.
  Only the fallback-service URL stays, because it is not a routable service
  (clients never call it directly) but the gateway needs it internally.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Identity
    service_name: str = "api-service"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Path to the shared services_config.json.
    # Mounted into the container as /app/services_config.json via docker-compose.
    services_config_path: str = "/app/services_config.json"

    # Fallback-service is not a routable service — clients never call it directly.
    # The gateway uses it internally when a "fallback" strategy fires.
    fallback_service_url: str = "http://fallback-service:8002"

    # Default HTTP timeout (seconds) used when a service entry in the config
    # does not specify its own "timeout" field.
    request_timeout: float = 2.0

    # Circuit breaker defaults — applied to every service in the registry.
    circuit_failure_threshold: int         = 3
    circuit_recovery_timeout_seconds: int  = 30
    circuit_half_open_max_calls: int       = 1

    # ── CloudWatch metrics ────────────────────────────────────────────────────
    cloudwatch_enabled: bool   = False
    cloudwatch_namespace: str  = "SelfHealingSystem"
    aws_region: str            = "us-east-1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
