# Demo Guide — Self-Healing System

> Keep this open during interviews or demos. Walk through Steps 0–5 in order.
> Total time: ~8–10 minutes.

---

## Step 0 — Start EC2 if stopped (~2 min)

```bash
# Check if running
aws ec2 describe-instances --instance-ids i-02a4c8460280a04ab \
  --query "Reservations[0].Instances[0].State.Name" --output text

# If "stopped", start it
aws ec2 start-instances --instance-ids i-02a4c8460280a04ab
```

Wait ~60 seconds. All 6 Docker containers start automatically (`restart: unless-stopped`).

> **Important:** After starting EC2, verify core-service and fallback-service are running:
> ```bash
> curl http://54.224.134.71:8001/health   # should return {"status":"healthy"}
> curl http://54.224.134.71:8002/health   # should return {"status":"healthy"}
> ```
> If either returns an error, run:
> ```bash
> aws ssm send-command --instance-ids i-02a4c8460280a04ab \
>   --document-name "AWS-RunShellScript" \
>   --parameters '{"commands":["cd /home/ubuntu/self-healing-system && docker compose up -d core-service fallback-service"]}'
> ```

---

## Step 1 — Show all services are live (~30 sec)

```bash
EC2=54.224.134.71

curl http://$EC2:8001/health   # core-service     (strategy: restart)
curl http://$EC2:8010/health   # payment-service  (strategy: escalate — critical)
curl http://$EC2:8020/health   # movie-service    (strategy: fallback)
curl http://$EC2:8003/health   # recovery-agent
```

**Say:** *"Six services running in Docker on EC2. The monitor polls all of them every 5 seconds from the host."*

---

## Step 2 — Show normal operation (~30 sec)

```bash
curl http://$EC2:8000/process
# { "source": "core-service", "degraded": false }
```

**Say:** *"Client hits api-service, which routes to core-service. Circuit breaker is CLOSED — everything is healthy."*

---

## Step 3 — Trigger a crash and watch the full pipeline (~3–4 min)

Open **two terminal windows** side by side.

**Terminal 1 — tail monitor logs:**
```bash
# Run this, grab the CommandId, then poll it OR just SSH in
aws ssm send-command \
  --instance-ids i-02a4c8460280a04ab \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["tail -50 /home/ubuntu/monitor.log"]}' \
  --query "Command.CommandId" --output text
```

**Terminal 2 — trigger the crash:**
```bash
curl -X POST http://$EC2:8001/fail
# { "crashed": true }

curl http://$EC2:8000/process
# { "source": "fallback-service", "degraded": true }  ← fallback active immediately
```

**Say:** *"Monitor detected the 503 in under 5 seconds, published an EventBridge event, Lambda fired, recovery-agent ran docker restart. The client never got a 503 — fallback took over instantly."*

**After ~30 seconds:**
```bash
curl http://$EC2:8000/process
# { "source": "core-service", "degraded": false }  ← fully healed
```

**Say:** *"Back to normal. Zero human intervention."*

---

## Step 4 — Show the CloudWatch dashboard (~2 min)

Open in browser:
```
https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=SelfHealingSystemDashboard
```

Scroll top to bottom and point to each section:

| Section | What to say |
|---|---|
| **Failure Detected** | *"That spike is the crash we just triggered. Detected by the monitor."* |
| **Recovery Outcomes** | *"Green dot = successful docker restart. Zero red = never failed to recover."* |
| **Circuit Breaker State** | *"Hit 2 (OPEN) during the crash, back to 0 (CLOSED) after recovery."* |
| **Lambda DLQ** | *"No dead letters — Lambda handled every single event successfully."* |
| **Phase 6 — Severity** | *"1st crash = LOW. 3 crashes in 5 min = HIGH. 5 in 10 min = CRITICAL → fallback."* |
| **Phase 7 — Multi-Service** | *"Three services, three strategies. Lambda picks the right one automatically."* |

---

## Step 5 — Demo the escalate strategy (~1 min)

```bash
curl -X POST http://$EC2:8010/fail
```

**Say:** *"This is payment-service — marked critical with no fallback. Watch what Lambda does differently."*

Open Lambda logs:
```
AWS Console → CloudWatch → Log groups → /aws/lambda/SelfHealingRecoveryHandler → latest stream
```

You'll see:
```
CRITICAL_SERVICE_NO_FALLBACK: payment-service has no fallback —
upgrading severity LOW → HIGH. Operator intervention may be required.
```

**Say:** *"First failure on core-service = LOW severity, just restart quietly. First failure on payment-service = HIGH immediately, every single time — because it's critical and has no fallback. Same pipeline, different strategy."*

---

## What You're Demonstrating

| Concept | Where it shows |
|---|---|
| Microservices + Docker | 6 containers running on EC2 |
| Health monitoring | Monitor logs — DOWN then UP in <5s |
| Event-driven architecture | EventBridge → Lambda invocation |
| Serverless recovery | Lambda log shows the decision logic |
| Circuit breaker pattern | api-service returns `degraded=true` during crash |
| Severity escalation | CloudWatch Phase 6 widgets |
| Multi-service strategy | payment (escalate) vs movie (fallback) vs core (restart) |
| Observability | 20-widget CloudWatch dashboard in real-time |
| Recovery audit | `recovery_history.jsonl` — append-only log |

---

## The 60-Second Talking Track

> *"This system automatically detects when a service crashes, decides how severe it is, and fixes it — in under 30 seconds, no human needed.*
>
> *The monitor polls /health every 5 seconds. When it sees a failure, it publishes an event to AWS EventBridge. EventBridge triggers a Lambda function that uses a SmartRecoveryPolicy — a severity ladder — to decide the right action. For a first crash it restarts the container. After 5 crashes in 10 minutes it stops trusting restarts and routes traffic to a fallback instead.*
>
> *In Phase 7, I extended this to multiple services where each one carries its own recovery strategy — payment-service always escalates to HIGH severity because it's critical with no fallback, movie-service routes to a shared fallback on CRITICAL. All of this is driven by a single JSON registry file — you can add a new service without changing any code.*
>
> *Everything is visible on this CloudWatch dashboard — failures, recovery outcomes, circuit breaker state, severity distribution, per-service breakdowns — 20 widgets in real-time."*

---

## How Services Crash in Real Life

*(Explain this if the interviewer asks "why does this matter?" or "where would this be used?")*

### The three most common real-world crash types

#### 1. Traffic spike
A popular event causes sudden 10x–100x traffic — way more than servers can handle.

- **Netflix:** Stranger Things Season 5 drops. 50 million people hit play at the same time. The recommendation service, which normally handles 5M requests/min, gets 40M. It runs out of memory and crashes.
- **Jio Hotstar:** India vs Pakistan IPL final. Every second, millions hit the stream. The CDN edge servers, the auth service, and the payment service (for new subscriptions) all get slammed simultaneously.
- **What self-healing does:** Monitor detects the service going DOWN → Lambda fires → recovery-agent restarts with a clean slate → service comes back. If it keeps crashing (5x in 10 min), it stops trying to restart and routes to a fallback to protect the user.

#### 2. Memory leak / resource exhaustion
A bug causes the service to slowly consume memory or database connections without releasing them. It runs fine for hours, then dies.

- **Real example:** A Node.js microservice at a fintech company held open database connections on every request but never closed them. After ~4 hours, the connection pool hit the limit of 100 connections. Every new request after that got "connection refused." The service appeared healthy (it was running) but was completely broken.
- **What self-healing does:** The health check returns 503 → monitor detects it → pipeline fires → container restart clears all held connections.

#### 3. Cascading failure (the dangerous one)
One service slows down → the service calling it waits → that service's thread pool fills up → it also slows down → chain reaction.

- **Netflix real incident (2012):** Their Cassandra database in one region had latency. Services waiting for Cassandra responses piled up. Eventually the entire US-East region went down for hours — from one database slowdown.
- **Jio Hotstar equivalent:** Payment gateway (Razorpay) is slow. Subscription service keeps retrying. It queues up 10,000 pending requests. It runs out of memory. Now users can't log in because login depends on subscription status.
- **What self-healing does:** The circuit breaker is the key pattern here. After 3 failed calls to core-service, api-service STOPS calling it and immediately routes to fallback. It doesn't keep retrying and doesn't let the failure cascade. After 30 seconds it probes once — if healthy, it resumes. This is exactly how Netflix Hystrix and AWS service meshes work.

#### 4. Bad deployment
New code goes out with a bug. Everything was fine 5 minutes ago.

- **Real example:** A team at Amazon deployed a configuration change that pointed their service at the wrong S3 bucket region. All downstream services that needed those config files started crashing. 40 minutes of partial outage before the rollback.
- **What self-healing does:** RollbackManager (Phase 6) records the last-known-good image tag. When CRITICAL severity is reached after repeated failures post-deployment, it logs a rollback recommendation. A future phase would automatically execute `docker pull <previous-tag>` and redeploy.

### Why this matters at scale

| Company | Microservices count | Deployments/day | Why self-healing is essential |
|---|---|---|---|
| Netflix | ~1,000 | ~100 | Any manual intervention at this scale takes too long — seconds of downtime = millions of dollars |
| Amazon | ~10,000+ | ~10,000 | Impossible to have a human on-call for every service |
| Jio/Hotstar | ~500+ | ~50–100 | Live sports is unforgiving — 1 minute down during IPL = massive user churn |

> *"The pattern in this project — monitor → event → decide → act — is the same pattern used by Kubernetes liveness probes, AWS Auto Scaling health checks, and Netflix's Chaos Engineering platform. The difference is scale."*
