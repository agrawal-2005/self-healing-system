"""
Settings for fallback-service.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "fallback-service"
    host: str = "0.0.0.0"
    port: int = 8002

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
