# Manual Test: Critical Core Failure Recovery

**Test ID:** `critical_core_failure_recovery`  
**Type:** End-to-End Chaos Test  
**Author:** SRE Team  
**Estimated time:** 5–10 minutes

---

## Purpose

Validate that the **entire self-healing pipeline** works correctly when
`core-service` crashes in production.

This test intentionally triggers a real failure and verifies that:

- `api-service` falls back to `fallback-service` automatically
- The circuit breaker opens after repeated core-service failures
- The monitor detects the HTTP 503 and publishes an EventBridge event
- Event cooldown prevents duplicate Lambda invocations
- Lambda receives the event and decides `restart_service`
- Lambda calls `recovery-agent` with the correct `X-Recovery-Token`
- `recovery-agent` validates the token and checks the allowed-services list
- `recovery-agent` runs `docker restart core-service`
- A record is written to the recovery history JSONL file
- `core-service` becomes healthy again
- `api-service` returns `degraded=false` using `core-service`

---

## Prerequisites

Before starting, confirm everything below is true.

### 1. All Docker containers are healthy

```bash
docker compose ps
```

Expected — all four containers show `(healthy)`:

```
NAME               STATUS
api-service        Up X seconds (healthy)
core-service       Up X seconds (healthy)
fallback-service   Up X seconds (healthy)
recovery-agent     Up X seconds (healthy)
```

If any are missing:

```bash
docker compose up --build -d
```

### 2. Monitor is running

```bash
pgrep -f monitor.py && echo "running" || echo "NOT running"
```

If not running:

```bash
cd monitor
export $(grep -v '^#' .env | xargs)
python3 monitor.py > /tmp/monitor.log 2>&1 &
cd ..
```

### 3. Tunnel is active and Lambda is pointed to it

```bash
# Check ngrok is running on port 8003
curl -s http://127.0.0.1:4040/api/tunnels | python3 -c "
import sys, json
t = json.load(sys.stdin)['tunnels']
for x in t:
    print(x['public_url'], '→', x['config']['addr'])
"
```

Expected:
```
https://your-url.ngrok-free.app → http://localhost:8003
```

Verify Lambda is using that URL:

```bash
aws lambda get-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --region us-east-1 \
  --query "Environment.Variables.RECOVERY_AGENT_URL" \
  --output text
```

If the Lambda URL is stale, update it:

```bash
aws lambda update-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --environment "Variables={RECOVERY_AGENT_URL=https://YOUR_NGROK_URL,TARGET_SERVICE=core-service,RECOVERY_TOKEN=dev-token,MAX_RETRIES=3}" \
  --region us-east-1
```

### 4. core-service is currently healthy (clean start)

```bash
curl -s http://localhost:8001/health
```

Expected:
```json
{"status": "healthy", "service": "core-service"}
```

If it shows `"unhealthy"`, recover it first:

```bash
curl -s -X POST http://localhost:8001/recover
```

---

## Test Steps

Run each command exactly as shown. Check the expected output before proceeding.

---

### Step 1 — Verify baseline (all services healthy)

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8001/health
curl -s http://localhost:8002/health
curl -s http://localhost:8003/health
```

**Expected output for each:**
```json
{"status": "healthy", "service": "<service-name>"}
```

**Expected `/process` (normal traffic):**

```bash
curl -s http://localhost:8000/process
```

```json
{
  "source": "core-service",
  "result": {"message": "Work completed successfully.", "service": "core-service"},
  "degraded": false
}
```

**PASS criteria:** All four return `healthy`. `/process` returns `source: core-service, degraded: false`.

---

### Step 2 — Trigger crash

```bash
curl -s -X POST http://localhost:8001/fail
```

**Expected:**
```json
{
  "message": "core-service is now simulating a crash. POST /recover to reset.",
  "crashed": true
}
```

```bash
curl -s http://localhost:8001/health
```

**Expected:**
```json
{"status": "unhealthy", "service": "core-service"}
```

HTTP status code should be 503:

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8001/health
```

**Expected:** `503`

**PASS criteria:** core-service returns `503` on health check.

---

### Step 3 — Verify api-service falls back

```bash
for i in 1 2 3 4 5; do
  RESULT=$(curl -s http://localhost:8000/process)
  echo "Call $i: $(echo $RESULT | python3 -c 'import sys,json; d=json.load(sys.stdin); print("source=" + d["source"] + " degraded=" + str(d["degraded"]))')"
  sleep 0.5
done
```

**Expected (all 5 calls):**
```
Call 1: source=fallback-service degraded=True
Call 2: source=fallback-service degraded=True
Call 3: source=fallback-service degraded=True
Call 4: source=fallback-service degraded=True
Call 5: source=fallback-service degraded=True
```

Note: Calls 1–3 will hit core-service and fail (incrementing the circuit breaker counter).
After 3 failures, the circuit opens. Calls 4–5 skip core-service entirely.

**PASS criteria:** All calls return `fallback-service` with `degraded=True`.

---

### Step 4 — Verify circuit breaker opened

```bash
docker logs api-service 2>&1 | grep -i circuit | tail -5
```

**Expected (look for these lines):**
```
CircuitBreaker: failure recorded 1/3
CircuitBreaker: failure recorded 2/3
CircuitBreaker: CLOSED → OPEN (failures=3/3 threshold=3)
CircuitBreaker: OPEN — blocking core-service call (Xs until probe)
```

**PASS criteria:** At least one `OPEN` line appears.

---

### Step 5 — Verify monitor detected the failure

```bash
tail -20 /tmp/monitor.log
```

**Expected (look for these two lines):**
```
WARNING: HealthChecker [core-service]: HTTP 503 (Xms)
INFO:    EventBridgePublisher: published event service=core-service failure=crash latency=Xms
```

**PASS criteria:** Both lines appear within 10 seconds of triggering the crash.

---

### Step 6 — Verify event cooldown is working

Wait 10 more seconds and re-check the monitor log:

```bash
sleep 10 && tail -20 /tmp/monitor.log
```

**Expected:**
```
INFO: EventCooldown: suppressing event service=core-service failure=crash (Xs remaining in 60s cooldown window)
```

This line confirms cooldown is active — no duplicate Lambda invocations during the 60-second window.

**PASS criteria:** Suppression line appears. No second `published event` line within 60 seconds.

---

### Step 7 — Wait for auto-recovery

The pipeline takes approximately 5–15 seconds end-to-end.  
Wait and poll for core-service to come back:

```bash
echo "Waiting for auto-recovery (up to 90 seconds)..."
for i in $(seq 1 18); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8001/health)
  echo "$(date +%H:%M:%S) core-service HTTP $STATUS"
  if [ "$STATUS" = "200" ]; then
    echo "RECOVERED at attempt $i"
    break
  fi
  sleep 5
done
```

**Expected:** `HTTP 200` appears within 90 seconds.

**PASS criteria:** core-service returns `200` before timeout.

---

### Step 8 — Verify Lambda ran (CloudWatch logs)

```bash
LOG_GROUP="/aws/lambda/SelfHealingRecoveryHandler"
STREAM=$(aws logs describe-log-streams \
  --log-group-name "$LOG_GROUP" \
  --order-by LastEventTime \
  --descending \
  --max-items 1 \
  --region us-east-1 \
  --query "logStreams[0].logStreamName" \
  --output text)

aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$STREAM" \
  --region us-east-1 \
  --query "events[*].message" \
  --output text 2>&1 | grep -E "received|decided|attempt|response|complete" | tail -10
```

**Expected:**
```
lambda_handler: received event — {...}
lambda_handler: decided action=restart_service for failure_type=crash
_call_recovery_agent: attempt 1/3
POST https://... payload={"action": "restart_service", ...}
response — {"success": true, ...}
lambda_handler: complete — success=True  duration=XXXXms  attempts=1
```

**PASS criteria:** `success=True` appears. `attempts=1` (no retries needed).

---

### Step 9 — Verify recovery-agent token + allowlist

Check recovery-agent logs to confirm security checks passed:

```bash
docker logs recovery-agent 2>&1 | grep -E "action=|allowed|RecoveryHistory" | tail -10
```

**Expected:**
```
RecoveryService: action=restart_service  target=core-service  reason='Lambda triggered...'
RecoveryHistory: recorded action=restart_service service=core-service success=True duration=XXXms
```

**PASS criteria:** Both lines present. No `403` or `401` errors.

---

### Step 10 — Check recovery history JSONL

```bash
cat recovery-agent/data/recovery_history.jsonl | python3 -c "
import sys, json
records = [json.loads(l) for l in sys.stdin if l.strip()]
latest = records[-1]
print('timestamp:            ', latest['timestamp'])
print('service_name:         ', latest['service_name'])
print('action:               ', latest['action'])
print('success:              ', latest['success'])
print('recovery_duration_ms: ', latest['recovery_duration_ms'], 'ms')
print('returncode:           ', latest['returncode'])
print('stdout:               ', latest['stdout'])
"
```

**Expected:**
```
timestamp:             2026-XX-XXT...
service_name:          core-service
action:                restart_service
success:               True
recovery_duration_ms:  XXX ms
returncode:            0
stdout:                core-service
```

**PASS criteria:** `success: True`, `returncode: 0`, `stdout: core-service`.

---

### Step 11 — Verify full recovery

```bash
# core-service health
curl -s http://localhost:8001/health

# api-service back to normal
curl -s http://localhost:8000/process
```

**Expected core-service:**
```json
{"status": "healthy", "service": "core-service"}
```

**Expected api-service:**
```json
{
  "source": "core-service",
  "result": {"message": "Work completed successfully.", "service": "core-service"},
  "degraded": false
}
```

**PASS criteria:** `source=core-service`, `degraded=false`. System fully healed.

---

### Step 12 — Check monitor cleared the cooldown

```bash
grep "cleared" /tmp/monitor.log | tail -3
```

**Expected:**
```
EventCooldown: cleared 1 timer(s) for 'core-service' on recovery
```

**PASS criteria:** Cleared line appears after recovery, confirming next failure will fire a fresh event.

---

## Pass / Fail Criteria Summary

| Step | Check | Pass |
|------|-------|------|
| 1 | All services healthy at start | `status=healthy` × 4 |
| 2 | core-service crashes | `503` on /health |
| 3 | api-service falls back | `source=fallback-service degraded=True` × 5 |
| 4 | Circuit breaker opened | `CLOSED → OPEN` in api-service logs |
| 5 | Monitor detects 503 | `published event` in monitor log |
| 6 | Cooldown suppresses duplicates | `suppressing event` in monitor log |
| 7 | Auto-recovery completes | `HTTP 200` within 90s |
| 8 | Lambda ran successfully | `success=True` in CloudWatch |
| 9 | Token + allowlist validated | No 401/403 in recovery-agent logs |
| 10 | History JSONL written | `returncode=0 stdout=core-service` |
| 11 | System fully healed | `source=core-service degraded=false` |
| 12 | Cooldown cleared on recovery | `cleared 1 timer(s)` in monitor log |

**All 12 steps must pass for the test to PASS.**

---

## Troubleshooting

### core-service not returning 503 after `/fail`

The `/fail` endpoint sets an in-memory flag. The health endpoint returns 503 when that flag is set.  
If you see `200` instead of `503` immediately after `/fail`, check that you rebuilt the image:

```bash
docker compose up --build -d core-service
```

---

### api-service not falling back

Check that api-service can reach both services:

```bash
docker logs api-service 2>&1 | tail -20
```

Check circuit breaker threshold is 3 (not 0):

```bash
docker inspect api-service | python3 -c "
import sys, json
cfg = json.load(sys.stdin)[0]['Config']['Env']
for e in cfg:
    if 'CIRCUIT' in e:
        print(e)
"
```

---

### Monitor not publishing events

Check monitor is running:

```bash
pgrep -f monitor.py && echo "running" || echo "NOT running"
```

Check monitor logs for errors:

```bash
tail -30 /tmp/monitor.log
```

Check AWS credentials are loaded:

```bash
grep "Found credentials" /tmp/monitor.log
```

If credentials are missing, re-export:

```bash
cd monitor
export $(grep -v '^#' .env | xargs)
python3 monitor.py > /tmp/monitor.log 2>&1 &
```

---

### Lambda not triggering

Check EventBridge rule is enabled:

```bash
aws events describe-rule \
  --name SelfHealingFailureRule \
  --region us-east-1 \
  --query "State" \
  --output text
```

Expected: `ENABLED`

Check Lambda has the correct tunnel URL:

```bash
aws lambda get-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --region us-east-1 \
  --query "Environment.Variables" \
  --output json
```

---

### Lambda gets 502 from recovery-agent

The tunnel (ngrok/serveo) dropped. Restart it:

```bash
# Restart ngrok
ngrok http 8003

# Update Lambda
aws lambda update-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --environment "Variables={RECOVERY_AGENT_URL=https://NEW_URL,TARGET_SERVICE=core-service,RECOVERY_TOKEN=dev-token,MAX_RETRIES=3}" \
  --region us-east-1
```

---

### Recovery history file is empty

Check the volume mount is working:

```bash
docker inspect recovery-agent | python3 -c "
import sys, json
mounts = json.load(sys.stdin)[0]['Mounts']
for m in mounts:
    print(m['Source'], '->', m['Destination'])
"
```

The `/app/data` path should be mounted to `./recovery-agent/data` on the host.

---

### core-service does not recover automatically (timeout reached)

Check Lambda logs for errors:

```bash
LOG_GROUP="/aws/lambda/SelfHealingRecoveryHandler"
STREAM=$(aws logs describe-log-streams \
  --log-group-name "$LOG_GROUP" \
  --order-by LastEventTime \
  --descending --max-items 1 \
  --region us-east-1 \
  --query "logStreams[0].logStreamName" --output text)
aws logs get-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$STREAM" \
  --region us-east-1 \
  --query "events[*].message" --output text
```

If Lambda is not being invoked at all, check the EventBridge target:

```bash
aws events list-targets-by-rule \
  --rule SelfHealingFailureRule \
  --region us-east-1
```

Manual recovery as fallback:

```bash
curl -s -X POST http://localhost:8001/recover
```

---

## Cleanup

After the test, services remain running. No AWS resources are deleted automatically.

```bash
# Confirm all services are healthy
docker compose ps

# Confirm core-service is recovered
curl -s http://localhost:8001/health

# Optionally restart clean
docker compose down && docker compose up --build -d
```
