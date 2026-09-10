"""Test the localhost proof seed with isolated PostgreSQL databases."""
# ruff: noqa: PLR0913, PLR2004

from __future__ import annotations

import hashlib
import importlib.util
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

import psycopg
import pytest
from llmrouter_backend.app import _run_due_accounting_maintenance
from llmrouter_backend.assignments import resolve_assignment, validate_all_assignments
from llmrouter_backend.database import migrate
from llmrouter_backend.security import ControlKeys
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def proof(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the tool without opening its deployment or reading control files."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "localhost_proof_seed_test", ROOT / "scripts/prove-localhost.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def controls() -> ControlKeys:
    """Use test-only keys without deployment files."""
    return ControlKeys(digest_key=b"d" * 32, encryption_key=b"e" * 32)


def _snapshot(connection: psycopg.Connection[dict[str, object]]) -> dict[str, str]:
    """Compare all stored rows without putting control values in test output."""
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'router'"
    ).fetchall()
    result = {}
    for table in tables:
        name = str(table["tablename"])
        rows = connection.execute(
            psycopg.sql.SQL(
                "SELECT row_to_json(record)::text AS value FROM router.{} AS record "
                "ORDER BY row_to_json(record)::text"
            ).format(psycopg.sql.Identifier(name))
        ).fetchall()
        result[name] = hashlib.sha256(repr(rows).encode()).hexdigest()
    return result


def _session_snapshot(connection: psycopg.Connection[dict[str, object]]) -> str:
    """Compare the retained session without showing its stored controls."""
    row = connection.execute(
        "SELECT * FROM router.administrator_sessions "
        "WHERE subject = 'existing-test-administrator'"
    ).fetchone()
    assert row is not None
    return hashlib.sha256(repr(row).encode()).hexdigest()


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("display_name", ["Root", "Existing root name"])
def test_seed_preserves_root_and_creates_valid_fake_fixture(
    database_url: str,
    proof: ModuleType,
    controls: ControlKeys,
    display_name: str,
    capsys: pytest.CaptureFixture[str],
    *,
    autocommit: bool,
) -> None:
    """Retain root identity and its empty default while adding the proof tree."""
    with psycopg.connect(
        database_url, autocommit=autocommit, row_factory=dict_row
    ) as connection:
        migrate(connection)
        proof.create_administrator_session(
            connection,
            session_verifier=controls.verifier("existing-test-session"),
            csrf_verifier=controls.verifier("existing-test-csrf"),
            encrypted_csrf_token=controls.encrypt({"csrf_token": "existing-test-csrf"}),
            issuer="https://local-development.invalid",
            subject="existing-test-administrator",
            display_name="Existing test administrator",
            expires_at=proof.datetime.now(tz=proof.UTC) + proof.timedelta(minutes=10),
        )
        session_before = _session_snapshot(connection)
        connection.execute(
            "UPDATE router.services SET display_name = %s WHERE api_name = 'root'",
            (display_name,),
        )
        root = connection.execute("SELECT * FROM router.services").fetchone()
        assert root is not None
        before = _snapshot(connection)
        default = resolve_assignment(
            connection, service_id=root["id"], api_name="default"
        )
        tokens = proof._seed(connection, controls)  # noqa: SLF001
        assert len(tokens) == 4
        assert len(set(tokens)) == 4
        assert (
            connection.execute(
                "SELECT * FROM router.services WHERE api_name = 'root'"
            ).fetchone()
            == root
        )
        assert (
            resolve_assignment(connection, service_id=root["id"], api_name="default")
            == default
        )
        assert default is not None
        assert default.definition_kind == "implicit"
        assert default.effective_chain == ()
        tree = connection.execute(
            """SELECT child.api_name, parent.api_name AS parent
               FROM router.services AS child
               LEFT JOIN router.services AS parent
                   ON parent.id = child.parent_service_id
               ORDER BY child.api_name"""
        ).fetchall()
        assert tree == [
            {"api_name": "alpha", "parent": "root"},
            {"api_name": "alpha-child", "parent": "alpha"},
            {"api_name": "beta", "parent": "root"},
            {"api_name": "root", "parent": None},
        ]
        connection.execute("SELECT router.validate_service_tree()")
        validate_all_assignments(connection, prune_usage=False)
        assert connection.execute(
            "SELECT adapter FROM router.provider_connections"
        ).fetchall() == [{"adapter": "fake"}]
        assert connection.execute(
            "SELECT count(*) AS count FROM router.provider_models"
        ).fetchone() == {"count": 5}
        assert connection.execute(
            "SELECT count(*) AS count FROM router.workspaces"
        ).fetchone() == {"count": 6}
        assert connection.execute(
            "SELECT count(*) AS count FROM router.service_api_keys"
        ).fetchone() == {"count": 3}
        for token, name in zip(
            tokens[:3], ("alpha", "alpha-child", "beta"), strict=True
        ):
            assert connection.execute(
                """SELECT service.api_name FROM router.service_api_keys AS key
                   JOIN router.services AS service ON service.id = key.service_id
                   WHERE key.verifier = %s""",
                (controls.verifier(token),),
            ).fetchone() == {"api_name": name}
        assert connection.execute(
            """SELECT expires_at < now() AS expired FROM router.administrator_sessions
               WHERE session_verifier = %s""",
            (controls.verifier(tokens[3]),),
        ).fetchone() == {"expired": True}
        assert _session_snapshot(connection) == session_before
        after = _snapshot(connection)
        for table in (
            "global_settings",
            "raw_accounting_calls",
            "raw_accounting_attempts",
            "daily_accounting",
            "request_logs",
            "media_jobs",
            "media_objects",
        ):
            assert after[table] == before[table]
        with pytest.raises(SystemExit, match="clean database with its stored root"):
            proof._seed(connection, controls)  # noqa: SLF001
        assert _snapshot(connection) == after
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "state",
    [
        "missing-root",
        "wrong-name",
        "invalid-parent",
        "extra-service",
        "workspace",
        "assignment",
        "provider",
        "activity",
    ],
)
def test_seed_rejects_unclean_state_before_fixture_writes(
    database_url: str,
    proof: ModuleType,
    controls: ControlKeys,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Reject invalid root and existing product data without any fixture write."""
    with psycopg.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        migrate(connection)
        if state in {"missing-root", "wrong-name", "invalid-parent"}:
            migrate(connection, target=1)
            if state == "missing-root":
                connection.execute("DELETE FROM router.services")
            elif state == "wrong-name":
                connection.execute("UPDATE router.services SET api_name = 'other'")
            else:
                connection.execute("SET session_replication_role = replica")
                connection.execute(
                    "UPDATE router.services SET parent_service_id = gen_random_uuid()"
                )
                connection.execute("SET session_replication_role = origin")
        elif state == "extra-service":
            connection.execute(
                """INSERT INTO router.services
                       (api_name, display_name, parent_service_id)
                   SELECT 'existing', 'Existing', id FROM router.services"""
            )
        elif state == "workspace":
            connection.execute(
                """INSERT INTO router.workspaces (service_id, api_name, display_name)
                   SELECT id, 'existing', 'Existing' FROM router.services"""
            )
        elif state == "assignment":
            connection.execute(
                """INSERT INTO router.assignment_definitions (service_id, api_name)
                   SELECT id, 'default' FROM router.services"""
            )
        elif state == "provider":
            connection.execute(
                """INSERT INTO router.provider_connections
                       (api_name, display_name, adapter, enabled)
                   VALUES ('existing', 'Existing', 'fake', true)"""
            )
        else:
            connection.execute(
                """INSERT INTO router.activity_events
                   (actor_subject, action, resource_type, resource_api_name, result)
                   VALUES ('test', 'test', 'service', 'root', 'succeeded')"""
            )
        before = _snapshot(connection)

        def unexpected_write(*_args: object) -> None:
            pytest.fail("The fixture writer must not run for an unclean database.")

        monkeypatch.setattr(proof, "_seed_fixture", unexpected_write)
        with pytest.raises(SystemExit, match="clean database with its stored root"):
            proof._seed(connection, controls)  # noqa: SLF001
        assert _snapshot(connection) == before


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("autocommit", [False, True])
def test_seed_failure_rolls_back_before_caller_can_commit(
    database_url: str,
    proof: ModuleType,
    controls: ControlKeys,
    monkeypatch: pytest.MonkeyPatch,
    *,
    startup: bool,
    autocommit: bool,
) -> None:
    """Roll back all fixture writes even if the caller catches the failure."""
    with psycopg.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        migrate(connection)
        if startup:
            _run_due_accounting_maintenance(
                database_url, {}, now=datetime(2026, 9, 1, 4, 56, tzinfo=UTC)
            )
        before = _snapshot(connection)

    def fail_session(*_args: object, **_kwargs: object) -> None:
        message = "Injected session storage failure."
        raise RuntimeError(message)

    monkeypatch.setattr(proof, "create_administrator_session", fail_session)
    with psycopg.connect(
        database_url, autocommit=autocommit, row_factory=dict_row
    ) as connection:
        if not autocommit:
            connection.execute(
                "UPDATE router.global_settings SET log_retention_days = 8"
            )
            before = _snapshot(connection)
        with pytest.raises(RuntimeError, match="Injected session storage failure"):
            proof._seed(connection, controls)  # noqa: SLF001
        connection.commit()
        assert _snapshot(connection) == before
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        assert _snapshot(connection) == before


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE router.price_synchronizations SET run_kind = 'on_demand'",
        "UPDATE router.price_synchronizations SET completed = false",
        "UPDATE router.price_synchronizations SET failure_class = 'source_failure'",
        """UPDATE router.price_synchronizations
           SET result = '[{"outcome": "unchanged"}]'::jsonb""",
        "DELETE FROM router.activity_events",
        "DELETE FROM router.price_synchronizations",
        "UPDATE router.activity_events SET actor_subject = 'administrator'",
        "UPDATE router.activity_events SET action = 'other'",
        "UPDATE router.activity_events SET resource_type = 'other'",
        "UPDATE router.activity_events SET result = 'failed'",
        "UPDATE router.activity_events SET resource_id = gen_random_uuid()",
        "UPDATE router.activity_events SET service_api_name = 'root'",
        "UPDATE router.activity_events SET resource_api_name = 'root'",
        """INSERT INTO router.activity_events
               (actor_subject, action, resource_type, resource_id, result)
           SELECT actor_subject, action, resource_type, resource_id, result
           FROM router.activity_events""",
        """INSERT INTO router.activity_events
               (actor_subject, action, resource_type, resource_api_name, result)
           VALUES ('other', 'other', 'service', 'root', 'succeeded')""",
        """INSERT INTO router.accounting_rollups
               (day, completed_at, attempt_count, call_count)
           VALUES ('2026-09-01', now(), 0, 0)""",
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, enabled)
           VALUES ('existing', 'Existing', 'fake', true)""",
    ],
    ids=[
        "manual",
        "incomplete",
        "failure-class",
        "nonempty",
        "missing-event",
        "missing-run",
        "foreign-actor",
        "wrong-action",
        "wrong-resource-type",
        "failed-event",
        "unlinked-event",
        "service-context",
        "resource-name",
        "duplicate-event",
        "foreign-event",
        "other-product-data",
        "nonempty-catalog",
    ],
)
def test_seed_rejects_changed_startup_records(
    database_url: str,
    proof: ModuleType,
    controls: ControlKeys,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Reject each changed startup record before any fixture write."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        migrate(connection)
    _run_due_accounting_maintenance(
        database_url, {}, now=datetime(2026, 9, 1, 4, 56, tzinfo=UTC)
    )
    with psycopg.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM router.price_synchronizations"
        ).fetchone() == {"count": 1}
        connection.execute(change)
        before = _snapshot(connection)

        def unexpected_write(*_args: object) -> None:
            pytest.fail("The fixture writer must not run for changed startup records.")

        monkeypatch.setattr(proof, "_seed_fixture", unexpected_write)
        with pytest.raises(SystemExit, match="clean database with its stored root"):
            proof._seed(connection, controls)  # noqa: SLF001
        assert _snapshot(connection) == before


def _wait_for_database_block(
    connection: psycopg.Connection[dict[str, object]], application_name: str
) -> None:
    """Wait for a measured database lock, with a fixed failure deadline."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        blocked = connection.execute(
            """SELECT 1 FROM pg_stat_activity
               WHERE datname = current_database() AND application_name = %s
                 AND cardinality(pg_blocking_pids(pid)) > 0""",
            (application_name,),
        ).fetchone()
        if blocked is not None:
            return
        time.sleep(0.005)
    pytest.fail("The concurrent transaction did not reach its database lock.")


@pytest.mark.parametrize("scheduler_first", [False, True])
def test_seed_serializes_with_real_startup_maintenance(
    database_url: str,
    proof: ModuleType,
    controls: ControlKeys,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scheduler_first: bool,
) -> None:
    """Commit both transactions in either order without a partial startup pair."""
    ready, release = Event(), Event()
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        migrate(connection)

    class PausedConnection(psycopg.Connection):
        def commit(self) -> None:
            if scheduler_first:
                ready.set()
                assert release.wait(5), "The seed did not wait for maintenance."
            super().commit()

    def connect_maintenance(*args: object, **kwargs: object) -> psycopg.Connection:
        return PausedConnection.connect(
            *args, **kwargs, application_name="proof-maintenance"
        )

    def run_maintenance() -> None:
        _run_due_accounting_maintenance(
            database_url,
            {},
            database_connect=connect_maintenance,
            now=datetime(2026, 9, 1, 2, 56, tzinfo=UTC),
        )

    fixture_writer = proof._seed_fixture  # noqa: SLF001

    def paused_fixture(*args: object) -> tuple[str, str, str, str]:
        if not scheduler_first:
            ready.set()
            assert release.wait(5), "Maintenance did not wait for the seed."
        return fixture_writer(*args)

    monkeypatch.setattr(proof, "_seed_fixture", paused_fixture)

    def run_seed() -> None:
        with psycopg.connect(
            database_url, row_factory=dict_row, application_name="proof-seed"
        ) as connection:
            assert len(proof._seed(connection, controls)) == 4  # noqa: SLF001

    with (
        psycopg.connect(
            database_url, autocommit=True, row_factory=dict_row
        ) as observer,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(run_maintenance if scheduler_first else run_seed)
        try:
            assert ready.wait(5)
            second = executor.submit(run_seed if scheduler_first else run_maintenance)
            _wait_for_database_block(
                observer, "proof-seed" if scheduler_first else "proof-maintenance"
            )
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
        assert observer.execute(
            "SELECT count(*) AS count FROM router.services"
        ).fetchone() == {"count": 4}
        assert observer.execute(
            """SELECT completed, failure_class, result
               FROM router.price_synchronizations"""
        ).fetchall() == [{"completed": True, "failure_class": None, "result": []}]
        assert observer.execute(
            """SELECT event.result FROM router.activity_events AS event
               JOIN router.price_synchronizations AS run ON event.resource_id = run.id
               WHERE event.actor_subject = 'system:price-synchronization'"""
        ).fetchall() == [{"result": "succeeded"}]
        assert (
            observer.execute(
                "SELECT 1 FROM router.activity_events WHERE result = 'failed'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("days", [1, 3])
def test_seed_accepts_empty_catalog_startup_maintenance(
    database_url: str, proof: ModuleType, controls: ControlKeys, days: int
) -> None:
    """Keep successful scheduled runs and their system events across restarts."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        migrate(connection)
    for day in range(1, days + 1):
        _run_due_accounting_maintenance(
            database_url, {}, now=datetime(2026, 9, day, 4, 56, tzinfo=UTC)
        )
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        runs = connection.execute(
            "SELECT * FROM router.price_synchronizations ORDER BY attempted_at"
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM router.activity_events ORDER BY resource_id"
        ).fetchall()
        root = connection.execute("SELECT * FROM router.services").fetchone()
        assert len(runs) == days
        assert len(events) == days
        assert all(
            run["run_kind"] == "scheduled"
            and run["completed"]
            and run["failure_class"] is None
            and run["result"] == []
            for run in runs
        )
        assert {event["resource_id"] for event in events} == {run["id"] for run in runs}
        assert all(
            event["actor_subject"] == "system:price-synchronization"
            and event["action"] == "price.synchronize"
            and event["resource_type"] == "price_synchronization"
            and event["result"] == "succeeded"
            and event["service_api_name"] is None
            and event["resource_api_name"] is None
            for event in events
        )
        assert len(proof._seed(connection, controls)) == 4  # noqa: SLF001
        assert (
            connection.execute(
                "SELECT * FROM router.price_synchronizations ORDER BY attempted_at"
            ).fetchall()
            == runs
        )
        assert (
            connection.execute(
                "SELECT * FROM router.activity_events "
                "WHERE actor_subject = 'system:price-synchronization' "
                "ORDER BY resource_id"
            ).fetchall()
            == events
        )
        assert (
            connection.execute(
                "SELECT * FROM router.services WHERE api_name = 'root'"
            ).fetchone()
            == root
        )
        after = _snapshot(connection)
        with pytest.raises(SystemExit, match="clean database with its stored root"):
            proof._seed(connection, controls)  # noqa: SLF001
        assert _snapshot(connection) == after
