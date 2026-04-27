# Self-Healing System — Test Suite

This folder contains end-to-end tests for the self-healing distributed system.  
Tests are intentionally written as **shell scripts and markdown guides** — no test framework  
dependency, no pip install, no setup.py. Any machine with `bash`, `curl`, and `python3` can run them.

---

## Test Index

| Test | Type | Script | Manual Guide |
|------|------|--------|--------------|
| Critical Core Failure Recovery | End-to-End Chaos | `scripts/critical_core_failure_recovery.sh` | `manual/critical_core_failure_recovery.md` |

---

## What the Test Covers

`critical_core_failure_recovery` validates the **entire production recovery pipeline**
in one automated run:

```
core-service crash
       ↓
api-service falls back to fallback-service     (circuit breaker)
       ↓
monitor detects HTTP 503                        (health checker)
       ↓
monitor publishes EventBridge event             (event cooldown prevents duplicates)
       ↓
Lambda receives event → decides restart_service
       ↓
Lambda calls recovery-agent with X-Recovery-Token
       ↓
recovery-agent validates token + allowlist
       ↓
docker restart core-service
       ↓
recovery history JSONL written
       ↓
core-service healthy → api-service degraded=false
```

---

## Prerequisites

### Local

| Tool | Check | Install |
|------|-------|---------|
| Docker + Docker Compose | `docker compose ps` | https://docs.docker.com/get-docker |
| curl | `curl --version` | usually pre-installed |
| python3 | `python3 --version` | https://python.org |
| bash 3.2+ | `bash --version` | macOS default is fine |

### All Docker services must be running and healthy

```bash
docker compose up --build -d
docker compose ps   # all 4 should show (healthy)
```

Required containers:

- `api-service`      — port 8000
- `core-service`     — port 8001
- `fallback-service` — port 8002
- `recovery-agent`   — port 8003

### Monitor must be running

```bash
cd monitor
export $(grep -v '^#' .env | xargs)
python3 monitor.py &
```

### AWS Resources Required

| Resource | Name | Region |
|----------|------|--------|
| EventBridge Rule | `SelfHealingFailureRule` | us-east-1 |
| Lambda Function | `SelfHealingRecoveryHandler` | us-east-1 |
| SQS DLQ | `SelfHealingLambdaDLQ` | us-east-1 |

### Tunnel Must Be Active

Lambda calls recovery-agent through a tunnel (ngrok or serveo).  
The tunnel must be running and Lambda's `RECOVERY_AGENT_URL` must be up to date.

```bash
# Start ngrok (correct port)
ngrok http 8003

# Update Lambda env var with new URL
aws lambda update-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --environment "Variables={RECOVERY_AGENT_URL=https://YOUR_URL,TARGET_SERVICE=core-service,RECOVERY_TOKEN=dev-token,MAX_RETRIES=3}" \
  --region us-east-1
```

---

## Environment Variables

The test script uses these variables (with defaults shown).  
Override them by exporting before running the script.

| Variable | Default | Description |
|----------|---------|-------------|
| `API_URL` | `http://localhost:8000` | api-service base URL |
| `CORE_URL` | `http://localhost:8001` | core-service base URL |
| `RECOVERY_HISTORY_FILE` | `recovery-agent/data/recovery_history.jsonl` | JSONL history path |
| `RECOVERY_TIMEOUT_SECONDS` | `90` | Max seconds to wait for auto-recovery |

No AWS secrets are needed by the script — it only talks to local services.

---

## How to Run

```bash
# From the project root
cd /path/to/self-healing-system

# Make script executable (first time only)
chmod +x tests/scripts/critical_core_failure_recovery.sh

# Run the test
./tests/scripts/critical_core_failure_recovery.sh
```

### Override variables if needed

```bash
RECOVERY_TIMEOUT_SECONDS=120 ./tests/scripts/critical_core_failure_recovery.sh
```

### Save results

```bash
./tests/scripts/critical_core_failure_recovery.sh \
  | tee tests/results/run_$(date +%Y%m%d_%H%M%S).txt
```

---

## Test Results

Results are stored in `tests/results/` (gitignored except `.gitkeep`).  
Save a run manually with the `tee` command above.

---

## Cleanup After Test

The test leaves all services running. No AWS resources are deleted.  
To reset to a clean state:

```bash
# Recover core-service if still crashed
curl -s -X POST http://localhost:8001/recover

# Restart all containers fresh
docker compose down && docker compose up --build -d
```
