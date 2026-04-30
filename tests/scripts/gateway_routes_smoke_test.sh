#!/usr/bin/env bash
# =============================================================================
# gateway_routes_smoke_test.sh
#
# Phase 8 smoke test — verifies the config-driven gateway and all routes.
#
# What this tests:
#   1.  All 6 services are running and healthy
#   2.  GET /core-service    → source=core-service     degraded=false
#   3.  GET /payment-service → source=payment-service  degraded=false
#   4.  GET /movie-service   → source=movie-service    degraded=false
#   5.  GET /unknown-service → HTTP 404
#   6.  Circuit breaker isolation:
#       - Crash movie-service
#       - GET /movie-service  → fallback-service degraded=true  (fallback strategy)
#       - GET /payment-service → still works (independent circuit)
#       - Recover movie-service
#   7.  Payment escalate strategy: crash payment-service → HTTP 503 (no fallback)
#       (recovered afterwards)
#
# Usage:
#   chmod +x tests/scripts/gateway_routes_smoke_test.sh
#   ./tests/scripts/gateway_routes_smoke_test.sh
#
# Override defaults:
#   API_URL=http://54.224.134.71:8000 ./tests/scripts/gateway_routes_smoke_test.sh
#
# Prerequisites:
#   - docker compose up --build -d (all 6 containers healthy)
#   - No AWS or tunnel needed — this is a pure local/HTTP test
# =============================================================================

set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
CORE_URL="${CORE_URL:-http://localhost:8001}"
FALLBACK_URL="${FALLBACK_URL:-http://localhost:8002}"
RECOVERY_AGENT_URL="${RECOVERY_AGENT_URL:-http://localhost:8003}"
PAYMENT_URL="${PAYMENT_URL:-http://localhost:8010}"
MOVIE_URL="${MOVIE_URL:-http://localhost:8020}"

# ── colour helpers ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

pass()   { echo -e "  ${GREEN}[PASS]${RESET} $1"; }
fail()   { echo -e "  ${RED}[FAIL]${RESET} $1"; FAILED+=("$1"); }
info()   { echo -e "  ${CYAN}[INFO]${RESET} $1"; }
warn()   { echo -e "  ${YELLOW}[WARN]${RESET} $1"; }
header() { echo -e "\n${BOLD}${CYAN}── $1 ──${RESET}"; }

FAILED=()
TOTAL=0
check() { TOTAL=$((TOTAL + 1)); }

# ── helpers ────────────────────────────────────────────────────────────────────

http_status() { curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$1" 2>/dev/null || echo "000"; }
http_body()   { curl -s --max-time 5 "$1" 2>/dev/null || echo "{}"; }
json_field()  { echo "$1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$2',''))" 2>/dev/null || echo ""; }

http_body_post() {
  curl -s -X POST --max-time 5 "$1" 2>/dev/null || echo "{}"
}

# ── Step 1: Health checks — all 6 services ────────────────────────────────────

header "Step 1: Health Checks — All 6 Services"

declare -A SVCS=(
  [api-service]="$API_URL"
  [core-service]="$CORE_URL"
  [fallback-service]="$FALLBACK_URL"
  [recovery-agent]="$RECOVERY_AGENT_URL"
  [payment-service]="$PAYMENT_URL"
  [movie-service]="$MOVIE_URL"
)

for NAME in api-service core-service fallback-service recovery-agent payment-service movie-service; do
  URL="${SVCS[$NAME]}"
  check
  STATUS=$(http_status "$URL/health")
  if [ "$STATUS" = "200" ]; then
    BODY=$(http_body "$URL/health")
    SVC_STATUS=$(json_field "$BODY" "status")
    pass "$NAME /health → HTTP 200  status=$SVC_STATUS"
  else
    fail "$NAME /health → HTTP $STATUS (expected 200) — run: docker compose up --build -d"
  fi
done

if [ ${#FAILED[@]} -gt 0 ]; then
  echo -e "\n${RED}${BOLD}ABORTED: Not all services are healthy. Fix above errors first.${RESET}"
  exit 1
fi

# Ensure clean state before route tests
info "Resetting any crashed services to clean state..."
http_body_post "$CORE_URL/recover"    > /dev/null 2>&1 || true
http_body_post "$PAYMENT_URL/recover" > /dev/null 2>&1 || true
http_body_post "$MOVIE_URL/recover"   > /dev/null 2>&1 || true
sleep 1

# ── Step 2: Gateway routes — all 3 services ──────────────────────────────────

header "Step 2: Gateway Routes — GET /{service_name}"

for ROUTE_SVC in core-service payment-service movie-service; do
  check
  BODY=$(http_body "$API_URL/$ROUTE_SVC")
  HTTP_STATUS=$(http_status "$API_URL/$ROUTE_SVC")
  SOURCE=$(json_field "$BODY" "source")
  DEGRADED=$(json_field "$BODY" "degraded")

  echo -e "    GET /$ROUTE_SVC → HTTP $HTTP_STATUS  source=${CYAN}$SOURCE${RESET}  degraded=${YELLOW}$DEGRADED${RESET}"

  if [ "$HTTP_STATUS" = "200" ] && [ "$SOURCE" = "$ROUTE_SVC" ] && [ "$DEGRADED" = "False" ]; then
    pass "GET /$ROUTE_SVC → source=$ROUTE_SVC degraded=False"
  else
    fail "GET /$ROUTE_SVC → HTTP $HTTP_STATUS source=$SOURCE degraded=$DEGRADED (expected $ROUTE_SVC / False)"
  fi
done

# ── Step 3: Unknown route → 404 ───────────────────────────────────────────────

header "Step 3: Unknown Service → HTTP 404"

check
STATUS=$(http_status "$API_URL/unknown-service-xyz")
BODY=$(http_body "$API_URL/unknown-service-xyz")
DETAIL=$(json_field "$BODY" "detail")
echo -e "    GET /unknown-service-xyz → HTTP $STATUS  detail=$DETAIL"

if [ "$STATUS" = "404" ]; then
  pass "GET /unknown-service-xyz → HTTP 404 (not registered)"
else
  fail "GET /unknown-service-xyz → HTTP $STATUS (expected 404)"
fi

# ── Step 4: Circuit breaker isolation ────────────────────────────────────────

header "Step 4: Circuit Breaker Isolation"
info "Crashing movie-service to test fallback strategy..."

check
CRASH_BODY=$(http_body_post "$MOVIE_URL/fail")
CRASHED=$(json_field "$CRASH_BODY" "crashed")
if [ "$CRASHED" = "True" ]; then
  pass "movie-service crashed successfully"
else
  fail "movie-service /fail did not crash (got: $CRASH_BODY)"
fi

# Open movie-service circuit by hitting the route multiple times
info "Sending 5 requests to GET /movie-service to open circuit breaker..."
FALLBACK_COUNT=0
for i in $(seq 1 5); do
  BODY=$(http_body "$API_URL/movie-service")
  SOURCE=$(json_field "$BODY" "source")
  DEGRADED=$(json_field "$BODY" "degraded")
  echo -e "    Call $i: source=${CYAN}$SOURCE${RESET}  degraded=${YELLOW}$DEGRADED${RESET}"
  if [ "$SOURCE" = "fallback-service" ]; then
    FALLBACK_COUNT=$((FALLBACK_COUNT + 1))
  fi
  sleep 0.3
done

check
if [ "$FALLBACK_COUNT" -ge 3 ]; then
  pass "movie-service circuit open → $FALLBACK_COUNT/5 calls routed to fallback-service"
else
  fail "Expected fallback routing for movie-service, got only $FALLBACK_COUNT/5 fallback responses"
fi

# Payment-service must still work (independent circuit)
check
PAYMENT_BODY=$(http_body "$API_URL/payment-service")
PAYMENT_STATUS=$(http_status "$API_URL/payment-service")
PAYMENT_SOURCE=$(json_field "$PAYMENT_BODY" "source")
PAYMENT_DEGRADED=$(json_field "$PAYMENT_BODY" "degraded")
echo -e "    GET /payment-service → HTTP $PAYMENT_STATUS  source=${CYAN}$PAYMENT_SOURCE${RESET}  degraded=${YELLOW}$PAYMENT_DEGRADED${RESET}"

if [ "$PAYMENT_STATUS" = "200" ] && [ "$PAYMENT_SOURCE" = "payment-service" ] && [ "$PAYMENT_DEGRADED" = "False" ]; then
  pass "payment-service unaffected by movie-service circuit (circuit isolation confirmed)"
else
  fail "payment-service degraded after movie-service crash (source=$PAYMENT_SOURCE status=$PAYMENT_STATUS)"
fi

# Recover movie-service
info "Recovering movie-service..."
http_body_post "$MOVIE_URL/recover" > /dev/null 2>&1 || true
sleep 1

# ── Step 5: Payment escalate strategy — no fallback on failure ─────────────

header "Step 5: Payment Escalate Strategy → HTTP 503"
info "Crashing payment-service (strategy=escalate)..."

check
CRASH=$(http_body_post "$PAYMENT_URL/fail")
CRASHED=$(json_field "$CRASH" "crashed")
if [ "$CRASHED" = "True" ]; then
  pass "payment-service crashed"
else
  fail "payment-service /fail returned: $CRASH"
fi

# Make enough calls to open the circuit
info "Sending 5 requests to GET /payment-service — expect 503, NO fallback..."
ESCALATE_COUNT=0
for i in $(seq 1 5); do
  STATUS=$(http_status "$API_URL/payment-service")
  BODY=$(http_body "$API_URL/payment-service")
  SOURCE=$(json_field "$BODY" "source")
  echo -e "    Call $i: HTTP $STATUS  source=${CYAN}$SOURCE${RESET}"
  if [ "$STATUS" = "503" ]; then
    ESCALATE_COUNT=$((ESCALATE_COUNT + 1))
  fi
  sleep 0.3
done

check
if [ "$ESCALATE_COUNT" -ge 3 ]; then
  pass "payment-service escalate: $ESCALATE_COUNT/5 calls returned 503 (no fallback)"
else
  fail "Expected HTTP 503 from payment escalate, got only $ESCALATE_COUNT/5 503 responses"
fi

# Confirm fallback-service was NOT used for payment
check
FALLBACK_IN_PAYMENT=$(docker logs api-service 2>&1 | grep "FALLBACK_TRIGGERED.*payment-service" | tail -1 || echo "")
if [ -z "$FALLBACK_IN_PAYMENT" ]; then
  pass "No FALLBACK_TRIGGERED log for payment-service — escalate path confirmed"
else
  fail "FALLBACK_TRIGGERED found for payment-service (should not happen with escalate)"
fi

# Recover payment-service
info "Recovering payment-service..."
http_body_post "$PAYMENT_URL/recover" > /dev/null 2>&1 || true
sleep 1

# ── Step 6: Full recovery — all routes working again ─────────────────────────

header "Step 6: Post-Recovery Verification"
info "Waiting 35s for circuit breakers to probe and close..."
sleep 35

for ROUTE_SVC in core-service payment-service movie-service; do
  check
  BODY=$(http_body "$API_URL/$ROUTE_SVC")
  STATUS=$(http_status "$API_URL/$ROUTE_SVC")
  SOURCE=$(json_field "$BODY" "source")
  DEGRADED=$(json_field "$BODY" "degraded")
  echo -e "    GET /$ROUTE_SVC → HTTP $STATUS  source=${CYAN}$SOURCE${RESET}  degraded=${YELLOW}$DEGRADED${RESET}"
  if [ "$STATUS" = "200" ] && [ "$SOURCE" = "$ROUTE_SVC" ] && [ "$DEGRADED" = "False" ]; then
    pass "GET /$ROUTE_SVC → fully recovered"
  else
    fail "GET /$ROUTE_SVC → not fully recovered (HTTP $STATUS source=$SOURCE degraded=$DEGRADED)"
  fi
done

# ── Summary ────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}${CYAN}  TEST SUMMARY: gateway_routes_smoke_test${RESET}"
echo -e "${BOLD}${CYAN}══════════════════════════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Total checks  : $TOTAL"
echo -e "  Passed        : $((TOTAL - ${#FAILED[@]}))"
echo -e "  Failed        : ${#FAILED[@]}"
echo ""

if [ ${#FAILED[@]} -eq 0 ]; then
  echo -e "  ${GREEN}${BOLD}██████████████████████████████████████████${RESET}"
  echo -e "  ${GREEN}${BOLD}  RESULT: PASS — Config-driven gateway OK  ${RESET}"
  echo -e "  ${GREEN}${BOLD}██████████████████████████████████████████${RESET}"
  echo ""
  exit 0
else
  echo -e "  ${RED}${BOLD}██████████████████████████████████████████${RESET}"
  echo -e "  ${RED}${BOLD}  RESULT: FAIL — ${#FAILED[@]} check(s) failed      ${RESET}"
  echo -e "  ${RED}${BOLD}██████████████████████████████████████████${RESET}"
  echo ""
  for f in "${FAILED[@]}"; do
    echo -e "    ${RED}✗${RESET} $f"
  done
  echo ""
  exit 1
fi
