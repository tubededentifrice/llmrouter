"""Native asynchronous media-job API, isolation, and durable worker tests."""
# ruff: noqa: D102, D103, D107, EM101, TRY003

from __future__ import annotations

import asyncio
import base64
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from llmrouter_backend import create_app, media_api
from llmrouter_backend.adapters import FakeAdapter
from llmrouter_backend.calls import (
    CallExecutor,
    ProviderAttemptRequest,
    ProviderCompleted,
    ProviderOutput,
)
from llmrouter_backend.config import Settings
from llmrouter_backend.database import migrate
from llmrouter_backend.media_api import media_worker_loop, run_media_worker_once
from llmrouter_backend.object_store import (
    ObjectNotFoundError,
    ObjectStoreError,
    StoredObject,
)
from llmrouter_backend.security import ControlKeys
from llmrouter_backend.store import create_key
from psycopg.rows import dict_row

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from llmrouter_backend.object_store import ObjectStore

_PNG = b"\x89PNG\r\n\x1a\njob-input"
_RECOVERED_CLAIMS = 2


class MemoryObjectStore:
    """Keep private retained objects in memory for network-free API tests."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[bytes, str]] = {}
        self.fail_put = False
        self.fail_delete = False
        self.put_started: threading.Event | None = None
        self.put_release: threading.Event | None = None

    def put(self, key: str, body: bytes, content_type: str) -> None:
        if self.fail_put:
            self.values[key] = (body, content_type)
            raise RuntimeError("private storage detail")
        self.values[key] = (body, content_type)
        if self.put_started is not None and self.put_release is not None:
            self.put_started.set()
            if not self.put_release.wait(timeout=2):
                raise RuntimeError("private blocked upload detail")

    def get(self, key: str, maximum_bytes: int = 1024 * 1024 * 1024) -> StoredObject:
        try:
            body, content_type = self.values[key]
        except KeyError:
            raise ObjectNotFoundError from None
        if len(body) > maximum_bytes:
            raise ObjectNotFoundError
        return StoredObject(body, content_type)

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("private deletion detail")
        self.values.pop(key, None)

    def healthy(self) -> bool:
        return True


class BlockingMediaAdapter(FakeAdapter):
    """Hold one fake provider result so scope deletion can win the race."""

    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release

    async def attempt(
        self, request: ProviderAttemptRequest, /
    ) -> AsyncIterator[ProviderOutput | ProviderCompleted]:
        if request.route.provider_model_name == "fake-media-v1":
            self.started.set()
            await self.release.wait()
        async for event in super().attempt(request):
            yield event


@dataclass(slots=True)
class MediaApiContext:
    """Keep two services, one fake media chain, and retained objects."""

    database_url: str
    client: TestClient
    keys: dict[str, str]
    objects: MemoryObjectStore
    executor: CallExecutor

    def headers(self, service: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[service]}"}


@pytest.fixture
def media_api_context(database_url: str, tmp_path: Path) -> MediaApiContext:
    digest = tmp_path / "digest"
    encryption = tmp_path / "encryption"
    digest.write_text("d" * 64, encoding="utf-8")
    encryption.write_text("e" * 64, encoding="utf-8")
    settings = Settings(
        administrator_digest_key_file=digest,
        administrator_encryption_key_file=encryption,
        allowed_origins=("http://127.0.0.1:5174",),
    )
    controls = ControlKeys.load(settings)
    keys: dict[str, str] = {}
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        migrate(connection)
        service_ids: dict[str, uuid.UUID] = {}
        for service in ("alpha", "beta"):
            row = connection.execute(
                """INSERT INTO router.services (api_name, display_name)
                   VALUES (%s, %s) RETURNING id""",
                (service, service.title()),
            ).fetchone()
            assert row is not None
            service_ids[service] = row["id"]
            connection.execute(
                """INSERT INTO router.workspaces
                       (service_id, api_name, display_name)
                   VALUES (%s, 'main', 'Main')""",
                (row["id"],),
            )
            _key, secret = create_key(
                connection,
                service_id=row["id"],
                name="test",
                actor_subject="test:setup",
                control_keys=controls,
            )
            keys[service] = secret
        _seed_media_catalog(connection, service_ids["alpha"])
    objects = MemoryObjectStore()
    app = create_app(
        database_url=database_url,
        settings=settings,
        object_store=cast("ObjectStore", objects),
    )
    client = TestClient(app, base_url="http://127.0.0.1:8010")
    return MediaApiContext(
        database_url,
        client,
        keys,
        objects,
        cast("CallExecutor", app.state.call_executor),
    )


def _seed_media_catalog(
    connection: psycopg.Connection[Any], service_id: uuid.UUID
) -> None:
    price = {
        "currency": "USD",
        "unit_prices": [
            {"unit": unit, "amount": "0.01"}
            for unit in (
                "image",
                "video_second",
                "audio_second",
                "request",
                "provider_unit",
            )
        ],
        "source": "manual-test",
    }
    connection.execute(
        """INSERT INTO router.provider_connections
               (api_name, display_name, adapter, enabled)
           VALUES ('fake-provider', 'Fake provider', 'fake', true)"""
    )
    connection.execute(
        """INSERT INTO router.canonical_models
               (api_name, display_name, input_modalities, output_modalities,
                capabilities, constraints, manual_price)
           VALUES ('media-model', 'Media model', ARRAY['text', 'image'],
                   ARRAY['image', 'video', 'audio'], ARRAY[]::text[],
                   '{"max_input_images":8,"max_input_image_bytes":20971520,
                     "max_output_duration_seconds":300}'::jsonb, %s::jsonb)""",
        (json.dumps(price),),
    )
    for api_name, wire_name in (
        ("media-failure", "fake-error-transport-v1"),
        ("media-primary", "fake-media-v1"),
        ("media-uncertain", "fake-media-uncertain-v1"),
    ):
        connection.execute(
            """INSERT INTO router.provider_models
                   (api_name, provider_id, model_id, provider_model_name,
                    enabled, input_modalities, output_modalities, capabilities,
                    constraints, reasoning_mappings)
               SELECT %s, provider.id, model.id, %s, true,
                      model.input_modalities, model.output_modalities,
                      model.capabilities, model.constraints, '[]'::jsonb
               FROM router.provider_connections AS provider,
                    router.canonical_models AS model
               WHERE provider.api_name = 'fake-provider'
                 AND model.api_name = 'media-model'""",
            (api_name, wire_name),
        )
    assignment = connection.execute(
        """INSERT INTO router.assignment_definitions
               (service_id, api_name, display_name)
           VALUES (%s, 'media', 'Media') RETURNING id""",
        (service_id,),
    ).fetchone()
    assert assignment is not None
    for position, candidate in enumerate(("media-failure", "media-primary")):
        connection.execute(
            """INSERT INTO router.assignment_candidates
                   (assignment_id, position, provider_model_id)
               SELECT %s, %s, id FROM router.provider_models WHERE api_name = %s""",
            (assignment["id"], position, candidate),
        )


def _request(
    *,
    kind: str = "image",
    exact: str | None = None,
    input_image: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "workspace_api_name": "main",
        "selector": (
            {"provider_model_api_name": exact}
            if exact is not None
            else {"assignment_api_name": "media"}
        ),
        "kind": kind,
        "prompt": "Create private media.",
        "tags": ["zeta", "alpha", "zeta"],
    }
    if input_image:
        value["input_images"] = [
            {
                "type": "image",
                "media_type": "image/png",
                "data_base64": base64.b64encode(_PNG).decode("ascii"),
            }
        ]
    return value


def test_media_job_fallback_retention_accounting_and_service_isolation(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    created = context.client.post(
        "/v1/media-jobs",
        json=_request(input_image=True),
        headers=context.headers("alpha"),
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    job = created.json()
    assert job["state"] == "pending"
    assert job["provider_model_api_name"] == "media-failure"
    assert "object" not in json.dumps(job).lower()
    assert (
        context.client.get(
            f"/v1/media-jobs/{job['id']}", headers=context.headers("beta")
        ).status_code
        == HTTPStatus.NOT_FOUND
    )

    assert asyncio.run(
        run_media_worker_once(
            context.database_url, context.executor, cast("ObjectStore", context.objects)
        )
    )
    finished = context.client.get(
        f"/v1/media-jobs/{job['id']}", headers=context.headers("alpha")
    )
    assert finished.status_code == HTTPStatus.OK
    assert finished.json()["state"] == "succeeded"
    assert finished.json()["provider_model_api_name"] == "media-primary"
    assert finished.json()["content"] == {
        "media_type": "image/png",
        "size_bytes": 16,
    }
    content = context.client.get(
        f"/v1/media-jobs/{job['id']}/content", headers=context.headers("alpha")
    )
    assert content.status_code == HTTPStatus.OK
    assert content.headers["content-type"] == "image/png"
    assert content.content == b"fake-media-bytes"

    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        attempts = connection.execute(
            """SELECT provider_model_api_name, outcome, failure_class
               FROM router.raw_accounting_attempts ORDER BY position"""
        ).fetchall()
        assert attempts == [
            {
                "provider_model_api_name": "media-failure",
                "outcome": "failed",
                "failure_class": "transport",
            },
            {
                "provider_model_api_name": "media-primary",
                "outcome": "succeeded",
                "failure_class": None,
            },
        ]
        row = connection.execute(
            "SELECT payload FROM router.media_jobs WHERE id = %s", (job["id"],)
        ).fetchone()
        assert row is not None
        assert row["payload"] == {}
        roles = connection.execute(
            """SELECT role FROM router.media_objects
               WHERE media_job_id = %s ORDER BY role""",
            (job["id"],),
        ).fetchall()
        assert roles == [{"role": "input"}, {"role": "output"}]


def test_exact_uncertain_job_has_no_fallback_and_only_safe_error(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    created = context.client.post(
        "/v1/media-jobs",
        json=_request(exact="media-uncertain"),
        headers=context.headers("alpha"),
    )
    assert created.status_code == HTTPStatus.ACCEPTED
    job_id = created.json()["id"]
    asyncio.run(
        run_media_worker_once(
            context.database_url, context.executor, cast("ObjectStore", context.objects)
        )
    )

    result = context.client.get(
        f"/v1/media-jobs/{job_id}", headers=context.headers("alpha")
    ).json()
    assert result["state"] == "failed"
    assert result["error"] == {
        "code": "upstream_failed",
        "message": "The provider result state is uncertain.",
    }
    serialized = json.dumps(result)
    assert "transport" not in serialized
    assert "credential" not in serialized
    assert (
        context.client.get(
            f"/v1/media-jobs/{job_id}/content", headers=context.headers("alpha")
        ).json()["error"]["code"]
        == "content_unavailable"
    )


def test_media_contract_rejects_removed_surfaces_and_rolls_back_storage_failure(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    invalid_audio = context.client.post(
        "/v1/media-jobs",
        json=_request(kind="audio", input_image=True),
        headers=context.headers("alpha"),
    )
    assert invalid_audio.status_code == HTTPStatus.BAD_REQUEST
    removed = _request()
    removed["progress"] = True
    assert (
        context.client.post(
            "/v1/media-jobs", json=removed, headers=context.headers("alpha")
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )
    invalid_tag = _request()
    invalid_tag["tags"] = ["x" * 129]
    assert (
        context.client.post(
            "/v1/media-jobs", json=invalid_tag, headers=context.headers("alpha")
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )
    assert (
        context.client.delete(
            f"/v1/media-jobs/{uuid.uuid4()}",
            headers=context.headers("alpha"),
        ).status_code
        == HTTPStatus.BAD_REQUEST
    )

    context.objects.fail_put = True
    failed = context.client.post(
        "/v1/media-jobs",
        json=_request(input_image=True),
        headers=context.headers("alpha"),
    )
    assert failed.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "private storage detail" not in failed.text
    assert not context.objects.values
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.media_jobs"
        ).fetchone() == (0,)
        assert connection.execute(
            """SELECT observed_requirements FROM router.assignment_usage
               WHERE service_id = (
                   SELECT id FROM router.services WHERE api_name = 'alpha'
               ) AND api_name = 'media'"""
        ).fetchone() == (["image_input", "image_output", "text_input"],)


def test_media_job_rejects_an_unavailable_object_store_before_provider_work(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    cast("Any", context.client.app).state.object_store = None

    response = context.client.post(
        "/v1/media-jobs",
        json=_request(),
        headers=context.headers("alpha"),
    )

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json()["error"]["code"] == "internal_error"
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.media_jobs"
        ).fetchone() == (0,)
        assert connection.execute(
            """SELECT observed_requirements FROM router.assignment_usage
               WHERE service_id = (
                   SELECT id FROM router.services WHERE api_name = 'alpha'
               ) AND api_name = 'media'"""
        ).fetchone() == (["image_output", "text_input"],)


def test_media_job_rejects_the_deployment_json_bound_as_invalid_input(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    request = _request()
    request["prompt"] = "\U0001f642" * 525_000

    response = context.client.post(
        "/v1/media-jobs",
        json=request,
        headers=context.headers("alpha"),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["error"]["code"] == "invalid_request"
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.media_jobs"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.assignment_usage"
        ).fetchone() == (0,)


@pytest.mark.parametrize("cleanup_mode", ["delete", "queue"])
def test_media_admission_commit_failure_rolls_back_and_cleans_objects(
    media_api_context: MediaApiContext,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_mode: str,
) -> None:
    context = media_api_context
    delete_fails = cleanup_mode == "queue"
    context.objects.fail_delete = delete_fails

    def fail_commit(_connection: psycopg.Connection[Any]) -> None:
        raise psycopg.OperationalError("private commit detail")

    monkeypatch.setattr(media_api, "_commit_media_admission", fail_commit)
    failed = context.client.post(
        "/v1/media-jobs",
        json=_request(input_image=True),
        headers=context.headers("alpha"),
    )

    assert failed.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "private commit detail" not in failed.text
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.media_jobs"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.media_objects"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM router.object_deletion_queue"
        ).fetchone() == ((1 if delete_fails else 0),)
    assert bool(context.objects.values) is delete_fails


@pytest.mark.parametrize("mutation", ["workspace", "route"])
def test_media_upload_holds_no_scope_or_catalog_lock_and_revalidates(
    media_api_context: MediaApiContext,
    mutation: str,
) -> None:
    context = media_api_context
    context.objects.put_started = threading.Event()
    context.objects.put_release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(
            context.client.post,
            "/v1/media-jobs",
            json=_request(input_image=True),
            headers=context.headers("alpha"),
        )
        assert context.objects.put_started.wait(timeout=2)
        try:
            with psycopg.connect(
                context.database_url,
                options="-c statement_timeout=2000 -c lock_timeout=500",
            ) as connection:
                if mutation == "workspace":
                    connection.execute(
                        """DELETE FROM router.workspaces
                           WHERE service_id = (
                               SELECT id FROM router.services
                               WHERE api_name = 'alpha'
                           ) AND api_name = 'main'"""
                    )
                else:
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (4_993_044_345_823,)
                    )
                    connection.execute(
                        """UPDATE router.provider_models SET enabled = false
                           WHERE api_name = 'media-failure'"""
                    )
        finally:
            context.objects.put_release.set()
        rejected = response.result(timeout=2)

    assert rejected.status_code == (
        HTTPStatus.NOT_FOUND if mutation == "workspace" else HTTPStatus.BAD_REQUEST
    )
    assert not context.objects.values
    with psycopg.connect(context.database_url) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.media_jobs"
        ).fetchone() == (0,)


@pytest.mark.parametrize("failure_kind", ["database", "object-store"])
def test_media_worker_loop_recovers_after_dependency_failure(
    media_api_context: MediaApiContext,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    context = media_api_context
    claims = 0
    monkeypatch.setattr(media_api, "_WORKER_DEPENDENCY_RETRY_SECONDS", 0.001)

    async def run_until_recovered() -> None:
        recovered = asyncio.Event()

        async def fail_then_recover(
            _database_url: str,
            _executor: CallExecutor,
            _object_store: ObjectStore | None,
        ) -> bool:
            nonlocal claims
            claims += 1
            if claims == 1:
                if failure_kind == "database":
                    raise psycopg.OperationalError("private dependency detail")
                raise ObjectStoreError
            recovered.set()
            return False

        monkeypatch.setattr(media_api, "run_media_worker_once", fail_then_recover)
        worker = asyncio.create_task(
            media_worker_loop(
                context.database_url,
                context.executor,
                cast("ObjectStore", context.objects),
            )
        )
        await asyncio.wait_for(recovered.wait(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    asyncio.run(run_until_recovered())
    assert claims >= _RECOVERED_CLAIMS


def test_media_worker_claims_concurrent_jobs_once_and_expires_deadlines(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    _created_ids = [
        context.client.post(
            "/v1/media-jobs", json=_request(), headers=context.headers("alpha")
        ).json()["id"]
        for _ in range(2)
    ]

    async def run_both() -> list[bool]:
        return await asyncio.gather(
            *(
                run_media_worker_once(
                    context.database_url,
                    context.executor,
                    cast("ObjectStore", context.objects),
                )
                for _ in range(2)
            )
        )

    assert asyncio.run(run_both()) == [True, True]
    with psycopg.connect(context.database_url, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT count(*) FROM router.raw_accounting_calls"
        ).fetchone() == {"count": 2}
        expired = connection.execute(
            """INSERT INTO router.media_jobs
                   (service_id, workspace_id, provider_model_api_name, kind,
                    payload, created_at, deadline_at)
               SELECT service.id, workspace.id, 'media-primary', 'image', '{}',
                      statement_timestamp() - interval '2 seconds',
                      statement_timestamp() - interval '1 second'
               FROM router.services AS service
               JOIN router.workspaces AS workspace
                 ON workspace.service_id = service.id
               WHERE service.api_name = 'alpha'
                 AND workspace.api_name = 'main'
               RETURNING id"""
        ).fetchone()
        assert expired is not None
        expired_id = expired["id"]
    assert not asyncio.run(
        run_media_worker_once(
            context.database_url, context.executor, cast("ObjectStore", context.objects)
        )
    )
    expired = context.client.get(
        f"/v1/media-jobs/{expired_id}", headers=context.headers("alpha")
    ).json()
    assert expired["state"] == "failed"
    assert expired["error"]["message"] == "The media-job deadline expired."


def test_pending_job_resumes_with_a_new_worker_instance(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    job_id = context.client.post(
        "/v1/media-jobs",
        json=_request(exact="media-primary"),
        headers=context.headers("alpha"),
    ).json()["id"]
    restarted = CallExecutor(
        database_url=context.database_url,
        adapters={"fake": FakeAdapter()},
        object_store=cast("ObjectStore", context.objects),
    )

    assert asyncio.run(
        run_media_worker_once(
            context.database_url, restarted, cast("ObjectStore", context.objects)
        )
    )
    result = context.client.get(
        f"/v1/media-jobs/{job_id}", headers=context.headers("alpha")
    ).json()
    assert result["state"] == "succeeded"


def test_workspace_delete_hides_running_job_and_discards_late_result(
    media_api_context: MediaApiContext,
) -> None:
    context = media_api_context
    job_id = context.client.post(
        "/v1/media-jobs",
        json=_request(exact="media-primary"),
        headers=context.headers("alpha"),
    ).json()["id"]

    async def race_delete() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        executor = CallExecutor(
            database_url=context.database_url,
            adapters={"fake": BlockingMediaAdapter(started, release)},
            object_store=cast("ObjectStore", context.objects),
        )
        worker = asyncio.create_task(
            run_media_worker_once(
                context.database_url,
                executor,
                cast("ObjectStore", context.objects),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)

        with psycopg.connect(context.database_url) as connection:
            assert connection.execute(
                """SELECT count(*) FROM pg_stat_activity
                   WHERE application_name = 'llmrouter-call-executor'"""
            ).fetchone() == (0,)

        def delete_scope() -> None:
            with psycopg.connect(context.database_url) as connection:
                connection.execute(
                    """DELETE FROM router.workspaces
                       WHERE service_id = (
                           SELECT id FROM router.services WHERE api_name = 'alpha'
                       ) AND api_name = 'main'"""
                )

        await asyncio.wait_for(asyncio.to_thread(delete_scope), timeout=2)
        release.set()
        assert await asyncio.wait_for(worker, timeout=2)

    asyncio.run(race_delete())
    assert (
        context.client.get(
            f"/v1/media-jobs/{job_id}", headers=context.headers("alpha")
        ).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert all(
        body != b"fake-media-bytes" for body, _type in context.objects.values.values()
    )
