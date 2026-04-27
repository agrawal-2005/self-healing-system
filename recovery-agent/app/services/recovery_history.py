"""
RecoveryHistoryRepository — append-only audit log for every recovery action.

Why JSONL (JSON Lines)?
  - One JSON object per line → trivial to tail, grep, and parse with any tool.
  - No database setup or migration needed.
  - File survives container restarts because it lives in a mounted volume.
  - Easy to import into CloudWatch Logs Insights, Splunk, or any log aggregator.

File location: /app/data/recovery_history.jsonl (override via RECOVERY_HISTORY_PATH)
Host path:     ./recovery-agent/data/recovery_history.jsonl (see docker-compose.yml)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class IncidentRecord(BaseModel):
    """
    Structured record for a single recovery action.

    Stored as one JSON line in the JSONL history file.
    Every field has a type so you can read them back with full validation.
    """

    timestamp:            str             # ISO-8601 UTC, e.g. "2026-04-27T10:00:00+00:00"
    service_name:         str             # which container was acted on
    failure_type:         str             # "crash", "timeout", "slow", etc.
    action:               str             # "restart_service", "enable_fallback", etc.
    success:              bool            # did the docker command succeed?
    message:              str             # human-readable result
    recovery_duration_ms: float           # wall-clock time for the docker command
    reason:               str             # why this action was triggered (from Lambda)
    stdout:               Optional[str]   # raw docker stdout
    stderr:               Optional[str]   # raw docker stderr (useful for debugging)
    returncode:           Optional[int]   # docker CLI exit code (0 = success)


class RecoveryHistoryRepository:
    """
    Append-only store for IncidentRecord objects.

    write_record() — appends one record to the JSONL file (never raises).
    read_records() — returns the last N records for inspection/debugging.
    """

    def __init__(self, file_path: str = "/app/data/recovery_history.jsonl") -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("RecoveryHistoryRepository: history at %s", self.file_path)

    def write_record(self, record: IncidentRecord) -> None:
        """Append one record to the JSONL file. Never raises — a history write failure
        must not abort the recovery action that already succeeded."""
        try:
            with self.file_path.open("a", encoding="utf-8") as f:
                f.write(record.model_dump_json() + "\n")
            logger.info(
                "RecoveryHistory: recorded action=%s service=%s success=%s duration=%.0fms",
                record.action, record.service_name, record.success, record.recovery_duration_ms,
            )
        except OSError as exc:
            logger.error("RecoveryHistory: failed to write record — %s", exc)

    def read_records(self, last_n: int = 50) -> list[IncidentRecord]:
        """
        Return the last N IncidentRecords from the history file.
        Returns an empty list if the file does not exist yet.
        Safe to call at any time — never raises.
        """
        if not self.file_path.exists():
            return []
        try:
            lines  = self.file_path.read_text(encoding="utf-8").splitlines()
            recent = lines[-last_n:]
            return [
                IncidentRecord.model_validate_json(line)
                for line in recent
                if line.strip()
            ]
        except Exception as exc:
            logger.error("RecoveryHistory: failed to read records — %s", exc)
            return []
