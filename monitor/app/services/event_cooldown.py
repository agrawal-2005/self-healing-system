"""
EventCooldown — prevents duplicate EventBridge events for the same failure.

Problem it solves:
  If core-service is DOWN for 5 minutes and the monitor checks every 5 seconds,
  that is 60 checks × 1 event each = 60 Lambda invocations for ONE incident.
  This wastes Lambda invocations, costs money, and causes repeated restarts.

How it works:
  Each unique (service_name, failure_type) combination gets its own cooldown timer.
  "core-service:crash" and "core-service:slow" are tracked independently.
  When a service RECOVERS, its timers are cleared so the NEXT failure fires fresh.

Event fingerprinting:
  The key "service_name:failure_type" is the event fingerprint.
  Two events with the same fingerprint within cooldown_seconds are deduplicated.
  Two events with different fingerprints (e.g. crash then slow) are allowed.
"""

import logging
import time

logger = logging.getLogger(__name__)


class EventCooldown:
    def __init__(self, cooldown_seconds: int = 60) -> None:
        self.cooldown_seconds          = cooldown_seconds
        self._last_sent: dict[str, float] = {}

    def _key(self, service_name: str, failure_type: str) -> str:
        """Create a unique fingerprint string for this service + failure combo."""
        return f"{service_name}:{failure_type}"

    def should_send(self, service_name: str, failure_type: str) -> bool:
        """
        Returns True if an event should be published right now.
        Returns False (with a log) if the cooldown window has not expired yet.

        Updates the internal timer when returning True.
        """
        key  = self._key(service_name, failure_type)
        now  = time.monotonic()
        last = self._last_sent.get(key)

        # First time we see this fingerprint → send immediately
        if last is None:
            self._last_sent[key] = now
            return True

        elapsed   = now - last
        remaining = self.cooldown_seconds - elapsed

        if elapsed >= self.cooldown_seconds:
            self._last_sent[key] = now
            return True

        logger.info(
            "EventCooldown: suppressing event service=%s failure=%s "
            "(%.0fs remaining in %ds cooldown window)",
            service_name, failure_type, remaining, self.cooldown_seconds,
        )
        return False

    def clear(self, service_name: str) -> None:
        """
        Called when a service recovers (status goes UP).
        Clears all cooldown timers for this service so the next failure
        always fires a fresh event regardless of recent history.
        """
        prefix         = f"{service_name}:"
        keys_to_delete = [k for k in self._last_sent if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._last_sent[k]
        if keys_to_delete:
            logger.info(
                "EventCooldown: cleared %d timer(s) for '%s' on recovery",
                len(keys_to_delete), service_name,
            )
