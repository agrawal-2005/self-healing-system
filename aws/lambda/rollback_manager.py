"""
RollbackManager — Phase 6 AutoRollback prototype (dry-run).

Tracks the last-known-good image reference for each service across warm
Lambda invocations.  When a service reaches CRITICAL severity, recommends
a rollback to the last-known-good image.

Phase 6: DRY-RUN ONLY.
  - Logs ROLLBACK_RECOMMENDED with the image reference
  - Returns the recommended image reference to the caller (Lambda handler)
    so it can emit a CloudWatch metric
  - Does NOT execute docker pull or docker restart with the old image

Phase 7 would add ROLLBACK_EXECUTED by calling a new /rollback action on
recovery-agent (or via an AWS Step Functions state machine).

Image baseline:
  Lambda reads IMAGE_TAG env var (default: "latest") and uses
  "{service_name}:{image_tag}" as a symbolic image reference.
  In a real CD pipeline, IMAGE_TAG would be the previous Git SHA or
  semver tag that was deployed before the current failing version.

Why module-level state?
  Same warm-invocation reasoning as SmartRecoveryPolicy — no DynamoDB/SSM
  needed for a prototype while still demonstrating the rollback concept.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from smart_recovery_policy import IncidentSeverity

logger = logging.getLogger(__name__)

# Symbolic image tag — override with IMAGE_TAG env var in Lambda config
_IMAGE_TAG = os.environ.get("IMAGE_TAG", "latest")


# ── Rollback state ────────────────────────────────────────────────────────────

@dataclass
class RollbackEntry:
    """Tracks image state for a single service."""
    last_known_good_image:    Optional[str] = None
    successful_recovery_count: int           = 0


# Maps service_name → RollbackEntry
_rollback_state: dict[str, RollbackEntry] = {}


# ── RollbackManager ───────────────────────────────────────────────────────────

class RollbackManager:
    """
    Dry-run rollback recommendation engine.

    Typical call sequence in Lambda handler:
        rm = RollbackManager()

        # At startup (or before first recovery), record baseline
        rm.record_baseline(service_name)

        # After a successful recovery, note it
        rm.record_successful_recovery(service_name)

        # When severity is CRITICAL, check and possibly recommend
        if rm.should_recommend(service_name, severity):
            image_ref = rm.recommend_rollback(service_name)
            # emit RollbackRecommendedCount metric
    """

    def record_baseline(self, service_name: str) -> str:
        """
        Store '{service_name}:{IMAGE_TAG}' as the last-known-good image reference.
        Called before initiating recovery so we always have a rollback target.
        Returns the image reference string.
        """
        image_ref = f"{service_name}:{_IMAGE_TAG}"
        entry = _rollback_state.setdefault(service_name, RollbackEntry())
        if entry.last_known_good_image != image_ref:
            entry.last_known_good_image = image_ref
            logger.info(
                "RollbackManager: baseline recorded — service=%s image=%s",
                service_name, image_ref,
            )
        return image_ref

    def record_successful_recovery(self, service_name: str) -> None:
        """
        Increment stable recovery counter.
        Called when recovery-agent reports success=True.
        """
        entry = _rollback_state.setdefault(service_name, RollbackEntry())
        entry.successful_recovery_count += 1
        logger.debug(
            "RollbackManager: recovery #%d succeeded for %s",
            entry.successful_recovery_count, service_name,
        )

    def should_recommend(
        self, service_name: str, severity: IncidentSeverity
    ) -> bool:
        """
        Returns True when severity is CRITICAL AND a baseline image is stored.
        """
        if severity != IncidentSeverity.CRITICAL:
            return False
        entry = _rollback_state.get(service_name)
        return entry is not None and entry.last_known_good_image is not None

    def recommend_rollback(self, service_name: str) -> Optional[str]:
        """
        Log ROLLBACK_RECOMMENDED and return the image reference.
        Returns None if no baseline is recorded (caller should handle gracefully).
        """
        entry = _rollback_state.get(service_name)
        if not entry or not entry.last_known_good_image:
            logger.warning(
                "RollbackManager: no baseline recorded for %s — cannot recommend rollback",
                service_name,
            )
            return None

        image_ref = entry.last_known_good_image
        logger.warning(
            "ROLLBACK_RECOMMENDED: service=%s  image=%s  "
            "(Phase 6 dry-run — execute: docker pull %s && docker restart %s)",
            service_name, image_ref, image_ref, service_name,
        )
        return image_ref
