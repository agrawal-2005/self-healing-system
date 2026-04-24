"""
Phase 1 Health Monitor
----------------------
Pings the /health endpoint of each service every CHECK_INTERVAL seconds.
Measures response latency and prints colour-coded logs to the terminal.
No AWS integration — local only.
"""

import time
import os
import logging
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [monitor] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
SERVICES = {
    "api-service":      os.getenv("API_SERVICE_URL",      "http://localhost:8000"),
    "core-service":     os.getenv("CORE_SERVICE_URL",     "http://localhost:8001"),
    "fallback-service": os.getenv("FALLBACK_SERVICE_URL", "http://localhost:8002"),
}

CHECK_INTERVAL   = int(float(os.getenv("CHECK_INTERVAL",   "5")))   # seconds between rounds
LATENCY_WARN_MS  = int(float(os.getenv("LATENCY_WARN_MS",  "500"))) # warn if response > this
REQUEST_TIMEOUT  = float(os.getenv("REQUEST_TIMEOUT", "3.0"))

# Simple ANSI colours (works in most terminals)
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"


def check_service(name: str, base_url: str) -> dict:
    url = f"{base_url}/health"
    start = time.monotonic()
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code == 200:
            status = "UP"
        else:
            status = f"DEGRADED (HTTP {resp.status_code})"

    except requests.exceptions.Timeout:
        latency_ms = REQUEST_TIMEOUT * 1000
        status = "TIMEOUT"

    except requests.exceptions.ConnectionError:
        latency_ms = (time.monotonic() - start) * 1000
        status = "DOWN"

    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        status = f"ERROR ({exc})"

    return {"name": name, "url": url, "status": status, "latency_ms": round(latency_ms, 1)}


def log_result(result: dict):
    name       = result["name"]
    status     = result["status"]
    latency_ms = result["latency_ms"]

    if status == "UP":
        if latency_ms > LATENCY_WARN_MS:
            colour = YELLOW
            tag    = "SLOW"
        else:
            colour = GREEN
            tag    = "OK"
    else:
        colour = RED
        tag    = "FAIL"

    logger.info(
        f"{colour}[{tag}]{RESET} {name:20s}  status={status:25s}  latency={latency_ms:.1f}ms"
    )


def run():
    logger.info("Starting health monitor — checking every %ds", CHECK_INTERVAL)
    logger.info("Services: %s", list(SERVICES.keys()))
    logger.info("-" * 70)

    while True:
        print()  # blank line between rounds for readability
        results = [check_service(name, url) for name, url in SERVICES.items()]
        for r in results:
            log_result(r)

        # Summary line
        up_count = sum(1 for r in results if r["status"] == "UP")
        total    = len(results)
        if up_count == total:
            logger.info(f"{GREEN}All {total}/{total} services healthy{RESET}")
        else:
            logger.warning(f"{RED}{up_count}/{total} services healthy — check above for failures{RESET}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
