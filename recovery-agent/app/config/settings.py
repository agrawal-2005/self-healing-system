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

    recovery_token: str = "dev-token"
    allowed_services: str = "core-service"
    recovery_history_path: str = "/app/data/recovery_history.jsonl"
    crash_reports_dir: str = "/app/data/crash_reports"

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


settings = Settings()
