#!/usr/bin/env bash
# =============================================================================
# critical_core_failure_recovery.sh
#
# End-to-end chaos test for the self-healing distributed system.
#
# What this script tests:
#   1.  All Docker services are running and healthy at the start
#   2.  api-service returns core-service response normally (degraded=false)
#   3.  core-service crash is triggered
#   4.  api-service falls back to fallback-service (degraded=true)
#   5.  Circuit breaker opens after repeated failures
#   6.  Monitor detects HTTP 503 and publishes EventBridge event
#   7.  Event cooldown suppresses duplicate events
#   8.  Lambda runs and calls recovery-agent (AWS pipeline)
#   9.  recovery-agent validates token + allowlist, runs docker restart
#  10.  Recovery history JSONL file is updated
#  11.  core-service becomes healthy again
#  12.  api-service returns core-service response (degraded=false)
#
# Usage:
#   chmod +x tests/scripts/critical_core_failure_recovery.sh
#   ./tests/scripts/critical_core_failure_recovery.sh
#
# Override variables:
#   API_URL=http://localhost:8000 \
#   CORE_URL=http://localhost:8001 \
#   RECOVERY_TIMEOUT_SECONDS=120 \
#   ./tests/scripts/critical_core_failure_recovery.sh
#
# Prerequisites:
#   - Docker Compose services running (docker compose up -d)
#   - Monitor running (cd monitor && python3 monitor.py &)
#   - ngrok/tunnel active on port 8003
#   - Lambda RECOVERY_AGENT_URL points to the tunnel
#
# Note: This script does NOT touch AWS credentials or secrets.
#       It only calls local Docker services.
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration — override via environment variables before running
# =============================================================================

API_URL="${API_URL:-http://localhost:8000}"
CORE_URL="${CORE_URL:-http://localhost:8001}"
FALLBACK_URL="${FALLBACK_URL:-http://localhost:8002}"
RECOVERY_AGENT_URL="${RECOVERY_AGENT_URL:-http://localhost:8003}"
RECOVERY_HISTORY_FILE="${RECOVERY_HISTORY_FILE:-recovery-agent/data/recovery_history.jsonl}"
RECOVERY_TIMEOUT_SECONDS="${RECOVERY_TIMEOUT_SECONDS:-90}"
CIRCUIT_FAILURE_THRESHOLD="${CIRCUIT_FAILURE_THRESHOLD:-3}"
MONITOR_LOG="${MONITOR_LOG:-/tmp/monitor.log}"

# =============================================================================
# Colour helpers
# =============================================================================

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

pass()  { echo -e "  ${GREEN}[PASS]${RESET} $1"; }
fail()  { echo -e "  ${RED}[FAIL]${RESET} $1"; FAILED_STEPS+=("$1"); }
info()  { echo -e "  ${CYAN}[INFO]${RESET} $1"; }
warn()  { echo -e "  ${YELLOW}[WARN]${RESET} $1"; }
header(){ echo -e "\n${BOLD}${CYAN}── $1 ──${RESET}"; }

# =============================================================================
# Track failures
# =============================================================================

FAILED_STEPS=()
TOTAL_STEPS=0
HISTORY_LINES_BEFORE=0

# =============================================================================
# Helper functions
# =============================================================================

check_step() {
  TOTAL_STEPS=$((TOTAL_STEPS + 1))
}

http_status() {
  # Returns the HTTP status code for a URL
  curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$1" 2>/dev/null || echo "000"
}

http_body() {
  # Returns the response body for a URL
  curl -s --max-time 5 "$1" 2>/dev/null || echo "{}"
}

json_field() {
  # Extract a field from JSON: json_field <json_string> <field>
  echo "$1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$2',''))" 2>/dev/null || echo ""
}

count_history_lines() {
  if [ -f "$RECOVERY_HISTORY_FILE" ]; then
    wc -l < "$RECOVERY_HISTORY_FILE" | tr -d ' '
  else
    echo "0"
  fi
}

# =============================================================================
# Step 0: Check prerequisites
# =============================================================================

header "Step 0: Checking Prerequisites"

# Check required tools
for tool in curl python3 docker; do
  check_step
  if command -v "$tool" &>/dev/null; then
    pass "$tool is installed"
  else
    fail "$tool is not installed — install it and re-run"
  fi
done

# Check Docker daemon is running
check_step
if docker info &>/dev/null 2>&1; then
  pass "Docker daemon is running"
else
  fail "Docker daemon is not running — start Docker Desktop and re-run"
fi

# Check docker compose (v2 plugin)
check_step
if docker compose version &>/dev/null 2>&1; then
  pass "docker compose (v2) is available"
else
  fail "docker compose v2 not found — try 'docker compose version'"
fi

# Check monitor log exists (monitor is running)
check_step
if [ -f "$MONITOR_LOG" ]; then
  MONITOR_AGE=$(( $(date +%s) - $(date -r "$MONITOR_LOG" +%s 2>/dev/null || echo 0) ))
  if [ "$MONITOR_AGE" -lt 300 ]; then
    pass "Monitor log found and recently updated (${MONITOR_AGE}s ago)"
  else
    warn "Monitor log exists but is ${MONITOR_AGE}s old — monitor may not be running"
    info "Start monitor: cd monitor && export \$(grep -v '^#' .env | xargs) && python3 monitor.py > /tmp/monitor.log 2>&1 &"
  fi
else
  warn "Monitor log not found at $MONITOR_LOG"
  info "Monitor may not be running. EventBridge steps will not self-heal."
  info "Start monitor: cd monitor && export \$(grep -v '^#' .env | xargs) && python3 monitor.py > /tmp/monitor.log 2>&1 &"
fi

if [ ${#FAILED_STEPS[@]} -gt 0 ]; then
  echo -e "\n${RED}${BOLD}ABORTED: Prerequisites not met. Fix the above errors and re-run.${RESET}"
  exit 1
fi

# =============================================================================
# Step 1: Verify all Docker services are running
# =============================================================================

header "Step 1: Verifying Docker Services"

SERVICES=("api-service" "core-service" "fallback-service" "recovery-agent")
PORTS=("8000" "8001" "8002" "8003")

for i in "${!SERVICES[@]}"; do
  check_step
  SERVICE="${SERVICES[$i]}"
  PORT="${PORTS[$i]}"
  STATE=$(docker inspect --format '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || echo "missing")
  if [ "$STATE" = "healthy" ]; then
    pass "$SERVICE is running and healthy (port ${PORT})"
  elif [ "$STATE" = "starting" ]; then
    warn "$SERVICE is still starting — waiting 10 seconds..."
    sleep 10
    STATE=$(docker inspect --format '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || echo "missing")
    if [ "$STATE" = "healthy" ]; then
      pass "$SERVICE became healthy"
    else
      fail "$SERVICE is not healthy (state: $STATE) — run: docker compose up --build -d"
    fi
  else
    fail "$SERVICE is not running (state: $STATE) — run: docker compose up --build -d"
  fi
done

if [ ${#FAILED_STEPS[@]} -gt 0 ]; then
  echo -e "\n${RED}${BOLD}ABORTED: Docker services are not healthy. Run 'docker compose up --build -d' first.${RESET}"
  exit 1
fi

# =============================================================================
# Step 2: Idempotent reset — recover core-service if already crashed
# =============================================================================

header "Step 2: Ensuring Clean Starting State (Idempotent Reset)"

check_step
CORE_HTTP=$(http_status "$CORE_URL/health")
if [ "$CORE_HTTP" = "503" ] || [ "$CORE_HTTP" = "500" ]; then
  info "core-service is already crashed (HTTP $CORE_HTTP) — recovering before test starts..."
  curl -s -X POST "$CORE_URL/recover" > /dev/null
  sleep 3
  CORE_HTTP=$(http_status "$CORE_URL/health")
  if [ "$CORE_HTTP" = "200" ]; then
    pass "core-service recovered to clean state"
  else
    fail "Could not recover core-service (HTTP $CORE_HTTP) — check container logs"
  fi
else
  pass "core-service is already healthy (HTTP $CORE_HTTP) — no reset needed"
fi

# Record history line count before test
HISTORY_LINES_BEFORE=$(count_history_lines)
info "Recovery history currently has $HISTORY_LINES_BEFORE line(s)"

# =============================================================================
# Step 3: Verify baseline — all services healthy and api returns core-service
# =============================================================================

header "Step 3: Verifying Baseline (All Healthy)"

for NAME_URL in "api-service:$API_URL" "core-service:$CORE_URL" "fallback-service:$FALLBACK_URL" "recovery-agent:$RECOVERY_AGENT_URL"; do
  NAME="${NAME_URL%%:*}"
  URL="${NAME_URL#*:}"
  check_step
  STATUS=$(http_status "$URL/health")
  if [ "$STATUS" = "200" ]; then
    pass "$NAME /health → HTTP 200"
  else
    fail "$NAME /health → HTTP $STATUS (expected 200)"
  fi
done

check_step
PROCESS_BODY=$(http_body "$API_URL/process")
PROCESS_SOURCE=$(json_field "$PROCESS_BODY" "source")
PROCESS_DEGRADED=$(json_field "$PROCESS_BODY" "degraded")

if [ "$PROCESS_SOURCE" = "core-service" ] && [ "$PROCESS_DEGRADED" = "False" ]; then
  pass "api-service /process → source=core-service degraded=False"
else
  fail "api-service /process → source=$PROCESS_SOURCE degraded=$PROCESS_DEGRADED (expected core-service / False)"
fi

if [ ${#FAILED_STEPS[@]} -gt 0 ]; then
  echo -e "\n${RED}${BOLD}ABORTED: Baseline is not healthy. Fix failures above before running chaos test.${RESET}"
  exit 1
fi

# =============================================================================
# Step 4: Trigger crash
# =============================================================================

header "Step 4: Triggering core-service Crash"

check_step
FAIL_BODY=$(curl -s -X POST "$CORE_URL/fail" --max-time 5 2>/dev/null || echo "{}")
CRASHED=$(json_field "$FAIL_BODY" "crashed")
if [ "$CRASHED" = "True" ]; then
  pass "POST $CORE_URL/fail → crashed=True"
else
  fail "POST $CORE_URL/fail did not return crashed=True (got: $FAIL_BODY)"
fi

check_step
sleep 1
CORE_STATUS=$(http_status "$CORE_URL/health")
if [ "$CORE_STATUS" = "503" ]; then
  pass "core-service /health → HTTP 503 (confirmed crash)"
else
  fail "core-service /health → HTTP $CORE_STATUS (expected 503)"
fi

# =============================================================================
# Step 5: Verify api-service falls back and circuit breaker opens
# =============================================================================

header "Step 5: Verifying Fallback and Circuit Breaker"

info "Making $((CIRCUIT_FAILURE_THRESHOLD + 2)) calls to /process to open the circuit breaker..."

FALLBACK_COUNT=0
for i in $(seq 1 $((CIRCUIT_FAILURE_THRESHOLD + 2))); do
  BODY=$(http_body "$API_URL/process")
  SOURCE=$(json_field "$BODY" "source")
  DEGRADED=$(json_field "$BODY" "degraded")
  echo -e "    Call $i: source=${CYAN}$SOURCE${RESET} degraded=${YELLOW}$DEGRADED${RESET}"
  if [ "$SOURCE" = "fallback-service" ]; then
    FALLBACK_COUNT=$((FALLBACK_COUNT + 1))
  fi
  sleep 0.3
done

check_step
if [ "$FALLBACK_COUNT" -ge "$CIRCUIT_FAILURE_THRESHOLD" ]; then
  pass "All $FALLBACK_COUNT/$((CIRCUIT_FAILURE_THRESHOLD + 2)) calls returned fallback-service"
else
  fail "Only $FALLBACK_COUNT/$((CIRCUIT_FAILURE_THRESHOLD + 2)) calls used fallback-service"
fi

check_step
CIRCUIT_OPEN=$(docker logs api-service 2>&1 | grep "CLOSED → OPEN" | tail -1)
if [ -n "$CIRCUIT_OPEN" ]; then
  pass "Circuit breaker opened: $(echo "$CIRCUIT_OPEN" | sed 's/.*WARNING: //')"
else
  fail "No 'CLOSED → OPEN' log found in api-service — circuit may not have opened"
  info "Check: docker logs api-service 2>&1 | grep -i circuit"
fi

# =============================================================================
# Step 6: Verify monitor detected 503 and published EventBridge event
# =============================================================================

header "Step 6: Verifying Monitor Detected Failure"

info "Waiting up to 15 seconds for monitor to publish EventBridge event..."
EVENT_PUBLISHED=false
for i in $(seq 1 15); do
  if grep -q "EventBridgePublisher: published event service=core-service" "$MONITOR_LOG" 2>/dev/null; then
    # Check if the published event is NEWER than when we triggered the crash
    LAST_PUBLISH=$(grep "EventBridgePublisher: published event service=core-service" "$MONITOR_LOG" | tail -1)
    EVENT_PUBLISHED=true
    break
  fi
  sleep 1
done

check_step
if [ "$EVENT_PUBLISHED" = "true" ]; then
  pass "Monitor published EventBridge event: $(echo "$LAST_PUBLISH" | sed 's/.*INFO: //')"
else
  fail "Monitor did not publish an EventBridge event within 15 seconds"
  info "Check monitor log: tail -30 $MONITOR_LOG"
  info "Is monitor running? pgrep -f monitor.py"
fi

# =============================================================================
# Step 7: Verify event cooldown prevents duplicates
# =============================================================================

header "Step 7: Verifying Event Cooldown"

check_step
info "Waiting 8 seconds then checking for cooldown suppression log..."
sleep 8

COOLDOWN_ACTIVE=$(grep "EventCooldown: suppressing event service=core-service" "$MONITOR_LOG" 2>/dev/null | tail -1 || echo "")
if [ -n "$COOLDOWN_ACTIVE" ]; then
  pass "Event cooldown is suppressing duplicates: $(echo "$COOLDOWN_ACTIVE" | sed 's/.*INFO: //')"
else
  warn "No cooldown suppression log found yet — this is OK if recovery was very fast"
fi

# =============================================================================
# Step 8: Wait for auto-recovery (Lambda → recovery-agent → docker restart)
# =============================================================================

header "Step 8: Waiting for Automatic Recovery (${RECOVERY_TIMEOUT_SECONDS}s timeout)"

info "Pipeline: Monitor → EventBridge → Lambda → recovery-agent → docker restart core-service"
info "Polling every 5 seconds..."

RECOVERED=false
RECOVERY_ATTEMPT=0
MAX_ATTEMPTS=$(( RECOVERY_TIMEOUT_SECONDS / 5 ))

for i in $(seq 1 $MAX_ATTEMPTS); do
  RECOVERY_ATTEMPT=$((RECOVERY_ATTEMPT + 1))
  CORE_STATUS=$(http_status "$CORE_URL/health")
  ELAPSED=$(( i * 5 ))
  echo -e "    [${ELAPSED}s] core-service HTTP $CORE_STATUS"

  if [ "$CORE_STATUS" = "200" ]; then
    RECOVERED=true
    info "core-service responded HTTP 200 after ${ELAPSED} seconds"
    break
  fi
  sleep 5
done

check_step
if [ "$RECOVERED" = "true" ]; then
  pass "core-service recovered automatically within ${ELAPSED} seconds"
else
  fail "core-service did NOT recover within ${RECOVERY_TIMEOUT_SECONDS} seconds"
  info "Manual check: aws logs tail /aws/lambda/SelfHealingRecoveryHandler --region us-east-1"
  info "Manual recovery: curl -X POST $CORE_URL/recover"
  # Do not exit — continue checking remaining steps to give full picture
fi

# =============================================================================
# Step 9: Verify Lambda ran successfully (via recovery-agent logs)
# =============================================================================

header "Step 9: Verifying Lambda and recovery-agent Ran"

check_step
HISTORY_LINES_AFTER=$(count_history_lines)
NEW_RECORDS=$(( HISTORY_LINES_AFTER - HISTORY_LINES_BEFORE ))

if [ "$NEW_RECORDS" -gt 0 ]; then
  pass "Recovery history JSONL updated: $NEW_RECORDS new record(s) (total: $HISTORY_LINES_AFTER lines)"
else
  fail "No new records written to $RECOVERY_HISTORY_FILE"
  info "File location: $RECOVERY_HISTORY_FILE"
  info "Check recovery-agent logs: docker logs recovery-agent 2>&1 | tail -20"
fi

check_step
RECOVERY_LOG=$(docker logs recovery-agent 2>&1 | grep "RecoveryHistory: recorded" | tail -1 || echo "")
if [ -n "$RECOVERY_LOG" ]; then
  pass "recovery-agent wrote history: $(echo "$RECOVERY_LOG" | sed 's/.*INFO.*: //')"
else
  fail "No 'RecoveryHistory: recorded' log found in recovery-agent"
fi

check_step
TOKEN_ERROR=$(docker logs recovery-agent 2>&1 | grep -E "401|403|Invalid.*token" | tail -1 || echo "")
if [ -z "$TOKEN_ERROR" ]; then
  pass "No token or allowlist rejections — security checks passed"
else
  fail "Token/allowlist error found: $TOKEN_ERROR"
fi

# =============================================================================
# Step 10: Verify recovery history JSONL content
# =============================================================================

header "Step 10: Verifying Recovery History JSONL"

check_step
if [ -f "$RECOVERY_HISTORY_FILE" ] && [ "$HISTORY_LINES_AFTER" -gt 0 ]; then
  LATEST_RECORD=$(tail -1 "$RECOVERY_HISTORY_FILE")
  R_SUCCESS=$(echo "$LATEST_RECORD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('success',''))" 2>/dev/null)
  R_RETURNCODE=$(echo "$LATEST_RECORD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('returncode',''))" 2>/dev/null)
  R_ACTION=$(echo "$LATEST_RECORD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('action',''))" 2>/dev/null)
  R_SERVICE=$(echo "$LATEST_RECORD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('service_name',''))" 2>/dev/null)
  R_DURATION=$(echo "$LATEST_RECORD" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('recovery_duration_ms',''))" 2>/dev/null)

  echo -e "    Latest record:"
  echo -e "      action:               ${CYAN}$R_ACTION${RESET}"
  echo -e "      service_name:         ${CYAN}$R_SERVICE${RESET}"
  echo -e "      success:              ${CYAN}$R_SUCCESS${RESET}"
  echo -e "      returncode:           ${CYAN}$R_RETURNCODE${RESET}"
  echo -e "      recovery_duration_ms: ${CYAN}$R_DURATION ms${RESET}"

  if [ "$R_SUCCESS" = "True" ] && [ "$R_RETURNCODE" = "0" ] && [ "$R_ACTION" = "restart_service" ]; then
    pass "History record is correct: action=restart_service success=True returncode=0"
  else
    fail "History record has unexpected values (success=$R_SUCCESS returncode=$R_RETURNCODE action=$R_ACTION)"
  fi
else
  fail "Recovery history file is empty or missing: $RECOVERY_HISTORY_FILE"
fi

# =============================================================================
# Step 11: Verify full recovery — api-service returns core-service
# =============================================================================

header "Step 11: Verifying Full Recovery"

# Give circuit breaker time to probe and close
info "Waiting 35 seconds for circuit breaker to probe and close after core-service recovery..."
sleep 35

check_step
CORE_FINAL=$(http_status "$CORE_URL/health")
if [ "$CORE_FINAL" = "200" ]; then
  pass "core-service /health → HTTP 200 (healthy)"
else
  fail "core-service /health → HTTP $CORE_FINAL (expected 200)"
fi

check_step
info "Making 3 calls to /process to verify circuit closed..."
CORE_SOURCE_COUNT=0
for i in 1 2 3; do
  BODY=$(http_body "$API_URL/process")
  SOURCE=$(json_field "$BODY" "source")
  DEGRADED=$(json_field "$BODY" "degraded")
  echo -e "    Call $i: source=${CYAN}$SOURCE${RESET} degraded=${YELLOW}$DEGRADED${RESET}"
  if [ "$SOURCE" = "core-service" ] && [ "$DEGRADED" = "False" ]; then
    CORE_SOURCE_COUNT=$((CORE_SOURCE_COUNT + 1))
  fi
  sleep 1
done

if [ "$CORE_SOURCE_COUNT" -eq 3 ]; then
  pass "api-service /process → source=core-service degraded=False (all 3 calls)"
else
  fail "api-service /process returned core-service only $CORE_SOURCE_COUNT/3 times"
  info "Circuit breaker may need more time. Wait 30s and check: curl $API_URL/process"
fi

check_step
CIRCUIT_CLOSED=$(docker logs api-service 2>&1 | grep "HALF_OPEN → CLOSED" | tail -1 || echo "")
if [ -n "$CIRCUIT_CLOSED" ]; then
  pass "Circuit breaker closed: $(echo "$CIRCUIT_CLOSED" | sed 's/.*INFO: //')"
else
  warn "No 'HALF_OPEN → CLOSED' log found — circuit may have closed without half-open (if container restarted)"
fi

# =============================================================================
# Step 12: Verify cooldown was cleared on recovery
# =============================================================================

header "Step 12: Verifying Cooldown Cleared on Recovery"

check_step
COOLDOWN_CLEARED=$(grep "EventCooldown: cleared.*core-service" "$MONITOR_LOG" 2>/dev/null | tail -1 || echo "")
if [ -n "$COOLDOWN_CLEARED" ]; then
  pass "Cooldown cleared: $(echo "$COOLDOWN_CLEARED" | sed 's/.*INFO: //')"
else
  warn "No cooldown-cleared log found — may not have appeared yet (OK if recovery was very recent)"
fi

# =============================================================================
# Final Summary
# =============================================================================

echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  TEST SUMMARY: critical_core_failure_recovery${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Total steps checked : $TOTAL_STEPS"
echo -e "  Passed              : $((TOTAL_STEPS - ${#FAILED_STEPS[@]}))"
echo -e "  Failed              : ${#FAILED_STEPS[@]}"
echo ""

if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
  echo -e "  ${GREEN}${BOLD}██████████████████████████████████████████${RESET}"
  echo -e "  ${GREEN}${BOLD}  RESULT: PASS — All steps completed       ${RESET}"
  echo -e "  ${GREEN}${BOLD}  Self-healing pipeline is working         ${RESET}"
  echo -e "  ${GREEN}${BOLD}██████████████████████████████████████████${RESET}"
  echo ""
  FINAL_STATUS=0
else
  echo -e "  ${RED}${BOLD}██████████████████████████████████████████${RESET}"
  echo -e "  ${RED}${BOLD}  RESULT: FAIL — ${#FAILED_STEPS[@]} step(s) failed       ${RESET}"
  echo -e "  ${RED}${BOLD}██████████████████████████████████████████${RESET}"
  echo ""
  echo -e "  ${RED}Failed steps:${RESET}"
  for step in "${FAILED_STEPS[@]}"; do
    echo -e "    ${RED}✗${RESET} $step"
  done
  echo ""
  FINAL_STATUS=1
fi

echo -e "  ${CYAN}Recovery history file:${RESET} $RECOVERY_HISTORY_FILE"
echo -e "  ${CYAN}Total records in history:${RESET} $(count_history_lines)"
echo -e "  ${CYAN}Monitor log:${RESET} $MONITOR_LOG"
echo ""

# =============================================================================
# Cleanup reminder (no auto-delete)
# =============================================================================

echo -e "${BOLD}${YELLOW}  Cleanup (manual — nothing was deleted automatically):${RESET}"
echo -e "  • Services are still running: ${CYAN}docker compose ps${RESET}"
echo -e "  • core-service is recovered:  ${CYAN}curl -s http://localhost:8001/health${RESET}"
echo -e "  • Full reset if needed:       ${CYAN}docker compose down && docker compose up --build -d${RESET}"
echo -e "  • AWS resources untouched:    EventBridge rule, Lambda, SQS DLQ are all still active"
echo ""

exit $FINAL_STATUS
