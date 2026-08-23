"""PostgreSQL and native API tests for direct current assignments."""
# ruff: noqa: D107, PLR0915, PLR2004

from __future__ import annotations

import concurrent.futures
import threading
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import catalog, create_app
from llmrouter_backend.assignments import resolve_assignment_for_call
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.errors import ApiError
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session, create_key
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from pathlib import Path

ADMIN_ORIGIN = "http://127.0.0.1:5174"


class AssignmentContext:
    """Local controls and actors for one assignment contract test."""

    def __init__(self, database_url: str, settings: Settings) -> None:
        self.database_url = database_url
        self.settings = settings
        self.controls = ControlKeys.load(settings)
        self.client = TestClient(
            create_app(database_url=database_url, settings=settings),
            base_url="https://llmrouter.test",
        )
        self.session = new_token()
        self.csrf = new_token()
        self.keys: dict[str, str] = {}
        self.actor_subjects: dict[str, str] = {}

    def seed(self) -> None:
        """Create the service tree, workspaces, actors, and provider catalog."""
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            service_ids: dict[str, Any] = {}
            for name, parent in (
                ("root", None),
                ("child", "root"),
                ("leaf", "child"),
                ("other", None),
            ):
                parent_id = service_ids[parent] if parent is not None else None
                row = connection.execute(
                    """INSERT INTO router.services
                           (api_name, display_name, parent_service_id)
                       VALUES (%s, %s, %s) RETURNING id""",
                    (name, name.title(), parent_id),
                ).fetchone()
                assert row is not None
                service_ids[name] = row["id"]
                connection.execute(
                    """INSERT INTO router.workspaces
                           (service_id, api_name, display_name)
                       VALUES (%s, 'main', 'Main')""",
                    (row["id"],),
                )
                key, secret = create_key(
                    connection,
                    service_id=row["id"],
                    name="test",
                    actor_subject="test:setup",
                    control_keys=self.controls,
                )
                self.keys[name] = secret
                self.actor_subjects[name] = f"service:{name}:key:{key['id']}"
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
            _seed_catalog(connection)

    def service_headers(self, name: str) -> dict[str, str]:
        """Build one service bearer header."""
        return {"Authorization": f"Bearer {self.keys[name]}"}

    @property
    def admin_headers(self) -> dict[str, str]:
        """Build administrator browser write headers."""
        return {
            "Cookie": f"llmrouter_admin_session={self.session}",
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": self.csrf,
        }

    @property
    def admin_read_headers(self) -> dict[str, str]:
        """Build one administrator read header."""
        return {"Cookie": f"llmrouter_admin_session={self.session}"}


@pytest.fixture
def assignment_settings(tmp_path: Path) -> Settings:
    """Create only local test control files."""
    digest = tmp_path / "digest"
    encryption = tmp_path / "encryption"
    digest.write_text("d" * 64, encoding="utf-8")
    encryption.write_text("e" * 64, encoding="utf-8")
    return Settings(
        administrator_digest_key_file=digest,
        administrator_encryption_key_file=encryption,
        allowed_origins=(ADMIN_ORIGIN,),
    )


@pytest.fixture
def assignment_context(
    database_url: str, assignment_settings: Settings
) -> AssignmentContext:
    """Apply a clean schema and seed one complete assignment test context."""
    with psycopg.connect(database_url) as connection:
        migrate(connection)
    context = AssignmentContext(database_url, assignment_settings)
    context.seed()
    return context


def test_native_assignment_inheritance_replacement_cycles_and_isolation(
    assignment_context: AssignmentContext,
) -> None:
    """Enforce nearest replacement, graph validation, and actor separation."""
    context = assignment_context
    client = context.client
    root = context.service_headers("root")
    child = context.service_headers("child")
    other = context.service_headers("other")

    implicit = client.get("/v1/assignments/default", headers=root)
    assert implicit.status_code == HTTPStatus.OK
    assert implicit.json() == {
        "api_name": "default",
        "display_name": "Default",
        "definition_kind": "implicit",
        "defined_by_service_api_name": "root",
        "effective_chain": [],
        "observed_requirements": [],
    }
    assert client.get("/v1/assignments/unknown", headers=root).status_code == 404

    root_default = client.put(
        "/v1/assignments/default",
        json={
            "display_name": "Root default",
            "direct_chain": [
                {"provider_model_api_name": "text-one"},
                {"provider_model_api_name": "text-two"},
            ],
            "reasoning_level": "high",
        },
        headers=root,
    )
    assert root_default.status_code == HTTPStatus.OK
    assert [
        item["provider_model_api_name"]
        for item in root_default.json()["effective_chain"]
    ] == ["text-one", "text-two"]
    inherited = client.get("/v1/assignments/default", headers=child).json()
    assert inherited["defined_by_service_api_name"] == "root"

    replaced = client.put(
        "/v1/assignments/default",
        json={"direct_chain": [{"provider_model_api_name": "text-two"}]},
        headers=child,
    )
    assert replaced.status_code == HTTPStatus.OK
    assert replaced.json()["effective_chain"] == [
        {"provider_model_api_name": "text-two"}
    ]
    assert client.delete("/v1/assignments/default", headers=child).status_code == 204
    inherited_again = client.get("/v1/assignments/default", headers=child).json()
    assert len(inherited_again["effective_chain"]) == 2

    assert client.put(
        "/v1/assignments/a",
        json={"direct_chain": [{"provider_model_api_name": "text-one"}]},
        headers=child,
    ).status_code == 200
    assert client.put(
        "/v1/assignments/b",
        json={"inherits_assignment_api_name": "a"},
        headers=child,
    ).status_code == 200
    blocked_delete = client.delete("/v1/assignments/a", headers=child)
    assert blocked_delete.status_code == HTTPStatus.BAD_REQUEST
    assert client.get("/v1/assignments/a", headers=child).status_code == HTTPStatus.OK
    cycle = client.put(
        "/v1/assignments/a",
        json={"inherits_assignment_api_name": "b"},
        headers=child,
    )
    assert cycle.status_code == HTTPStatus.CONFLICT
    assert cycle.json()["error"]["code"] == "assignment_cycle"
    assert client.get("/v1/assignments/a", headers=child).json()["direct_chain"]
    missing = client.put(
        "/v1/assignments/missing",
        json={"inherits_assignment_api_name": "absent"},
        headers=child,
    )
    assert missing.status_code == HTTPStatus.BAD_REQUEST
    assert client.get("/v1/assignments/missing", headers=child).status_code == 404

    assert client.get("/v1/assignments/a", headers=other).status_code == 404
    assert client.get(
        "/v1/admin/services/child/assignments/a", headers=child
    ).status_code == HTTPStatus.UNAUTHORIZED
    assert client.get(
        "/v1/assignments/a", headers=context.admin_read_headers
    ).status_code == HTTPStatus.UNAUTHORIZED
    assert client.get(
        "/v1/workspaces/main/assignments", headers=child
    ).status_code == HTTPStatus.NOT_FOUND

    administrator_write = client.put(
        "/v1/admin/services/child/assignments/admin-defined",
        json={"inherits_assignment_api_name": "default"},
        headers=context.admin_headers,
    )
    assert administrator_write.status_code == HTTPStatus.OK
    assert client.get(
        "/v1/assignments/admin-defined", headers=child
    ).status_code == HTTPStatus.OK
    denied_browser_write = client.put(
        "/v1/admin/services/child/assignments/denied",
        json={"inherits_assignment_api_name": "default"},
        headers={**context.admin_headers, "X-CSRF-Token": new_token()},
    )
    assert denied_browser_write.status_code == HTTPStatus.FORBIDDEN
    assert client.get("/v1/assignments/denied", headers=child).status_code == 404

    assert client.put(
        "/v1/assignments/base",
        json={"direct_chain": [{"provider_model_api_name": "text-one"}]},
        headers=root,
    ).status_code == HTTPStatus.OK
    assert client.put(
        "/v1/assignments/parent-dependent",
        json={"inherits_assignment_api_name": "base"},
        headers=child,
    ).status_code == HTTPStatus.OK
    invalid_parent_move = client.put(
        "/v1/admin/services/child",
        json={"display_name": "Child", "parent_service_api_name": "other"},
        headers=context.admin_headers,
    )
    assert invalid_parent_move.status_code == HTTPStatus.BAD_REQUEST
    current_child = client.get(
        "/v1/admin/services/child", headers=context.admin_read_headers
    ).json()
    assert current_child["parent_service_api_name"] == "root"

    activity = client.get(
        "/v1/admin/activity",
        params={
            "from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
            "to": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
        },
        headers=context.admin_read_headers,
    ).json()["items"]
    assert any(
        item["action"] == "assignment.update" and item["result"] == "failed"
        for item in activity
    )


def test_assignment_validation_bounds_reasoning_and_atomic_rollback(
    assignment_context: AssignmentContext,
) -> None:
    """Reject invalid exact writes without changing the current definition."""
    context = assignment_context
    client = context.client
    headers = context.service_headers("root")
    path = "/v1/assignments/strict"
    valid = {"direct_chain": [{"provider_model_api_name": "text-one"}]}
    assert client.put(path, json=valid, headers=headers).status_code == 200
    documents: tuple[dict[str, Any], ...] = (
        {},
        {
            "inherits_assignment_api_name": "default",
            "direct_chain": [{"provider_model_api_name": "text-one"}],
        },
        {"direct_chain": []},
        {
            "direct_chain": [
                {"provider_model_api_name": "text-one"},
                {"provider_model_api_name": "text-one"},
            ]
        },
        {"direct_chain": [{"provider_model_api_name": "disabled"}]},
        {
            "direct_chain": [{"provider_model_api_name": "text-one"}],
            "reasoning_level": 1,
        },
        {
            "direct_chain": [
                {"provider_model_api_name": f"text-{index}"}
                for index in range(17)
            ]
        },
    )
    for document in documents:
        assert client.put(path, json=document, headers=headers).status_code == 400
    rollback = client.put(
        path,
        json={
            "direct_chain": [
                {"provider_model_api_name": "text-two"},
                {"provider_model_api_name": "unknown"},
            ]
        },
        headers=headers,
    )
    assert rollback.status_code == HTTPStatus.BAD_REQUEST
    current = client.get(path, headers=headers).json()
    assert current["direct_chain"] == valid["direct_chain"]
    unsupported_reasoning = client.put(
        path,
        json={
            "direct_chain": [{"provider_model_api_name": "embedding"}],
            "reasoning_level": "high",
        },
        headers=headers,
    )
    assert unsupported_reasoning.status_code == HTTPStatus.BAD_REQUEST
    sixteen_candidates = [
        "text-one",
        "text-two",
        *[f"text-{index}" for index in range(3, 17)],
    ]
    exact_sixteen = client.put(
        "/v1/assignments/sixteen",
        json={
            "direct_chain": [
                {"provider_model_api_name": name} for name in sixteen_candidates
            ],
            "reasoning_level": "high",
        },
        headers=headers,
    )
    assert exact_sixteen.status_code == HTTPStatus.OK
    assert [
        item["provider_model_api_name"]
        for item in exact_sixteen.json()["effective_chain"]
    ] == sixteen_candidates


def test_runtime_uses_actual_requirements_and_persists_use_evidence(
    assignment_context: AssignmentContext,
) -> None:
    """Filter with the current call and keep an explicit removable use union."""
    context = assignment_context
    client = context.client
    headers = context.service_headers("root")
    assert client.put(
        "/v1/assignments/mixed",
        json={
            "direct_chain": [
                {"provider_model_api_name": "embedding"},
                {"provider_model_api_name": "text-one"},
            ]
        },
        headers=headers,
    ).status_code == 200
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        service = connection.execute(
            "SELECT id FROM router.services WHERE api_name = 'root'"
        ).fetchone()
        assert service is not None
        resolved, routes = resolve_assignment_for_call(
            connection,
            service_id=service["id"],
            workspace_api_name="main",
            assignment_api_name="mixed",
            required_inputs=frozenset({"text"}),
            required_output="text",
            required_capabilities=frozenset({"streaming"}),
            actor_subject=context.actor_subjects["root"],
        )
        assert resolved.api_name == "mixed"
        assert [route.provider_model_api_name for route in routes] == ["text-one"]
    observed = client.get("/v1/assignments/mixed", headers=headers).json()
    assert observed["last_used_at"]
    assert observed["observed_requirements"] == [
        "streaming",
        "text_input",
        "text_output",
    ]
    assert client.delete(
        "/v1/assignments/mixed/observed-requirements/streaming", headers=headers
    ).status_code == 204
    assert "streaming" not in client.get(
        "/v1/assignments/mixed", headers=headers
    ).json()["observed_requirements"]

    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        service = connection.execute(
            "SELECT id FROM router.services WHERE api_name = 'root'"
        ).fetchone()
        assert service is not None
        with pytest.raises(ApiError) as unavailable:
            resolve_assignment_for_call(
                connection,
                service_id=service["id"],
                workspace_api_name="main",
                assignment_api_name="automatic",
                required_inputs=frozenset({"text"}),
                required_output="video",
                required_capabilities=frozenset(),
                actor_subject=context.actor_subjects["root"],
            )
        assert unavailable.value.code == "provider_unavailable"
    automatic = client.get("/v1/assignments/automatic", headers=headers)
    assert automatic.status_code == 200
    assert automatic.json()["inherits_assignment_api_name"] == "default"
    assert automatic.json()["observed_requirements"] == ["text_input", "video_output"]

    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        other_service = connection.execute(
            "SELECT id FROM router.services WHERE api_name = 'other'"
        ).fetchone()
        assert other_service is not None
        with pytest.raises(ApiError) as foreign_workspace:
            resolve_assignment_for_call(
                connection,
                service_id=other_service["id"],
                workspace_api_name="foreign",
                assignment_api_name="automatic",
                required_inputs=frozenset({"text"}),
                required_output="text",
                required_capabilities=frozenset(),
                actor_subject=context.actor_subjects["other"],
            )
        assert foreign_workspace.value.code == "not_found"
        with pytest.raises(ApiError) as invalid_name:
            resolve_assignment_for_call(
                connection,
                service_id=other_service["id"],
                workspace_api_name="main",
                assignment_api_name="Invalid",
                required_inputs=frozenset({"text"}),
                required_output="text",
                required_capabilities=frozenset(),
                actor_subject=context.actor_subjects["other"],
            )
        assert invalid_name.value.code == "invalid_request"
        assert connection.execute(
            """SELECT count(*) FROM router.assignment_definitions
               WHERE service_id = %s AND api_name::text = 'Invalid'""",
            (other_service["id"],),
        ).fetchone() == {"count": 0}


def test_used_assignment_deletion_keeps_only_effective_evidence(
    assignment_context: AssignmentContext,
) -> None:
    """Keep evidence for inherited state and remove evidence for absent state."""
    context = assignment_context
    client = context.client
    root = context.service_headers("root")
    child = context.service_headers("child")
    for headers, candidate in ((root, "text-one"), (child, "text-two")):
        assert client.put(
            "/v1/assignments/shared",
            json={"direct_chain": [{"provider_model_api_name": candidate}]},
            headers=headers,
        ).status_code == HTTPStatus.OK
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        child_service = connection.execute(
            "SELECT id FROM router.services WHERE api_name = 'child'"
        ).fetchone()
        assert child_service is not None
        resolve_assignment_for_call(
            connection,
            service_id=child_service["id"],
            workspace_api_name="main",
            assignment_api_name="shared",
            required_inputs=frozenset({"text"}),
            required_output="text",
            required_capabilities=frozenset(),
            actor_subject=context.actor_subjects["child"],
        )
    assert client.delete(
        "/v1/admin/services/child/assignments/shared",
        headers=context.admin_headers,
    ).status_code == HTTPStatus.NO_CONTENT
    inherited = client.get("/v1/assignments/shared", headers=child).json()
    assert inherited["defined_by_service_api_name"] == "root"
    assert inherited["observed_requirements"] == ["text_input", "text_output"]
    assert "shared" in {
        item["api_name"]
        for item in client.get("/v1/assignments", headers=child).json()["items"]
    }

    assert client.put(
        "/v1/assignments/ephemeral",
        json={"direct_chain": [{"provider_model_api_name": "text-one"}]},
        headers=child,
    ).status_code == HTTPStatus.OK
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        child_service = connection.execute(
            "SELECT id FROM router.services WHERE api_name = 'child'"
        ).fetchone()
        assert child_service is not None
        resolve_assignment_for_call(
            connection,
            service_id=child_service["id"],
            workspace_api_name="main",
            assignment_api_name="ephemeral",
            required_inputs=frozenset({"text"}),
            required_output="text",
            required_capabilities=frozenset(),
            actor_subject=context.actor_subjects["child"],
        )
    assert client.delete(
        "/v1/assignments/ephemeral", headers=child
    ).status_code == HTTPStatus.NO_CONTENT
    assert client.get("/v1/assignments/ephemeral", headers=child).status_code == 404
    assert "ephemeral" not in {
        item["api_name"]
        for item in client.get("/v1/assignments", headers=child).json()["items"]
    }
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            """SELECT count(*) FROM router.assignment_usage AS usage
               JOIN router.services AS service ON service.id = usage.service_id
               WHERE service.api_name = 'child'
                 AND usage.api_name = 'ephemeral'"""
        ).fetchone() == {"count": 0}

    assert client.put(
        "/v1/assignments/guarded",
        json={"direct_chain": [{"provider_model_api_name": "text-one"}]},
        headers=child,
    ).status_code == HTTPStatus.OK
    assert client.put(
        "/v1/assignments/guard-dependent",
        json={"inherits_assignment_api_name": "guarded"},
        headers=child,
    ).status_code == HTTPStatus.OK
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        child_service = connection.execute(
            "SELECT id FROM router.services WHERE api_name = 'child'"
        ).fetchone()
        assert child_service is not None
        resolve_assignment_for_call(
            connection,
            service_id=child_service["id"],
            workspace_api_name="main",
            assignment_api_name="guarded",
            required_inputs=frozenset({"text"}),
            required_output="text",
            required_capabilities=frozenset(),
            actor_subject=context.actor_subjects["child"],
        )
    assert client.delete("/v1/assignments/guarded", headers=child).status_code == 400
    guarded = client.get("/v1/assignments/guarded", headers=child).json()
    assert guarded["observed_requirements"] == ["text_input", "text_output"]
    assert "guarded" in {
        item["api_name"]
        for item in client.get("/v1/assignments", headers=child).json()["items"]
    }


def test_actual_candidate_constraints_control_fallback(
    assignment_context: AssignmentContext,
) -> None:
    """Filter ordered candidates with actual embedding, image, and media bounds."""
    context = assignment_context
    client = context.client
    headers = context.service_headers("root")
    definitions = {
        "embed-bounds": ["embedding", "embedding-four"],
        "image-bounds": ["image-small", "image-large"],
        "media-bounds": ["media-short", "media-long"],
    }
    for name, candidates in definitions.items():
        assert client.put(
            f"/v1/assignments/{name}",
            json={
                "direct_chain": [
                    {"provider_model_api_name": candidate}
                    for candidate in candidates
                ]
            },
            headers=headers,
        ).status_code == HTTPStatus.OK
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        root_service = connection.execute(
            "SELECT id FROM router.services WHERE api_name = 'root'"
        ).fetchone()
        assert root_service is not None
        common = {
            "connection": connection,
            "service_id": root_service["id"],
            "workspace_api_name": "main",
            "required_capabilities": frozenset(),
            "actor_subject": context.actor_subjects["root"],
        }
        _resolved, embedding_routes = resolve_assignment_for_call(
            **common,
            assignment_api_name="embed-bounds",
            required_inputs=frozenset({"text"}),
            required_output="embedding",
            embedding_dimension=4,
        )
        assert [route.provider_model_api_name for route in embedding_routes] == [
            "embedding-four"
        ]
        with pytest.raises(ApiError) as no_embedding:
            resolve_assignment_for_call(
                **common,
                assignment_api_name="embed-bounds",
                required_inputs=frozenset({"text"}),
                required_output="embedding",
                embedding_dimension=5,
            )
        assert no_embedding.value.code == "provider_unavailable"

        _resolved, image_routes = resolve_assignment_for_call(
            **common,
            assignment_api_name="image-bounds",
            required_inputs=frozenset({"text", "image"}),
            required_output="text",
            input_image_sizes=(50,),
        )
        assert [route.provider_model_api_name for route in image_routes] == [
            "image-large"
        ]
        _resolved, image_count_routes = resolve_assignment_for_call(
            **common,
            assignment_api_name="image-bounds",
            required_inputs=frozenset({"text", "image"}),
            required_output="text",
            input_image_sizes=(5, 5),
        )
        assert [route.provider_model_api_name for route in image_count_routes] == [
            "image-large"
        ]
        with pytest.raises(ApiError) as no_image:
            resolve_assignment_for_call(
                **common,
                assignment_api_name="image-bounds",
                required_inputs=frozenset({"text", "image"}),
                required_output="text",
                input_image_sizes=(101,),
            )
        assert no_image.value.code == "provider_unavailable"
        with pytest.raises(ApiError) as too_many_images:
            resolve_assignment_for_call(
                **common,
                assignment_api_name="image-bounds",
                required_inputs=frozenset({"text", "image"}),
                required_output="text",
                input_image_sizes=(5, 5, 5),
            )
        assert too_many_images.value.code == "provider_unavailable"
        with pytest.raises(ApiError):
            resolve_assignment_for_call(
                **common,
                assignment_api_name="image-bounds",
                required_inputs=frozenset({"text"}),
                required_output="video",
            )
        _resolved, repeated_image_routes = resolve_assignment_for_call(
            **common,
            assignment_api_name="image-bounds",
            required_inputs=frozenset({"text", "image"}),
            required_output="text",
            input_image_sizes=(50,),
        )
        assert repeated_image_routes[0].provider_model_api_name == "image-large"

        for output in ("video", "audio"):
            _resolved, media_routes = resolve_assignment_for_call(
                **common,
                assignment_api_name="media-bounds",
                required_inputs=frozenset({"text"}),
                required_output=output,
                output_duration_seconds=50,
            )
            assert [route.provider_model_api_name for route in media_routes] == [
                "media-long"
            ]
            with pytest.raises(ApiError) as no_media:
                resolve_assignment_for_call(
                    **common,
                    assignment_api_name="media-bounds",
                    required_inputs=frozenset({"text"}),
                    required_output=output,
                    output_duration_seconds=101,
                )
            assert no_media.value.code == "provider_unavailable"
    image_record = client.get("/v1/assignments/image-bounds", headers=headers).json()
    assert "video_output" in image_record["observed_requirements"]


def test_concurrent_first_use_creates_one_local_assignment(
    assignment_context: AssignmentContext,
) -> None:
    """Use the unique key to serialize one automatic local definition."""
    context = assignment_context
    assert context.client.put(
        "/v1/assignments/default",
        json={"direct_chain": [{"provider_model_api_name": "text-one"}]},
        headers=context.service_headers("root"),
    ).status_code == HTTPStatus.OK

    def call_once(assignment_name: str = "concurrent") -> str:
        with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
            service = connection.execute(
                "SELECT id FROM router.services WHERE api_name = 'child'"
            ).fetchone()
            assert service is not None
            resolved, _routes = resolve_assignment_for_call(
                connection,
                service_id=service["id"],
                workspace_api_name="main",
                assignment_api_name=assignment_name,
                required_inputs=frozenset({"text"}),
                required_output="text",
                required_capabilities=frozenset(),
                actor_subject=context.actor_subjects["child"],
            )
            return resolved.api_name

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(lambda _index: call_once(), range(8))) == [
            "concurrent"
        ] * 8
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            """SELECT count(*) FROM router.assignment_definitions AS assignment
               JOIN router.services AS service ON service.id = assignment.service_id
               WHERE service.api_name = 'child'
                 AND assignment.api_name = 'concurrent'"""
        ).fetchone() == (1,)
        activities = connection.execute(
            """SELECT actor_subject FROM router.activity_events
               WHERE action = 'assignment.create'
                 AND service_api_name = 'child'
                 AND resource_api_name = 'concurrent'"""
        ).fetchall()
        assert activities == [(context.actor_subjects["child"],)]

    with (
        psycopg.connect(context.database_url) as lock_connection,
        concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor,
    ):
        lock_connection.execute("SELECT pg_advisory_xact_lock(4993044345823)")
        automatic_future = executor.submit(call_once, "locked-boundary")
        catalog_future = executor.submit(
            context.client.put,
            "/v1/admin/provider-models/text-one",
            json={
                "api_name": "text-one",
                "provider_api_name": "fake",
                "model_api_name": "text",
                "provider_model_name": "one",
                "enabled": True,
                "reasoning_mappings": [
                    {"level": level, "provider_value": level}
                    for level in ("none", "low", "medium", "high")
                ],
            },
            headers=context.admin_headers,
        )
        done, _pending = concurrent.futures.wait(
            (automatic_future, catalog_future), timeout=0.1
        )
        assert not done
        lock_connection.commit()
        assert automatic_future.result(timeout=2) == "locked-boundary"
        assert catalog_future.result(timeout=2).status_code == HTTPStatus.OK
    with psycopg.connect(context.database_url) as connection:
        activities = connection.execute(
            """SELECT actor_subject FROM router.activity_events
               WHERE action = 'assignment.create'
                 AND service_api_name = 'child'
                 AND resource_api_name = 'locked-boundary'"""
        ).fetchall()
        assert activities == [(context.actor_subjects["child"],)]


def test_call_admission_serializes_assignment_and_catalog_writes(
    assignment_context: AssignmentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep one call snapshot while later configuration writes wait."""
    context = assignment_context
    client = context.client
    headers = context.service_headers("root")
    assert client.put(
        "/v1/assignments/race",
        json={"direct_chain": [{"provider_model_api_name": "text-one"}]},
        headers=headers,
    ).status_code == HTTPStatus.OK

    entered = threading.Event()
    release = threading.Event()
    armed = threading.Event()
    original_resolve = catalog.resolve_provider_route

    def paused_resolve(  # noqa: PLR0913 - Match the catalog call boundary.
        connection: psycopg.Connection[Any],
        api_name: str,
        *,
        required_inputs: frozenset[str],
        required_output: str,
        required_capabilities: frozenset[str],
        reasoning_level: Literal["none", "low", "medium", "high"] | None,
    ) -> catalog.ProviderRoute:
        route = original_resolve(
            connection,
            api_name,
            required_inputs=required_inputs,
            required_output=required_output,
            required_capabilities=required_capabilities,
            reasoning_level=reasoning_level,
        )
        if armed.is_set():
            armed.clear()
            entered.set()
            assert release.wait(timeout=5)
        return route

    monkeypatch.setattr(catalog, "resolve_provider_route", paused_resolve)

    def call_once() -> tuple[str, str]:
        with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
            service = connection.execute(
                "SELECT id FROM router.services WHERE api_name = 'root'"
            ).fetchone()
            assert service is not None
            resolved, routes = resolve_assignment_for_call(
                connection,
                service_id=service["id"],
                workspace_api_name="main",
                assignment_api_name="race",
                required_inputs=frozenset({"text"}),
                required_output="text",
                required_capabilities=frozenset(),
                actor_subject=context.actor_subjects["root"],
            )
            return resolved.effective_chain[0], routes[0].provider_model_name

    def start_call(
        executor: concurrent.futures.ThreadPoolExecutor,
    ) -> concurrent.futures.Future[tuple[str, str]]:
        entered.clear()
        release.clear()
        armed.set()
        future = executor.submit(call_once)
        assert entered.wait(timeout=2)
        return future

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        admitted = start_call(executor)
        deletion = executor.submit(
            client.delete, "/v1/assignments/race", headers=headers
        )
        assert not concurrent.futures.wait((deletion,), timeout=0.1).done
        release.set()
        assert admitted.result(timeout=2) == ("text-one", "one")
        assert deletion.result(timeout=2).status_code == HTTPStatus.NO_CONTENT
    assert client.get("/v1/assignments/race", headers=headers).status_code == 404
    assignment_list = client.get("/v1/assignments", headers=headers)
    assert assignment_list.status_code == HTTPStatus.OK
    assert "race" not in {
        item["api_name"] for item in assignment_list.json()["items"]
    }
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.assignment_usage WHERE api_name = 'race'"
        ).fetchone() == (0,)

    assert client.put(
        "/v1/assignments/race",
        json={"direct_chain": [{"provider_model_api_name": "text-one"}]},
        headers=headers,
    ).status_code == HTTPStatus.OK
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        admitted = start_call(executor)
        replacement = executor.submit(
            client.put,
            "/v1/assignments/race",
            json={"direct_chain": [{"provider_model_api_name": "text-two"}]},
            headers=headers,
        )
        assert not concurrent.futures.wait((replacement,), timeout=0.1).done
        release.set()
        assert admitted.result(timeout=2) == ("text-one", "one")
        assert replacement.result(timeout=2).status_code == HTTPStatus.OK
    assert call_once() == ("text-two", "two")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        admitted = start_call(executor)
        catalog_change = executor.submit(
            client.put,
            "/v1/admin/provider-models/text-two",
            json={
                "api_name": "text-two",
                "provider_api_name": "fake",
                "model_api_name": "text",
                "provider_model_name": "changed",
                "enabled": True,
                "reasoning_mappings": [
                    {"level": level, "provider_value": level}
                    for level in ("none", "low", "medium", "high")
                ],
            },
            headers=context.admin_headers,
        )
        assert not concurrent.futures.wait((catalog_change,), timeout=0.1).done
        release.set()
        assert admitted.result(timeout=2) == ("text-two", "two")
        assert catalog_change.result(timeout=2).status_code == HTTPStatus.OK
    assert call_once() == ("text-two", "changed")


def _seed_catalog(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, enabled)
           VALUES ('fake', 'Fake', 'fake', true),
                  ('off', 'Disabled', 'fake', false)"""
    )
    connection.execute(
        """INSERT INTO router.canonical_models
               (api_name, display_name, input_modalities, output_modalities,
                capabilities, constraints)
           VALUES
               ('text', 'Text', ARRAY['text'], ARRAY['text'],
                ARRAY['streaming', 'reasoning'], '{}'::jsonb),
               ('embed', 'Embedding', ARRAY['text'], ARRAY['embedding'],
                ARRAY[]::text[], '{"embedding_dimensions":[3,4]}'::jsonb),
               ('image-text', 'Image text', ARRAY['text','image'], ARRAY['text'],
                ARRAY[]::text[],
                '{"max_input_images":2,"max_input_image_bytes":100}'::jsonb),
               ('media', 'Media', ARRAY['text'], ARRAY['video','audio'],
                ARRAY[]::text[],
                '{"max_output_duration_seconds":100}'::jsonb)"""
    )
    reasoning = (
        '[{"level":"none","provider_value":"none"},'
        '{"level":"low","provider_value":"low"},'
        '{"level":"medium","provider_value":"medium"},'
        '{"level":"high","provider_value":"high"}]'
    )
    for api_name, provider, model, provider_name, capabilities, mappings, enabled in (
        (
            "text-one",
            "fake",
            "text",
            "one",
            ["streaming", "reasoning"],
            reasoning,
            True,
        ),
        (
            "text-two",
            "fake",
            "text",
            "two",
            ["streaming", "reasoning"],
            reasoning,
            True,
        ),
        ("embedding", "fake", "embed", "embed", [], "[]", True),
        ("disabled", "off", "text", "off", ["streaming", "reasoning"], reasoning, True),
    ):
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name, enabled,
                    input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, %s,
                      model.input_modalities, model.output_modalities, %s,
                      model.constraints, %s::jsonb
               FROM router.provider_connections AS provider,
                    router.canonical_models AS model
               WHERE provider.api_name = %s AND model.api_name = %s""",
            (
                api_name,
                provider_name,
                enabled,
                capabilities,
                mappings,
                provider,
                model,
            ),
        )
    for index in range(3, 18):
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name, enabled,
                    input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, true,
                      model.input_modalities, model.output_modalities,
                      ARRAY['streaming', 'reasoning'], model.constraints, %s::jsonb
               FROM router.provider_connections AS provider,
                    router.canonical_models AS model
               WHERE provider.api_name = 'fake' AND model.api_name = 'text'""",
            (f"text-{index}", f"text-{index}", reasoning),
        )
    for api_name, model, constraints in (
        ("embedding-four", "embed", '{"embedding_dimensions":[4]}'),
        (
            "image-small",
            "image-text",
            '{"max_input_images":1,"max_input_image_bytes":10}',
        ),
        (
            "image-large",
            "image-text",
            '{"max_input_images":2,"max_input_image_bytes":100}',
        ),
        ("media-short", "media", '{"max_output_duration_seconds":10}'),
        ("media-long", "media", '{"max_output_duration_seconds":100}'),
    ):
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name, enabled,
                    input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, true,
                      model.input_modalities, model.output_modalities,
                      ARRAY[]::text[], %s::jsonb, '[]'::jsonb
               FROM router.provider_connections AS provider,
                    router.canonical_models AS model
               WHERE provider.api_name = 'fake' AND model.api_name = %s""",
            (api_name, api_name, constraints, model),
        )
    connection.execute(
        """UPDATE router.provider_models
           SET constraints = '{"embedding_dimensions":[3]}'::jsonb
           WHERE api_name = 'embedding'"""
    )
