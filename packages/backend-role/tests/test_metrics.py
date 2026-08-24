"""Tests for bounded metrics, deployment limits, and overload errors."""

from __future__ import annotations

import concurrent.futures
import threading
from decimal import Decimal
from http import HTTPStatus
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.config import Settings
from llmrouter_backend.metrics import MetricsRegistry

_DATABASE_PROBE_LIMIT = 2
_EXPECTED_PROVIDER_MODEL_SERIES = 1_025


def test_metrics_have_fixed_safe_dimensions_and_survive_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose required facts without request, identity, or control values."""
    registry = MetricsRegistry()
    assert registry.try_admit_database_request(1)
    assert not registry.try_admit_database_request(1)
    registry.release_database_request()
    registry.set_call_saturation(1, 4)
    registry.reject_call("model")
    registry.observe_request("model", "succeeded", 0.25)
    registry.observe_attempt(
        kind="model",
        provider_model="safe-model",
        outcome="succeeded",
        duration=0.2,
        usage=(("input_token", Decimal(3)),),
        cost=Decimal("0.125"),
        currency="USD",
    )

    def unavailable(*_args: object, **_kwargs: object) -> None:
        message = "private database credential"
        raise psycopg.OperationalError(message)

    monkeypatch.setattr("llmrouter_backend.metrics.psycopg.connect", unavailable)
    body = registry.render(
        database_url="postgresql://control-secret@database/private",
        cooldowns=(("safe-model", 10.0, "timeout"),),
    )

    required = {
        "llmrouter_requests_total",
        "llmrouter_attempts_total",
        "llmrouter_request_duration_seconds_bucket",
        "llmrouter_attempt_duration_seconds_bucket",
        "llmrouter_usage_units_total",
        "llmrouter_cost_total",
        "llmrouter_provider_model_cooldown_seconds",
        "llmrouter_media_jobs",
        "llmrouter_database_healthy 0",
        "llmrouter_saturation_active",
        "llmrouter_admission_rejections_total",
    }
    assert all(value in body for value in required)
    assert 'provider_model="safe-model"' in body
    assert 'failure_class="timeout"' in body
    assert "control-secret" not in body
    assert "private database credential" not in body
    assert "service" not in {
        label.split("=")[0]
        for line in body.splitlines()
        if not line.startswith("#") and "{" in line
        for label in line.split("{", 1)[1].split("}", 1)[0].split(",")
    }


def test_metrics_bound_database_probes_and_historical_model_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep scrape dependencies and deleted-model label churn finite."""
    registry = MetricsRegistry()
    entered = 0
    entered_lock = threading.Lock()
    two_entered = threading.Event()
    release = threading.Event()

    def blocked_snapshot(
        _database_url: str | None,
    ) -> tuple[dict[tuple[str, str], int], bool]:
        nonlocal entered
        with entered_lock:
            entered += 1
            if entered == _DATABASE_PROBE_LIMIT:
                two_entered.set()
        assert release.wait(timeout=2)
        return {}, True

    monkeypatch.setattr(
        "llmrouter_backend.metrics._database_snapshot", blocked_snapshot
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(registry.render, database_url="test", cooldowns=())
        second = executor.submit(registry.render, database_url="test", cooldowns=())
        assert two_entered.wait(timeout=2)
        overflow = executor.submit(
            registry.render, database_url="test", cooldowns=()
        ).result(timeout=1)
        release.set()
        assert "llmrouter_database_healthy 1" in first.result(timeout=2)
        assert "llmrouter_database_healthy 1" in second.result(timeout=2)
    assert entered == _DATABASE_PROBE_LIMIT
    assert "llmrouter_database_healthy 0" in overflow

    for index in range(1_050):
        registry.observe_attempt(
            kind="model",
            provider_model=f"model-{index}",
            outcome="succeeded",
            duration=0.1,
            usage=(("input_token", Decimal("1e9999")),),
            cost=Decimal("1e9999"),
            currency="USD",
        )
    body = registry.render(database_url=None, cooldowns=())
    attempt_series = [
        line
        for line in body.splitlines()
        if line.startswith("llmrouter_attempts_total{")
    ]
    assert len(attempt_series) == _EXPECTED_PROVIDER_MODEL_SERIES
    assert any('provider_model="(other)"' in line for line in attempt_series)
    for line in body.splitlines():
        if line and not line.startswith("#"):
            assert line.rsplit(" ", 1)[1] not in {"Inf", "+Inf", "-Inf", "NaN"}


def test_public_metrics_route_and_database_admission_are_retryable() -> None:
    """Keep scrape access open and reject overloaded database work safely."""
    application = create_app(settings=Settings(database_concurrency=1))
    registry: MetricsRegistry = application.state.metrics
    assert registry.try_admit_database_request(1)
    try:
        client = TestClient(application)
        rejected = client.get("/v1/workspaces")
        metrics = client.get("/v1/metrics")
    finally:
        registry.release_database_request()

    assert rejected.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert rejected.headers["retry-after"] == "1"
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.json() == {
        "error": {
            "code": "rate_limited",
            "message": "The Router concurrency limit is full.",
        }
    }
    assert metrics.status_code == HTTPStatus.OK
    assert metrics.headers["content-type"] == (
        "text/plain; version=0.0.4; charset=utf-8"
    )
    assert "llmrouter_database_healthy 0" in metrics.text


@pytest.mark.parametrize(
    ("name", "values", "message"),
    [
        (
            "provider timeout",
            {"provider_attempt_timeout_seconds": 601},
            "provider-attempt timeout",
        ),
        (
            "connection timeout",
            {"call_connection_timeout_seconds": 901},
            "connection timeout",
        ),
        ("call concurrency", {"call_concurrency": 0}, "Call concurrency"),
        (
            "database concurrency",
            {"database_concurrency": 100_001},
            "Database concurrency",
        ),
        (
            "Boolean provider timeout",
            {"provider_attempt_timeout_seconds": True},
            "provider-attempt timeout",
        ),
        (
            "float connection timeout",
            {"call_connection_timeout_seconds": 1.5},
            "connection timeout",
        ),
        (
            "Boolean media timeout",
            {"media_job_deadline_seconds": True},
            "media-job deadline",
        ),
    ],
)
def test_deployment_call_limits_are_bounded(
    name: str, values: dict[str, object], message: str
) -> None:
    """Reject each unsafe deployment limit before the application starts."""
    del name
    with pytest.raises(ValueError, match=message):
        Settings(**cast("Any", values))


def test_deployment_call_limits_load_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load each explicit call and database bound from deployment controls."""
    values = {
        "LLMROUTER_PROVIDER_ATTEMPT_TIMEOUT_SECONDS": "30",
        "LLMROUTER_CALL_CONNECTION_TIMEOUT_SECONDS": "120",
        "LLMROUTER_CALL_CONCURRENCY": "8",
        "LLMROUTER_DATABASE_CONCURRENCY": "16",
        "LLMROUTER_MEDIA_JOB_DEADLINE_SECONDS": "600",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_environment()
    assert (
        settings.provider_attempt_timeout_seconds,
        settings.call_connection_timeout_seconds,
        settings.call_concurrency,
        settings.database_concurrency,
        settings.media_job_deadline_seconds,
    ) == (30, 120, 8, 16, 600)
