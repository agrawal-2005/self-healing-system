# Phase 5 — EC2 + Docker Compose Deployment

> **Goal:** Remove the ngrok tunnel and laptop dependency by moving the entire
> Docker Compose stack to an AWS EC2 instance.  Lambda will call
> `http://EC2_PUBLIC_IP:8003/action` directly — no tunnel required.

---

## Table of Contents

1. [EC2 Launch Settings](#1-ec2-launch-settings)
2. [Security Group Configuration](#2-security-group-configuration)
3. [EC2 Setup Commands](#3-ec2-setup-commands)
4. [Project Deployment](#4-project-deployment)
5. [AWS Credentials Strategy](#5-aws-credentials-strategy)
6. [IAM Role for EC2](#6-iam-role-for-ec2)
7. [Lambda Update](#7-lambda-update)
8. [Monitor Deployment](#8-monitor-deployment)
9. [End-to-End Test](#9-end-to-end-test)
10. [Troubleshooting](#10-troubleshooting)
11. [Security Improvements After Demo](#11-security-improvements-after-demo)

---

## 1. EC2 Launch Settings

### Why EC2?

Your Docker Compose stack runs on your laptop today.  Lambda can only reach
`recovery-agent` because ngrok/serveo creates a public tunnel.  EC2 gives the
stack a permanent public IP — no tunnel needed and no dependency on your laptop
being awake.

### Open the EC2 console

Go to **AWS Console → EC2 → Launch Instance** (region `us-east-1`).

### Settings to choose

| Setting | Value | Why |
|---|---|---|
| **Name** | `self-healing-server` | Easy to identify |
| **AMI** | Ubuntu Server 24.04 LTS (64-bit x86) | LTS release, Docker well-supported |
| **Architecture** | x86_64 | Standard; cheapest instance types |
| **Instance type** | `t3.small` (2 vCPU, 2 GB RAM) | t2.micro works but can OOM with 4 containers; t3.small is ~$15/month |
| **Key pair** | Create new → `self-healing-key` → `.pem` format | You need this to SSH in; download and keep it safe |
| **Network** | Default VPC | Fine for this project |
| **Auto-assign public IP** | **Enable** | Lambda needs a fixed public IP to reach port 8003 |
| **Storage** | 20 GB gp3 | Images + logs; gp3 is cheaper than gp2 |

> **Free tier note:** `t2.micro` is free-tier eligible but 1 GB RAM is tight
> for 4 containers.  If you hit OOM issues, upgrade to `t3.small`.

### After launch — note the public IP

```
EC2 Console → Instances → your instance → Public IPv4 address
Example: 54.210.33.112
```

You will use this IP throughout the rest of this guide.  Save it:

```bash
export EC2_IP=54.210.33.112   # replace with your actual IP
```

---

## 2. Security Group Configuration

### Why security groups matter

A security group is a virtual firewall on the EC2 instance.  By default,
everything is blocked.  You open exactly what's needed.

### Create a new security group: `self-healing-sg`

Go to **EC2 → Security Groups → Create security group**.

#### Inbound rules

| Port | Protocol | Source | Purpose | Permanent? |
|---|---|---|---|---|
| 22 | TCP | My IP (your current IP) | SSH access | Keep, but update IP when it changes |
| 8000 | TCP | 0.0.0.0/0 | api-service — for testing from browser/curl | Demo only — restrict or remove in production |
| 8001 | TCP | 0.0.0.0/0 | core-service — for crash trigger tests | Demo only |
| 8002 | TCP | 0.0.0.0/0 | fallback-service — for response tests | Demo only |
| 8003 | TCP | 0.0.0.0/0 | recovery-agent — Lambda calls this port | Required for Lambda; harden with Lambda IP later |

#### Outbound rules

Leave the default: **All traffic, 0.0.0.0/0**.
The instance needs to reach AWS APIs (EventBridge, CloudWatch, STS).

#### Rules that should be hardened later

- Port 8003 source `0.0.0.0/0` → narrow to the Lambda NAT IP or use VPC
  (see [Section 11](#11-security-improvements-after-demo))
- Port 22 source → set to your static home/office IP
- Ports 8000–8002 → remove entirely once you add an ALB

---

## 3. EC2 Setup Commands

SSH into the instance first:

```bash
# Fix key permissions (required by SSH — it refuses world-readable keys)
chmod 400 ~/Downloads/self-healing-key.pem

ssh -i ~/Downloads/self-healing-key.pem ubuntu@$EC2_IP
```

> All commands below run **on the EC2 instance** (inside the SSH session).

### 3.1 Update packages

```bash
sudo apt-get update -y && sudo apt-get upgrade -y
```

*Why:* Ubuntu 24.04 ships with packages that may have CVEs fixed in updates.
Fresh server, clean start.

### 3.2 Install Git

```bash
sudo apt-get install -y git
git --version   # verify: git version 2.x
```

### 3.3 Install Docker

Ubuntu 24.04 ships a Docker package but it can be outdated.  Use Docker's
official APT repository:

```bash
# Install prerequisites
sudo apt-get install -y ca-certificates curl

# Add Docker's GPG key
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
     -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine + Compose plugin
sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
     docker-buildx-plugin docker-compose-plugin

# Verify
docker --version        # Docker version 27.x
docker compose version  # Docker Compose version v2.x
```

### 3.4 Add ubuntu user to Docker group

```bash
sudo usermod -aG docker ubuntu
```

*Why:* Without this, every `docker` command requires `sudo`.  The recovery-agent
container mounts `/var/run/docker.sock` — the `ubuntu` user running compose must
have permission to use the socket.

**Log out and back in for the group change to take effect:**

```bash
exit
ssh -i ~/Downloads/self-healing-key.pem ubuntu@$EC2_IP
```

### 3.5 Verify Docker works without sudo

```bash
docker run --rm hello-world
# Expected output: "Hello from Docker!"
```

### 3.6 Enable Docker to start on boot

```bash
sudo systemctl enable docker
sudo systemctl enable containerd
```

*Why:* If EC2 is rebooted (e.g. stop/start to change instance type), Docker
starts automatically and `restart: unless-stopped` in docker-compose.yml brings
all containers back up.

---

## 4. Project Deployment

All commands run on the EC2 instance.

### 4.1 Clone the repository

```bash
cd ~
git clone https://github.com/agrawal-2005/self-healing-system.git
cd self-healing-system
```

### 4.2 Create monitor/.env

This file holds your AWS credentials and monitor settings.
**Never commit this file** — it is already listed in `.gitignore`.

```bash
cp .env.example monitor/.env
nano monitor/.env
```

Fill in the real values:

```bash
# ── AWS Credentials ──────────────────────────────────────────────────────────
# Option A (beginner): paste IAM user keys here
# Option B (recommended): leave these empty and use an IAM role instead
#   (see Section 5 and 6)
AWS_ACCESS_KEY_ID=YOUR_ACCESS_KEY_HERE
AWS_SECRET_ACCESS_KEY=YOUR_SECRET_KEY_HERE
AWS_DEFAULT_REGION=us-east-1

# ── Service URLs — point at localhost on EC2 ──────────────────────────────────
API_SERVICE_URL=http://localhost:8000
CORE_SERVICE_URL=http://localhost:8001
FALLBACK_SERVICE_URL=http://localhost:8002

# ── Monitor behaviour ─────────────────────────────────────────────────────────
CHECK_INTERVAL_SECONDS=5
REQUEST_TIMEOUT_SECONDS=3.0
LATENCY_WARN_MS=500.0
LATENCY_SLOW_MS=1000.0
EVENT_COOLDOWN_SECONDS=60

# ── EventBridge ───────────────────────────────────────────────────────────────
EVENTBRIDGE_ENABLED=true
EVENTBRIDGE_EVENT_BUS=default
EVENTBRIDGE_SOURCE=selfhealing.local
EVENTBRIDGE_DETAIL_TYPE=ServiceFailureDetected
DRY_RUN=false

# ── CloudWatch ────────────────────────────────────────────────────────────────
CLOUDWATCH_ENABLED=true
CLOUDWATCH_NAMESPACE=SelfHealingSystem
```

> **If using an IAM role (recommended):** You can leave `AWS_ACCESS_KEY_ID` and
> `AWS_SECRET_ACCESS_KEY` blank or remove those lines entirely.  boto3 will
> automatically use the instance metadata credentials.  See Section 5 and 6.

### 4.3 Set a strong recovery token

The default `dev-token` must be replaced.  It must match the value set in Lambda.

```bash
# Generate a secure random token
python3 -c "import secrets; print(secrets.token_hex(32))"
# Example output: a3f9c2e1b4d87f60e2a5c9d3b1f4e8a7c2d5e6f9a1b3c4d5e6f7a8b9c0d1e2f3
```

Edit `docker-compose.yml` and replace `dev-token`:

```bash
nano docker-compose.yml
```

Find the recovery-agent section and update:

```yaml
      RECOVERY_TOKEN: "a3f9c2e1b4d87f60e2a5c9d3b1f4e8a7c2d5e6f9a1b3c4d5e6f7a8b9c0d1e2f3"
```

Save this token — you will set the same value in Lambda (Section 7).

### 4.4 Build and start containers

```bash
docker compose up -d --build
```

*Why `--build`:* On EC2 this is the first run — no cached images exist.
Subsequent restarts can use `docker compose up -d` (no `--build`).

Build takes ~2–3 minutes on a fresh instance.

### 4.5 Verify all containers are healthy

```bash
docker compose ps
```

Expected output (all `(healthy)`):

```
NAME               IMAGE                  STATUS
api-service        ...api-service         Up 30 seconds (healthy)
core-service       ...core-service        Up 30 seconds (healthy)
fallback-service   ...fallback-service    Up 30 seconds (healthy)
recovery-agent     ...recovery-agent      Up 40 seconds (healthy)
```

### 4.6 Smoke-test each service

```bash
curl http://localhost:8000/health     # api-service
curl http://localhost:8001/health     # core-service
curl http://localhost:8002/health     # fallback-service
curl http://localhost:8003/health     # recovery-agent
```

All should return HTTP 200 with `{"status": "ok"}` or similar.

### 4.7 Test the recovery-agent from your laptop

Open a new terminal on **your laptop**:

```bash
curl http://$EC2_IP:8003/health
```

If you get a response, Lambda can reach recovery-agent.  If you get
"connection refused", check the security group (port 8003 inbound rule).

---

## 5. AWS Credentials Strategy

The monitor, api-service, and recovery-agent all call AWS APIs (EventBridge,
CloudWatch).  There are two ways to provide credentials on EC2.

### Option A — Beginner: IAM user keys in monitor/.env

You already did this in Section 4.2.  The keys are read from `monitor/.env`,
which is loaded by Docker Compose for api-service and recovery-agent via
`env_file: - monitor/.env`.

**Pros:** Simple, works today.
**Cons:** Long-lived credentials stored on disk.  If the instance is
compromised, an attacker has your AWS keys.  Keys must be rotated manually.

### Option B — Recommended: EC2 IAM Instance Profile

Attach an IAM role to the EC2 instance.  boto3 automatically fetches
short-lived credentials from the EC2 instance metadata service (IMDS) —
no keys stored anywhere.

**Pros:** No credentials on disk.  Automatically rotated every hour.
IAM role can be audited and scoped precisely.

**How to use it:**
1. Create the IAM role (Section 6).
2. Attach it to your EC2 instance.
3. Remove `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` from `monitor/.env`.
4. Restart containers: `docker compose down && docker compose up -d`.

boto3 inside every container will find credentials at:
`http://169.254.169.254/latest/meta-data/iam/security-credentials/<role-name>`

---

## 6. IAM Role for EC2

### 6.1 Create the IAM role (AWS Console)

1. Go to **IAM → Roles → Create role**
2. **Trusted entity type:** AWS service
3. **Use case:** EC2
4. Click **Next**
5. Skip the permissions page for now (we'll add an inline policy)
6. **Role name:** `SelfHealingEC2Role`
7. Click **Create role**

### 6.2 Attach the inline policy

Go to the role → **Add permissions → Create inline policy → JSON** tab.
Paste this policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublishEvents",
      "Effect": "Allow",
      "Action": "events:PutEvents",
      "Resource": "*"
    },
    {
      "Sid": "PutCustomMetrics",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "cloudwatch:namespace": "SelfHealingSystem"
        }
      }
    },
    {
      "Sid": "ReadMetrics",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "cloudwatch:GetMetricData"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ManageDashboard",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutDashboard",
        "cloudwatch:GetDashboard",
        "cloudwatch:DeleteDashboards",
        "cloudwatch:ListDashboards"
      ],
      "Resource": "arn:aws:cloudwatch::*:dashboard/SelfHealingSystemDashboard"
    },
    {
      "Sid": "ReadDLQAttributes",
      "Effect": "Allow",
      "Action": "sqs:GetQueueAttributes",
      "Resource": "arn:aws:sqs:us-east-1:*:SelfHealingLambdaDLQ"
    }
  ]
}
```

Name the policy: `SelfHealingEC2InlinePolicy` → **Create policy**.

### 6.3 Attach the role to the EC2 instance

**EC2 Console → Instances → select `self-healing-server` →
Actions → Security → Modify IAM role → select `SelfHealingEC2Role` → Update IAM role**

Verify it worked:

```bash
# On EC2 instance
aws sts get-caller-identity --region us-east-1
# Should return the role ARN, not an IAM user ARN
```

### 6.4 Remove hard-coded keys (if using Option B)

```bash
nano monitor/.env
```

Delete or comment out these two lines:

```bash
# AWS_ACCESS_KEY_ID=...    ← delete
# AWS_SECRET_ACCESS_KEY=... ← delete
```

Restart containers to pick up the change:

```bash
docker compose down
docker compose up -d
```

---

## 7. Lambda Update

Lambda currently has your old ngrok URL for `RECOVERY_AGENT_URL`.
Replace it with the EC2 public IP.

### 7.1 Update the environment variable

Run from **your laptop** (or any machine with AWS CLI and admin credentials):

```bash
export EC2_IP=54.210.33.112   # your actual EC2 public IP

aws lambda update-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --environment "Variables={
    RECOVERY_AGENT_URL=http://${EC2_IP}:8003,
    TARGET_SERVICE=core-service,
    RECOVERY_TOKEN=YOUR_NEW_TOKEN_HERE,
    MAX_RETRIES=3
  }" \
  --region us-east-1
```

Replace `YOUR_NEW_TOKEN_HERE` with the token you generated in Section 4.3.

### 7.2 Verify the update

```bash
aws lambda get-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --region us-east-1 \
  --query 'Environment.Variables'
```

Expected:

```json
{
    "RECOVERY_AGENT_URL": "http://54.210.33.112:8003",
    "TARGET_SERVICE": "core-service",
    "RECOVERY_TOKEN": "a3f9c2e1...",
    "MAX_RETRIES": "3"
}
```

### 7.3 Test Lambda can reach EC2

Invoke Lambda manually:

```bash
aws lambda invoke \
  --function-name SelfHealingRecoveryHandler \
  --payload '{"detail":{"service_name":"core-service","failure_type":"crash","latency_ms":0,"timestamp":"2026-01-01T00:00:00Z"}}' \
  --cli-binary-format raw-in-base64-out \
  --region us-east-1 \
  /tmp/lambda_test_response.json

cat /tmp/lambda_test_response.json
```

A successful response looks like:

```json
{"statusCode": 200, "body": "{\"success\": true, \"action\": \"restart_service\", ...}"}
```

---

## 8. Monitor Deployment

The monitor process is a Python script that polls health endpoints and
publishes EventBridge events.  It is not a Docker container — it runs as a
background process directly on the EC2 instance.

First, install Python dependencies:

```bash
cd ~/self-healing-system/monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Option A — nohup (simple, for quick demo)

```bash
cd ~/self-healing-system/monitor
export $(grep -v '^#' .env | xargs)
nohup python3 monitor.py > /tmp/monitor.log 2>&1 &
echo "Monitor PID: $!"

# Watch the log
tail -f /tmp/monitor.log
```

**Pros:** One command to start.
**Cons:** Does not survive EC2 reboot.  Must be restarted manually after stop/start.

Stop it:

```bash
pkill -f monitor.py
```

### Option B — systemd service (recommended for persistent deployment)

systemd manages the process lifecycle.  The monitor starts automatically on
boot and restarts on crash.

#### Create the service file

```bash
sudo nano /etc/systemd/system/self-healing-monitor.service
```

Paste this content (adjust paths if needed):

```ini
[Unit]
Description=Self-Healing System Monitor
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/self-healing-system/monitor
EnvironmentFile=/home/ubuntu/self-healing-system/monitor/.env
ExecStart=/home/ubuntu/self-healing-system/monitor/.venv/bin/python3 monitor.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=self-healing-monitor

[Install]
WantedBy=multi-user.target
```

#### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable self-healing-monitor
sudo systemctl start self-healing-monitor

# Check status
sudo systemctl status self-healing-monitor
```

Expected:

```
● self-healing-monitor.service - Self-Healing System Monitor
     Loaded: loaded (/etc/systemd/system/self-healing-monitor.service; enabled)
     Active: active (running) since ...
```

#### View live logs

```bash
sudo journalctl -u self-healing-monitor -f
```

#### Restart after config change

```bash
sudo systemctl restart self-healing-monitor
```

---

## 9. End-to-End Test

Run this sequence to confirm the complete pipeline works on EC2.

### 9.1 Verify baseline

From **your laptop**:

```bash
curl http://$EC2_IP:8000/process
# Expected: {"source": "core-service", "degraded": false}
```

### 9.2 Trigger core-service crash

```bash
curl -X POST http://$EC2_IP:8001/fail
# Expected: {"crashed": true}
```

### 9.3 Confirm fallback is active

```bash
curl http://$EC2_IP:8000/process
# Expected: {"source": "fallback-service", "degraded": true}
```

### 9.4 Watch monitor detect the failure (on EC2)

```bash
# If using nohup:
tail -f /tmp/monitor.log

# If using systemd:
sudo journalctl -u self-healing-monitor -f
```

Expected sequence:

```
WARNING: HealthChecker [core-service]: HTTP 503
INFO:    EventBridgePublisher: published event service=core-service failure=crash
INFO:    EventCooldown: suppressing event (55s remaining)
```

### 9.5 Watch recovery-agent receive the Lambda call

```bash
docker logs recovery-agent -f
```

Expected:

```
INFO: RecoveryService: action=restart_service target=core-service
INFO: DockerExecutor: running docker restart core-service
INFO: RecoveryHistory: recorded action=restart_service success=True duration=520ms
```

### 9.6 Verify core-service has recovered

```bash
# From laptop, within ~30s of crash:
curl http://$EC2_IP:8000/process
# Expected: {"source": "core-service", "degraded": false}
```

### 9.7 Check recovery history on EC2

```bash
cat ~/self-healing-system/recovery-agent/data/recovery_history.jsonl | python3 -m json.tool | tail -30
```

### 9.8 Run the automated chaos test

Copy the test script configuration from your laptop or run it directly on EC2:

```bash
cd ~/self-healing-system
export $(grep -v '^#' monitor/.env | xargs)

# Temporarily point the test at localhost
CORE_URL=http://localhost:8001 \
RECOVERY_TIMEOUT_SECONDS=90 \
./tests/scripts/critical_core_failure_recovery.sh
```

### 9.9 Verify CloudWatch metrics

```bash
cd ~/self-healing-system
export $(grep -v '^#' monitor/.env | xargs)
./tests/scripts/verify_cloudwatch_metrics.sh
```

Expected: all 6 metrics show PASS.

### 9.10 Check the CloudWatch dashboard

Open in browser:

```
https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=SelfHealingSystemDashboard
```

You should see spikes in `FailureDetectedCount`, `RecoverySuccessCount`,
`FallbackUsedCount`, and `CircuitBreakerOpenCount` from the test run.

---

## 10. Troubleshooting

### SSH connection failed

```
ssh: connect to host 54.210.33.112 port 22: Connection refused
```

**Causes:**
- Security group missing port 22 inbound rule
- EC2 instance not fully started yet (wait 60s after launch)
- Wrong IP (check EC2 Console → Public IPv4)

```bash
# Check if the instance is reachable at all
ping $EC2_IP

# Check security group in console
# EC2 → Security Groups → self-healing-sg → Inbound rules
# Must have: Type=SSH, Port=22, Source=My IP
```

---

### Permission denied for key pair

```
Permissions 0644 for 'self-healing-key.pem' are too open.
```

```bash
chmod 400 ~/Downloads/self-healing-key.pem
```

---

### Docker permission denied

```
Got permission denied while trying to connect to the Docker daemon socket
```

You forgot to log out and back in after `usermod -aG docker ubuntu`.

```bash
exit
ssh -i ~/Downloads/self-healing-key.pem ubuntu@$EC2_IP
groups   # should show: ubuntu adm ... docker ...
```

---

### Port 8003 not reachable from outside

```
curl: (7) Failed to connect to 54.210.33.112 port 8003
```

**Check 1:** Security group

```bash
# On laptop:
aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=self-healing-sg" \
  --query 'SecurityGroups[].IpPermissions' \
  --region us-east-1
```

Port 8003 must appear with `IpRanges: 0.0.0.0/0`.

**Check 2:** Container is running

```bash
# On EC2:
docker compose ps recovery-agent
curl http://localhost:8003/health
```

---

### Lambda timeout

Lambda logs show `Task timed out after X seconds`.

**Cause:** Lambda's default timeout is 3 seconds — too short for Docker restart.

```bash
aws lambda update-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --timeout 60 \
  --region us-east-1
```

Also confirm Lambda can reach port 8003:

```bash
# Lambda is in a VPC? It needs a NAT gateway or public subnet.
# Lambda is not in a VPC? It uses the public internet — port 8003 must be open.
aws lambda get-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --query 'VpcConfig' \
  --region us-east-1
# If VpcConfig is empty, Lambda uses the internet — open port 8003 in SG.
```

---

### recovery-agent returns 401 / token invalid

```json
{"detail": "Invalid token or service not in allowed list"}
```

The token in Lambda's environment variable does not match the token in
`docker-compose.yml`.

```bash
# Check token in Lambda:
aws lambda get-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --query 'Environment.Variables.RECOVERY_TOKEN' \
  --region us-east-1

# Check token on EC2:
docker compose exec recovery-agent env | grep RECOVERY_TOKEN
```

If they differ, update Lambda (Section 7) with the correct token.

---

### monitor cannot publish EventBridge events

```
EventBridgePublisher: error publishing event — AccessDenied
```

**If using IAM keys:** Verify the IAM user has `events:PutEvents` permission.

```bash
aws events put-events \
  --entries '[{"Source":"test","DetailType":"test","Detail":"{}","EventBusName":"default"}]' \
  --region us-east-1
```

**If using IAM role:** Verify the role is attached.

```bash
# On EC2:
curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/
# Should print the role name, e.g. SelfHealingEC2Role
```

---

### CloudWatch metrics missing after test

**Check 1:** `CLOUDWATCH_ENABLED=true` in monitor/.env

```bash
grep CLOUDWATCH monitor/.env
```

**Check 2:** Containers rebuilt after config change

```bash
docker compose down && docker compose up -d --build
```

**Check 3:** Time window — CloudWatch has ~1 minute ingestion delay.
Wait 90 seconds after the recovery event, then query.

**Check 4:** Correct AWS region

```bash
grep AWS_DEFAULT_REGION monitor/.env
# Must be: us-east-1
```

**Check 5:** Container has credentials

```bash
docker exec api-service env | grep AWS
docker exec recovery-agent env | grep AWS
```

---

## 11. Security Improvements After Demo

The settings above are designed to get you running quickly.  Before you treat
this as anything beyond a personal project, apply these hardening steps.

### Restrict SSH to your IP

```bash
# Get your current IP
curl -s https://checkip.amazonaws.com

# In AWS Console: EC2 → Security Groups → self-healing-sg
# Edit SSH rule: Source → My IP (or paste your IP/32)
```

### Do not expose recovery-agent publicly

Port 8003 open to `0.0.0.0/0` means anyone on the internet can attempt to
call your recovery-agent.  The token provides some protection, but the right
fix is to lock down the source.

**Short term:** Limit port 8003 to `35.0.0.0/8, 52.0.0.0/8` (AWS Lambda IP
ranges for us-east-1).  Find the ranges at:
`https://ip-ranges.amazonaws.com/ip-ranges.json`

**Long term:** Move recovery-agent inside the same VPC as Lambda and use
a VPC endpoint — no public exposure at all.

### Use HTTPS / ALB

Lambda calling HTTP is unencrypted.  The recovery token is sent in plaintext.

Add an Application Load Balancer:
- ALB listens on HTTPS (port 443) with an ACM certificate
- ALB forwards to recovery-agent on port 8003 internally
- Lambda calls `https://your-alb-dns/action`
- Security group on EC2 allows port 8003 only from the ALB's security group

### Use IAM roles instead of access keys

If you followed Option A (keys in `.env`), switch to Option B (IAM role)
as described in Sections 5 and 6.

### Rotate the recovery token periodically

```bash
# Generate new token
python3 -c "import secrets; print(secrets.token_hex(32))"

# Update docker-compose.yml on EC2, then:
docker compose up -d   # rolling restart picks up the new value

# Update Lambda:
aws lambda update-function-configuration \
  --function-name SelfHealingRecoveryHandler \
  --environment "Variables={RECOVERY_AGENT_URL=http://$EC2_IP:8003,TARGET_SERVICE=core-service,RECOVERY_TOKEN=NEW_TOKEN,MAX_RETRIES=3}" \
  --region us-east-1
```

### Use Elastic IP for a stable public address

EC2 public IPs change if you stop and start the instance.  Allocate an
Elastic IP and associate it with the instance so Lambda's `RECOVERY_AGENT_URL`
never needs updating:

```bash
# AWS Console → EC2 → Elastic IPs → Allocate → Associate with instance
# Or CLI:
aws ec2 allocate-address --domain vpc --region us-east-1
# Returns AllocationId
aws ec2 associate-address --instance-id i-xxxx --allocation-id eipalloc-xxxx --region us-east-1
```

### Move to ECS Fargate (next phase)

EC2 + Docker Compose is a great stepping stone.  When you outgrow it:

| Limitation | ECS solution |
|---|---|
| Manual EC2 patching | Fargate is serverless — no OS to manage |
| `docker.sock` mount for recovery-agent | ECS exec API or SSM Run Command |
| ngrok removed but EC2 still public | ECS runs inside VPC; Lambda in same VPC, no public exposure |
| Manual scale | ECS auto-scales task count on CPU/memory |

---

*Phase 5 complete — the system now runs entirely on AWS without any laptop
or tunnel dependency.*
