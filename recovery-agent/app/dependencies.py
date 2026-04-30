"""
Dependency injection wiring for recovery-agent.
"""

from app.config.settings import settings
from app.publishers.cloudwatch_publisher import CloudWatchMetricsPublisher
from app.publishers.s3_crash_report_publisher import S3CrashReportPublisher
from app.services.docker_executor import DockerExecutor
from app.services.recovery_history import RecoveryHistoryRepository
from app.services.recovery_service import RecoveryService

_docker_executor = DockerExecutor(command_timeout=settings.docker_command_timeout)

_history_repository = RecoveryHistoryRepository(
    file_path=settings.recovery_history_path
)

_cloudwatch_publisher = CloudWatchMetricsPublisher(
    region    = settings.aws_region,
    namespace = settings.cloudwatch_namespace,
    enabled   = settings.cloudwatch_enabled,
)

_s3_crash_publisher = S3CrashReportPublisher(
    bucket = settings.s3_crash_reports_bucket,
    region = settings.aws_region,
    prefix = settings.s3_crash_reports_prefix,
)

_recovery_service = RecoveryService(
    docker_executor      = _docker_executor,
    service_name         = settings.service_name,
    allowed_services     = [
        item.strip()
        for item in settings.allowed_services.split(",")
        if item.strip()
    ],
    history_repository   = _history_repository,
    cloudwatch_publisher = _cloudwatch_publisher,
    crash_reports_dir    = settings.crash_reports_dir,
    s3_crash_publisher   = _s3_crash_publisher,
)


def get_recovery_service() -> RecoveryService:
    return _recovery_service
