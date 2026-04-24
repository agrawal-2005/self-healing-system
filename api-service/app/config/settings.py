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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


# Module-level singleton — import this everywhere instead of re-creating it
settings = Settings()
