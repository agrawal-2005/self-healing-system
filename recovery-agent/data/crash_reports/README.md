# Crash Reports

This directory holds container log captures taken **before** the recovery-agent
runs `docker stop` on a service that has been escalated to CRITICAL severity.
A crash report contains the last 200 lines of the container's stdout+stderr
along with metadata about the escalation (severity, failure count, reason).

These reports are how a developer diagnoses **why** a service was crashing
when the automated self-healing finally gave up and switched to fallback.

---

## Directory layout

```
crash_reports/
├── incidents/   ← REAL CRITICAL escalations from Lambda (production signal)
└── tests/       ← Manual test runs (synthetic, ignored by alerts)
```

### `incidents/` — real production failures

Files here are written when Lambda has decided a service is critically unstable
(typically 5+ crashes within 10 minutes) and called `enable_fallback`.
**This is the directory developers and on-call engineers should care about.**

A new file in `incidents/` means:
- Auto-healing has stopped trying to restart the service
- Fallback is now serving traffic in degraded mode
- A human needs to investigate the root cause from the captured logs

Treat these like incident reports — review them, attach them to post-mortems,
keep them around for trend analysis.

### `tests/` — synthetic/manual triggers

Files here are written when the request's `reason` field starts with `[TEST]`
(used by `tests/scripts/crash_report_capture_test.sh` and other dev tooling).
**Alerting and dashboards should never watch this folder.**

Safe to clean these up periodically:
```bash
# Remove test reports older than 7 days
find tests/ -type f -mtime +7 -delete
```

---

## File naming

```
<service-name>_<UTC-timestamp>.txt
```

Example: `core-service_2026-04-30T22-31-05Z.txt`

The timestamp is the moment the report was captured (just before `docker stop`).

---

## How a developer uses this

```bash
# 1. List recent real incidents
ls -lt incidents/ | head -10

# 2. Read the latest one
cat "$(ls -t incidents/*.txt | head -1)"

# 3. Search incidents for a specific error pattern
grep -l "OutOfMemory" incidents/*.txt
grep -l "OperationalError" incidents/*.txt
```

The metadata block at the top tells you severity, failure count, and the
escalation reason. The log block underneath shows the actual stack trace,
exception, or runtime error that caused the repeated crashes.

---

## Routing rule (for reference)

The recovery-agent routes a report based on the `reason` field of the
incoming `enable_fallback` request:

| `reason` starts with | Goes to |
|---|---|
| `[TEST]` (case-insensitive) | `tests/` |
| anything else (including Lambda's reasons) | `incidents/` |

Lambda always sets a real reason like `"Lambda triggered by crash on core-service"`,
so production traffic always lands in `incidents/`.
