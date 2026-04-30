"""
DockerExecutor — executes Docker CLI commands via subprocess.

Single responsibility:  run a docker command, capture output, return a result.
It does NOT decide which command to run — that is RecoveryService's job.

Security note:
  subprocess.run() is always called with a LIST, never a shell string.
  This prevents shell-injection attacks (e.g. a bad container name like
  "core-service; rm -rf /").

Runtime note:
  This container must have:
    1. The docker CLI binary installed (added in the Dockerfile).
    2. The host Docker socket mounted:
         /var/run/docker.sock:/var/run/docker.sock
  Without both, every command will fail with FileNotFoundError or
  "Cannot connect to the Docker daemon".
"""

import logging
import subprocess

from app.models.schemas import CommandResult

logger = logging.getLogger(__name__)


class DockerExecutor:
    def __init__(self, command_timeout: int = 30) -> None:
        """
        Parameters
        ----------
        command_timeout : int
            Maximum seconds to wait for any docker command before raising
            subprocess.TimeoutExpired.
        """
        self.command_timeout = command_timeout

    # ── private ───────────────────────────────────────────────────────────────

    def _run(self, cmd: list[str]) -> CommandResult:
        """
        Core execution method.  All public methods delegate here.
        """
        cmd_str = " ".join(cmd)
        logger.info("DockerExecutor: running → %s", cmd_str)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
            )
            success = result.returncode == 0

            if success:
                logger.info("DockerExecutor: success  stdout=%s", result.stdout.strip())
            else:
                logger.error(
                    "DockerExecutor: failed   rc=%d stderr=%s",
                    result.returncode,
                    result.stderr.strip(),
                )

            return CommandResult(
                success=success,
                stdout=result.stdout.strip(),
                stderr=result.stderr.strip(),
                returncode=result.returncode,
            )

        except subprocess.TimeoutExpired:
            msg = f"Command timed out after {self.command_timeout}s: {cmd_str}"
            logger.error("DockerExecutor: %s", msg)
            return CommandResult(success=False, error=msg, returncode=-1)

        except FileNotFoundError:
            msg = "Docker CLI not found. Is docker installed inside this container?"
            logger.error("DockerExecutor: %s", msg)
            return CommandResult(success=False, error=msg, returncode=-1)

        except Exception as exc:
            msg = f"Unexpected error: {exc}"
            logger.exception("DockerExecutor: %s", msg)
            return CommandResult(success=False, error=msg, returncode=-1)

    # ── public commands ───────────────────────────────────────────────────────

    def restart(self, container_name: str) -> CommandResult:
        """docker restart <container_name>"""
        return self._run(["docker", "restart", container_name])

    def stop(self, container_name: str) -> CommandResult:
        """docker stop <container_name>"""
        return self._run(["docker", "stop", container_name])

    def start(self, container_name: str) -> CommandResult:
        """docker start <container_name>"""
        return self._run(["docker", "start", container_name])

    def capture_logs(self, container_name: str, tail: int = 200) -> CommandResult:
        """docker logs --tail <tail> <container_name>  — capture last N lines before stop."""
        return self._run(["docker", "logs", "--tail", str(tail), container_name])
