"""PostgreSQL integration tests for service and administrator identity."""
# ruff: noqa: ANN401, D107, PLR0915, PLR2004

from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from pathlib import Path

ADMIN_ORIGIN = "http://127.0.0.1:5174"
ISSUER = "https://identity.example.test"
REDIRECT_URI = "https://llmrouter.opendle.dev/v1/admin/oidc/callback"
ADMIN_SUBJECT = "allowed-subject"


class IdentityTestContext:
    """Test client and safe local administrator controls."""

    def __init__(
        self,
        database_url: str,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.database_url = database_url
        self.settings = settings
        self.controls = ControlKeys.load(settings)
        self.client = TestClient(
            create_app(
                database_url=database_url,
                settings=settings,
                oidc_transport=transport,
            ),
            base_url="https://llmrouter.test",
            follow_redirects=False,
        )
        self.session_token = ""
        self.csrf_token = ""

    def seed_administrator(self, *, expires_at: datetime | None = None) -> None:
        """Create one local session without bypassing request authentication."""
        self.session_token = new_token()
        self.csrf_token = new_token()
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            create_administrator_session(
                connection,
                session_verifier=self.controls.verifier(self.session_token),
                csrf_verifier=self.controls.verifier(self.csrf_token),
                encrypted_csrf_token=self.controls.encrypt(
                    {"csrf_token": self.csrf_token}
                ),
                issuer=ISSUER,
                subject=ADMIN_SUBJECT,
                display_name="Allowed administrator",
                expires_at=expires_at or datetime.now(tz=UTC) + timedelta(hours=1),
            )

    @property
    def admin_headers(self) -> dict[str, str]:
        """Return all controls required for one administrator browser write."""
        return {
            "Cookie": f"llmrouter_admin_session={self.session_token}",
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": self.csrf_token,
        }

    @property
    def admin_read_headers(self) -> dict[str, str]:
        """Return the local session cookie without browser write controls."""
        return {"Cookie": f"llmrouter_admin_session={self.session_token}"}


@pytest.fixture
def identity_settings(tmp_path: Path) -> Settings:
    """Create complete non-production control files."""
    paths: dict[str, Path] = {}
    for name, value in {
        "client": "test-client",
        "secret": "test-client-secret-value",
        "subjects": ADMIN_SUBJECT,
        "digest": "d" * 64,
        "encryption": "e" * 64,
    }.items():
        path = tmp_path / name
        path.write_text(value, encoding="utf-8")
        paths[name] = path
    return Settings(
        public_admin_auth=True,
        oidc_issuer=ISSUER,
        oidc_redirect_uri=REDIRECT_URI,
        oidc_client_id_file=paths["client"],
        oidc_client_secret_file=paths["secret"],
        administrator_subjects_file=paths["subjects"],
        administrator_digest_key_file=paths["digest"],
        administrator_encryption_key_file=paths["encryption"],
        administrator_session_hours=1,
        allowed_origins=(ADMIN_ORIGIN,),
    )


@pytest.fixture
def migrated_database(database_url: str) -> str:
    """Apply the clean schema to one isolated test database."""
    with psycopg.connect(database_url) as connection:
        migrate(connection)
    return database_url


def test_services_workspaces_keys_and_activity_are_isolated(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Enforce administrator, service, workspace, and key boundaries."""
    context = IdentityTestContext(migrated_database, identity_settings)
    context.seed_administrator()
    client = context.client

    denied_origin = client.post(
        "/v1/admin/services",
        json={"api_name": "alpha", "display_name": "Alpha"},
        headers={**context.admin_headers, "Origin": "https://attacker.example"},
    )
    assert denied_origin.status_code == HTTPStatus.FORBIDDEN
    assert denied_origin.json()["error"]["code"] == "permission_denied"
    denied_csrf = client.post(
        "/v1/admin/services",
        json={"api_name": "alpha", "display_name": "Alpha"},
        headers={**context.admin_headers, "X-CSRF-Token": new_token()},
    )
    assert denied_csrf.status_code == HTTPStatus.FORBIDDEN
    duplicate_origin = client.post(
        "/v1/admin/services",
        json={"api_name": "alpha", "display_name": "Alpha"},
        headers=[
            ("Cookie", f"llmrouter_admin_session={context.session_token}"),
            ("Origin", ADMIN_ORIGIN),
            ("Origin", ADMIN_ORIGIN),
            ("X-CSRF-Token", context.csrf_token),
        ],
    )
    assert duplicate_origin.status_code == HTTPStatus.FORBIDDEN

    for api_name in ("alpha", "beta"):
        response = client.post(
            "/v1/admin/services",
            json={"api_name": api_name, "display_name": api_name.title()},
            headers=context.admin_headers,
        )
        assert response.status_code == HTTPStatus.CREATED
        assert response.headers["cache-control"] == "no-store"
        assert set(response.json()) == {
            "api_name",
            "display_name",
            "created_at",
        }

    duplicate = client.post(
        "/v1/admin/services",
        json={"api_name": "alpha", "display_name": "Other"},
        headers=context.admin_headers,
    )
    assert duplicate.status_code == HTTPStatus.CONFLICT
    assert "Other" not in duplicate.text

    keys: dict[str, tuple[str, str]] = {}
    for service_name in ("alpha", "beta"):
        response = client.post(
            f"/v1/admin/services/{service_name}/keys",
            json={"name": f"{service_name} backend"},
            headers=context.admin_headers,
        )
        assert response.status_code == HTTPStatus.CREATED
        assert response.headers["cache-control"] == "no-store"
        document = response.json()
        key_prefix = f"llmr_sk_{document['key']['id']}_"
        assert document["secret"].startswith(key_prefix)
        assert len(document["secret"].removeprefix(key_prefix)) == 43
        assert "secret" not in document["key"]
        keys[service_name] = (document["key"]["id"], document["secret"])

    alpha_secret = keys["alpha"][1]
    with psycopg.connect(migrated_database) as connection:
        verifier = connection.execute(
            "SELECT verifier FROM router.service_api_keys WHERE id = %s",
            (keys["alpha"][0],),
        ).fetchone()
        assert verifier is not None
        assert alpha_secret.encode() not in verifier[0]

    alpha_headers = {"Authorization": f"Bearer {alpha_secret}"}
    beta_headers = {"Authorization": f"Bearer {keys['beta'][1]}"}
    duplicate_authorization = client.get(
        "/v1/workspaces",
        headers=[
            ("Authorization", f"Bearer {alpha_secret}"),
            ("Authorization", f"Bearer {alpha_secret}"),
        ],
    )
    assert duplicate_authorization.status_code == HTTPStatus.UNAUTHORIZED
    created_workspace = client.post(
        "/v1/workspaces",
        json={"api_name": "main", "display_name": "Main"},
        headers=alpha_headers,
    )
    assert created_workspace.status_code == HTTPStatus.CREATED
    assert client.get("/v1/workspaces/main", headers=alpha_headers).status_code == 200
    hidden = client.get("/v1/workspaces/main", headers=beta_headers)
    assert hidden.status_code == HTTPStatus.NOT_FOUND
    assert hidden.json()["error"]["code"] == "not_found"
    beta_workspace = client.post(
        "/v1/workspaces",
        json={"api_name": "main", "display_name": "Beta main"},
        headers=beta_headers,
    )
    assert beta_workspace.status_code == HTTPStatus.CREATED

    service_cannot_administer = client.get("/v1/admin/services", headers=alpha_headers)
    assert service_cannot_administer.status_code == HTTPStatus.UNAUTHORIZED
    administrator_cannot_use_service_api = client.get(
        "/v1/workspaces", headers=context.admin_read_headers
    )
    assert administrator_cannot_use_service_api.status_code == HTTPStatus.UNAUTHORIZED
    assert (
        client.get("/v1/admin/activity", headers=alpha_headers).status_code
        == HTTPStatus.UNAUTHORIZED
    )
    service_key_list = client.get("/v1/service-keys", headers=alpha_headers).json()
    assert alpha_secret not in json.dumps(service_key_list)
    assert all("secret" not in item for item in service_key_list["items"])

    service_created_key = client.post(
        "/v1/service-keys", json={"name": "replacement"}, headers=alpha_headers
    )
    assert service_created_key.status_code == HTTPStatus.CREATED
    replacement = service_created_key.json()
    assert replacement["secret"] not in json.dumps(
        client.get("/v1/service-keys", headers=alpha_headers).json()
    )
    assert (
        client.delete(
            f"/v1/service-keys/{replacement['key']['id']}", headers=alpha_headers
        ).status_code
        == HTTPStatus.NO_CONTENT
    )
    assert (
        client.get(
            "/v1/workspaces",
            headers={"Authorization": f"Bearer {replacement['secret']}"},
        ).status_code
        == HTTPStatus.UNAUTHORIZED
    )

    admin_alpha_workspaces = client.get(
        "/v1/admin/services/alpha/workspaces", headers=context.admin_read_headers
    )
    assert [item["api_name"] for item in admin_alpha_workspaces.json()["items"]] == [
        "main"
    ]
    admin_beta_workspace = client.get(
        "/v1/admin/services/beta/workspaces/main",
        headers=context.admin_read_headers,
    )
    assert admin_beta_workspace.status_code == HTTPStatus.OK
    assert admin_beta_workspace.json()["display_name"] == "Beta main"

    revoked = client.delete(
        f"/v1/admin/services/beta/keys/{keys['beta'][0]}",
        headers=context.admin_headers,
    )
    assert revoked.status_code == HTTPStatus.NO_CONTENT
    assert client.get("/v1/workspaces", headers=beta_headers).status_code == 401

    now = datetime.now(tz=UTC)
    activity = client.get(
        "/v1/admin/activity",
        params={
            "from": (now - timedelta(hours=1)).isoformat(),
            "to": (now + timedelta(hours=1)).isoformat(),
        },
        headers=context.admin_read_headers,
    )
    assert activity.status_code == HTTPStatus.OK
    activity_text = activity.text
    for key_secret in (
        alpha_secret,
        keys["beta"][1],
        replacement["secret"],
    ):
        assert key_secret not in activity_text
    assert context.csrf_token not in activity_text
    assert {item["action"] for item in activity.json()["items"]} >= {
        "service.create",
        "service_key.create",
        "workspace.create",
        "service_key.revoke",
    }
    expected_administrator_actor = (
        "oidc:" + hashlib.sha256(f"{ISSUER}\0{ADMIN_SUBJECT}".encode()).hexdigest()
    )
    actors = {item["actor_subject"] for item in activity.json()["items"]}
    assert expected_administrator_actor in actors
    assert f"service:alpha:key:{keys['alpha'][0]}" in actors
    workspace_targets = {
        (item.get("service_api_name"), item.get("resource_api_name"))
        for item in activity.json()["items"]
        if item["action"] == "workspace.create"
    }
    assert workspace_targets >= {("alpha", "main"), ("beta", "main")}
    service_key_events = [
        item
        for item in activity.json()["items"]
        if item["resource_type"] == "service_key"
    ]
    expected_key_ids = {
        keys["alpha"][0],
        keys["beta"][0],
        replacement["key"]["id"],
    }
    assert {item["resource_id"] for item in service_key_events} >= expected_key_ids
    assert all(
        item["service_api_name"] in {"alpha", "beta"}
        and "resource_api_name" not in item
        and "resource_id" in item
        for item in service_key_events
    )
    service_events = [
        item for item in activity.json()["items"] if item["resource_type"] == "service"
    ]
    assert all(
        "service_api_name" not in item
        and "resource_id" in item
        and item["resource_api_name"] in {"alpha", "beta"}
        for item in service_events
    )
    assert all(
        "resource_id" in item
        for item in activity.json()["items"]
        if item["resource_type"] == "workspace"
    )


def test_parent_cycles_are_atomic_and_child_deletion_is_blocked(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Reject cycles without changing the current service tree."""
    context = IdentityTestContext(migrated_database, identity_settings)
    context.seed_administrator()
    client = context.client
    for document in (
        {"api_name": "root", "display_name": "Root"},
        {
            "api_name": "child",
            "display_name": "Child",
            "parent_service_api_name": "root",
        },
    ):
        assert (
            client.post(
                "/v1/admin/services", json=document, headers=context.admin_headers
            ).status_code
            == HTTPStatus.CREATED
        )

    blocked_delete = client.delete(
        "/v1/admin/services/root", headers=context.admin_headers
    )
    assert blocked_delete.status_code == HTTPStatus.CONFLICT
    cycle = client.put(
        "/v1/admin/services/root",
        json={"display_name": "Changed", "parent_service_api_name": "child"},
        headers=context.admin_headers,
    )
    assert cycle.status_code == HTTPStatus.CONFLICT
    current = client.get(
        "/v1/admin/services/root", headers=context.admin_read_headers
    ).json()
    assert current["display_name"] == "Root"
    assert "parent_service_api_name" not in current

    invalid_names = ("A", "1starts", "ends-", "a" * 64)
    for api_name in invalid_names:
        response = client.post(
            "/v1/admin/services",
            json={"api_name": api_name, "display_name": "Invalid"},
            headers=context.admin_headers,
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json()["error"]["code"] == "invalid_request"


def test_activity_targets_stay_unambiguous_after_api_name_reuse(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Keep one stable resource ID for each historical service instance."""
    context = IdentityTestContext(migrated_database, identity_settings)
    context.seed_administrator()
    for _ in range(2):
        created = context.client.post(
            "/v1/admin/services",
            json={"api_name": "reused", "display_name": "Reused"},
            headers=context.admin_headers,
        )
        assert created.status_code == HTTPStatus.CREATED
        if _ == 0:
            deleted = context.client.delete(
                "/v1/admin/services/reused", headers=context.admin_headers
            )
            assert deleted.status_code == HTTPStatus.NO_CONTENT
    now = datetime.now(tz=UTC)
    events = context.client.get(
        "/v1/admin/activity",
        params={
            "from": (now - timedelta(minutes=1)).isoformat(),
            "to": (now + timedelta(minutes=1)).isoformat(),
        },
        headers=context.admin_read_headers,
    ).json()["items"]
    reused = [item for item in events if item.get("resource_api_name") == "reused"]
    creates = [item for item in reused if item["action"] == "service.create"]
    deleted = next(item for item in reused if item["action"] == "service.delete")
    assert len(creates) == 2
    assert creates[0]["resource_id"] != creates[1]["resource_id"]
    assert deleted["resource_id"] in {item["resource_id"] for item in creates}


def test_concurrent_opposite_parent_moves_create_no_cycle(
    migrated_database: str,
) -> None:
    """Serialize concurrent parent changes at the database boundary."""
    with psycopg.connect(migrated_database) as connection:
        first_row = connection.execute(
            """INSERT INTO router.services (api_name, display_name)
               VALUES ('one', 'One') RETURNING id"""
        ).fetchone()
        second_row = connection.execute(
            """INSERT INTO router.services (api_name, display_name)
               VALUES ('two', 'Two') RETURNING id"""
        ).fetchone()
        assert first_row is not None
        assert second_row is not None
        first_id = first_row[0]
        second_id = second_row[0]

    barrier = threading.Barrier(2)
    results: list[str] = []

    def move(service: Any, parent: Any) -> None:
        try:
            with psycopg.connect(migrated_database) as connection:
                barrier.wait()
                connection.execute(
                    "UPDATE router.services SET parent_service_id = %s WHERE id = %s",
                    (parent, service),
                )
            results.append("saved")
        except psycopg.errors.CheckViolation:
            results.append("cycle")

    threads = (
        threading.Thread(target=move, args=(first_id, second_id)),
        threading.Thread(target=move, args=(second_id, first_id)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == ["cycle", "saved"]


def test_workspace_delete_cascades_and_blocks_late_media_results(
    migrated_database: str,
) -> None:
    """Delete all dependent roots and reject a result after public deletion."""
    with psycopg.connect(migrated_database) as connection:
        service_row = connection.execute(
            """INSERT INTO router.services (api_name, display_name)
               VALUES ('media', 'Media') RETURNING id"""
        ).fetchone()
        assert service_row is not None
        service = service_row[0]
        workspace_row = connection.execute(
            """INSERT INTO router.workspaces (service_id, api_name, display_name)
               VALUES (%s, 'main', 'Main') RETURNING id""",
            (service,),
        ).fetchone()
        assert workspace_row is not None
        workspace = workspace_row[0]
        job_row = connection.execute(
            """INSERT INTO router.media_jobs
                   (service_id, workspace_id, provider_model_api_name)
               VALUES (%s, %s, 'example') RETURNING id, state""",
            (service, workspace),
        ).fetchone()
        assert job_row is not None
        job = job_row[0]
        assert job_row[1] == "pending"
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.media_jobs
                       (service_id, workspace_id, provider_model_api_name, state)
                   VALUES (%s, %s, 'example', 'queued')""",
                (service, workspace),
            )
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.media_jobs
                       (service_id, workspace_id, provider_model_api_name, payload)
                   VALUES (%s, %s, 'example', '[]'::jsonb)""",
                (service, workspace),
            )
        connection.execute(
            "UPDATE router.media_jobs SET state = 'running' WHERE id = %s", (job,)
        )
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                "UPDATE router.media_jobs SET state = 'pending' WHERE id = %s", (job,)
            )
        connection.execute(
            """UPDATE router.media_jobs
               SET state = 'succeeded', completed_at = statement_timestamp()
               WHERE id = %s""",
            (job,),
        )
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                "UPDATE router.media_jobs SET state = 'failed' WHERE id = %s", (job,)
            )
        accounting_call = connection.execute(
            """INSERT INTO router.raw_accounting_calls
                   (service_id, workspace_id, outcome, started_at, completed_at)
               VALUES (%s, %s, 'failed', statement_timestamp(), statement_timestamp())
               RETURNING id""",
            (service, workspace),
        ).fetchone()
        assert accounting_call is not None
        connection.execute(
            """INSERT INTO router.request_logs
                   (logical_call_id, service_id, workspace_id, kind, outcome,
                    request_json, started_at)
               VALUES (%s, %s, %s, 'model', 'failed', '{}',
                       statement_timestamp())""",
            (accounting_call[0], service, workspace),
        )
        connection.execute(
            """INSERT INTO router.raw_accounting_attempts
                   (id, call_id, service_id, workspace_id, position,
                    provider_connection_api_name, provider_model_api_name,
                    outcome, usage, applied_price, cost, currency,
                    failure_class, started_at, completed_at)
               VALUES (gen_random_uuid(), %s, %s, %s, 0, 'example', 'example', 'failed',
                       '[{"unit":"request","quantity":"1"}]',
                       '{"currency":"USD","unit_prices":[{"unit":"request","amount":"0"}]}',
                       0, 'USD', 'upstream_failed', statement_timestamp(),
                       statement_timestamp())""",
            (accounting_call[0], service, workspace),
        )
        connection.execute(
            """INSERT INTO router.daily_accounting
                   (service_id, workspace_id, day, provider_model_api_name,
                    outcome, tags, usage_unit, currency, calls, attempts,
                    quantity, cost)
               VALUES (%s, %s, CURRENT_DATE, 'example', 'failed', '{}',
                       'request', 'USD', 1, 1, 1, 0)""",
            (service, workspace),
        )
        connection.execute(
            """INSERT INTO router.media_objects
                   (service_id, workspace_id, media_job_id, object_key,
                    media_type, role, size_bytes, content_sha256)
               VALUES (%s, %s, %s, 'object', 'image/png', 'output', 1,
                       decode(repeat('00', 32), 'hex'))""",
            (service, workspace, job),
        )
        with (
            pytest.raises(psycopg.errors.UniqueViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.media_objects
                       (service_id, workspace_id, media_job_id, object_key,
                        media_type, role, size_bytes, content_sha256)
                   VALUES (%s, %s, %s, 'object', 'image/png', 'output', 1,
                           decode(repeat('00', 32), 'hex'))""",
                (service, workspace, job),
            )
        connection.execute("DELETE FROM router.workspaces WHERE id = %s", (workspace,))
        for table in (
            "request_logs",
            "raw_accounting_attempts",
            "raw_accounting_calls",
            "daily_accounting",
            "media_jobs",
            "media_objects",
        ):
            assert connection.execute(
                f"SELECT count(*) FROM router.{table}"  # noqa: S608
            ).fetchone() == (0,)
        with (
            pytest.raises(psycopg.errors.ForeignKeyViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.media_objects
                           (service_id, workspace_id, media_job_id, object_key,
                            media_type, role, size_bytes, content_sha256)
                       VALUES (%s, %s, %s, 'late', 'video/mp4', 'output', 1,
                               decode(repeat('00', 32), 'hex'))""",
                (service, workspace, job),
            )
        connection.execute(
            """INSERT INTO router.assignment_definitions
                   (service_id, api_name)
               VALUES (%s, 'default')""",
            (service,),
        )
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.assignment_definitions
                       (service_id, api_name)
                   VALUES (%s, 'Invalid')""",
                (service,),
            )
        connection.execute(
            """INSERT INTO router.service_api_keys
                   (service_id, name, verifier)
               VALUES (%s, 'remaining', %s)""",
            (service, b"v" * 32),
        )
        connection.execute("DELETE FROM router.services WHERE id = %s", (service,))
        assert connection.execute(
            "SELECT count(*) FROM router.assignment_definitions"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.service_api_keys"
        ).fetchone() == (0,)


class OidcMock:
    """Deterministic signed OIDC provider for local integration tests."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(
            public_exponent=65_537, key_size=2_048
        )
        public = self.private_key.public_key().public_numbers()
        self.jwk = {
            "kty": "RSA",
            "kid": "test-key",
            "use": "sig",
            "alg": "RS256",
            "e": _integer_b64(public.e),
            "n": _integer_b64(public.n),
        }
        self.nonce = ""
        self.code_mode = "valid"
        self.token_form: dict[str, list[str]] = {}
        self.token_authorization = ""
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        """Serve discovery, token, and JWKS responses."""
        if request.url.path == "/.well-known/openid-configuration":
            if self.code_mode == "oversized_discovery":
                return httpx.Response(200, headers={"Content-Length": "1000001"})
            if self.code_mode == "duplicate_discovery":
                return httpx.Response(
                    200,
                    content=(
                        '{"issuer":"https://identity.example.test",'
                        '"issuer":"https://identity.example.test"}'
                    ),
                )
            if self.code_mode == "malformed_discovery":
                return httpx.Response(200, content="{")
            token_auth_methods = (
                ["client_secret_post"]
                if self.code_mode == "unsupported_token_auth"
                else ["client_secret_basic"]
            )
            document: dict[str, Any] = {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": (
                    "http://127.0.0.1:1/token"
                    if self.code_mode == "insecure_endpoint"
                    else f"{ISSUER}/token"
                ),
                "jwks_uri": f"{ISSUER}/jwks",
            }
            if self.code_mode != "omitted_token_auth":
                document["token_endpoint_auth_methods_supported"] = token_auth_methods
            return httpx.Response(
                200,
                json=document,
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [self.jwk]})
        if request.url.path == "/token":
            self.token_form = parse_qs(request.content.decode(), strict_parsing=True)
            self.token_authorization = request.headers.get("Authorization", "")
            if self.code_mode == "token_failure":
                return httpx.Response(HTTPStatus.UNAUTHORIZED, json={"error": "denied"})
            return httpx.Response(200, json={"id_token": self._token()})
        return httpx.Response(404)

    def _token(self) -> str:  # noqa: C901
        now = datetime.now(tz=UTC).timestamp()
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": ADMIN_SUBJECT,
            "aud": "test-client",
            "exp": now + 300,
            "iat": now,
            "nonce": self.nonce,
            "name": "Allowed administrator",
        }
        if self.code_mode == "issuer":
            claims["iss"] = "https://attacker.example"
        elif self.code_mode == "audience":
            claims["aud"] = "other-client"
        elif self.code_mode == "expiry":
            claims["exp"] = now - 1
        elif self.code_mode == "nonce":
            claims["nonce"] = "other"
        elif self.code_mode == "subject":
            claims["sub"] = "not-allowed"
        elif self.code_mode == "issued_at":
            claims.pop("iat")
        elif self.code_mode == "duplicate_audience":
            claims["aud"] = ["test-client", "test-client"]
        elif self.code_mode == "oversized_numeric_date":
            claims["exp"] = 10**400
        if self.code_mode == "duplicate_claim":
            payload = json.dumps(claims, separators=(",", ":"))
            payload = payload[:-1] + ',"aud":"test-client"}'
            token = _signed_token_payload(self.private_key, payload)
        elif self.code_mode == "non_finite_time":
            claims["exp"] = float("inf")
            payload = json.dumps(claims, separators=(",", ":")).replace(
                '"exp":Infinity', '"exp":1e400'
            )
            token = _signed_token_payload(self.private_key, payload)
        else:
            token = _signed_token(self.private_key, claims)
        if self.code_mode == "signature":
            token = f"{token.rsplit('.', 1)[0]}.{_b64(b'bad-signature')}"
        return token


def test_oidc_pkce_callback_session_csrf_logout_and_expiry(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Complete the safe OIDC and local-session lifecycle."""
    provider = OidcMock()
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    client = context.client

    for unsafe in (
        "//attacker.example/path",
        "https://attacker.example/",
        "/bad\\path",
    ):
        response = client.post("/v1/admin/session/start", json={"return_to": unsafe})
        assert response.status_code == HTTPStatus.BAD_REQUEST

    started = client.post(
        "/v1/admin/session/start", json={"return_to": "/services?selected=one"}
    )
    assert started.status_code == HTTPStatus.OK
    assert started.headers["cache-control"] == "no-store"
    authorization = urlsplit(started.json()["authorization_url"])
    query = parse_qs(authorization.query, strict_parsing=True)
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    flow_binding = started.cookies["llmrouter_admin_oidc_flow"]
    assert flow_binding not in started.text
    assert flow_binding not in started.json()["authorization_url"]
    flow_cookie = started.headers["set-cookie"]
    assert "Secure" in flow_cookie
    assert "HttpOnly" in flow_cookie
    assert "SameSite=lax" in flow_cookie
    assert "Domain=" not in flow_cookie
    assert "Path=/v1/admin/oidc/callback" in flow_cookie
    with psycopg.connect(migrated_database) as connection:
        stored_flow = connection.execute(
            "SELECT encrypted_control FROM router.administrator_oidc_flows"
        ).fetchone()
        assert stored_flow is not None
        assert flow_binding.encode() not in stored_flow[0]
    provider.nonce = query["nonce"][0]

    wrong_state = client.get(
        "/v1/admin/oidc/callback", params={"code": "code", "state": new_token()}
    )
    assert wrong_state.status_code == HTTPStatus.UNAUTHORIZED
    completed = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": query["state"][0]},
    )
    assert completed.status_code == HTTPStatus.SEE_OTHER
    assert completed.headers["cache-control"] == "no-store"
    assert completed.headers["location"] == "/services?selected=one"
    cookie = completed.headers["set-cookie"]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie
    assert any(
        header.startswith("llmrouter_admin_oidc_flow=") and "Max-Age=0" in header
        for header in completed.headers.get_list("set-cookie")
    )
    assert provider.token_form["redirect_uri"] == [REDIRECT_URI]
    assert provider.token_form["code_verifier"]
    assert "client_id" not in provider.token_form
    assert "client_secret" not in provider.token_form
    expected_basic = (
        "Basic " + base64.b64encode(b"test-client:test-client-secret-value").decode()
    )
    assert provider.token_authorization == expected_basic
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(provider.token_form["code_verifier"][0].encode()).digest()
    ).rstrip(b"=")
    assert query["code_challenge"] == [challenge.decode()]

    session_token = completed.cookies["llmrouter_admin_session"]
    read_headers = {"Cookie": f"llmrouter_admin_session={session_token}"}
    session = client.get("/v1/admin/session", headers=read_headers)
    assert session.status_code == HTTPStatus.OK
    assert session.headers["cache-control"] == "no-store"
    duplicate_session_cookie = client.get(
        "/v1/admin/session",
        headers={
            "Cookie": (
                f"llmrouter_admin_session={session_token}; "
                f"llmrouter_admin_session={session_token}"
            )
        },
    )
    assert duplicate_session_cookie.status_code == HTTPStatus.UNAUTHORIZED
    csrf_token = session.json()["csrf_token"]
    assert session.json()["subject"] == ADMIN_SUBJECT
    write_headers = {
        **read_headers,
        "Origin": ADMIN_ORIGIN,
        "X-CSRF-Token": csrf_token,
    }
    assert (
        client.post(
            "/v1/admin/services",
            json={"api_name": "signed-in", "display_name": "Signed in"},
            headers=write_headers,
        ).status_code
        == HTTPStatus.CREATED
    )
    logged_out = client.delete("/v1/admin/session", headers=write_headers)
    assert logged_out.status_code == HTTPStatus.NO_CONTENT
    assert client.get("/v1/admin/session", headers=read_headers).status_code == 401
    replay = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": query["state"][0]},
    )
    assert replay.status_code == HTTPStatus.UNAUTHORIZED
    assert client.get("/v1/admin/session/callback").status_code == 404

    context.seed_administrator(expires_at=datetime.now(tz=UTC) - timedelta(seconds=1))
    assert (
        client.get("/v1/admin/session", headers=context.admin_read_headers).status_code
        == HTTPStatus.UNAUTHORIZED
    )


def test_corrupt_session_control_data_fails_as_authentication(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Return one safe authentication result for invalid encrypted control data."""
    context = IdentityTestContext(migrated_database, identity_settings)
    context.seed_administrator()
    with psycopg.connect(migrated_database) as connection:
        connection.execute(
            "UPDATE router.administrator_sessions SET encrypted_csrf_token = %s",
            (b"invalid",),
        )
    response = context.client.get(
        "/v1/admin/session", headers=context.admin_read_headers
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"


def test_oidc_flow_is_bound_to_the_starting_browser(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Do not let another browser consume the valid one-time state."""
    provider = OidcMock()
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    client = context.client

    missing_start = client.post(
        "/v1/admin/session/start", json={"return_to": "/services"}
    )
    missing_query = parse_qs(urlsplit(missing_start.json()["authorization_url"]).query)
    missing_binding = missing_start.cookies["llmrouter_admin_oidc_flow"]
    client.cookies.clear()
    missing = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": missing_query["state"][0]},
    )
    assert missing.status_code == HTTPStatus.UNAUTHORIZED
    assert "location" not in missing.headers
    provider.nonce = missing_query["nonce"][0]
    missing_retry = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": missing_query["state"][0]},
        headers={"Cookie": f"llmrouter_admin_oidc_flow={missing_binding}"},
    )
    assert missing_retry.status_code == HTTPStatus.SEE_OTHER
    missing_replay = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": missing_query["state"][0]},
        headers={"Cookie": f"llmrouter_admin_oidc_flow={missing_binding}"},
    )
    assert missing_replay.status_code == HTTPStatus.UNAUTHORIZED

    wrong_start = client.post(
        "/v1/admin/session/start", json={"return_to": "/services"}
    )
    wrong_query = parse_qs(urlsplit(wrong_start.json()["authorization_url"]).query)
    correct_binding = wrong_start.cookies["llmrouter_admin_oidc_flow"]
    client.cookies.clear()
    wrong = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": wrong_query["state"][0]},
        headers={"Cookie": f"llmrouter_admin_oidc_flow={new_token()}"},
    )
    assert wrong.status_code == HTTPStatus.UNAUTHORIZED
    assert "location" not in wrong.headers
    provider.nonce = wrong_query["nonce"][0]
    wrong_retry = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": wrong_query["state"][0]},
        headers={"Cookie": f"llmrouter_admin_oidc_flow={correct_binding}"},
    )
    assert wrong_retry.status_code == HTTPStatus.SEE_OTHER
    wrong_replay = client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": wrong_query["state"][0]},
        headers={"Cookie": f"llmrouter_admin_oidc_flow={correct_binding}"},
    )
    assert wrong_replay.status_code == HTTPStatus.UNAUTHORIZED
    with psycopg.connect(migrated_database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.administrator_sessions"
        ).fetchone() == (2,)


def test_oidc_rejects_duplicate_query_controls_without_consuming_state(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Reject duplicate callback controls and keep the valid browser flow."""
    provider = OidcMock()
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    started = context.client.post("/v1/admin/session/start", json={"return_to": "/"})
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    provider.nonce = query["nonce"][0]
    duplicate = context.client.get(
        "/v1/admin/oidc/callback",
        params=[
            ("code", "code"),
            ("state", query["state"][0]),
            ("state", query["state"][0]),
        ],
    )
    assert duplicate.status_code == HTTPStatus.BAD_REQUEST
    assert duplicate.headers["cache-control"] == "no-store"
    completed = context.client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": query["state"][0]},
    )
    assert completed.status_code == HTTPStatus.SEE_OTHER


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("issuer", HTTPStatus.UNAUTHORIZED),
        ("audience", HTTPStatus.UNAUTHORIZED),
        ("expiry", HTTPStatus.UNAUTHORIZED),
        ("nonce", HTTPStatus.UNAUTHORIZED),
        ("signature", HTTPStatus.UNAUTHORIZED),
        ("issued_at", HTTPStatus.UNAUTHORIZED),
        ("duplicate_audience", HTTPStatus.UNAUTHORIZED),
        ("duplicate_claim", HTTPStatus.UNAUTHORIZED),
        ("non_finite_time", HTTPStatus.UNAUTHORIZED),
        ("oversized_numeric_date", HTTPStatus.UNAUTHORIZED),
        ("subject", HTTPStatus.FORBIDDEN),
    ],
)
def test_oidc_rejects_invalid_identity_controls(
    migrated_database: str,
    identity_settings: Settings,
    mode: str,
    expected_status: HTTPStatus,
) -> None:
    """Reject each invalid signature, authority, replay, and allowlist input."""
    provider = OidcMock()
    provider.code_mode = mode
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    started = context.client.post("/v1/admin/session/start", json={"return_to": "/"})
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    provider.nonce = query["nonce"][0]
    response = context.client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": query["state"][0]},
    )
    assert response.status_code == expected_status
    assert "location" not in response.headers
    assert "test-client-secret-value" not in response.text
    assert ADMIN_SUBJECT not in response.text
    replay = context.client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": query["state"][0]},
    )
    assert replay.status_code == HTTPStatus.UNAUTHORIZED


def test_oidc_rejects_an_oversized_provider_document(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Bound provider documents before the client reads their body."""
    provider = OidcMock()
    provider.code_mode = "oversized_discovery"
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    response = context.client.post("/v1/admin/session/start", json={"return_to": "/"})
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "mode", ["duplicate_discovery", "malformed_discovery", "insecure_endpoint"]
)
def test_oidc_rejects_duplicate_or_malformed_provider_data(
    migrated_database: str, identity_settings: Settings, mode: str
) -> None:
    """Reject invalid provider JSON before an authorization flow starts."""
    provider = OidcMock()
    provider.code_mode = mode
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    response = context.client.post("/v1/admin/session/start", json={"return_to": "/"})
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["cache-control"] == "no-store"


def test_oidc_rejects_an_incompatible_token_authentication_method(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Require confidential-client HTTP Basic when discovery declares methods."""
    provider = OidcMock()
    provider.code_mode = "unsupported_token_auth"
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    response = context.client.post("/v1/admin/session/start", json={"return_to": "/"})
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert "location" not in response.headers


def test_oidc_defaults_to_basic_token_authentication(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Use the OpenID Connect Basic default when discovery omits the field."""
    provider = OidcMock()
    provider.code_mode = "omitted_token_auth"
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    started = context.client.post("/v1/admin/session/start", json={"return_to": "/"})
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    provider.nonce = query["nonce"][0]
    completed = context.client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": query["state"][0]},
    )
    assert completed.status_code == HTTPStatus.SEE_OTHER
    assert provider.token_authorization.startswith("Basic ")
    assert "client_secret" not in provider.token_form


def test_oidc_token_failure_excludes_confidential_client_secrets(
    migrated_database: str, identity_settings: Settings
) -> None:
    """Return one safe error when the confidential token request fails."""
    provider = OidcMock()
    provider.code_mode = "token_failure"
    context = IdentityTestContext(
        migrated_database, identity_settings, transport=provider.transport
    )
    started = context.client.post("/v1/admin/session/start", json={"return_to": "/"})
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    failed = context.client.get(
        "/v1/admin/oidc/callback",
        params={"code": "code", "state": query["state"][0]},
    )
    assert failed.status_code == HTTPStatus.UNAUTHORIZED
    assert "location" not in failed.headers
    assert "test-client-secret-value" not in failed.text
    assert provider.token_authorization not in failed.text


def test_session_duration_configuration_is_bounded(identity_settings: Settings) -> None:
    """Reject session expiry outside the accepted 1-hour to 30-day range."""
    values = {
        field: getattr(identity_settings, field)
        for field in identity_settings.__slots__
    }
    for hours in (0, 721):
        values["administrator_session_hours"] = hours
        with pytest.raises(ValueError, match="1 hour to 30 days"):
            Settings(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oidc_issuer", "http://identity.example.test"),
        ("oidc_issuer", "https://identity.example.test/path"),
        ("oidc_issuer", "https://identity.example.test?"),
        ("oidc_redirect_uri", "https://llmrouter.opendle.dev/other-callback"),
        ("oidc_redirect_uri", "http://llmrouter.opendle.dev/v1/admin/oidc/callback"),
    ],
)
def test_oidc_authorities_and_callback_configuration_are_exact(
    identity_settings: Settings, field: str, value: str
) -> None:
    """Reject an unsafe issuer or a redirect that cannot reach the callback."""
    values = {
        setting: getattr(identity_settings, setting)
        for setting in identity_settings.__slots__
    }
    values[field] = value
    with pytest.raises(ValueError, match="OpenID Connect"):
        Settings(**values)


@pytest.mark.parametrize(
    "origins",
    [
        (ADMIN_ORIGIN, ADMIN_ORIGIN),
        ("http://administration.example.test",),
        (f"{ADMIN_ORIGIN}/path",),
    ],
)
def test_administrator_origin_configuration_is_exact(
    identity_settings: Settings, origins: tuple[str, ...]
) -> None:
    """Reject duplicate, insecure, or path-bearing browser origins."""
    values = {
        setting: getattr(identity_settings, setting)
        for setting in identity_settings.__slots__
    }
    values["allowed_origins"] = origins
    with pytest.raises(ValueError, match="origin"):
        Settings(**values)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _integer_b64(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _signed_token(private_key: rsa.RSAPrivateKey, claims: dict[str, Any]) -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": "test-key"}).encode())
    return _signed_token_payload(private_key, json.dumps(claims), header=header)


def _signed_token_payload(
    private_key: rsa.RSAPrivateKey, payload_json: str, *, header: str | None = None
) -> str:
    header = header or _b64(json.dumps({"alg": "RS256", "kid": "test-key"}).encode())
    payload = _b64(payload_json.encode())
    signed = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{signed.decode()}.{_b64(signature)}"
