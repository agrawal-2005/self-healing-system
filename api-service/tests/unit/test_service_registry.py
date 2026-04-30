"""
Unit tests for ServiceRegistry.

Tests JSON loading, service filtering (only entries with gateway_url +
gateway_endpoint are registered), strategy validation, and error handling.
"""
import json
import os
import tempfile

import pytest

from app.services.service_registry import ServiceRegistry


def _write_config(data: dict) -> str:
    """Write a services_config dict to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return path


@pytest.fixture
def valid_config_path():
    """Three services: core (fallback), payment (escalate), movie (fallback)."""
    config = {
        "version": "2",
        "services": [
            {
                "service_name": "core-service",
                "gateway_url": "http://core-service:8001",
                "gateway_endpoint": "/work",
                "strategy": "fallback",
                "timeout": 2.0,
            },
            {
                "service_name": "payment-service",
                "gateway_url": "http://payment-service:8010",
                "gateway_endpoint": "/process-payment",
                "strategy": "escalate",
                "timeout": 2.0,
            },
            {
                "service_name": "movie-service",
                "gateway_url": "http://movie-service:8020",
                "gateway_endpoint": "/catalog",
                "strategy": "fallback",
                "timeout": 2.0,
            },
        ],
    }
    path = _write_config(config)
    yield path
    os.unlink(path)


@pytest.fixture
def registry(valid_config_path):
    return ServiceRegistry.from_config_file(
        path=valid_config_path,
        cb_failure_threshold=3,
        cb_recovery_timeout_seconds=30,
        cb_half_open_max_calls=1,
        default_timeout=2.0,
    )


# ── Loading ────────────────────────────────────────────────────────────────────

def test_loads_three_services(registry):
    assert len(registry.names()) == 3


def test_names_contains_all_services(registry):
    assert set(registry.names()) == {"core-service", "payment-service", "movie-service"}


def test_missing_file_returns_empty_registry():
    reg = ServiceRegistry.from_config_file(
        path="/non/existent/path.json",
        cb_failure_threshold=3,
        cb_recovery_timeout_seconds=30,
        cb_half_open_max_calls=1,
        default_timeout=2.0,
    )
    assert reg.names() == []


def test_malformed_json_returns_empty_registry():
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write("{ not valid json")
    try:
        reg = ServiceRegistry.from_config_file(
            path=path,
            cb_failure_threshold=3,
            cb_recovery_timeout_seconds=30,
            cb_half_open_max_calls=1,
            default_timeout=2.0,
        )
        assert reg.names() == []
    finally:
        os.unlink(path)


# ── Filtering — only services with gateway fields are registered ───────────────

def test_services_without_gateway_url_are_skipped():
    config = {
        "services": [
            {
                "service_name": "monitor-only",
                "health_url": "http://localhost:8001/health",
                # no gateway_url, no gateway_endpoint
            },
            {
                "service_name": "core-service",
                "gateway_url": "http://core-service:8001",
                "gateway_endpoint": "/work",
                "strategy": "fallback",
            },
        ]
    }
    path = _write_config(config)
    try:
        reg = ServiceRegistry.from_config_file(
            path=path,
            cb_failure_threshold=3,
            cb_recovery_timeout_seconds=30,
            cb_half_open_max_calls=1,
            default_timeout=2.0,
        )
        assert reg.names() == ["core-service"]
    finally:
        os.unlink(path)


# ── ServiceConfig values ───────────────────────────────────────────────────────

def test_core_service_url_is_base_plus_endpoint(registry):
    cfg = registry.get("core-service")
    assert cfg.url == "http://core-service:8001/work"


def test_payment_service_strategy_is_escalate(registry):
    cfg = registry.get("payment-service")
    assert cfg.strategy == "escalate"


def test_core_service_strategy_is_fallback(registry):
    cfg = registry.get("core-service")
    assert cfg.strategy == "fallback"


def test_timeout_is_loaded_from_config(registry):
    cfg = registry.get("core-service")
    assert cfg.timeout == 2.0


def test_each_service_gets_independent_circuit_breaker(registry):
    core_cb    = registry.get("core-service").circuit_breaker
    payment_cb = registry.get("payment-service").circuit_breaker
    assert core_cb is not payment_cb


# ── get() ──────────────────────────────────────────────────────────────────────

def test_get_known_service_returns_config(registry):
    cfg = registry.get("movie-service")
    assert cfg.name == "movie-service"


def test_get_unknown_service_raises_key_error(registry):
    with pytest.raises(KeyError, match="not registered"):
        registry.get("unknown-service")


# ── Invalid strategy fallback ──────────────────────────────────────────────────

def test_unknown_strategy_defaults_to_fallback():
    config = {
        "services": [
            {
                "service_name": "weird-service",
                "gateway_url": "http://weird:9000",
                "gateway_endpoint": "/data",
                "strategy": "teleport",   # not a valid strategy
            }
        ]
    }
    path = _write_config(config)
    try:
        reg = ServiceRegistry.from_config_file(
            path=path,
            cb_failure_threshold=3,
            cb_recovery_timeout_seconds=30,
            cb_half_open_max_calls=1,
            default_timeout=2.0,
        )
        assert reg.get("weird-service").strategy == "fallback"
    finally:
        os.unlink(path)


# ── default_timeout fallback ───────────────────────────────────────────────────

def test_default_timeout_used_when_entry_has_no_timeout():
    config = {
        "services": [
            {
                "service_name": "svc",
                "gateway_url": "http://svc:9000",
                "gateway_endpoint": "/data",
                "strategy": "fallback",
                # no "timeout" field
            }
        ]
    }
    path = _write_config(config)
    try:
        reg = ServiceRegistry.from_config_file(
            path=path,
            cb_failure_threshold=3,
            cb_recovery_timeout_seconds=30,
            cb_half_open_max_calls=1,
            default_timeout=5.0,
        )
        assert reg.get("svc").timeout == 5.0
    finally:
        os.unlink(path)
