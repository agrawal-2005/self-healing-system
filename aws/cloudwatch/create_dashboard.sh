#!/usr/bin/env bash
# =============================================================================
# create_dashboard.sh
#
# Creates (or updates) the SelfHealingSystemDashboard in CloudWatch.
#
# Usage:
#   cd /path/to/self-healing-system
#   chmod +x aws/cloudwatch/create_dashboard.sh
#   ./aws/cloudwatch/create_dashboard.sh
#
# Prerequisites:
#   - AWS CLI installed: aws --version
#   - Credentials loaded: export $(grep -v '^#' monitor/.env | xargs)
#   - Region us-east-1 (or override: AWS_DEFAULT_REGION=ap-southeast-1 ./create_dashboard.sh)
#
# What it does:
#   1. Reads dashboard.json from the same directory as this script.
#   2. Calls aws cloudwatch put-dashboard.
#   3. Prints the console URL to open in your browser.
#
# Re-running this script is safe — it replaces the dashboard with the latest JSON.
# =============================================================================

set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
DASHBOARD_NAME="SelfHealingSystemDashboard"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_JSON_PATH="$SCRIPT_DIR/dashboard.json"

# ── Preflight checks ──────────────────────────────────────────────────────────

if ! command -v aws &>/dev/null; then
  echo "ERROR: AWS CLI not found. Install it: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  exit 1
fi

if [ ! -f "$DASHBOARD_JSON_PATH" ]; then
  echo "ERROR: dashboard.json not found at $DASHBOARD_JSON_PATH"
  exit 1
fi

echo "Creating CloudWatch dashboard..."
echo "  Dashboard : $DASHBOARD_NAME"
echo "  Region    : $REGION"
echo "  JSON file : $DASHBOARD_JSON_PATH"
echo ""

# ── Create or update the dashboard ───────────────────────────────────────────

aws cloudwatch put-dashboard \
  --dashboard-name "$DASHBOARD_NAME" \
  --dashboard-body "$(cat "$DASHBOARD_JSON_PATH")" \
  --region "$REGION" \
  --output json

echo ""
echo "Dashboard created successfully."
echo ""
echo "Open in browser:"
echo "  https://console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards:name=${DASHBOARD_NAME}"
echo ""
echo "Note: Custom metrics appear in CloudWatch only after the first data point is emitted."
echo "      Trigger a failure and recovery run to populate the graphs:"
echo "      curl -X POST http://localhost:8001/fail"
