"""Detailed request log, object storage, and retention integration tests."""
# ruff: noqa: D107, PLR2004

from __future__ import annotations

import dataclasses
import importlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import create_app
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.diagnostics import (
    CapturedMedia,
    DetailedLogWrite,
    apply_retention_and_cleanup,
    cleanup_health,
    write_detailed_log_best_effort,
)
from llmrouter_backend.models import RequestAttempt
from llmrouter_backend.object_store import (
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    StoredObject,
)
from llmrouter_backend.security import ControlKeys, new_token
from llmrouter_backend.store import create_administrator_session, create_key
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from pathlib import Path

ADMIN_ORIGIN = "http://127.0.0.1:5174"
MODEL_CONTENT = (
    '{"messages":[{"role":"user","content":"Authorization: Bearer secret-like-text; '
    'csrf_token=candidate; AKIAEXAMPLE"}],"tools":[{"name":"exact"}]}'
)
MEDIA_BYTES = b"\x89PNG\r\n\x1a\nretained-model-input"


class MemoryObjectStore:
    """Deterministic S3-compatible behavior for route tests."""

    def __init__(self, values: dict[str, tuple[bytes, str]] | None = None) -> None:
        self.values = values if values is not None else {}
        self.fail_put = False
        self.uncertain_put = False
        self.fail_get = False
        self.fail_delete = False
        self.put_calls = 0

    def put(self, key: str, body: bytes, content_type: str) -> None:
        """Store one object or raise the configured safe failure."""
        self.put_calls += 1
        if self.fail_put:
            raise ObjectStoreError
        self.values[key] = (body, content_type)
        if self.uncertain_put:
            raise TimeoutError

    def get(self, key: str, maximum_bytes: int = 1024 * 1024 * 1024) -> StoredObject:
        """Read one object or raise the configured safe failure."""
        if self.fail_get:
            raise ObjectStoreError
        try:
            body, content_type = self.values[key]
        except KeyError:
            raise ObjectNotFoundError from None
        if len(body) > maximum_bytes:
            raise ObjectStoreError
        return StoredObject(body, content_type)

    def delete(self, key: str) -> None:
        """Delete one object or raise the configured safe failure."""
        if self.fail_delete:
            raise ObjectStoreError
        self.values.pop(key, None)

    def healthy(self) -> bool:
        """Report whether a configured failure is active."""
        return not (self.fail_get or self.fail_put or self.fail_delete)


class LogContext:
    """One isolated administrator, service, workspace, and object store."""

    def __init__(self, database_url: str, tmp_path: Path) -> None:
        self.database_url = database_url
        paths: dict[str, Path] = {}
        for name, value in {
            "client": "client",
            "secret": "client-secret",
            "subjects": "administrator-subject",
            "digest": "d" * 64,
            "encryption": "e" * 64,
        }.items():
            path = tmp_path / name
            path.write_text(value, encoding="utf-8")
            paths[name] = path
        self.settings = Settings(
            public_admin_auth=True,
            oidc_issuer="https://identity.example.test",
            oidc_redirect_uri=("https://llmrouter.opendle.dev/v1/admin/oidc/callback"),
            oidc_client_id_file=paths["client"],
            oidc_client_secret_file=paths["secret"],
            administrator_subjects_file=paths["subjects"],
            administrator_digest_key_file=paths["digest"],
            administrator_encryption_key_file=paths["encryption"],
            allowed_origins=(ADMIN_ORIGIN,),
        )
        self.controls = ControlKeys.load(self.settings)
        self.objects = MemoryObjectStore()
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            migrate(connection)
            service = connection.execute(
                """INSERT INTO router.services (api_name, display_name)
                   VALUES ('alpha', 'Alpha') RETURNING id"""
            ).fetchone()
            assert service is not None
            workspace = connection.execute(
                """INSERT INTO router.workspaces (service_id, api_name, display_name)
                   VALUES (%s, 'primary', 'Primary') RETURNING id""",
                (service["id"],),
            ).fetchone()
            assert workspace is not None
            self.service_id = service["id"]
            self.workspace_id = workspace["id"]
            self.service_key = create_key(
                connection,
                service_id=self.service_id,
                name="runtime",
                actor_subject="test:setup",
                control_keys=self.controls,
            )[1]
            self.session_token = new_token()
            self.csrf_token = new_token()
            create_administrator_session(
                connection,
                session_verifier=self.controls.verifier(self.session_token),
                csrf_verifier=self.controls.verifier(self.csrf_token),
                encrypted_csrf_token=self.controls.encrypt(
                    {"csrf_token": self.csrf_token}
                ),
                issuer="https://identity.example.test",
                subject="administrator-subject",
                display_name="Administrator",
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            )
        self.client = TestClient(
            create_app(
                database_url=database_url,
                settings=self.settings,
                object_store=cast("ObjectStore", self.objects),
            ),
            base_url="https://llmrouter.test",
        )

    @property
    def read_headers(self) -> dict[str, str]:
        """Return one administrator read session."""
        return {"Cookie": f"llmrouter_admin_session={self.session_token}"}

    @property
    def write_headers(self) -> dict[str, str]:
        """Return one complete administrator browser write authority."""
        return {
            **self.read_headers,
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": self.csrf_token,
        }

    def write_log(
        self,
        *,
        started_at: datetime | None = None,
        media: tuple[CapturedMedia, ...] | None = None,
    ) -> uuid.UUID:
        """Write one complete default detailed log."""
        call_id = uuid.uuid4()
        call_started = started_at or datetime.now(tz=UTC)
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """INSERT INTO router.raw_accounting_calls
                       (id, service_id, workspace_id, assignment_api_name,
                        outcome, started_at, completed_at)
                   VALUES (%s, %s, %s, 'default', 'succeeded', %s, %s)""",
                (
                    call_id,
                    self.service_id,
                    self.workspace_id,
                    call_started,
                    call_started,
                ),
            )
        value = DetailedLogWrite(
            service_id=self.service_id,
            workspace_id=self.workspace_id,
            assignment_api_name="default",
            provider_model_api_name="fake-model",
            kind="model",
            outcome="succeeded",
            request_json=MODEL_CONTENT,
            response_json='{"content":"complete model result"}',
            attempts=(_attempt(),),
            tags=("zeta", "alpha", "alpha"),
            started_at=call_started,
            media=media
            if media is not None
            else (CapturedMedia(MEDIA_BYTES, "image/png", "input"),),
            accounting_call_id=call_id,
        )
        result = write_detailed_log_best_effort(
            self.database_url, cast("ObjectStore", self.objects), value
        )
        assert result is not None
        return result


def _attempt() -> RequestAttempt:
    return RequestAttempt.model_validate(
        {
            "provider_model_api_name": "fake-model",
            "outcome": "succeeded",
            "started_at": datetime.now(tz=UTC),
            "completed_at": datetime.now(tz=UTC),
            "usage": {
                "units": [{"unit": "input_token", "quantity": "7"}],
                "cost": "0.01",
                "currency": "USD",
            },
            "applied_prices": {
                "currency": "USD",
                "unit_prices": [{"unit": "input_token", "amount": "0.001"}],
                "source": "test",
            },
        }
    )


def test_complete_logs_preserve_model_content_and_hide_control_data(
    database_url: str, tmp_path: Path
) -> None:
    """Keep arbitrary model text exact and expose it only to an administrator."""
    context = LogContext(database_url, tmp_path)
    log_id = context.write_log()
    now = datetime.now(tz=UTC)
    parameters = {
        "from": (now - timedelta(days=1)).isoformat(),
        "to": (now + timedelta(days=1)).isoformat(),
        "limit": 1,
    }
    assert (
        context.client.get("/v1/admin/request-logs", params=parameters).status_code
        == 401
    )
    denied = context.client.get(
        "/v1/admin/request-logs",
        params=parameters,
        headers={"Authorization": f"Bearer {context.service_key}"},
    )
    assert denied.status_code == HTTPStatus.UNAUTHORIZED

    page = context.client.get(
        "/v1/admin/request-logs", params=parameters, headers=context.read_headers
    )
    assert page.status_code == HTTPStatus.OK
    assert page.headers["cache-control"] == "no-store"
    assert page.json()["items"][0]["tags"] == ["alpha", "zeta"]
    assert "object" not in page.text.lower()

    complete = context.client.get(
        f"/v1/admin/request-logs/{log_id}", headers=context.read_headers
    )
    assert complete.status_code == HTTPStatus.OK
    assert complete.json()["request_json"] == MODEL_CONTENT
    assert complete.json()["response_json"] == '{"content":"complete model result"}'
    media = complete.json()["media"][0]
    assert set(media) == {"id", "media_type", "role", "size_bytes"}
    for control in (
        context.service_key,
        context.session_token,
        context.csrf_token,
        "client-secret",
    ):
        assert control not in complete.text
    assert {field.name for field in dataclasses.fields(DetailedLogWrite)}.isdisjoint(
        {"authorization", "cookie", "csrf", "credentials", "object_key", "bucket"}
    )

    content = context.client.get(
        f"/v1/admin/request-logs/{log_id}/media/{media['id']}/content",
        headers=context.read_headers,
    )
    assert content.status_code == HTTPStatus.OK
    assert content.content == MEDIA_BYTES
    assert content.headers["content-type"] == "application/octet-stream"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert complete.headers["cache-control"] == "no-store"
    assert content.headers["cache-control"] == "no-store"

    administrator_routes = (
        f"/v1/admin/request-logs/{log_id}",
        f"/v1/admin/request-logs/{log_id}/media/{media['id']}/content",
        "/v1/admin/settings/log-retention",
        "/v1/admin/health",
    )
    for route in administrator_routes:
        assert context.client.get(route).status_code == HTTPStatus.UNAUTHORIZED
        assert (
            context.client.get(
                route, headers={"Authorization": f"Bearer {context.service_key}"}
            ).status_code
            == HTTPStatus.UNAUTHORIZED
        )


def test_log_pagination_and_filters_are_bounded(
    database_url: str, tmp_path: Path
) -> None:
    """Use one stable cursor and reject unsafe list bounds."""
    context = LogContext(database_url, tmp_path)
    first_id = context.write_log(started_at=datetime.now(tz=UTC) - timedelta(minutes=2))
    second_id = context.write_log(
        started_at=datetime.now(tz=UTC) - timedelta(minutes=1)
    )
    parameters = {
        "from": (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat(),
        "to": (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat(),
        "limit": 1,
    }
    first_page = context.client.get(
        "/v1/admin/request-logs", params=parameters, headers=context.read_headers
    )
    assert first_page.status_code == HTTPStatus.OK
    assert first_page.json()["items"][0]["id"] == str(second_id)
    assert first_page.json()["page"]["has_more"] is True
    second_page = context.client.get(
        "/v1/admin/request-logs",
        params={**parameters, "cursor": first_page.json()["page"]["next_cursor"]},
        headers=context.read_headers,
    )
    assert second_page.json()["items"][0]["id"] == str(first_id)
    assert second_page.json()["page"] == {"has_more": False}
    for limit in (0, 201):
        assert (
            context.client.get(
                "/v1/admin/request-logs",
                params={**parameters, "limit": limit},
                headers=context.read_headers,
            ).status_code
            == HTTPStatus.BAD_REQUEST
        )
    too_wide = {
        "from": (datetime.now(tz=UTC) - timedelta(days=32)).isoformat(),
        "to": datetime.now(tz=UTC).isoformat(),
    }
    assert (
        context.client.get(
            "/v1/admin/request-logs", params=too_wide, headers=context.read_headers
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )
    invalid_cursor = context.client.get(
        "/v1/admin/request-logs",
        params={**parameters, "cursor": str(uuid.uuid4())},
        headers=context.read_headers,
    )
    assert invalid_cursor.status_code == HTTPStatus.BAD_REQUEST


def test_object_loss_failure_and_corruption_are_safe(
    database_url: str, tmp_path: Path
) -> None:
    """Map early object loss, timeouts, and corrupt bytes to one safe result."""
    context = LogContext(database_url, tmp_path)
    log_id = context.write_log()
    log = context.client.get(
        f"/v1/admin/request-logs/{log_id}", headers=context.read_headers
    ).json()
    media_id = log["media"][0]["id"]
    route = f"/v1/admin/request-logs/{log_id}/media/{media_id}/content"
    key = next(iter(context.objects.values))

    original = context.objects.values.pop(key)
    lost = context.client.get(route, headers=context.read_headers)
    assert lost.status_code == HTTPStatus.NOT_FOUND
    assert lost.json()["error"]["code"] == "content_unavailable"
    assert key not in lost.text

    context.objects.values[key] = original
    context.objects.fail_get = True
    failed = context.client.get(route, headers=context.read_headers)
    assert failed.json()["error"]["code"] == "content_unavailable"
    context.objects.fail_get = False

    context.objects.values[key] = (b"corrupt", "image/png")
    corrupt = context.client.get(route, headers=context.read_headers)
    assert corrupt.json()["error"]["code"] == "content_unavailable"
    assert key not in corrupt.text


def test_retention_write_bounds_csrf_and_cleanup_failure_health(
    database_url: str, tmp_path: Path
) -> None:
    """Apply one setting to logs, activity, media, and operator cleanup health."""
    context = LogContext(database_url, tmp_path)
    old_log = context.write_log(started_at=datetime.now(tz=UTC) - timedelta(hours=25))
    retained_log = context.write_log(
        started_at=datetime.now(tz=UTC) - timedelta(hours=23)
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO router.activity_events
                   (actor_subject, action, resource_type, resource_id, result,
                    occurred_at)
               VALUES ('test', 'old.event', 'test', %s, 'succeeded',
                       statement_timestamp() - interval '25 hours')""",
            (uuid.uuid4(),),
        )
    denied = context.client.put(
        "/v1/admin/settings/log-retention",
        json={"duration_days": 1},
        headers=context.read_headers,
    )
    assert denied.status_code == HTTPStatus.FORBIDDEN
    wrong_origin = context.client.put(
        "/v1/admin/settings/log-retention",
        json={"duration_days": 1},
        headers={**context.write_headers, "Origin": "https://attacker.example"},
    )
    assert wrong_origin.status_code == HTTPStatus.FORBIDDEN
    for duplicate_headers in (
        [
            ("Cookie", f"llmrouter_admin_session={context.session_token}"),
            ("Origin", ADMIN_ORIGIN),
            ("Origin", ADMIN_ORIGIN),
            ("X-CSRF-Token", context.csrf_token),
        ],
        [
            ("Cookie", f"llmrouter_admin_session={context.session_token}"),
            ("Origin", ADMIN_ORIGIN),
            ("X-CSRF-Token", context.csrf_token),
            ("X-CSRF-Token", context.csrf_token),
        ],
    ):
        duplicate = context.client.put(
            "/v1/admin/settings/log-retention",
            json={"duration_days": 1},
            headers=duplicate_headers,
        )
        assert duplicate.status_code == HTTPStatus.FORBIDDEN
    for invalid in (0, 31):
        response = context.client.put(
            "/v1/admin/settings/log-retention",
            json={"duration_days": invalid},
            headers=context.write_headers,
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST

    context.objects.fail_delete = True
    saved = context.client.put(
        "/v1/admin/settings/log-retention",
        json={"duration_days": 1},
        headers=context.write_headers,
    )
    assert saved.status_code == HTTPStatus.OK
    assert saved.json() == {"duration_days": 1}
    assert (
        context.client.get(
            f"/v1/admin/request-logs/{old_log}", headers=context.read_headers
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        context.client.get(
            f"/v1/admin/request-logs/{retained_log}", headers=context.read_headers
        ).status_code
        == HTTPStatus.OK
    )
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.activity_events WHERE action = 'old.event'"
        ).fetchone() == {"count": 0}
        assert cleanup_health(connection) == "degraded"
        assert connection.execute(
            "SELECT count(*) FROM router.object_deletion_queue WHERE failure_count > 0"
        ).fetchone() == {"count": 1}
        connection.execute(
            """UPDATE router.object_deletion_queue
               SET queued_at = statement_timestamp() - interval '25 hours'"""
        )
        assert cleanup_health(connection) == "unavailable"

    context.objects.fail_delete = False
    restarted_objects = MemoryObjectStore(context.objects.values)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        apply_retention_and_cleanup(connection, cast("ObjectStore", restarted_objects))
        assert cleanup_health(connection) == "healthy"
    assert list(restarted_objects.values.values()) == [(MEDIA_BYTES, "image/png")]


def test_retention_reads_hide_backlog_immediately(
    database_url: str, tmp_path: Path
) -> None:
    """Hide expired rows even when more than six cleanup batches remain."""
    context = LogContext(database_url, tmp_path)
    started_at = datetime.now(tz=UTC) - timedelta(days=2)
    target_log_id = context.write_log(started_at=started_at)
    context.write_log(started_at=started_at - timedelta(seconds=1), media=())
    before = context.client.get(
        "/v1/admin/request-logs",
        params={
            "from": (started_at - timedelta(days=1)).isoformat(),
            "to": datetime.now(tz=UTC).isoformat(),
            "limit": 1,
        },
        headers=context.read_headers,
    )
    assert before.status_code == HTTPStatus.OK
    assert before.json()["items"][0]["id"] == str(target_log_id)
    stale_cursor = before.json()["page"]["next_cursor"]
    target_media_id = context.client.get(
        f"/v1/admin/request-logs/{target_log_id}", headers=context.read_headers
    ).json()["media"][0]["id"]
    target_activity_id = uuid.uuid4()
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """WITH calls AS (
                   INSERT INTO router.raw_accounting_calls
                       (service_id, workspace_id, outcome, started_at, completed_at)
                   SELECT %s, %s, 'failed',
                          %s - series * interval '1 second',
                          %s - series * interval '1 second'
                   FROM generate_series(1, 1200) AS series
                   RETURNING id, started_at
               )
               INSERT INTO router.request_logs
                   (logical_call_id, service_id, workspace_id, kind, outcome,
                    request_json, started_at)
               SELECT id, %s, %s, 'model', 'failed', '{}', started_at
               FROM calls""",
            (
                context.service_id,
                context.workspace_id,
                started_at,
                started_at,
                context.service_id,
                context.workspace_id,
            ),
        )
        connection.execute(
            """INSERT INTO router.activity_events
                   (id, actor_subject, action, resource_type, resource_id, result,
                    occurred_at)
               VALUES (%s, 'test', 'expired.target', 'test', %s, 'succeeded', %s)""",
            (target_activity_id, uuid.uuid4(), started_at),
        )
        connection.execute(
            """INSERT INTO router.activity_events
                   (actor_subject, action, resource_type, resource_id, result,
                    occurred_at)
               SELECT 'test', 'expired.backlog', 'test', gen_random_uuid(),
                      'succeeded', %s - series * interval '1 second'
               FROM generate_series(1, 1200) AS series""",
            (started_at,),
        )

    saved = context.client.put(
        "/v1/admin/settings/log-retention",
        json={"duration_days": 1},
        headers=context.write_headers,
    )
    assert saved.status_code == HTTPStatus.OK
    parameters = {
        "from": (started_at - timedelta(days=1)).isoformat(),
        "to": datetime.now(tz=UTC).isoformat(),
        "limit": 200,
    }
    page = context.client.get(
        "/v1/admin/request-logs", params=parameters, headers=context.read_headers
    )
    assert page.status_code == HTTPStatus.OK
    assert page.json()["items"] == []
    stale = context.client.get(
        "/v1/admin/request-logs",
        params={**parameters, "cursor": stale_cursor},
        headers=context.read_headers,
    )
    assert stale.status_code == HTTPStatus.BAD_REQUEST
    assert (
        context.client.get(
            f"/v1/admin/request-logs/{target_log_id}", headers=context.read_headers
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    media = context.client.get(
        f"/v1/admin/request-logs/{target_log_id}/media/{target_media_id}/content",
        headers=context.read_headers,
    )
    assert media.status_code == HTTPStatus.NOT_FOUND
    assert "object" not in media.text.lower()
    activity = context.client.get(
        "/v1/admin/activity", params=parameters, headers=context.read_headers
    )
    assert activity.status_code == HTTPStatus.OK
    assert all(item["action"] != "expired.target" for item in activity.json()["items"])
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.request_logs WHERE id = %s", (target_log_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM router.media_objects WHERE request_log_id = %s",
            (target_log_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM router.activity_events WHERE id = %s",
            (target_activity_id,),
        ).fetchone() == (1,)


def test_deleted_workspace_is_absent_before_cleanup_and_blocks_late_logs(
    database_url: str, tmp_path: Path
) -> None:
    """Keep deleted-scope data absent through failure and late work."""
    context = LogContext(database_url, tmp_path)
    log_id = context.write_log()
    context.objects.fail_delete = True
    deleted = context.client.delete(
        "/v1/admin/services/alpha/workspaces/primary",
        headers=context.write_headers,
    )
    assert deleted.status_code == HTTPStatus.NO_CONTENT
    assert (
        context.client.get(
            f"/v1/admin/request-logs/{log_id}", headers=context.read_headers
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    late = DetailedLogWrite(
        service_id=context.service_id,
        workspace_id=context.workspace_id,
        kind="media",
        outcome="succeeded",
        request_json='{"prompt":"late"}',
        response_json='{"result":"late"}',
        attempts=(_attempt(),),
        started_at=datetime.now(tz=UTC),
        media=(CapturedMedia(b"late result", "video/mp4", "output"),),
    )
    assert (
        write_detailed_log_best_effort(
            database_url, cast("ObjectStore", context.objects), late
        )
        is None
    )
    assert all(value[0] != b"late result" for value in context.objects.values.values())


def test_invalid_or_failed_best_effort_write_does_not_affect_call_state(
    database_url: str, tmp_path: Path
) -> None:
    """Drop invalid diagnostics and continue when object storage is unavailable."""
    context = LogContext(database_url, tmp_path)
    context.objects.fail_put = True
    log_id = context.write_log(
        media=(
            CapturedMedia(MEDIA_BYTES, "image/png", "input"),
            CapturedMedia(b"second", "video/mp4", "output"),
        )
    )
    complete = context.client.get(
        f"/v1/admin/request-logs/{log_id}", headers=context.read_headers
    )
    assert complete.status_code == HTTPStatus.OK
    assert "media" not in complete.json()
    assert context.objects.put_calls == 1

    invalid = DetailedLogWrite(
        service_id=context.service_id,
        workspace_id=context.workspace_id,
        kind="model",
        outcome="succeeded",
        request_json="{}",
        response_json=None,
        attempts=(_attempt(),),
        started_at=datetime.now(tz=UTC),
        media=(CapturedMedia(b"not an accepted input", "text/plain", "input"),),
    )
    assert (
        write_detailed_log_best_effort(
            database_url, cast("ObjectStore", context.objects), invalid
        )
        is None
    )
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.request_logs"
        ).fetchone() == (1,)

    context.objects.fail_put = False
    context.objects.uncertain_put = True
    uncertain_id = context.write_log()
    uncertain = context.client.get(
        f"/v1/admin/request-logs/{uncertain_id}", headers=context.read_headers
    )
    assert uncertain.status_code == HTTPStatus.OK
    assert "media" not in uncertain.json()
    assert context.objects.values == {}


def test_transaction_rollback_removes_uploaded_orphans(
    database_url: str, tmp_path: Path
) -> None:
    """Delete an uploaded object when its metadata transaction fails."""
    context = LogContext(database_url, tmp_path)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """CREATE FUNCTION router.reject_proof_media() RETURNS trigger
               LANGUAGE plpgsql AS $$ BEGIN
                   RAISE EXCEPTION 'proof rejection';
               END $$"""
        )
        connection.execute(
            """CREATE TRIGGER reject_proof_media
               BEFORE INSERT ON router.media_objects
               FOR EACH ROW EXECUTE FUNCTION router.reject_proof_media()"""
        )
        accounting_call_id = uuid.uuid4()
        connection.execute(
            """INSERT INTO router.raw_accounting_calls
                   (id, service_id, workspace_id, outcome, started_at,
                    completed_at)
               VALUES (%s, %s, %s, 'succeeded', statement_timestamp(),
                       statement_timestamp())""",
            (accounting_call_id, context.service_id, context.workspace_id),
        )
    value = DetailedLogWrite(
        service_id=context.service_id,
        workspace_id=context.workspace_id,
        kind="model",
        outcome="succeeded",
        request_json="{}",
        response_json="{}",
        attempts=(_attempt(),),
        started_at=datetime.now(tz=UTC),
        media=(CapturedMedia(MEDIA_BYTES, "image/png", "input"),),
        accounting_call_id=accounting_call_id,
    )
    context.objects.fail_delete = True
    assert (
        write_detailed_log_best_effort(
            database_url, cast("ObjectStore", context.objects), value
        )
        is None
    )
    assert list(context.objects.values.values()) == [(MEDIA_BYTES, "image/png")]
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.request_logs"
        ).fetchone() == (0,)
        assert connection.execute(
            """SELECT failure_count, failure_class
               FROM router.object_deletion_queue"""
        ).fetchone() == (1, "upload_rollback_failed")
    context.objects.fail_delete = False
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        assert apply_retention_and_cleanup(
            connection, cast("ObjectStore", context.objects)
        ) == (0, 1)
    assert context.objects.values == {}


def test_best_effort_database_boundaries_return_safely(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound connection and lock waits and absorb extreme timestamp failures."""
    context = LogContext(database_url, tmp_path)
    value = DetailedLogWrite(
        service_id=context.service_id,
        workspace_id=context.workspace_id,
        kind="model",
        outcome="failed",
        request_json="{}",
        response_json=None,
        attempts=(_attempt(),),
        started_at=datetime.now(tz=UTC),
    )
    observed: dict[str, object] = {}

    def unavailable_connect(*_args: object, **kwargs: object) -> object:
        observed.update(kwargs)
        raise psycopg.OperationalError

    with monkeypatch.context() as patch:
        patch.setattr(
            "llmrouter_backend.diagnostics.psycopg.connect", unavailable_connect
        )
        assert write_detailed_log_best_effort(database_url, None, value) is None
    assert observed["connect_timeout"] == 2

    with psycopg.connect(database_url) as blocker:
        blocker.execute("LOCK TABLE router.workspaces IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        assert write_detailed_log_best_effort(database_url, None, value) is None
        assert time.monotonic() - started < 3

    extreme = dataclasses.replace(
        value,
        started_at=datetime.max.replace(tzinfo=timezone(timedelta(hours=-23))),
        media=(CapturedMedia(MEDIA_BYTES, "image/png", "input"),),
    )
    assert (
        write_detailed_log_best_effort(
            database_url, cast("ObjectStore", context.objects), extreme
        )
        is None
    )
    assert context.objects.values == {}


def test_application_database_waits_and_cleanup_failures_are_bounded(
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set database bounds and keep the scheduled retry entry point safe."""
    context = LogContext(database_url, tmp_path)
    backend_app: Any = importlib.import_module("llmrouter_backend.app")
    observed: dict[str, str] = {}

    def retention(connection: psycopg.Connection[dict[str, object]]) -> int:
        statement = connection.execute("SHOW statement_timeout").fetchone()
        lock = connection.execute("SHOW lock_timeout").fetchone()
        assert statement is not None
        assert lock is not None
        observed["statement"] = cast("str", statement["statement_timeout"])
        observed["lock"] = cast("str", lock["lock_timeout"])
        return 7

    with monkeypatch.context() as patch:
        patch.setattr(backend_app, "get_log_retention", retention)
        response = context.client.get(
            "/v1/admin/settings/log-retention", headers=context.read_headers
        )
    assert response.status_code == HTTPStatus.OK
    assert observed == {"statement": "2s", "lock": "500ms"}

    with monkeypatch.context() as patch:
        patch.setattr(
            backend_app,
            "apply_retention_and_cleanup",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(LookupError),
        )
        backend_app._run_scheduled_cleanup(database_url, None)  # noqa: SLF001


def test_object_store_configuration_and_keys_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require trusted endpoints, safe control files, and private bounded keys."""
    access = tmp_path / "access"
    secret = tmp_path / "secret"
    access.write_text("access-control", encoding="utf-8")
    secret.write_text("secret-control", encoding="utf-8")

    def object_settings(
        endpoint: str,
        *,
        bucket: str = "valid-bucket",
        region: str = "garage",
        access_path: Path = access,
    ) -> Settings:
        return Settings(
            object_store_endpoint=endpoint,
            object_store_bucket=bucket,
            object_store_region=region,
            object_store_access_key_file=access_path,
            object_store_secret_key_file=secret,
        )

    with pytest.raises(ValueError, match="endpoint"):
        object_settings("http://object-storage:3900")
    with pytest.raises(ValueError, match="bucket"):
        object_settings("https://storage.example", bucket="127.0.0.1")
    with pytest.raises(ValueError, match="region"):
        object_settings("https://storage.example", region="invalid_region")

    observed: dict[str, object] = {}

    class Client:
        def put_object(self, **values: object) -> None:
            observed.update(values)

    def client_factory(_name: str, **values: object) -> Client:
        observed.update(values)
        return Client()

    with monkeypatch.context() as patch:
        patch.setattr("llmrouter_backend.object_store.boto3.client", client_factory)
        storage = ObjectStore.from_settings(object_settings("http://127.0.0.1:3900"))
    assert storage is not None
    assert observed["endpoint_url"] == "http://127.0.0.1:3900"
    storage.put("2026-08-23/service/workspace/object", b"body", "image/png")
    assert observed["Key"] == "2026-08-23/service/workspace/object"
    for unsafe_key in ("../escape", "/absolute", "double//segment", "secret-ä"):
        with pytest.raises(ObjectStoreError):
            storage.put(unsafe_key, b"body", "image/png")
    with pytest.raises(ObjectStoreError):
        storage.put("safe/key", b"body", "image/png\r\nX-Control: value")

    linked_access = tmp_path / "linked-access"
    linked_access.hardlink_to(access)
    with pytest.raises(ObjectStoreError):
        ObjectStore.from_settings(
            object_settings("https://storage.example", access_path=linked_access)
        )


def test_concurrent_retention_and_scope_isolation_are_safe(
    database_url: str, tmp_path: Path
) -> None:
    """Serialize bounded cleanup and reject a mixed service-workspace scope."""
    context = LogContext(database_url, tmp_path)
    first_log_id: uuid.UUID | None = None
    for hours in (48, 49, 50):
        log_id = context.write_log(
            started_at=datetime.now(tz=UTC) - timedelta(hours=hours)
        )
        first_log_id = first_log_id or log_id
    assert first_log_id is not None
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        second_service = connection.execute(
            """INSERT INTO router.services (api_name, display_name)
               VALUES ('beta', 'Beta') RETURNING id"""
        ).fetchone()
        assert second_service is not None
        second_workspace = connection.execute(
            """INSERT INTO router.workspaces (service_id, api_name, display_name)
               VALUES (%s, 'other', 'Other') RETURNING id""",
            (second_service["id"],),
        ).fetchone()
        assert second_workspace is not None
        connection.execute(
            "UPDATE router.global_settings SET log_retention_days = 1 WHERE singleton"
        )
        with (
            pytest.raises(psycopg.errors.ForeignKeyViolation),
            connection.transaction(),
        ):
            connection.execute(
                """INSERT INTO router.media_objects
                       (service_id, workspace_id, request_log_id, object_key,
                        media_type, role, size_bytes, content_sha256)
                   VALUES (%s, %s, %s, 'cross-scope', 'image/png', 'output',
                           1, %s)""",
                (
                    second_service["id"],
                    second_workspace["id"],
                    first_log_id,
                    b"x" * 32,
                ),
            )
    mixed = DetailedLogWrite(
        service_id=context.service_id,
        workspace_id=second_workspace["id"],
        kind="embedding",
        outcome="failed",
        request_json="{}",
        response_json=None,
        attempts=(_attempt(),),
        started_at=datetime.now(tz=UTC),
    )
    assert (
        write_detailed_log_best_effort(
            database_url, cast("ObjectStore", context.objects), mixed
        )
        is None
    )

    def cleanup() -> tuple[int, int]:
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            return apply_retention_and_cleanup(
                connection, cast("ObjectStore", context.objects), batch=2
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: cleanup(), range(2)))
    assert sum(result[0] for result in results) >= 3
    with psycopg.connect(database_url) as final_connection:
        assert final_connection.execute(
            "SELECT count(*) FROM router.request_logs"
        ).fetchone() == (0,)


def test_object_cleanup_releases_database_locks_before_dependency_wait(
    database_url: str, tmp_path: Path
) -> None:
    """Do not hold a database row lock during an object-store timeout."""
    context = LogContext(database_url, tmp_path)
    context.write_log(started_at=datetime.now(tz=UTC) - timedelta(days=2))
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute(
            "UPDATE router.global_settings SET log_retention_days = 1 WHERE singleton"
        )
        apply_retention_and_cleanup(connection, None)

    entered = threading.Event()
    release = threading.Event()

    class WaitingStore(MemoryObjectStore):
        def delete(self, key: str) -> None:
            entered.set()
            assert release.wait(timeout=2)
            super().delete(key)

    waiting = WaitingStore(context.objects.values)

    def cleanup() -> tuple[int, int]:
        with psycopg.connect(database_url, row_factory=dict_row) as connection:
            return apply_retention_and_cleanup(connection, cast("ObjectStore", waiting))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cleanup)
        assert entered.wait(timeout=2)
        started = time.monotonic()
        with psycopg.connect(database_url) as updater:
            updater.execute(
                """UPDATE router.object_deletion_queue
                   SET failure_count = failure_count + 1"""
            )
        assert time.monotonic() - started < 1
        release.set()
        assert future.result(timeout=2) == (0, 1)
