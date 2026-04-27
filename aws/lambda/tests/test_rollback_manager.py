"""
Unit tests for RollbackManager (dry-run rollback prototype).
"""
import pytest

import rollback_manager as rm_module
from rollback_manager import RollbackManager
from smart_recovery_policy import IncidentSeverity


@pytest.fixture(autouse=True)
def reset_rollback_state():
    """Clear module-level state before and after every test."""
    rm_module._rollback_state.clear()
    yield
    rm_module._rollback_state.clear()


@pytest.fixture
def rm() -> RollbackManager:
    return RollbackManager()


# ── Baseline recording ─────────────────────────────────────────────────────────

def test_record_baseline_stores_image_ref(rm):
    image_ref = rm.record_baseline("core-service")
    assert "core-service" in image_ref


def test_record_baseline_returns_service_name_and_tag(rm, monkeypatch):
    monkeypatch.setattr(rm_module, "_IMAGE_TAG", "v1.2.3")
    image_ref = rm.record_baseline("core-service")
    assert image_ref == "core-service:v1.2.3"


# ── should_recommend ───────────────────────────────────────────────────────────

def test_no_recommendation_without_baseline(rm):
    assert not rm.should_recommend("core-service", IncidentSeverity.CRITICAL)


def test_no_recommendation_for_low_severity(rm):
    rm.record_baseline("core-service")
    assert not rm.should_recommend("core-service", IncidentSeverity.LOW)


def test_no_recommendation_for_medium_severity(rm):
    rm.record_baseline("core-service")
    assert not rm.should_recommend("core-service", IncidentSeverity.MEDIUM)


def test_no_recommendation_for_high_severity(rm):
    rm.record_baseline("core-service")
    assert not rm.should_recommend("core-service", IncidentSeverity.HIGH)


def test_recommendation_on_critical_with_baseline(rm):
    rm.record_baseline("core-service")
    assert rm.should_recommend("core-service", IncidentSeverity.CRITICAL)


# ── recommend_rollback ─────────────────────────────────────────────────────────

def test_recommend_rollback_returns_image_ref(rm):
    rm.record_baseline("core-service")
    image_ref = rm.recommend_rollback("core-service")
    assert image_ref is not None
    assert "core-service" in image_ref


def test_recommend_rollback_no_baseline_returns_none(rm):
    result = rm.recommend_rollback("core-service")
    assert result is None


def test_recommend_rollback_logs_rollback_recommended(rm, caplog):
    rm.record_baseline("core-service")
    with caplog.at_level("WARNING"):
        rm.recommend_rollback("core-service")
    assert "ROLLBACK_RECOMMENDED" in caplog.text


def test_recommend_rollback_logs_dry_run(rm, caplog):
    rm.record_baseline("core-service")
    with caplog.at_level("WARNING"):
        rm.recommend_rollback("core-service")
    assert "dry-run" in caplog.text


# ── Successful recovery tracking ───────────────────────────────────────────────

def test_successful_recovery_increments_counter(rm):
    rm.record_successful_recovery("core-service")
    rm.record_successful_recovery("core-service")
    entry = rm_module._rollback_state["core-service"]
    assert entry.successful_recovery_count == 2


def test_successful_recovery_different_services_independent(rm):
    rm.record_successful_recovery("core-service")
    rm.record_successful_recovery("other-service")
    rm.record_successful_recovery("other-service")
    assert rm_module._rollback_state["core-service"].successful_recovery_count == 1
    assert rm_module._rollback_state["other-service"].successful_recovery_count == 2


# ── End-to-end flow ────────────────────────────────────────────────────────────

def test_full_flow_critical_triggers_recommendation(rm):
    """Full usage sequence: baseline → recovery success × 2 → CRITICAL → recommend."""
    rm.record_baseline("core-service")
    rm.record_successful_recovery("core-service")
    rm.record_successful_recovery("core-service")

    # CRITICAL severity now
    assert rm.should_recommend("core-service", IncidentSeverity.CRITICAL)
    image_ref = rm.recommend_rollback("core-service")
    assert image_ref is not None


def test_full_flow_non_critical_no_recommendation(rm):
    rm.record_baseline("core-service")
    rm.record_successful_recovery("core-service")

    assert not rm.should_recommend("core-service", IncidentSeverity.HIGH)
    assert not rm.should_recommend("core-service", IncidentSeverity.MEDIUM)
    assert not rm.should_recommend("core-service", IncidentSeverity.LOW)
