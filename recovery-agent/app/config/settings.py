"""
Settings for recovery-agent.

All values can be overridden via environment variables or a .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "recovery-agent"
    host: str = "0.0.0.0"
    port: int = 8003

    # Seconds to wait before killing a hung docker command
    docker_command_timeout: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
