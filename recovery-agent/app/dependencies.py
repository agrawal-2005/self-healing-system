"""
Dependency injection wiring for recovery-agent.
"""

from app.config.settings import settings
from app.services.docker_executor import DockerExecutor
from app.services.recovery_history import RecoveryHistoryRepository
from app.services.recovery_service import RecoveryService

_docker_executor = DockerExecutor(command_timeout=settings.docker_command_timeout)

_history_repository = RecoveryHistoryRepository(
    file_path=settings.recovery_history_path
)

_recovery_service = RecoveryService(
    docker_executor=_docker_executor,
    service_name=settings.service_name,
    allowed_services=[
        item.strip()
        for item in settings.allowed_services.split(",")
        if item.strip()
    ],
    history_repository=_history_repository,
)


def get_recovery_service() -> RecoveryService:
    return _recovery_service
