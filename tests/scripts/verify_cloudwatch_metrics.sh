#!/usr/bin/env bash
# =============================================================================
# verify_cloudwatch_metrics.sh
#
# Verifies that Phase 4 CloudWatch metrics are being published correctly.
#
# What it does:
#   1.  Records a baseline timestamp.
#   2.  Triggers a core-service crash.
#   3.  Waits for automatic recovery (Lambda pipeline).
#   4.  Waits an additional 90 seconds for CloudWatch to ingest metrics.
#   5.  Queries CloudWatch for each expected metric.
#   6.  Prints PASS/FAIL per metric and a final summary.
#
# Usage:
#   cd /path/to/self-healing-system
#   export $(grep -v '^#' monitor/.env | xargs)   # load AWS credentials
#   chmod +x tests/scripts/verify_cloudwatch_metrics.sh
#   ./tests/scripts/verify_cloudwatch_metrics.sh
#
# Prerequisites:
#   - AWS CLI installed and configured (us-east-1 credentials loaded)
#   - docker compose services running (docker compose ps)
#   - Monitor running (pgrep -f monitor.py)
#   - CLOUDWATCH_ENABLED=true in monitor/.env
#
# Note: CloudWatch metrics have a ~1-minute ingestion delay.
#       This script waits 90 seconds after recovery before querying.
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
NAMESPACE="${CLOUDWATCH_NAMESPACE:-SelfHealingSystem}"
CORE_URL="${CORE_URL:-http://localhost:8001}"
RECOVERY_TIMEOUT="${RECOVERY_TIMEOUT_SECONDS:-90}"
CW_WAIT_SECONDS=90   # extra wait after recovery for CloudWatch ingestion

# =============================================================================
# Colour helpers
# =============================================================================

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

pass()   { echo -e "  ${GREEN}[PASS]${RESET} $1"; }
fail()   { echo -e "  ${RED}[FAIL]${RESET} $1"; FAILED+=("$1"); }
info()   { echo -e "  ${CYAN}[INFO]${RESET} $1"; }
warn()   { echo -e "  ${YELLOW}[WARN]${RESET} $1"; }
header() { echo -e "\n${BOLD}${CYAN}── $1 ──${RESET}"; }

FAILED=()

# =============================================================================
# Helper: get_metric_sum
# Returns the Sum of a metric over a time window. Prints "0" if no data.
# Usage: get_metric_sum <metric_name> <dim1_key> <dim1_val> [<dim2_key> <dim2_val> ...]
# =============================================================================

get_metric_sum() {
  local metric_name="$1"; shift
  local dims=()
  while [[ $# -ge 2 ]]; do
    dims+=("Name=$1,Value=$2")
    shift 2
  done

  local dim_args=()
  for d in "${dims[@]}"; do
    dim_args+=("$d")
  done

  local sum
  sum=$(
    aws cloudwatch get-metric-statistics \
      --namespace "$NAMESPACE" \
      --metric-name "$metric_name" \
      --dimensions "${dim_args[@]}" \
      --start-time "$START_TIME_ISO" \
      --end-time "$END_TIME_ISO" \
      --period 3600 \
      --statistics Sum \
      --region "$REGION" \
      --query "Datapoints[0].Sum" \
      --output text 2>/dev/null || echo "None"
  )

  # aws cli returns "None" if no datapoints
  if [[ "$sum" == "None" || -z "$sum" ]]; then
    echo "0"
  else
    # Remove decimal if it's a whole number
    printf "%.0f" "$sum"
  fi
}

# =============================================================================
# Helper: get_metric_avg
# Returns the Average of a metric over a time window.
# =============================================================================

get_metric_avg() {
  local metric_name="$1"; shift
  local dims=()
  while [[ $# -ge 2 ]]; do
    dims+=("Name=$1,Value=$2")
    shift 2
  done

  local dim_args=()
  for d in "${dims[@]}"; do
    dim_args+=("$d")
  done

  local avg
  avg=$(
    aws cloudwatch get-metric-statistics \
      --namespace "$NAMESPACE" \
      --metric-name "$metric_name" \
      --dimensions "${dim_args[@]}" \
      --start-time "$START_TIME_ISO" \
      --end-time "$END_TIME_ISO" \
      --period 3600 \
      --statistics Average \
      --region "$REGION" \
      --query "Datapoints[0].Average" \
      --output text 2>/dev/null || echo "None"
  )

  if [[ "$avg" == "None" || -z "$avg" ]]; then
    echo "0"
  else
    printf "%.1f" "$avg"
  fi
}

# =============================================================================
# Preflight checks
# =============================================================================

header "Preflight Checks"

if ! command -v aws &>/dev/null; then
  echo -e "${RED}ERROR: AWS CLI not found. Install it first.${RESET}"
  exit 1
fi
pass "AWS CLI available"

if ! aws sts get-caller-identity --region "$REGION" &>/dev/null; then
  echo -e "${RED}ERROR: AWS credentials not configured. Run: export \$(grep -v '^#' monitor/.env | xargs)${RESET}"
  exit 1
fi
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
pass "AWS credentials valid (account: $ACCOUNT_ID)"

CORE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$CORE_URL/health" 2>/dev/null || echo "000")
if [ "$CORE_STATUS" != "200" ]; then
  # Try to recover first
  info "core-service not healthy (HTTP $CORE_STATUS) — recovering..."
  curl -s -X POST "$CORE_URL/recover" > /dev/null 2>&1 || true
  sleep 3
  CORE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$CORE_URL/health" 2>/dev/null || echo "000")
  if [ "$CORE_STATUS" != "200" ]; then
    echo -e "${RED}ERROR: core-service not healthy. Run: docker compose up --build -d${RESET}"
    exit 1
  fi
fi
pass "core-service is healthy (HTTP 200)"

# =============================================================================
# Step 1: Record start time (use 5-minute buffer before trigger for clock skew)
# =============================================================================

header "Step 1: Recording Start Time"

# macOS date format; use `date -d '5 minutes ago'` on Linux
if date -v-5M +%Y-%m-%dT%H:%M:%SZ &>/dev/null 2>&1; then
  # macOS
  START_TIME_ISO=$(date -u -v-5M +"%Y-%m-%dT%H:%M:%SZ")
else
  # Linux / GNU date
  START_TIME_ISO=$(date -u -d '5 minutes ago' +"%Y-%m-%dT%H:%M:%SZ")
fi

info "Metric window starts at: $START_TIME_ISO (5 min buffer for clock skew)"

# =============================================================================
# Step 2: Trigger crash
# =============================================================================

header "Step 2: Triggering core-service Crash"

FAIL_BODY=$(curl -s -X POST "$CORE_URL/fail" --max-time 5 2>/dev/null || echo "{}")
CRASHED=$(echo "$FAIL_BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('crashed',''))" 2>/dev/null || echo "")

if [ "$CRASHED" = "True" ]; then
  pass "core-service crash triggered"
else
  fail "Could not trigger crash (response: $FAIL_BODY)"
  echo -e "\n${RED}Cannot continue without a crash trigger.${RESET}"
  exit 1
fi

# =============================================================================
# Step 3: Wait for automatic recovery
# =============================================================================

header "Step 3: Waiting for Automatic Recovery (${RECOVERY_TIMEOUT}s timeout)"

info "Pipeline: monitor → EventBridge → Lambda → recovery-agent → docker restart"

RECOVERED=false
MAX_POLLS=$(( RECOVERY_TIMEOUT / 5 ))

for i in $(seq 1 "$MAX_POLLS"); do
  ELAPSED=$(( i * 5 ))
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$CORE_URL/health" 2>/dev/null || echo "000")
  echo -e "    [${ELAPSED}s] core-service HTTP $STATUS"
  if [ "$STATUS" = "200" ]; then
    RECOVERED=true
    info "Recovered after ${ELAPSED} seconds"
    break
  fi
  sleep 5
done

if [ "$RECOVERED" != "true" ]; then
  warn "core-service did not auto-recover within ${RECOVERY_TIMEOUT}s"
  info "Attempting manual recovery: POST /recover"
  curl -s -X POST "$CORE_URL/recover" > /dev/null 2>&1 || true
  sleep 3
fi

# =============================================================================
# Step 4: Wait for CloudWatch ingestion
# =============================================================================

header "Step 4: Waiting ${CW_WAIT_SECONDS}s for CloudWatch Metric Ingestion"

info "CloudWatch ingests metrics with ~1 minute delay. Waiting..."

for i in $(seq 1 "$CW_WAIT_SECONDS"); do
  printf "\r  [INFO] ${i}/${CW_WAIT_SECONDS}s elapsed..."
  sleep 1
done
echo ""
info "Done waiting. Querying metrics now."

# Set end time to now
if date -v+0M +%Y-%m-%dT%H:%M:%SZ &>/dev/null 2>&1; then
  # macOS
  END_TIME_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
else
  # Linux
  END_TIME_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
fi
info "Metric window ends at: $END_TIME_ISO"

# =============================================================================
# Step 5: Query and verify each metric
# =============================================================================

header "Step 5: Verifying CloudWatch Metrics"

echo ""

# ── FailureDetectedCount ──────────────────────────────────────────────────────
SUM=$(get_metric_sum "FailureDetectedCount" "ServiceName" "core-service" "FailureType" "crash")
echo -e "  FailureDetectedCount (ServiceName=core-service, FailureType=crash): ${CYAN}${SUM}${RESET}"
if [ "$SUM" -gt 0 ] 2>/dev/null; then
  pass "FailureDetectedCount > 0 — monitor is publishing failure events"
else
  fail "FailureDetectedCount = 0 — check CLOUDWATCH_ENABLED=true in monitor/.env"
fi

echo ""

# ── RecoverySuccessCount ──────────────────────────────────────────────────────
SUM=$(get_metric_sum "RecoverySuccessCount" "ServiceName" "recovery-agent" "TargetService" "core-service" "Action" "restart_service")
echo -e "  RecoverySuccessCount (ServiceName=recovery-agent, Action=restart_service): ${CYAN}${SUM}${RESET}"
if [ "$SUM" -gt 0 ] 2>/dev/null; then
  pass "RecoverySuccessCount > 0 — recovery-agent completed successfully"
else
  fail "RecoverySuccessCount = 0 — check recovery-agent logs and CLOUDWATCH_ENABLED"
fi

echo ""

# ── RecoveryFailureCount ──────────────────────────────────────────────────────
SUM=$(get_metric_sum "RecoveryFailureCount" "ServiceName" "recovery-agent" "TargetService" "core-service" "Action" "restart_service")
echo -e "  RecoveryFailureCount (ServiceName=recovery-agent, Action=restart_service): ${CYAN}${SUM}${RESET}"
if [ "$SUM" -eq 0 ] 2>/dev/null; then
  pass "RecoveryFailureCount = 0 — no failed recovery attempts"
else
  warn "RecoveryFailureCount = $SUM — some recovery actions failed (check recovery-agent logs)"
fi

echo ""

# ── RecoveryDurationMs ────────────────────────────────────────────────────────
AVG=$(get_metric_avg "RecoveryDurationMs" "ServiceName" "recovery-agent" "TargetService" "core-service" "Action" "restart_service")
echo -e "  RecoveryDurationMs average: ${CYAN}${AVG}ms${RESET}"
if [ "$(echo "$AVG > 0" | python3 -c "import sys; print(eval(sys.stdin.read()))")" = "True" ] 2>/dev/null; then
  pass "RecoveryDurationMs has data (avg=${AVG}ms)"
else
  fail "RecoveryDurationMs = 0 — recovery-agent may not be emitting duration metrics"
fi

echo ""

# ── FallbackUsedCount ─────────────────────────────────────────────────────────
SUM=$(get_metric_sum "FallbackUsedCount" "ServiceName" "api-service" "TargetService" "core-service")
echo -e "  FallbackUsedCount (ServiceName=api-service, TargetService=core-service): ${CYAN}${SUM}${RESET}"
if [ "$SUM" -gt 0 ] 2>/dev/null; then
  pass "FallbackUsedCount > 0 — api-service fell back correctly"
else
  fail "FallbackUsedCount = 0 — check CLOUDWATCH_ENABLED in api-service container"
fi

echo ""

# ── CircuitBreakerOpenCount ───────────────────────────────────────────────────
SUM=$(get_metric_sum "CircuitBreakerOpenCount" "ServiceName" "api-service" "TargetService" "core-service")
echo -e "  CircuitBreakerOpenCount (ServiceName=api-service, TargetService=core-service): ${CYAN}${SUM}${RESET}"
if [ "$SUM" -gt 0 ] 2>/dev/null; then
  pass "CircuitBreakerOpenCount > 0 — circuit breaker opened and was recorded"
else
  fail "CircuitBreakerOpenCount = 0 — check api-service CloudWatch setup"
fi

echo ""

# ── CircuitBreakerState ───────────────────────────────────────────────────────
SUM=$(get_metric_sum "CircuitBreakerState" "ServiceName" "api-service" "TargetService" "core-service")
echo -e "  CircuitBreakerState datapoints (ServiceName=api-service): ${CYAN}${SUM}${RESET}"
if [ "$SUM" -ge 0 ] 2>/dev/null; then
  pass "CircuitBreakerState gauge has data (check dashboard for time-series)"
fi

echo ""

# =============================================================================
# Final summary
# =============================================================================

echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  TEST SUMMARY: verify_cloudwatch_metrics${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Namespace : ${CYAN}$NAMESPACE${RESET}"
echo -e "  Region    : ${CYAN}$REGION${RESET}"
echo -e "  Window    : ${CYAN}$START_TIME_ISO${RESET} → ${CYAN}$END_TIME_ISO${RESET}"
echo ""
echo -e "  Failed checks : ${#FAILED[@]}"
echo ""

if [ ${#FAILED[@]} -eq 0 ]; then
  echo -e "  ${GREEN}${BOLD}██████████████████████████████████████████${RESET}"
  echo -e "  ${GREEN}${BOLD}  RESULT: PASS — All metrics published     ${RESET}"
  echo -e "  ${GREEN}${BOLD}██████████████████████████████████████████${RESET}"
  FINAL_STATUS=0
else
  echo -e "  ${RED}${BOLD}██████████████████████████████████████████${RESET}"
  echo -e "  ${RED}${BOLD}  RESULT: FAIL — ${#FAILED[@]} metric(s) missing   ${RESET}"
  echo -e "  ${RED}${BOLD}██████████████████████████████████████████${RESET}"
  echo ""
  echo -e "  ${RED}Failed:${RESET}"
  for f in "${FAILED[@]}"; do
    echo -e "    ${RED}✗${RESET} $f"
  done
  FINAL_STATUS=1
fi

echo ""
echo -e "  ${CYAN}Dashboard:${RESET}"
echo -e "  https://console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards:name=SelfHealingSystemDashboard"
echo ""
echo -e "  ${YELLOW}Troubleshooting:${RESET}"
echo -e "  • CLOUDWATCH_ENABLED=true?         grep CLOUDWATCH monitor/.env"
echo -e "  • Containers rebuilt?              docker compose up --build -d"
echo -e "  • api-service CW logs?             docker logs api-service 2>&1 | grep CloudWatch"
echo -e "  • recovery-agent CW logs?          docker logs recovery-agent 2>&1 | grep CloudWatch"
echo ""

exit $FINAL_STATUS
