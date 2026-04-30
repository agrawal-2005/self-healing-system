#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Crash Report Capture Test
#
# Verifies that recovery-agent captures container logs to a crash report file
# when enable_fallback is triggered at CRITICAL severity (the action Lambda
# takes after 5 crashes in 10 minutes).
#
# Why this test exists:
#   When the system gives up auto-healing and stops the service, container
#   logs would normally be lost. The crash report is what a developer reads
#   to find the actual root cause of the repeated crashes.
#
# How it works:
#   1. Confirms recovery-agent is reachable
#   2. Confirms target service is running (so there are logs to capture)
#   3. Counts existing crash reports
#   4. POSTs enable_fallback (severity=CRITICAL, failure_count=5) to
#      recovery-agent — this is exactly what Lambda sends at CRITICAL
#   5. Verifies a NEW crash report file appeared in the data directory
#   6. Prints the report so you can see the captured logs
#   7. Restarts the stopped service so the system returns to healthy
#
# Usage:
#   ./tests/scripts/crash_report_capture_test.sh
#
# Override defaults:
#   RECOVERY_AGENT_URL=http://other-host:8003 \
#   TARGET_SERVICE=movie-service \
#     ./tests/scripts/crash_report_capture_test.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Configuration ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

RECOVERY_AGENT_URL="${RECOVERY_AGENT_URL:-http://localhost:8003}"
RECOVERY_TOKEN="${RECOVERY_TOKEN:-dev-token}"
TARGET_SERVICE="${TARGET_SERVICE:-core-service}"
TARGET_HEALTH_URL="${TARGET_HEALTH_URL:-http://localhost:8001/health}"
CRASH_REPORTS_DIR="${CRASH_REPORTS_DIR:-$PROJECT_ROOT/recovery-agent/data/crash_reports}"
# This script always uses [TEST] reason → reports land in the tests/ subfolder
WATCH_DIR="$CRASH_REPORTS_DIR/tests"

# ── Colours (bash-3.2-compatible) ───────────────────────────────────────────
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[0;33m'
BLUE=$'\033[0;34m'
DIM=$'\033[2m'
NC=$'\033[0m'

step()  { printf "%s[%s]%s %s\n" "$BLUE" "$1" "$NC" "$2"; }
ok()    { printf "  %sPASS%s  %s\n" "$GREEN" "$NC" "$1"; }
fail()  { printf "  %sFAIL%s  %s\n" "$RED" "$NC" "$1"; exit 1; }
info()  { printf "  %s%s%s\n" "$DIM" "$1" "$NC"; }

echo
printf "%s═══════════════════════════════════════════════════════════════════%s\n" "$BLUE" "$NC"
printf "%s  Crash Report Capture Test%s\n" "$BLUE" "$NC"
printf "%s═══════════════════════════════════════════════════════════════════%s\n" "$BLUE" "$NC"
echo
info "Target service:        $TARGET_SERVICE"
info "Recovery-agent URL:    $RECOVERY_AGENT_URL"
info "Crash reports dir:     $CRASH_REPORTS_DIR"
echo

# ── Step 1: Recovery-agent reachable ────────────────────────────────────────
step "1/6" "Checking recovery-agent reachability..."
HEALTH_BODY="$(curl -sf --max-time 5 "$RECOVERY_AGENT_URL/health" 2>/dev/null || echo "")"
if [[ -z "$HEALTH_BODY" ]]; then
  fail "recovery-agent unreachable at $RECOVERY_AGENT_URL — is the container running?"
fi
ok "recovery-agent up — $HEALTH_BODY"

# ── Step 2: Target service running (so logs exist to capture) ───────────────
step "2/6" "Verifying $TARGET_SERVICE container is running..."
if ! docker ps --filter "name=^${TARGET_SERVICE}$" --format "{{.Names}}" | grep -q "^${TARGET_SERVICE}$"; then
  fail "$TARGET_SERVICE container is not running. Start it: docker start $TARGET_SERVICE"
fi
LOG_LINES="$(docker logs --tail 5 "$TARGET_SERVICE" 2>&1 | wc -l | tr -d ' ')"
ok "$TARGET_SERVICE running with ~$LOG_LINES recent log lines available"

# ── Step 3: Snapshot existing crash reports ─────────────────────────────────
step "3/6" "Snapshotting existing test reports in $WATCH_DIR ..."
mkdir -p "$WATCH_DIR"
BEFORE_COUNT="$(find "$WATCH_DIR" -maxdepth 1 -type f -name "${TARGET_SERVICE}_*.txt" 2>/dev/null | wc -l | tr -d ' ')"
ok "existing test reports for $TARGET_SERVICE: $BEFORE_COUNT"

# ── Step 4: Trigger enable_fallback at CRITICAL ─────────────────────────────
step "4/6" "Triggering enable_fallback (severity=CRITICAL, failure_count=5)..."
PAYLOAD=$(cat <<EOF
{
  "action": "enable_fallback",
  "target_service": "$TARGET_SERVICE",
  "reason": "[TEST] Crash report capture test (manual trigger)",
  "severity": "CRITICAL",
  "failure_count": 5,
  "escalation_reason": "5 crashes in 10min — CRITICALLY UNSTABLE",
  "recovery_strategy": "enable_fallback"
}
EOF
)

HTTP_RESPONSE="$(curl -sS -w "\nHTTP_CODE:%{http_code}" -X POST "$RECOVERY_AGENT_URL/action" \
  -H "Content-Type: application/json" \
  -H "X-Recovery-Token: $RECOVERY_TOKEN" \
  -d "$PAYLOAD" 2>&1)"

HTTP_CODE="$(echo "$HTTP_RESPONSE" | grep '^HTTP_CODE:' | cut -d: -f2)"
RESPONSE_BODY="$(echo "$HTTP_RESPONSE" | grep -v '^HTTP_CODE:')"

if [[ "$HTTP_CODE" != "200" ]]; then
  fail "recovery-agent returned HTTP $HTTP_CODE — body: $RESPONSE_BODY"
fi
ok "recovery-agent returned 200"
info "$RESPONSE_BODY"

# ── Step 5: Verify a new crash report appeared in tests/ subdir ─────────────
step "5/6" "Verifying new crash report was written to tests/ subdir..."
sleep 1   # give the recovery-agent a moment to flush the file
AFTER_COUNT="$(find "$WATCH_DIR" -maxdepth 1 -type f -name "${TARGET_SERVICE}_*.txt" 2>/dev/null | wc -l | tr -d ' ')"

if [[ "$AFTER_COUNT" -le "$BEFORE_COUNT" ]]; then
  fail "no new test report appeared in $WATCH_DIR (before=$BEFORE_COUNT after=$AFTER_COUNT). Check recovery-agent logs: docker logs recovery-agent | tail -30"
fi

# macOS uses 'stat -f', Linux uses 'find -printf'. Try Linux first, fall back to macOS.
LATEST="$(find "$WATCH_DIR" -maxdepth 1 -type f -name "${TARGET_SERVICE}_*.txt" -printf '%T@ %p\n' 2>/dev/null \
          | sort -rn | head -1 | cut -d' ' -f2-)"

if [[ -z "$LATEST" ]] || [[ ! -f "$LATEST" ]]; then
  LATEST="$(find "$WATCH_DIR" -maxdepth 1 -type f -name "${TARGET_SERVICE}_*.txt" -exec stat -f '%m %N' {} \; 2>/dev/null \
            | sort -rn | head -1 | cut -d' ' -f2-)"
fi

if [[ -z "$LATEST" ]] || [[ ! -f "$LATEST" ]]; then
  fail "could not locate the newly-written crash report"
fi

REPORT_SIZE="$(wc -c < "$LATEST" | tr -d ' ')"
ok "new crash report: $(basename "$LATEST") (${REPORT_SIZE} bytes)"

# Display the captured report so the developer can see what was saved
echo
printf "%s── crash report contents ───────────────────────────────────────────%s\n" "$YELLOW" "$NC"
cat "$LATEST"
printf "%s────────────────────────────────────────────────────────────────────%s\n" "$YELLOW" "$NC"

# ── Step 6: Restart the stopped service to leave the system healthy ─────────
step "6/6" "Cleanup — restarting $TARGET_SERVICE..."
docker start "$TARGET_SERVICE" >/dev/null 2>&1 || true
# Poll until target is healthy or we give up
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf --max-time 2 "$TARGET_HEALTH_URL" >/dev/null 2>&1; then
    ok "$TARGET_SERVICE healthy again"
    break
  fi
  sleep 1
done

echo
printf "%s═══════════════════════════════════════════════════════════════════%s\n" "$GREEN" "$NC"
printf "%s  PASS — crash report captured at:%s\n" "$GREEN" "$NC"
printf "         %s\n" "$LATEST"
printf "%s═══════════════════════════════════════════════════════════════════%s\n" "$GREEN" "$NC"
echo
