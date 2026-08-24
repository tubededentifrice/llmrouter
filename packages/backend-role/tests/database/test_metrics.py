"""PostgreSQL tests for metrics and the small administrator health summary."""
# ruff: noqa: ANN401, PLR0915, PLR2004

from __future__ import annotations

import concurrent.futures
import importlib
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

import psycopg
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Any

    import pytest
    from llmrouter_backend.object_store import ObjectStore


class FailingHealthObjectStore:
    """Fail one object-store health check without control details."""

    def __init__(
        self,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        """Set optional controls for a deterministic dependency wait."""
        self.entered = entered
        self.release = release

    def healthy(self) -> bool:
        """Simulate a private dependency failure."""
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=2)
        message = "private object-store credential"
        raise RuntimeError(message)


def test_metrics_query_global_media_counts_and_health_stays_small(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report global operational facts and only six health components."""
    digest = tmp_path / "digest"
    encryption = tmp_path / "encryption"
    digest.write_text("d" * 64, encoding="utf-8")
    encryption.write_text("e" * 64, encoding="utf-8")
    settings = Settings(
        administrator_digest_key_file=digest,
        administrator_encryption_key_file=encryption,
    )
    controls = ControlKeys.load(settings)
    session = new_token()
    csrf = new_token()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        migrate(connection)
        service = connection.execute(
            """INSERT INTO router.services (api_name, display_name)
               VALUES ('metrics', 'Metrics') RETURNING id"""
        ).fetchone()
        assert service is not None
        workspace = connection.execute(
            """INSERT INTO router.workspaces
                   (service_id, api_name, display_name)
               VALUES (%s, 'main', 'Main') RETURNING id""",
            (service["id"],),
        ).fetchone()
        assert workspace is not None
        connection.execute(
            """INSERT INTO router.media_jobs
                   (service_id, workspace_id, provider_model_api_name, kind,
                    state, payload, deadline_at)
               VALUES (%s, %s, 'safe-model', 'image', 'pending', '{}',
                       statement_timestamp() + interval '1 hour')""",
            (service["id"], workspace["id"]),
        )
        create_administrator_session(
            connection,
            session_verifier=controls.verifier(session),
            csrf_verifier=controls.verifier(csrf),
            encrypted_csrf_token=controls.encrypt({"csrf_token": csrf}),
            issuer="https://identity.example.test",
            subject="administrator",
            display_name="Administrator",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )

    object_entered = threading.Event()
    object_release = threading.Event()
    application = create_app(
        database_url=database_url,
        settings=settings,
        object_store=cast(
            "ObjectStore", FailingHealthObjectStore(object_entered, object_release)
        ),
    )

    def cleanup_forbidden(*_args: object, **_kwargs: object) -> None:
        message = "Health must not run cleanup."
        raise AssertionError(message)

    backend_app = importlib.import_module("llmrouter_backend.app")
    monkeypatch.setattr(backend_app, "apply_retention_and_cleanup", cleanup_forbidden)
    client = TestClient(application)
    metrics = client.get("/v1/metrics")
    real_connect = backend_app.psycopg.connect
    active_connections = 0
    active_lock = threading.Lock()

    @contextmanager
    def tracked_connect(*args: Any, **kwargs: Any) -> Iterator[psycopg.Connection[Any]]:
        nonlocal active_connections
        connection = real_connect(*args, **kwargs)
        with active_lock:
            active_connections += 1
        try:
            with connection as database:
                yield database
        finally:
            with active_lock:
                active_connections -= 1

    monkeypatch.setattr(backend_app.psycopg, "connect", tracked_connect)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending_health = executor.submit(
            client.get,
            "/v1/admin/health",
            headers={"Cookie": f"llmrouter_admin_session={session}"},
        )
        assert object_entered.wait(timeout=2)
        with active_lock:
            assert active_connections == 0
        object_release.set()
        health = pending_health.result(timeout=2)

    assert metrics.status_code == HTTPStatus.OK
    assert "llmrouter_database_healthy 1" in metrics.text
    assert 'llmrouter_media_jobs{kind="image",state="pending"} 1' in metrics.text
    assert session not in metrics.text
    assert csrf not in metrics.text
    assert health.status_code == HTTPStatus.OK
    document = health.json()
    assert [component["name"] for component in document["components"]] == [
        "web_application",
        "postgresql",
        "object_storage",
        "price_synchronization",
        "log_retention",
        "accounting_rollup",
    ]
    assert document["status"] == "unavailable"
    assert document["components"][2] == {
        "name": "object_storage",
        "status": "unavailable",
    }
    assert "private object-store credential" not in health.text

    connect_count = 0

    def fail_second_connection(*args: Any, **kwargs: Any) -> object:
        nonlocal connect_count
        connect_count += 1
        if connect_count == 2:
            message = "private PostgreSQL detail"
            raise psycopg.OperationalError(message)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(backend_app.psycopg, "connect", fail_second_connection)
    failed_snapshot = client.get(
        "/v1/admin/health",
        headers={"Cookie": f"llmrouter_admin_session={session}"},
    )
    assert failed_snapshot.status_code == HTTPStatus.OK
    failed_components = {
        component["name"]: component["status"]
        for component in failed_snapshot.json()["components"]
    }
    assert {
        failed_components["postgresql"],
        failed_components["price_synchronization"],
        failed_components["log_retention"],
        failed_components["accounting_rollup"],
    } == {"unavailable"}
    assert "private PostgreSQL detail" not in failed_snapshot.text


def test_database_connection_limit_includes_unauthed_and_probe_work(
    database_url: str,
) -> None:
    """Reject a new request and keep scrape or readiness probes observational."""
    with psycopg.connect(database_url) as connection:
        migrate(connection)
    application = create_app(
        database_url=database_url,
        settings=Settings(database_concurrency=1),
    )
    held = application.state.database_connections.connect(
        database_url, connect_timeout=2
    )
    try:
        client = TestClient(application)
        rejected = client.get("/v1/workspaces")
        metrics = client.get("/v1/metrics")
        readiness = client.get("/ready")
    finally:
        held.close()

    assert rejected.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert rejected.headers["retry-after"] == "1"
    assert rejected.json()["error"]["code"] == "rate_limited"
    assert metrics.status_code == HTTPStatus.OK
    assert "llmrouter_database_healthy 0" in metrics.text
    assert readiness.status_code == HTTPStatus.SERVICE_UNAVAILABLE
