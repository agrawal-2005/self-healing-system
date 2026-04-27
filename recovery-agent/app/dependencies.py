"""
Dependency injection wiring for recovery-agent.
"""

from app.config.settings import settings
from app.services.docker_executor import DockerExecutor
from app.services.recovery_service import RecoveryService

_docker_executor = DockerExecutor(command_timeout=settings.docker_command_timeout)

_recovery_service = RecoveryService(
    docker_executor=_docker_executor,
    service_name=settings.service_name,
)


def get_recovery_service() -> RecoveryService:
    return _recovery_service
