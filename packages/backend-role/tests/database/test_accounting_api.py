"""PostgreSQL tests for prices, durable accounting, rollups, and statistics."""
# ruff: noqa: D102, D107, E501, EM101, PLC0415, PLR0913, PLR0917, PLR2004, PT012, PT018, S106, TRY003

from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import accounting, create_app
from llmrouter_backend.accounting import (
    AttemptAccountingWrite,
    AttemptPriceSnapshot,
    CallAccountingWrite,
    OpenRouterPriceSource,
    PriceRate,
    UsageAmount,
)
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.models import Price, UnitPriceWrite
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session, create_key
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ADMIN_ORIGIN = "http://127.0.0.1:5174"
DAY = date(2026, 8, 22)
START = datetime(2026, 8, 22, 10, tzinfo=UTC)


@dataclass
class MemoryPriceSource:
    """Return one fixed snapshot and count complete source fetches."""

    rows: dict[str, Price]
    calls: int = 0
    failure: Exception | None = None

    def fetch(self) -> dict[str, Price]:
        """Return a copy or the configured dependency failure."""
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return dict(self.rows)


class AccountingContext:
    """Own two isolated services and one administrator session."""

    def __init__(self, database_url: str, tmp_path: Path) -> None:
        self.database_url = database_url
        digest = tmp_path / "digest"
        encryption = tmp_path / "encryption"
        digest.write_text("d" * 64, encoding="utf-8")
        encryption.write_text("e" * 64, encoding="utf-8")
        self.settings = Settings(
            administrator_digest_key_file=digest,
            administrator_encryption_key_file=encryption,
            allowed_origins=(ADMIN_ORIGIN,),
        )
        self.controls = ControlKeys.load(self.settings)
        self.session = new_token()
        self.csrf = new_token()
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            migrate(connection)
            identities: dict[str, tuple[uuid.UUID, uuid.UUID, str]] = {}
            for name in ("alpha", "beta"):
                service = connection.execute(
                    """INSERT INTO router.services (api_name, display_name)
                       VALUES (%s, %s) RETURNING id""",
                    (name, name.title()),
                ).fetchone()
                assert service is not None
                workspace = connection.execute(
                    """INSERT INTO router.workspaces
                           (service_id, api_name, display_name)
                       VALUES (%s, 'primary', 'Primary') RETURNING id""",
                    (service["id"],),
                ).fetchone()
                assert workspace is not None
                key = create_key(
                    connection,
                    service_id=service["id"],
                    name="runtime",
                    actor_subject="test:setup",
                    control_keys=self.controls,
                )[1]
                identities[name] = (service["id"], workspace["id"], key)
            self.alpha_service, self.alpha_workspace, self.alpha_key = identities[
                "alpha"
            ]
            self.beta_service, self.beta_workspace, self.beta_key = identities["beta"]
            create_administrator_session(
                connection,
                session_verifier=self.controls.verifier(self.session),
                csrf_verifier=self.controls.verifier(self.csrf),
                encrypted_csrf_token=self.controls.encrypt({"csrf_token": self.csrf}),
                issuer="https://identity.example.test",
                subject="administrator",
                display_name="Administrator",
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            )
            _insert_catalog(connection)

    @property
    def admin_write_headers(self) -> dict[str, str]:
        return {
            "Cookie": f"llmrouter_admin_session={self.session}",
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": self.csrf,
        }

    @property
    def admin_read_headers(self) -> dict[str, str]:
        return {"Cookie": f"llmrouter_admin_session={self.session}"}


@pytest.fixture
def accounting_context(database_url: str, tmp_path: Path) -> AccountingContext:
    """Create the clean accounting schema and two service scopes."""
    return AccountingContext(database_url, tmp_path)


def test_openrouter_fetch_normalizes_supported_positive_prices_once() -> None:
    """Parse the public catalog without accepting zero or unsupported values."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == "https://openrouter.ai/api/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "source-text",
                        "pricing": {
                            "prompt": "0.00000125",
                            "completion": "0.00000250",
                            "request": "0",
                            "unsupported": "99",
                        },
                    }
                ]
            },
        )

    result = OpenRouterPriceSource(httpx.MockTransport(handler)).fetch()
    assert calls == 1
    assert result["source-text"].model_dump(mode="json", exclude_none=True) == {
        "currency": "USD",
        "unit_prices": [
            {"unit": "input_token", "amount": "0.00000125"},
            {"unit": "output_token", "amount": "0.0000025"},
        ],
    }


def test_openrouter_fetch_has_one_total_stream_deadline_without_sleep() -> None:
    """Stop a slow-drip stream even when each read operation can still finish."""
    clock_values = iter((0.0, 0.0, 1.0, 2.0, 3.0, 29.0, 31.0))

    def monotonic_clock() -> float:
        return next(clock_values)

    class SlowDripStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'{"data":'
            yield b"[]}"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=SlowDripStream(),
        )

    source = OpenRouterPriceSource(
        httpx.MockTransport(handler),
        monotonic_clock=monotonic_clock,
    )
    with pytest.raises(TimeoutError, match="deadline"):
        source.fetch()


def test_synchronization_fetches_each_source_once_and_preserves_failures(
    accounting_context: AccountingContext,
) -> None:
    """Apply canonical and mapping authority without erasing the last price."""
    openrouter = MemoryPriceSource(
        {
            "source-text": _price(
                "USD", input_token="0.00000125", output_token="0.0000025"
            ),
        }
    )
    wavespeed = MemoryPriceSource({"source-media": _price("EUR", image="0.125")})
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        _sync_id, first = accounting.synchronize_prices(
            connection,
            sources={"openrouter": openrouter, "wavespeed": wavespeed},
            now=START,
        )
    assert openrouter.calls == 1
    assert wavespeed.calls == 1
    assert [(item.provider_model_api_name, item.outcome) for item in first.items] == [
        ("source-a", "updated"),
        ("source-b", "updated"),
        ("source-media", "updated"),
    ]
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        _sync_id, selected = accounting.synchronize_prices(
            connection,
            sources={"openrouter": openrouter, "wavespeed": wavespeed},
            provider_model_api_names=["source-a"],
            now=START + timedelta(minutes=30),
        )
    assert openrouter.calls == 2
    assert [item.provider_model_api_name for item in selected.items] == [
        "source-a",
        "source-b",
    ]
    assert {item.outcome for item in selected.items} == {"unchanged"}

    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name, enabled,
                    input_modalities, output_modalities, capabilities, manual_price)
               SELECT 'manual-override', provider.id, model.id, 'manual-override',
                      true, ARRAY['text'], ARRAY['text'], '{}',
                      '{"currency":"USD","unit_prices":[{"unit":"input_token","amount":"9"}]}'
               FROM router.provider_connections AS provider
               CROSS JOIN router.canonical_models AS model
               WHERE provider.api_name = 'fake-source'
                 AND model.api_name = 'text-model'"""
        )
        canonical_before = connection.execute(
            """SELECT synchronized_price FROM router.canonical_models
               WHERE api_name = 'text-model'"""
        ).fetchone()
        assert canonical_before is not None
        openrouter.rows = {
            "source-text": _price("USD", input_token="7", output_token="8")
        }
        _sync_id, manual = accounting.synchronize_prices(
            connection,
            sources={"openrouter": openrouter, "wavespeed": wavespeed},
            provider_model_api_names=["manual-override"],
            now=START + timedelta(minutes=45),
        )
        canonical_after = connection.execute(
            """SELECT synchronized_price FROM router.canonical_models
               WHERE api_name = 'text-model'"""
        ).fetchone()
    assert [(item.provider_model_api_name, item.outcome) for item in manual.items] == [
        ("manual-override", "failed")
    ]
    assert canonical_after == canonical_before
    assert openrouter.calls == 2

    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        usd = accounting.effective_price_snapshot(connection, "source-a")
        eur = accounting.effective_price_snapshot(connection, "source-media")
        assert usd is not None and usd.currency == "USD"
        assert eur is not None and eur.currency == "EUR"
        openrouter.rows = {}
        wavespeed.failure = TimeoutError("Bearer private-upstream-control")
        _sync_id, second = accounting.synchronize_prices(
            connection,
            sources={"openrouter": openrouter, "wavespeed": wavespeed},
            now=START + timedelta(hours=1),
        )
        assert accounting.effective_price_snapshot(connection, "source-a") == usd
        assert accounting.effective_price_snapshot(connection, "source-media") == eur
    assert {
        item.outcome
        for item in second.items
        if item.provider_model_api_name.startswith("source-")
    } == {
        "missing",
        "failed",
    }
    assert "private-upstream-control" not in second.model_dump_json()


def test_attempt_snapshots_tags_fallback_and_rollup_are_exact(
    accounting_context: AccountingContext,
) -> None:
    """Keep every fallback attempt, normalized tags, prices, usage, and cost."""
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        accounting.record_call_accounting(
            connection,
            _call(
                accounting_context.alpha_service,
                accounting_context.alpha_workspace,
                tags=("zeta", "alpha", "zeta"),
                attempts=(
                    _attempt("source-a", "failed", "USD", "0.00000125", 100, 0),
                    _attempt("source-b", "succeeded", "USD", "0.00000250", 40, 1),
                ),
            ),
        )
        rows = connection.execute(
            """SELECT call.tags, attempt.outcome, attempt.applied_price, attempt.cost
               FROM router.raw_accounting_calls AS call
               JOIN router.raw_accounting_attempts AS attempt ON attempt.call_id = call.id
               ORDER BY attempt.position"""
        ).fetchall()
        assert rows[0]["tags"] == ["alpha", "zeta"]
        assert [row["outcome"] for row in rows] == ["failed", "succeeded"]
        assert rows[0]["applied_price"]["unit_prices"][0]["amount"] == "0.00000125"
        assert rows[0]["cost"] == Decimal("0.000125")
        assert rows[1]["cost"] == Decimal("0.0001")
        accounting.rollup_day(connection, DAY, now=START + timedelta(days=1))
        first = connection.execute(
            """SELECT provider_model_api_name, outcome, calls, attempts,
                      quantity, cost, currency
               FROM router.daily_accounting ORDER BY provider_model_api_name"""
        ).fetchall()
        accounting.rollup_day(connection, DAY, now=START + timedelta(days=1, minutes=1))
        second = connection.execute(
            """SELECT provider_model_api_name, outcome, calls, attempts,
                      quantity, cost, currency
               FROM router.daily_accounting ORDER BY provider_model_api_name"""
        ).fetchall()
    assert first == second
    assert [(row["calls"], row["attempts"]) for row in first] == [(1, 1), (1, 1)]


def test_rollup_is_concurrent_repeat_safe_and_transactional(
    accounting_context: AccountingContext,
) -> None:
    """Serialize replicas and roll back an incomplete aggregate replacement."""
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        accounting.record_call_accounting(
            connection,
            _call(
                accounting_context.alpha_service,
                accounting_context.alpha_workspace,
                attempts=(_attempt("source-a", "succeeded", "USD", "0.5", 2, 0),),
            ),
        )

    def run_rollup(index: int) -> int:
        with psycopg.connect(
            accounting_context.database_url, row_factory=dict_row
        ) as connection:
            accounting.rollup_day(
                connection,
                DAY,
                now=START + timedelta(days=1, minutes=index),
            )
            row = connection.execute(
                "SELECT count(*) AS count FROM router.daily_accounting"
            ).fetchone()
            assert row is not None
            return cast("int", row["count"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(run_rollup, (1, 2))) == [1, 1]

    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        before = connection.execute("SELECT * FROM router.daily_accounting").fetchall()
        with pytest.raises(RuntimeError), connection.transaction():
            accounting.rollup_day(connection, DAY, now=START + timedelta(days=2))
            raise RuntimeError("force rollback")
        after = connection.execute("SELECT * FROM router.daily_accounting").fetchall()
    assert after == before


def test_late_accounting_reopens_one_completed_day(
    accounting_context: AccountingContext,
) -> None:
    """Include a late durable attempt when the restart catch-up repeats a day."""
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        first = _call(
            accounting_context.alpha_service,
            accounting_context.alpha_workspace,
            attempts=(_attempt("source-a", "succeeded", "USD", "1", 1, 0),),
        )
        accounting.record_call_accounting(connection, first)
        accounting.rollup_day(connection, DAY, now=START + timedelta(days=1))
        second = _call(
            accounting_context.alpha_service,
            accounting_context.alpha_workspace,
            attempts=(_attempt("source-a", "succeeded", "USD", "1", 2, 2),),
        )
        accounting.record_call_accounting(connection, second)
        connection.execute(
            """UPDATE router.raw_accounting_attempts
               SET recorded_at = %s WHERE id = %s""",
            (START, second.attempts[0].id),
        )
        days = accounting.rollup_pending_days(
            connection, now=START + timedelta(days=1, hours=1)
        )
        row = connection.execute(
            """SELECT calls, attempts, quantity, cost
               FROM router.daily_accounting"""
        ).fetchone()
    assert days == (DAY,)
    assert row is not None
    assert (row["calls"], row["attempts"], row["quantity"], row["cost"]) == (
        2,
        2,
        Decimal(3),
        Decimal(3),
    )


def test_statistics_routes_enforce_scope_actor_bounds_and_currencies(
    accounting_context: AccountingContext,
) -> None:
    """Keep service rows isolated and preserve separate currency groups."""
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        accounting.record_call_accounting(
            connection,
            _call(
                accounting_context.alpha_service,
                accounting_context.alpha_workspace,
                attempts=(_attempt("source-a", "succeeded", "USD", "0.25", 2, 0),),
            ),
        )
        accounting.record_call_accounting(
            connection,
            _call(
                accounting_context.beta_service,
                accounting_context.beta_workspace,
                attempts=(_attempt("source-media", "succeeded", "EUR", "0.5", 4, 0),),
            ),
        )
    client = TestClient(
        create_app(
            database_url=accounting_context.database_url,
            settings=accounting_context.settings,
            price_sources={},
        ),
        base_url="https://llmrouter.test",
    )
    query = "from=2026-08-22T00:00:00Z&to=2026-08-23T00:00:00Z&group_by=service"
    alpha = client.get(
        f"/v1/statistics?{query}",
        headers={"Authorization": f"Bearer {accounting_context.alpha_key}"},
    )
    assert alpha.status_code == HTTPStatus.OK
    assert [
        (row["dimensions"], row["currency"]) for row in alpha.json()["buckets"]
    ] == [(["alpha"], "USD")]
    denied_admin = client.get(
        f"/v1/admin/statistics?{query}",
        headers={"Authorization": f"Bearer {accounting_context.alpha_key}"},
    )
    assert denied_admin.status_code == HTTPStatus.UNAUTHORIZED
    denied_service = client.get(
        f"/v1/statistics?{query}", headers=accounting_context.admin_read_headers
    )
    assert denied_service.status_code == HTTPStatus.UNAUTHORIZED
    global_result = client.get(
        f"/v1/admin/statistics?{query}", headers=accounting_context.admin_read_headers
    )
    assert global_result.status_code == HTTPStatus.OK
    assert {
        (row["dimensions"][0], row["currency"])
        for row in global_result.json()["buckets"]
    } == {
        ("alpha", "USD"),
        ("beta", "EUR"),
    }
    too_wide = client.get(
        "/v1/statistics?from=2025-01-01T00:00:00Z&to=2026-08-23T00:00:00Z",
        headers={"Authorization": f"Bearer {accounting_context.alpha_key}"},
    )
    assert too_wide.status_code == HTTPStatus.BAD_REQUEST
    duplicate_group = client.get(
        "/v1/statistics?from=2026-08-22T00:00:00Z&to=2026-08-23T00:00:00Z"
        "&group_by=outcome&group_by=outcome",
        headers={"Authorization": f"Bearer {accounting_context.alpha_key}"},
    )
    assert duplicate_group.status_code == HTTPStatus.BAD_REQUEST


def test_exact_call_marker_cannot_collide_with_an_assignment_name(
    accounting_context: AccountingContext,
) -> None:
    """Keep the valid `exact` assignment separate from an exact selection."""
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        accounting.record_call_accounting(
            connection,
            _call(
                accounting_context.alpha_service,
                accounting_context.alpha_workspace,
                assignment_api_name="exact",
                attempts=(_attempt("source-a", "succeeded", "USD", "1", 1, 0),),
            ),
        )
        accounting.record_call_accounting(
            connection,
            _call(
                accounting_context.alpha_service,
                accounting_context.alpha_workspace,
                assignment_api_name=None,
                attempts=(_attempt("source-a", "succeeded", "USD", "1", 1, 2),),
            ),
        )
        result = accounting.statistics(
            connection,
            from_time=START - timedelta(hours=1),
            to_time=START + timedelta(hours=1),
            group_by=["assignment"],
            service_id=accounting_context.alpha_service,
        )
    assert {bucket.dimensions[0] for bucket in result.buckets} == {"exact", "(exact)"}


def test_admin_price_route_requires_session_csrf_and_never_returns_source_error(
    accounting_context: AccountingContext,
) -> None:
    """Separate service keys from global price authority and keep errors safe."""
    failure = MemoryPriceSource({}, failure=RuntimeError("Bearer source-secret"))
    client = TestClient(
        create_app(
            database_url=accounting_context.database_url,
            settings=accounting_context.settings,
            price_sources={"openrouter": failure, "wavespeed": failure},
        ),
        base_url="https://llmrouter.test",
    )
    path = "/v1/admin/prices/synchronize"
    service_denied = client.post(
        path,
        json={},
        headers={"Authorization": f"Bearer {accounting_context.alpha_key}"},
    )
    assert service_denied.status_code == HTTPStatus.UNAUTHORIZED
    csrf_denied = client.post(
        path, json={}, headers=accounting_context.admin_read_headers
    )
    assert csrf_denied.status_code == HTTPStatus.FORBIDDEN
    result = client.post(path, json={}, headers=accounting_context.admin_write_headers)
    assert result.status_code == HTTPStatus.OK
    assert failure.calls == 2
    assert {item["outcome"] for item in result.json()["items"]} == {"failed"}
    assert "source-secret" not in result.text


def test_manual_prices_reject_unsafe_decimals_and_keep_current_state(
    accounting_context: AccountingContext,
) -> None:
    """Validate canonical and mapping decimal bounds before their transactions."""
    client = TestClient(
        create_app(
            database_url=accounting_context.database_url,
            settings=accounting_context.settings,
            price_sources={},
        ),
        base_url="https://llmrouter.test",
    )
    unsafe_scale = "0.1234567890123456789"
    model_body: dict[str, Any] = {
        "api_name": "manual-model",
        "display_name": "Manual",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "capabilities": [],
        "manual_price": {
            "currency": "GBP",
            "unit_prices": [{"unit": "input_token", "amount": unsafe_scale}],
        },
    }
    rejected_model = client.put(
        "/v1/admin/models/manual-model",
        json=model_body,
        headers=accounting_context.admin_write_headers,
    )
    assert rejected_model.status_code == HTTPStatus.BAD_REQUEST
    current_model = client.get(
        "/v1/admin/models/manual-model",
        headers=accounting_context.admin_read_headers,
    )
    assert current_model.status_code == HTTPStatus.OK
    assert current_model.json()["current_price"]["unit_prices"][0]["amount"] == "0.75"

    mapping_body: dict[str, Any] = {
        "api_name": "manual-model",
        "provider_api_name": "fake-source",
        "model_api_name": "manual-model",
        "provider_model_name": "manual-model",
        "enabled": True,
        "manual_price": {
            "currency": "GBP",
            "unit_prices": [{"unit": "input_token", "amount": "100000000000000000000"}],
        },
    }
    rejected_mapping = client.put(
        "/v1/admin/provider-models/manual-model",
        json=mapping_body,
        headers=accounting_context.admin_write_headers,
    )
    assert rejected_mapping.status_code == HTTPStatus.BAD_REQUEST
    mapping_body["manual_price"]["unit_prices"][0]["amount"] = "0" * 65
    oversized_mapping = client.put(
        "/v1/admin/provider-models/manual-model",
        json=mapping_body,
        headers=accounting_context.admin_write_headers,
    )
    assert oversized_mapping.status_code == HTTPStatus.BAD_REQUEST
    current_mapping = client.get(
        "/v1/admin/provider-models/manual-model",
        headers=accounting_context.admin_read_headers,
    )
    assert current_mapping.status_code == HTTPStatus.OK
    assert (
        current_mapping.json()["effective_price"]["unit_prices"][0]["amount"] == "0.5"
    )

    model_body["manual_price"]["unit_prices"][0]["amount"] = "0"
    accepted_zero = client.put(
        "/v1/admin/models/manual-model",
        json=model_body,
        headers=accounting_context.admin_write_headers,
    )
    assert accepted_zero.status_code == HTTPStatus.OK
    assert accepted_zero.json()["current_price"]["unit_prices"][0]["amount"] == "0"


@pytest.mark.parametrize("manual_hour", [1, 2])
def test_restart_maintenance_retries_dependency_failure_once_per_day(
    accounting_context: AccountingContext,
    manual_hour: int,
) -> None:
    """Do not let an on-demand run suppress the fixed daily run on restart."""
    failure = MemoryPriceSource({}, failure=TimeoutError("private"))
    from llmrouter_backend.app import _run_due_accounting_maintenance

    sources: dict[str, accounting.PriceSource] = {
        "openrouter": failure,
        "wavespeed": failure,
    }
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        accounting.synchronize_prices(
            connection,
            sources=sources,
            now=datetime(2026, 8, 23, manual_hour, 30, tzinfo=UTC),
        )
    _run_due_accounting_maintenance(
        accounting_context.database_url,
        sources,
        now=datetime(2026, 8, 23, 3, tzinfo=UTC),
    )
    _run_due_accounting_maintenance(
        accounting_context.database_url,
        sources,
        now=datetime(2026, 8, 23, 4, tzinfo=UTC),
    )
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        rows = connection.execute(
            """SELECT run_kind, count(*) AS count
               FROM router.price_synchronizations GROUP BY run_kind"""
        ).fetchall()
        assert {row["run_kind"]: row["count"] for row in rows} == {
            "on_demand": 1,
            "scheduled": 1,
        }
        assert (
            accounting.maintenance_health(
                connection, now=datetime(2026, 8, 23, 4, tzinfo=UTC)
            )[0]
            == "degraded"
        )
    assert failure.calls == 4


def test_accounting_rejects_foreign_workspace_missing_price_and_bad_tags(
    accounting_context: AccountingContext,
) -> None:
    """Reject isolation and contract errors without a partial durable call."""
    foreign = _call(
        accounting_context.alpha_service,
        accounting_context.beta_workspace,
        attempts=(_attempt("source-a", "failed", "USD", "1", 1, 0),),
    )
    with psycopg.connect(
        accounting_context.database_url, row_factory=dict_row
    ) as connection:
        with pytest.raises(ValueError, match="workspace"):
            accounting.record_call_accounting(connection, foreign)
        with pytest.raises(ValueError, match="cover"), connection.transaction():
            attempt = AttemptAccountingWrite(
                id=uuid.uuid4(),
                provider_connection_api_name="fake-source",
                provider_model_api_name="source-a",
                outcome="failed",
                usage=(UsageAmount("output_token", Decimal(1)),),
                applied_price=AttemptPriceSnapshot(
                    "USD", (PriceRate("input_token", Decimal(1)),)
                ),
                started_at=START,
                completed_at=START + timedelta(seconds=1),
                failure_class="upstream_failed",
            )
            accounting.record_call_accounting(
                connection,
                CallAccountingWrite(
                    id=uuid.uuid4(),
                    service_id=accounting_context.alpha_service,
                    workspace_id=accounting_context.alpha_workspace,
                    assignment_api_name="default",
                    tags=(),
                    outcome="failed",
                    started_at=START,
                    completed_at=START + timedelta(seconds=1),
                    attempts=(attempt,),
                ),
            )
        count = connection.execute(
            "SELECT count(*) AS count FROM router.raw_accounting_calls"
        ).fetchone()
        assert count is not None and count["count"] == 0
    with pytest.raises(ValueError, match="tags"):
        _call(
            accounting_context.alpha_service,
            accounting_context.alpha_workspace,
            tags=("",),
            attempts=(_attempt("source-a", "failed", "USD", "1", 1, 0),),
        )
    early_attempt = _attempt("source-a", "failed", "USD", "1", 1, 0)
    with pytest.raises(ValueError, match="inside"):
        CallAccountingWrite(
            id=uuid.uuid4(),
            service_id=accounting_context.alpha_service,
            workspace_id=accounting_context.alpha_workspace,
            assignment_api_name="default",
            tags=(),
            outcome="failed",
            started_at=early_attempt.started_at + timedelta(milliseconds=1),
            completed_at=early_attempt.completed_at,
            attempts=(early_attempt,),
        )


def _insert_catalog(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, enabled)
           VALUES ('fake-source', 'Fake source', 'fake', true)"""
    )
    connection.execute(
        """INSERT INTO router.canonical_models
               (api_name, display_name, input_modalities, output_modalities,
                capabilities, price_source, price_lookup_key, manual_price)
           VALUES
               ('text-model', 'Text', ARRAY['text'], ARRAY['text'], '{}',
                'openrouter', 'source-text', NULL),
               ('manual-model', 'Manual', ARRAY['text'], ARRAY['text'], '{}',
                NULL, NULL,
                '{"currency":"GBP","unit_prices":[{"unit":"input_token","amount":"0.75"}]}')"""
    )
    connection.execute(
        """INSERT INTO router.provider_models
               (api_name, provider_id, model_id, provider_model_name, enabled,
                input_modalities, output_modalities, capabilities,
                price_source, price_lookup_key, manual_price)
           SELECT value.api_name, provider.id, model.id, value.wire_name, true,
                  ARRAY['text'], ARRAY['text'], '{}', value.price_source,
                  value.lookup_key, value.manual_price
           FROM (VALUES
               ('source-a', 'text-model', 'source-a', NULL, NULL, NULL::jsonb),
               ('source-b', 'text-model', 'source-b', NULL, NULL, NULL::jsonb),
               ('source-media', 'text-model', 'source-media', 'wavespeed',
                'source-media', NULL::jsonb),
               ('manual-model', 'manual-model', 'manual-model', NULL, NULL,
                '{"currency":"GBP","unit_prices":[{"unit":"input_token","amount":"0.5"}]}'::jsonb)
           ) AS value(api_name, model_name, wire_name, price_source, lookup_key, manual_price)
           JOIN router.provider_connections AS provider ON provider.api_name = 'fake-source'
           JOIN router.canonical_models AS model ON model.api_name = value.model_name"""
    )


def _price(currency: str, **values: str) -> Price:
    return Price(
        currency=currency,
        unit_prices=[
            UnitPriceWrite(unit=cast("Any", unit), amount=amount)
            for unit, amount in values.items()
        ],
    )


def _attempt(
    provider_model: str,
    outcome: str,
    currency: str,
    rate: str,
    quantity: int,
    offset: int,
) -> AttemptAccountingWrite:
    started = START + timedelta(seconds=offset)
    return AttemptAccountingWrite(
        id=uuid.uuid4(),
        provider_connection_api_name="fake-source",
        provider_model_api_name=provider_model,
        outcome=outcome,
        usage=(UsageAmount("input_token", Decimal(quantity)),),
        applied_price=AttemptPriceSnapshot(
            currency,
            (PriceRate("input_token", Decimal(rate)),),
            source="test",
            synchronized_at=START,
        ),
        started_at=started,
        completed_at=started + timedelta(seconds=1),
        failure_class="upstream_failed" if outcome == "failed" else None,
    )


def _call(
    service_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    tags: tuple[str, ...] = ("daily",),
    assignment_api_name: str | None = "default",
    attempts: tuple[AttemptAccountingWrite, ...],
) -> CallAccountingWrite:
    return CallAccountingWrite(
        id=uuid.uuid4(),
        service_id=service_id,
        workspace_id=workspace_id,
        assignment_api_name=assignment_api_name,
        tags=tags,
        outcome="succeeded" if attempts[-1].outcome == "succeeded" else "failed",
        started_at=attempts[0].started_at,
        completed_at=attempts[-1].completed_at,
        attempts=attempts,
    )
