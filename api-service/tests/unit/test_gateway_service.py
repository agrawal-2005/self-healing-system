"""
Unit tests for GatewayService.

Tests the routing logic: success path, fallback strategy, escalate strategy,
unknown service, and circuit-breaker-open paths.

All HTTP calls are mocked — no network is needed.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.circuit_breaker import CircuitBreaker, CircuitState
from app.services.gateway_service import GatewayService
from app.services.service_registry import ServiceConfig, ServiceRegistry


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_cb(threshold: int = 3) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=threshold,
        recovery_timeout_seconds=30,
        half_open_max_calls=1,
    )


def _make_registry(*services) -> ServiceRegistry:
    return ServiceRegistry(list(services))


def _make_config(name: str, strategy: str = "fallback") -> ServiceConfig:
    return ServiceConfig(
        name=name,
        url=f"http://{name}:8000/endpoint",
        strategy=strategy,
        timeout=2.0,
        circuit_breaker=_make_cb(),
    )


@pytest.fixture
def mock_cw():
    cw = MagicMock()
    cw.record_fallback_used = MagicMock()
    cw.record_circuit_state = MagicMock()
    cw.record_circuit_open  = MagicMock()
    return cw


@pytest.fixture
def gateway(mock_cw):
    registry = _make_registry(
        _make_config("core-service",    strategy="fallback"),
        _make_config("payment-service", strategy="escalate"),
        _make_config("movie-service",   strategy="fallback"),
    )
    return GatewayService(
        registry=registry,
        fallback_url="http://fallback-service:8002/fallback",
        fallback_timeout=2.0,
        service_name="api-service",
        cloudwatch_publisher=mock_cw,
    )


# ── Successful primary call ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_successful_call_returns_source_service(gateway):
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(
            return_value={"message": "ok", "service": "core-service"}
        )
        response = await gateway.call("core-service")

    assert response.source == "core-service"
    assert response.degraded is False


@pytest.mark.asyncio
async def test_successful_payment_call_returns_payment_source(gateway):
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(
            return_value={"message": "payment ok", "service": "payment-service"}
        )
        response = await gateway.call("payment-service")

    assert response.source == "payment-service"
    assert response.degraded is False


@pytest.mark.asyncio
async def test_result_payload_is_forwarded(gateway):
    payload = {"status": "processed", "id": "txn-123"}
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(return_value=payload)
        response = await gateway.call("core-service")

    assert response.result == payload


# ── Unknown service ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_service_raises_key_error(gateway):
    with pytest.raises(KeyError, match="not registered"):
        await gateway.call("non-existent-service")


# ── Fallback strategy on primary failure ──────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_on_primary_failure(gateway, mock_cw):
    fallback_payload = {"message": "degraded", "service": "fallback-service"}

    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        # First call (primary) raises; second call (fallback) succeeds
        MockClient.return_value.call = AsyncMock(
            side_effect=[Exception("connection refused"), fallback_payload]
        )
        response = await gateway.call("core-service")

    assert response.source == "fallback-service"
    assert response.degraded is True
    mock_cw.record_fallback_used.assert_called_once_with("core-service")


@pytest.mark.asyncio
async def test_movie_service_falls_back(gateway, mock_cw):
    fallback_payload = {"message": "fallback", "service": "fallback-service"}
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(
            side_effect=[Exception("timeout"), fallback_payload]
        )
        response = await gateway.call("movie-service")

    assert response.source == "fallback-service"
    assert response.degraded is True


# ── Escalate strategy on primary failure ──────────────────────────────────────

@pytest.mark.asyncio
async def test_escalate_on_payment_failure_raises_runtime_error(gateway):
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(
            side_effect=Exception("payment-service unavailable")
        )
        with pytest.raises(RuntimeError, match="strategy=escalate"):
            await gateway.call("payment-service")


@pytest.mark.asyncio
async def test_escalate_does_not_call_fallback(gateway, mock_cw):
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(
            side_effect=Exception("payment down")
        )
        with pytest.raises(RuntimeError):
            await gateway.call("payment-service")

    mock_cw.record_fallback_used.assert_not_called()


# ── Circuit breaker OPEN path ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_open_triggers_fallback_for_fallback_service(mock_cw):
    """When circuit is already OPEN, fallback strategy kicks in without calling primary."""
    config = _make_config("core-service", strategy="fallback")
    # Pre-open the circuit
    for _ in range(3):
        config.circuit_breaker.record_failure()
    assert config.circuit_breaker.state == CircuitState.OPEN

    registry = _make_registry(config)
    gw = GatewayService(
        registry=registry,
        fallback_url="http://fallback-service:8002/fallback",
        fallback_timeout=2.0,
        service_name="api-service",
        cloudwatch_publisher=mock_cw,
    )

    fallback_payload = {"message": "degraded", "service": "fallback-service"}
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(return_value=fallback_payload)
        response = await gw.call("core-service")

    assert response.source == "fallback-service"
    assert response.degraded is True


@pytest.mark.asyncio
async def test_circuit_open_triggers_escalate_raises(mock_cw):
    """When circuit OPEN for escalate service → RuntimeError immediately."""
    config = _make_config("payment-service", strategy="escalate")
    for _ in range(3):
        config.circuit_breaker.record_failure()

    registry = _make_registry(config)
    gw = GatewayService(
        registry=registry,
        fallback_url="http://fallback-service:8002/fallback",
        fallback_timeout=2.0,
        service_name="api-service",
        cloudwatch_publisher=mock_cw,
    )

    with pytest.raises(RuntimeError, match="strategy=escalate"):
        await gw.call("payment-service")


# ── Circuit breaker isolation ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_movie_failure_does_not_affect_payment_circuit(mock_cw):
    """Tripping movie-service circuit does NOT affect payment-service circuit."""
    movie_cfg   = _make_config("movie-service",   strategy="fallback")
    payment_cfg = _make_config("payment-service", strategy="escalate")

    # Open movie-service circuit
    for _ in range(3):
        movie_cfg.circuit_breaker.record_failure()

    registry = _make_registry(movie_cfg, payment_cfg)
    gw = GatewayService(
        registry=registry,
        fallback_url="http://fallback-service:8002/fallback",
        fallback_timeout=2.0,
        service_name="api-service",
        cloudwatch_publisher=mock_cw,
    )

    # payment-service should still succeed
    with patch("app.services.gateway_service.GenericServiceClient") as MockClient:
        MockClient.return_value.call = AsyncMock(
            return_value={"message": "payment ok", "service": "payment-service"}
        )
        response = await gw.call("payment-service")

    assert response.source == "payment-service"
    assert response.degraded is False


# ── health() ──────────────────────────────────────────────────────────────────

def test_health_returns_healthy(gateway):
    h = gateway.health()
    assert h.status == "healthy"
    assert h.service == "api-service"
