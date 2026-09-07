"""Permanent service root, atomic bootstrap, and exact administration errors."""
# ruff: noqa: PLR0913, PLR0917, PLR2004

from __future__ import annotations

import concurrent.futures
import importlib
import threading
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from llmrouter_backend import store
from llmrouter_backend.assignments import resolve_assignment
from llmrouter_backend.database import applied_versions, migrate
from llmrouter_backend.errors import ApiError
from psycopg.rows import dict_row

from . import test_identity_api as identity
from .test_identity_api import IdentityTestContext

root_settings = identity.identity_settings
root_database = identity.migrated_database

if TYPE_CHECKING:
    from llmrouter_backend.config import Settings


def _services(connection: psycopg.Connection[Any]) -> list[Any]:
    return connection.execute(
        "SELECT id, api_name, display_name, parent_service_id, created_at "
        "FROM router.services ORDER BY api_name"
    ).fetchall()


def _activity(connection: psycopg.Connection[Any]) -> list[Any]:
    return connection.execute(
        "SELECT * FROM router.activity_events ORDER BY occurred_at, id"
    ).fetchall()


def _legacy(
    connection: psycopg.Connection[Any], names: list[tuple[str, str | None]]
) -> None:
    migrate(connection, target=1)
    for name, parent in names:
        connection.execute(
            """INSERT INTO router.services (api_name, display_name, parent_service_id)
               VALUES (%s, %s, (SELECT id FROM router.services WHERE api_name = %s))""",
            (name, f"Existing {name}", parent),
        )


@pytest.mark.parametrize(
    "names",
    [
        [],
        [("one", None)],
        [("one", None), ("two", None)],
        [("root", None), ("child", "root"), ("other", None)],
        [("one", None), ("middle", "one"), ("root", "middle"), ("child", "root")],
    ],
)
def test_bootstrap_preserves_identity_data_and_is_a_no_op_on_repeat(
    database_url: str, names: list[tuple[str, str | None]]
) -> None:
    """Normalize each approved legacy example without replacing stored records."""
    with psycopg.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        _legacy(connection, names)
        connection.execute(
            """INSERT INTO router.assignment_usage (service_id, api_name)
               SELECT id, 'unused' FROM router.services"""
        )
        connection.execute(
            """INSERT INTO router.workspaces (service_id, api_name, display_name)
               SELECT id, 'main', 'Main' FROM router.services"""
        )
        connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name, inherits_assignment_api_name)
               SELECT id, 'alias', 'default' FROM router.services"""
        )
        before = _services(connection)
        tables = ("assignment_usage", "assignment_definitions", "workspaces")
        retained = {
            table: connection.execute(
                psycopg.sql.SQL("SELECT * FROM router.{} ORDER BY service_id").format(
                    psycopg.sql.Identifier(table)
                )
            ).fetchall()
            for table in tables
        }
        migrate(connection)
        after = _services(connection)
        root = next(row for row in after if row["api_name"] == "root")
        assert root["parent_service_id"] is None
        by_id = {row["id"]: row for row in after}
        for old in before:
            current = by_id[old["id"]]
            assert {
                key: value
                for key, value in current.items()
                if key != "parent_service_id"
            } == {
                key: value for key, value in old.items() if key != "parent_service_id"
            }
            expected = (
                None
                if old["api_name"] == "root"
                else old["parent_service_id"] or root["id"]
            )
            assert current["parent_service_id"] == expected
        for table, rows in retained.items():
            assert (
                connection.execute(
                    psycopg.sql.SQL(
                        "SELECT * FROM router.{} ORDER BY service_id"
                    ).format(psycopg.sql.Identifier(table))
                ).fetchall()
                == rows
            )
        default = resolve_assignment(
            connection, service_id=root["id"], api_name="default"
        )
        assert default is not None
        assert default.definition_kind == "implicit"
        assert default.effective_chain == ()
        assert default.defined_by_service_api_name == "root"
        assert _activity(connection) == []
        versions = connection.execute(
            "SELECT * FROM public.router_schema_migrations"
        ).fetchall()
        migrate(connection)
        assert _services(connection) == after
        assert (
            connection.execute(
                "SELECT * FROM public.router_schema_migrations"
            ).fetchall()
            == versions
        )
        assert _activity(connection) == []
        migrate(connection, target=1)
        assert _services(connection) == after
        connection.execute(
            "UPDATE router.services SET parent_service_id = NULL WHERE api_name "
            "<> 'root'"
        )
        migrate(connection)
        assert (
            next(row for row in _services(connection) if row["api_name"] == "root")
            == root
        )


@pytest.mark.parametrize(
    "failure",
    [
        "cycle",
        "no-top-level",
        "missing-parent",
        "assignment-cycle",
        "missing-assignment",
    ],
)
def test_bootstrap_failure_rolls_back_every_change(
    database_url: str, failure: str
) -> None:
    """Leave legacy records and migration history intact after a failed bootstrap."""
    with psycopg.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        _legacy(connection, [("one", None), ("two", "one")])
        connection.execute("SET session_replication_role = replica")
        if failure in {"cycle", "no-top-level"}:
            connection.execute(
                "UPDATE router.services SET parent_service_id = "
                "(SELECT id FROM router.services WHERE api_name = 'two') WHERE "
                "api_name = 'one'"
            )
            if failure == "cycle":
                connection.execute(
                    "INSERT INTO router.services (api_name, display_name) "
                    "VALUES ('top', 'Top')"
                )
        elif failure == "missing-parent":
            connection.execute(
                "UPDATE router.services SET parent_service_id = "
                "gen_random_uuid() WHERE api_name = 'two'"
            )
        else:
            connection.execute(
                """INSERT INTO router.assignment_definitions
                       (service_id, api_name, inherits_assignment_api_name)
                   SELECT id, 'alias', %s
                       FROM router.services WHERE api_name = 'one'""",
                ("second" if failure == "assignment-cycle" else "absent",),
            )
            if failure == "assignment-cycle":
                connection.execute(
                    """INSERT INTO router.assignment_definitions
                           (service_id, api_name, inherits_assignment_api_name)
                       SELECT id, 'second', 'alias'
                       FROM router.services WHERE api_name = 'one'"""
                )
        connection.execute("SET session_replication_role = origin")
        before = _services(connection)
        definitions = connection.execute(
            "SELECT * FROM router.assignment_definitions ORDER BY id"
        ).fetchall()
        with pytest.raises((psycopg.errors.CheckViolation, ApiError)):
            migrate(connection)
        assert _services(connection) == before
        assert (
            connection.execute(
                "SELECT * FROM router.assignment_definitions ORDER BY id"
            ).fetchall()
            == definitions
        )
        assert applied_versions(connection) == (1,)
        assert _activity(connection) == []


@pytest.fixture
def root_context(root_database: str, root_settings: Settings) -> IdentityTestContext:
    """Create authenticated controls and one two-level child tree."""
    context = IdentityTestContext(root_database, root_settings)
    context.seed_administrator()
    for name, parent in (("branch", "root"), ("leaf", "branch"), ("other", "root")):
        response = context.client.post(
            "/v1/admin/services",
            json={
                "api_name": name,
                "display_name": name.title(),
                "parent_service_api_name": parent,
            },
            headers=context.admin_headers,
        )
        assert response.status_code == 201
    return context


@pytest.mark.parametrize(
    ("method", "target", "body", "status", "code", "message"),
    [
        (
            "POST",
            "",
            {
                "api_name": "root",
                "display_name": "Changed",
                "parent_service_api_name": "absent",
            },
            409,
            "conflict",
            "The root service already exists.",
        ),
        (
            "POST",
            "",
            {
                "api_name": "branch",
                "display_name": "Changed",
                "parent_service_api_name": "branch",
            },
            409,
            "conflict",
            "Service API name already exists.",
        ),
        (
            "POST",
            "",
            {
                "api_name": "new",
                "display_name": "Changed",
                "parent_service_api_name": "new",
            },
            409,
            "conflict",
            "The service parent would create a cycle.",
        ),
        (
            "POST",
            "",
            {
                "api_name": "new",
                "display_name": "Changed",
                "parent_service_api_name": "absent",
            },
            404,
            "not_found",
            "Parent service was not found.",
        ),
        (
            "PUT",
            "/absent",
            {"display_name": "Changed", "parent_service_api_name": None},
            404,
            "not_found",
            "The service does not exist.",
        ),
        (
            "PUT",
            "/root",
            {"display_name": "Changed", "parent_service_api_name": "absent"},
            400,
            "invalid_request",
            "The root service must not have a parent.",
        ),
        (
            "PUT",
            "/branch",
            {"display_name": "Changed", "parent_service_api_name": None},
            400,
            "invalid_request",
            "A non-root service must have a parent.",
        ),
        (
            "PUT",
            "/branch",
            {"display_name": "Changed", "parent_service_api_name": "absent"},
            404,
            "not_found",
            "Parent service was not found.",
        ),
        (
            "PUT",
            "/branch",
            {"display_name": "Changed", "parent_service_api_name": "branch"},
            409,
            "conflict",
            "The service parent would create a cycle.",
        ),
        (
            "PUT",
            "/branch",
            {"display_name": "Changed", "parent_service_api_name": "leaf"},
            409,
            "conflict",
            "The service parent would create a cycle.",
        ),
        ("DELETE", "/absent", None, 404, "not_found", "The service does not exist."),
        (
            "DELETE",
            "/root",
            None,
            409,
            "conflict",
            "The root service cannot be deleted.",
        ),
        (
            "DELETE",
            "/branch",
            None,
            409,
            "conflict",
            "Move or delete the child services first.",
        ),
    ],
)
def test_service_failures_have_exact_precedence_and_activity(
    root_context: IdentityTestContext,
    method: str,
    target: str,
    body: dict[str, Any] | None,
    status: int,
    code: str,
    message: str,
) -> None:
    """Keep all stored service values and record each rejected validation attempt."""
    with psycopg.connect(
        root_context.database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        before = _services(connection)
        events = _activity(connection)
        response = root_context.client.request(
            method,
            f"/v1/admin/services{target}",
            json=body,
            headers=root_context.admin_headers,
        )
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["message"] == message
        assert _services(connection) == before
        after_events = _activity(connection)
        assert len(after_events) == len(events) + 1
        event = after_events[-1]
        assert event["result"] == "failed"
        assert (
            event["action"]
            == {
                "POST": "service.create",
                "PUT": "service.update",
                "DELETE": "service.delete",
            }[method]
        )
        assert event["resource_type"] == "service"
        assert event["resource_api_name"] == (
            body["api_name"] if method == "POST" and body else target[1:]
        )
        assert "Changed" not in str(event)


@pytest.mark.parametrize(
    ("method", "target", "body"),
    [
        ("POST", "", {"api_name": "new", "display_name": "New"}),
        (
            "POST",
            "",
            {"api_name": "new", "display_name": "New", "parent_service_api_name": None},
        ),
        ("PUT", "/root", {"display_name": "Root"}),
        ("PUT", "/branch", {"display_name": "Branch"}),
        (
            "POST",
            "",
            {
                "api_name": "new",
                "display_name": "New",
                "parent_service_api_name": "root",
                "is_root": False,
            },
        ),
        (
            "PUT",
            "/root",
            {"display_name": "Root", "parent_service_api_name": None, "is_root": True},
        ),
    ],
)
def test_service_contract_requires_parent_and_rejects_root_flags(
    root_context: IdentityTestContext, method: str, target: str, body: dict[str, Any]
) -> None:
    """Reject invalid wire shapes before service validation changes any data."""
    with psycopg.connect(root_context.database_url, autocommit=True) as connection:
        before, events = _services(connection), _activity(connection)
        response = root_context.client.request(
            method,
            f"/v1/admin/services{target}",
            json=body,
            headers=root_context.admin_headers,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert response.json()["error"]["message"] == "The request is invalid."
        assert _services(connection) == before
        assert _activity(connection) == events


def test_service_successes_and_terminal_page_preserve_exact_wire_shape(
    root_context: IdentityTestContext,
) -> None:
    """Keep required null parents while omitting an absent next-page cursor."""
    context = root_context
    with psycopg.connect(
        context.database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        root_before = store.service_by_api_name(connection, "root")
        for name, parent in (("root", None), ("leaf", "other")):
            events = _activity(connection)
            response = context.client.put(
                f"/v1/admin/services/{name}",
                json={"display_name": "Changed", "parent_service_api_name": parent},
                headers=context.admin_headers,
            )
            assert response.status_code == 200
            assert set(response.json()) == {
                "api_name",
                "display_name",
                "parent_service_api_name",
                "is_root",
                "created_at",
            }
            assert response.json()["parent_service_api_name"] == parent
            assert response.json()["is_root"] == (name == "root")
            stored = store.service_by_api_name(connection, name)
            assert stored is not None
            assert stored["display_name"] == "Changed"
            assert stored["parent_service_api_name"] == parent
            assert len(_activity(connection)) == len(events) + 1
            assert _activity(connection)[-1]["result"] == "succeeded"
        root_after = store.service_by_api_name(connection, "root")
        assert root_before is not None
        assert root_after is not None
        assert (root_after["id"], root_after["created_at"]) == (
            root_before["id"],
            root_before["created_at"],
        )
        response = context.client.get(
            "/v1/admin/services", headers=context.admin_read_headers
        )
        assert response.status_code == 200
        assert response.json()["page"] == {"has_more": False}
        root = next(
            row for row in response.json()["items"] if row["api_name"] == "root"
        )
        assert root["parent_service_api_name"] is None
        assert root["is_root"] is True
        first = context.client.get(
            "/v1/admin/services?limit=1", headers=context.admin_read_headers
        ).json()
        assert first["page"] == {"has_more": True, "next_cursor": "branch"}
        deleted = context.client.delete(
            "/v1/admin/services/leaf", headers=context.admin_headers
        )
        assert deleted.status_code == 204
        assert store.service_by_api_name(connection, "leaf") is None
        assert _activity(connection)[-1]["result"] == "succeeded"


def test_concurrent_parent_change_has_exact_conflict_and_no_partial_write(
    root_context: IdentityTestContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a tree that changed while a request waited for the write lock."""
    context = root_context
    snapshot_taken = threading.Event()
    release_snapshot = threading.Event()
    original = store._service_tree  # noqa: SLF001 - Pause the read before its lock.

    def paused_snapshot(connection: psycopg.Connection[Any]) -> list[dict[str, Any]]:
        result = original(connection)
        if not snapshot_taken.is_set():
            snapshot_taken.set()
            assert release_snapshot.wait(timeout=10)
        return result

    monkeypatch.setattr(store, "_service_tree", paused_snapshot)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            context.client.put,
            "/v1/admin/services/leaf",
            json={"display_name": "Changed", "parent_service_api_name": "other"},
            headers=context.admin_headers,
        )
        assert snapshot_taken.wait(timeout=10)
        try:
            with psycopg.connect(context.database_url) as connection:
                connection.execute(
                    "UPDATE router.services SET parent_service_id = "
                    "(SELECT id FROM router.services WHERE api_name = 'other') "
                    "WHERE api_name = 'branch'"
                )
            with psycopg.connect(context.database_url) as connection:
                expected = _services(connection)
        finally:
            release_snapshot.set()
        response = pending.result(timeout=10)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert (
        response.json()["error"]["message"]
        == "The service tree changed. Refresh and try again."
    )
    with psycopg.connect(
        context.database_url, row_factory=dict_row
    ) as activity_connection:
        assert _activity(activity_connection)[-1]["result"] == "failed"
    with psycopg.connect(context.database_url) as connection:
        assert _services(connection) == expected


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM router.services WHERE api_name = 'root'",
        "UPDATE router.services SET api_name = 'renamed' WHERE api_name = 'root'",
        "UPDATE router.services SET id = gen_random_uuid() WHERE api_name = 'root'",
        (
            "UPDATE router.services SET created_at = now() + interval '1 second' "
            "WHERE api_name = 'root'"
        ),
        "TRUNCATE router.services CASCADE",
        (
            "INSERT INTO router.services (api_name, display_name) "
            "VALUES ('orphan', 'Orphan')"
        ),
    ],
)
def test_database_protects_root_and_requires_non_root_parents(
    root_database: str, statement: str
) -> None:
    """Enforce permanent-root invariants for direct database writes too."""
    with psycopg.connect(root_database, autocommit=True) as connection:
        before = _services(connection)
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(statement)
        assert _services(connection) == before


@pytest.mark.parametrize("failure", ["missing", "cycle"])
def test_parent_change_rolls_back_invalid_assignment_inheritance(
    root_context: IdentityTestContext, failure: str
) -> None:
    """Validate all effective assignments before a service parent change commits."""
    context = root_context
    with psycopg.connect(
        context.database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        definitions = [("branch", "alias", "default"), ("leaf", "dependent", "alias")]
        if failure == "cycle":
            definitions.extend(
                [("other", "dependent", "default"), ("other", "alias", "dependent")]
            )
        for service, name, inherited in definitions:
            connection.execute(
                """INSERT INTO router.assignment_definitions
                       (service_id, api_name, inherits_assignment_api_name)
                   SELECT id, %s, %s FROM router.services WHERE api_name = %s""",
                (name, inherited, service),
            )
        before, events = _services(connection), _activity(connection)
        response = context.client.put(
            "/v1/admin/services/leaf",
            json={"display_name": "Changed", "parent_service_api_name": "other"},
            headers=context.admin_headers,
        )
        assert response.status_code == (409 if failure == "cycle" else 400)
        assert response.json()["error"]["code"] == (
            "assignment_cycle" if failure == "cycle" else "invalid_request"
        )
        assert response.json()["error"]["message"] == (
            "The assignment inheritance would contain a cycle."
            if failure == "cycle"
            else "The request is invalid."
        )
        assert _services(connection) == before
        assert len(_activity(connection)) == len(events) + 1
        event = _activity(connection)[-1]
        assert event["result"] == "failed"
        assert event["action"] == "service.update"
        assert event["resource_api_name"] == "leaf"
        assert "Changed" not in str(event)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_service_commit_failure_rolls_back_tree_dependents_and_success_event(
    root_context: IdentityTestContext, method: str
) -> None:
    """Record failure when PostgreSQL rejects the transaction at commit time."""
    context = root_context
    with psycopg.connect(
        context.database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        connection.execute(
            """INSERT INTO router.workspaces (service_id, api_name, display_name)
               SELECT id, 'main', 'Main' FROM router.services WHERE api_name = 'leaf'"""
        )
        connection.execute(
            """CREATE FUNCTION router.test_service_commit_failure() RETURNS trigger
               LANGUAGE plpgsql AS $$ BEGIN
                   RAISE EXCEPTION 'Synthetic storage failure' USING ERRCODE = 'XX000';
               END; $$;
               CREATE CONSTRAINT TRIGGER test_service_commit_failure
               AFTER INSERT OR UPDATE OR DELETE ON router.services
               DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
               EXECUTE FUNCTION router.test_service_commit_failure();"""
        )
        before, events = _services(connection), _activity(connection)
        workspaces = connection.execute(
            "SELECT * FROM router.workspaces ORDER BY id"
        ).fetchall()
        body: dict[str, Any] | None = None
        target = "/leaf"
        if method != "DELETE":
            body = {"display_name": "Changed", "parent_service_api_name": "other"}
            if method == "POST":
                body["api_name"] = "new"
                target = ""
        response = context.client.request(
            method,
            f"/v1/admin/services{target}",
            json=body,
            headers=context.admin_headers,
        )
        assert response.status_code == 500
        assert response.json()["error"] == {
            "code": "internal_error",
            "message": "The Router could not complete the operation.",
        }
        assert _services(connection) == before
        assert (
            connection.execute("SELECT * FROM router.workspaces ORDER BY id").fetchall()
            == workspaces
        )
        assert len(_activity(connection)) == len(events) + 1
        event = _activity(connection)[-1]
        assert event["result"] == "failed"
        assert (
            event["action"]
            == {
                "POST": "service.create",
                "PUT": "service.update",
                "DELETE": "service.delete",
            }[method]
        )
        assert event["resource_api_name"] == ("new" if method == "POST" else "leaf")
        assert "Changed" not in str(event)


def test_startup_stays_unavailable_until_bootstrap_is_repaired(
    database_url: str, root_settings: Settings
) -> None:
    """Block requests on invalid legacy inheritance and retry after operator repair."""
    with psycopg.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        _legacy(connection, [("legacy", None)])
        connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name, inherits_assignment_api_name)
               SELECT id, 'alias', 'missing' FROM router.services"""
        )
        before = _services(connection)
        context = IdentityTestContext(database_url, root_settings)
        context.seed_administrator()
        assert context.client.get("/ready").status_code == 503
        denied = context.client.get(
            "/v1/admin/services", headers=context.admin_read_headers
        )
        assert denied.status_code >= 400
        assert _services(connection) == before
        assert _activity(connection) == []
        connection.execute("DELETE FROM router.assignment_definitions")
        assert context.client.get("/ready").status_code == 200
        services = context.client.get(
            "/v1/admin/services", headers=context.admin_read_headers
        )
        assert services.status_code == 200
        assert [row["api_name"] for row in services.json()["items"]] == [
            "legacy",
            "root",
        ]
        root = context.client.get(
            "/v1/admin/services/root", headers=context.admin_read_headers
        )
        assert root.status_code == 200
        assert root.json()["parent_service_api_name"] is None
        assert root.json()["is_root"] is True
        assert _activity(connection) == []


def test_root_deletion_is_rejected_without_children(
    root_database: str, root_settings: Settings
) -> None:
    """Apply permanent-root protection even to a freshly bootstrapped tree."""
    context = IdentityTestContext(root_database, root_settings)
    context.seed_administrator()
    with psycopg.connect(
        root_database, autocommit=True, row_factory=dict_row
    ) as connection:
        before = _services(connection)
        response = context.client.delete(
            "/v1/admin/services/root", headers=context.admin_headers
        )
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "conflict",
            "message": "The root service cannot be deleted.",
        }
        assert _services(connection) == before
        assert len(_activity(connection)) == 1
        event = _activity(connection)[0]
        assert event["result"] == "failed"
        assert event["action"] == "service.delete"
        assert event["resource_api_name"] == "root"


def test_service_create_records_confirmed_parent_and_activity(
    root_database: str, root_settings: Settings
) -> None:
    """Commit one child and its success event with no service value snapshot."""
    context = IdentityTestContext(root_database, root_settings)
    context.seed_administrator()
    response = context.client.post(
        "/v1/admin/services",
        json={
            "api_name": "new",
            "display_name": "New display value",
            "parent_service_api_name": "root",
        },
        headers=context.admin_headers,
    )
    assert response.status_code == 201
    assert response.json()["parent_service_api_name"] == "root"
    assert response.json()["is_root"] is False
    with psycopg.connect(root_database, row_factory=dict_row) as connection:
        rows = _services(connection)
        assert [row["api_name"] for row in rows] == ["new", "root"]
        assert rows[0]["parent_service_id"] == rows[1]["id"]
        assert rows[0]["display_name"] == "New display value"
        events = _activity(connection)
        assert len(events) == 1
        assert events[0]["action"] == "service.create"
        assert events[0]["result"] == "succeeded"
        assert events[0]["resource_api_name"] == "new"
        assert events[0]["resource_id"] == rows[0]["id"]
        assert "New display value" not in str(events)


@pytest.mark.parametrize("actor", ["service", "administrator"])
def test_detached_calls_cannot_bypass_failed_bootstrap(
    database_url: str,
    root_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
) -> None:
    """Stop detached call authentication while legacy inheritance is invalid."""
    application_module = importlib.import_module("llmrouter_backend.app")

    def unexpected_authentication(*_args: object, **_kwargs: object) -> None:
        pytest.fail("A call reached authentication before bootstrap succeeded.")

    with psycopg.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        _legacy(connection, [("legacy", None)])
        connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name, inherits_assignment_api_name)
               SELECT id, 'alias', 'missing' FROM router.services"""
        )
        before = _services(connection)
        context = IdentityTestContext(database_url, root_settings)
        context.seed_administrator()
        assert context.client.get("/ready").status_code == 503
        monkeypatch.setattr(
            application_module,
            "_authenticate_service_request"
            if actor == "service"
            else "authenticate_administrator_session",
            unexpected_authentication,
        )
        path = (
            "/v1/model-calls"
            if actor == "service"
            else "/v1/admin/playground/model-calls"
        )
        response = context.client.post(path, json={}, headers=context.admin_headers)
        assert response.status_code == 400
        assert response.json()["error"] == {
            "code": "invalid_request",
            "message": "The request is invalid.",
            "details": {
                "field": "inherits_assignment_api_name",
                "reason": "An inherited assignment does not resolve.",
            },
        }
        assert _services(connection) == before
        assert applied_versions(connection) == (1,)
        assert _activity(connection) == []
