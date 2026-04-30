"""
Settings for core-service.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "core-service"
    host: str = "0.0.0.0"
    port: int = 8001

    # How many seconds /slow sleeps to simulate high latency.
    # Must be > api-service REQUEST_TIMEOUT (3.0) to trigger a timeout fallback.
    slow_delay_seconds: float = 5.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
