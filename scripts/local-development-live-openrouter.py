"""Run the bounded localhost OpenRouter proof without disclosing content."""
# ruff: noqa: INP001, PLR0913, PLR0915, PLR2004

from __future__ import annotations

import json
import os
import runpy
import secrets
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Never, cast

import httpx
import psycopg

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STATE_DIRECTORY = REPOSITORY_ROOT / ".local-development"
BASE_URL = "http://127.0.0.1:8010"
ADMIN_ORIGIN = "http://127.0.0.1:5174"
OPENROUTER_ORIGIN = "https://openrouter.ai"
OPENROUTER_API_PREFIX = "/api/v1"
SERVICE_ID = "0198a080-0000-7000-8000-000000000101"
WORKSPACE_ID = "0198a080-0000-7000-8000-000000000102"
OTHER_WORKSPACE_ID = "0198a080-0000-7000-8000-000000000104"
ASSIGNMENT = "live-model-proof"
MAXIMUM_OUTPUT_UNITS = 64
MAXIMUM_REQUEST_COST = Decimal("0.001")
MAXIMUM_TOTAL_COST = Decimal("0.002")
MAXIMUM_MODEL_PRICE_PER_TOKEN = Decimal("0.000001")
MAXIMUM_RETURNED_CONTENT_BYTES = 262_144
CONTROL_READ_TIMEOUT_SECONDS = 20.0
PROVIDER_READ_TIMEOUT_SECONDS = 90.0
_STREAM_PARSER_DETAIL_CODES = frozenset(
    {
        "response_content_length",
        "response_content_type",
        "response_header_limits",
        "response_json_depth",
        "response_json_duplicate_field",
        "response_json_encoding",
        "response_json_non_finite",
        "response_json_object",
        "response_json_size",
        "response_json_syntax",
        "response_json_value",
        "response_redirect_history",
        "response_usage_cached_tokens",
        "response_usage_details",
        "response_usage_inconsistent",
        "response_usage_object",
        "response_usage_tokens",
        "stream_body_limit",
        "stream_buffer_limit",
        "stream_choice_object",
        "stream_choices_shape",
        "stream_content_after_finish",
        "stream_content_encoding",
        "stream_content_type",
        "stream_data_after_done",
        "stream_delta_limit",
        "stream_delta_object",
        "stream_error_object",
        "stream_error_status",
        "stream_error_type",
        "stream_event_limit",
        "stream_finish_conflict",
        "stream_finish_type",
        "stream_finish_value",
        "stream_missing_content",
        "stream_missing_done",
        "stream_missing_finish",
        "stream_missing_usage",
        "stream_output_bytes_limit",
        "stream_output_events_limit",
        "stream_refusal_type",
        "stream_sse_field",
        "stream_sse_tail",
        "stream_usage_conflict",
    }
)


class LiveProofError(RuntimeError):
    """One safe live-proof failure without provider content."""


class LiveModel(NamedTuple):
    """One fixed public model identity for the bounded proof."""

    selector: str
    wire_model: str
    canonical_model_id: str
    display_name: str


class LiveProofPlan(NamedTuple):
    """One closed model and paid-operation sequence."""

    model: LiveModel
    operations: tuple[Literal["non-stream", "stream"], ...]


LIVE_MODELS = {
    "deepseek": LiveModel(
        "deepseek",
        "deepseek/deepseek-v4-flash",
        "0198a080-0000-7000-8000-000000000120",
        "DeepSeek V4 Flash",
    ),
    "mimo": LiveModel(
        "mimo",
        "xiaomi/mimo-v2.5",
        "0198a080-0000-7000-8000-000000000121",
        "MiMo 2.5",
    ),
    "granite": LiveModel(
        "granite",
        "ibm-granite/granite-4.1-8b",
        "0198a080-0000-7000-8000-000000000122",
        "Granite 4.1 8B",
    ),
}


def main(arguments: Sequence[str] = ()) -> None:  # noqa: C901
    """Run no-cost checks, then the selected bounded provider operations."""
    calls = 0
    request_ids: list[str] = []
    credential_id: str | None = None
    credential_revision: str | None = None
    key = ""
    phase = "secret validation"
    try:
        proof_plan = _selected_plan(arguments)
        model = proof_plan.model
        inherited_key = os.environ.pop("OPENROUTER_API_KEY", None)
        if (
            inherited_key is None
            or not inherited_key
            or len(inherited_key) > 65_536
            or "\n" in inherited_key
            or "\r" in inherited_key
        ):
            _fail("The inherited OpenRouter secret input is unavailable or invalid.")
        key = inherited_key
        phase = "no-cost preflight"
        prices = _provider_preflight(key, model)
        phase = "protected configuration"
        admin = _administrator_client()
        data_token = _secret(STATE_DIRECTORY / "data-plane-token")
        credential = _request(
            admin,
            "POST",
            "/v1/admin/credentials",
            operation="credential create",
            idempotency="local-live-openrouter-credential-v1",
            document={
                "owner_scope": "global",
                "provider_catalog_id": "openai_compatible.v1",
                "secret": key,
                "safe_label": "Local bounded live OpenRouter proof",
            },
            expected={200, 201},
        )
        credential_id = _required_string(credential, "credential_id")
        credential_revision = _required_string(credential, "revision")
        _assert_encrypted_credential(credential_id)
        route_id = _configure_route(admin, credential_id, prices, model)
        _assert_local_runtime_is_live()
        started_at = datetime.now(UTC)

        challenge = secrets.token_hex(4)
        prompt = f"Return only {challenge}."
        _assert_isolation(data_token, prompt)

        phase = (
            "stream provider phase"
            if proof_plan.operations == ("stream",)
            else "provider phases"
        )
        calls, returned_content = _run_provider_operations(
            data_token, prompt, request_ids, proof_plan.operations
        )

        phase = "postflight verification"
        cost = _accounting_cost(
            admin, started_at, expected_requests=len(proof_plan.operations)
        )
        if cost < 0 or cost > MAXIMUM_TOTAL_COST:
            _fail("The bounded live accounting cost is outside the safe limit.")
        _assert_safe_missing_status(data_token, challenge)
        _load_real_embed()
        _assert_no_sensitive_artifact(
            key.encode(),
            challenge.encode(),
            *returned_content,
        )
        del prompt, challenge
        print(f"Live OpenRouter proof passed. Paid provider calls: {calls}.")
        print(f"Bounded Router accounting cost: USD {cost}.")
        print("The outer command will reset all local live state.")
        _ = route_id
    except httpx.HTTPError:
        error = LiveProofError(f"The live {phase} network request failed safely.")
        if request_ids:
            with suppress(Exception):
                calls = _attempt_count(tuple(request_ids))
            with suppress(Exception):
                evidence = _redacted_timeout_evidence(request_ids[-1])
                if evidence is not None:
                    print(evidence)
        print(f"Live OpenRouter proof failed safely. Paid provider calls: {calls}.")
        raise SystemExit(str(error)) from None
    except LiveProofError as error:
        if request_ids:
            with suppress(Exception):
                calls = _attempt_count(tuple(request_ids))
        print(f"Live OpenRouter proof failed safely. Paid provider calls: {calls}.")
        raise SystemExit(str(error)) from None
    except Exception:  # noqa: BLE001 -- Never print secret-bearing exception details.
        if request_ids:
            with suppress(Exception):
                calls = _attempt_count(tuple(request_ids))
        print(f"Live OpenRouter proof failed safely. Paid provider calls: {calls}.")
        safe_message = f"The live {phase} operation failed safely."
        raise SystemExit(safe_message) from None
    finally:
        if credential_id is not None and credential_revision is not None:
            _retire_credential(credential_id, credential_revision)
        key = ""


def _selected_plan(arguments: Sequence[str]) -> LiveProofPlan:
    """Accept only fixed models and the guarded Granite stream diagnostic."""
    values = tuple(arguments)
    if not values:
        return LiveProofPlan(LIVE_MODELS["deepseek"], ("non-stream", "stream"))
    if len(values) == 2 and values[0] == "--model":
        selected = LIVE_MODELS.get(values[1])
        if selected is not None:
            return LiveProofPlan(selected, ("non-stream", "stream"))
    if values == ("--model", "granite", "--stream-only"):
        return LiveProofPlan(LIVE_MODELS["granite"], ("stream",))
    return _fail("The live model selector is invalid.")


def _provider_preflight(key: str, model_profile: LiveModel) -> dict[str, Decimal]:  # noqa: C901
    """Check authentication, model availability, and the maximum cost for free."""
    with httpx.Client(
        base_url=f"{OPENROUTER_ORIGIN}{OPENROUTER_API_PREFIX}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=_http_timeout(read=CONTROL_READ_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        key_response = client.get("/key")
        if key_response.status_code != 200:
            _fail("The no-cost OpenRouter authentication preflight failed.")
        key_document = _bounded_json(key_response)
        model_response = client.get(
            "/models/user", params={"q": model_profile.display_name}
        )
        if model_response.status_code != 200:
            _fail("The no-cost OpenRouter model-list preflight failed.")
        model_document = _bounded_json(model_response)
    models = model_document.get("data")
    if not isinstance(models, list):
        _fail("The OpenRouter model-list document is invalid.")
    model = next(
        (
            item
            for item in models
            if isinstance(item, dict) and item.get("id") == model_profile.wire_model
        ),
        None,
    )
    if model is None:
        _fail("The selected live-proof model is not available for this key.")
    supported = model.get("supported_parameters")
    if not isinstance(supported, list) or "max_tokens" not in supported:
        _fail("The selected live-proof model lacks the required output limit.")
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        _fail("The selected live-proof model pricing is unavailable.")
    prices = {
        "input_token": _price(pricing.get("prompt")),
        "output_token": _price(pricing.get("completion")),
        "request": _price(pricing.get("request", "0")),
    }
    if any(value > MAXIMUM_MODEL_PRICE_PER_TOKEN for value in prices.values()):
        _fail("The selected live-proof model price exceeds the proof limit.")
    estimated = Decimal(2) * (
        Decimal(128) * prices["input_token"]
        + Decimal(MAXIMUM_OUTPUT_UNITS) * prices["output_token"]
        + prices["request"]
    )
    if estimated > MAXIMUM_TOTAL_COST:
        _fail("The estimated live-proof cost exceeds the safe total limit.")
    key_data = key_document.get("data")
    remaining = key_data.get("limit_remaining") if isinstance(key_data, dict) else None
    if remaining is not None:
        try:
            if Decimal(str(remaining)) < estimated:
                _fail("The OpenRouter key limit cannot cover the bounded proof.")
        except (InvalidOperation, ValueError):
            _fail("The OpenRouter key limit metadata is invalid.")
    return prices


def _administrator_client() -> httpx.Client:
    session = _secret(STATE_DIRECTORY / "administrator-session")
    csrf = _secret(STATE_DIRECTORY / "administrator-csrf")
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Cookie": f"__Host-llmrouter-admin={session}",
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": csrf,
        },
        timeout=_http_timeout(read=CONTROL_READ_TIMEOUT_SECONDS),
        trust_env=False,
    )


def _configure_route(
    admin: httpx.Client,
    credential_id: str,
    prices: dict[str, Decimal],
    model_profile: LiveModel,
) -> str:
    provider = _request(
        admin,
        "POST",
        f"/v1/admin/services/{SERVICE_ID}/provider-instances",
        operation="provider create",
        idempotency="local-live-openrouter-provider-v1",
        document={
            "provider_catalog_id": "openai_compatible.v1",
            "display_name": "Local live OpenRouter",
            "endpoint": f"{OPENROUTER_ORIGIN}{OPENROUTER_API_PREFIX}",
            "credential_id": credential_id,
            "state": "active",
            "settings": {
                "schema_name": "adapter.openai_compatible.settings",
                "major_version": 1,
                "document": {
                    "profile": "openrouter",
                    "supported_operations": ["chat.complete", "chat.stream"],
                },
            },
            "expected_revision": None,
            "reason": "Create the bounded local live provider",
            "eligible_service_ids": [],
        },
        expected={200, 201},
    )
    provider_id = _required_string(provider, "resource_id")
    revision = _required_string(provider, "active_revision")
    route = _request(
        admin,
        "POST",
        f"/v1/admin/services/{SERVICE_ID}/provider-model-routes",
        operation="route create",
        idempotency="local-live-openrouter-route-v1",
        document={
            "provider_instance_id": provider_id,
            "canonical_model_id": model_profile.canonical_model_id,
            "wire_model": model_profile.wire_model,
            "capabilities": ["chat.complete", "chat.stream"],
            "settings": {
                "schema_name": "adapter.openai_compatible.route",
                "major_version": 1,
                "document": {},
            },
            "price_authority": {
                "mode": "manual",
                "source_name": None,
                "lookup_identifier": None,
            },
            "prices": [
                _route_price("input_token", prices["input_token"]),
                _route_price("output_token", prices["output_token"]),
                _route_price("request", prices["request"]),
            ],
            "synchronization_schedule": "0 0 * * 0",
            "stale_after_seconds": 1_209_600,
            "state": "active",
            "expected_revision": revision,
            "reason": "Create the bounded live-proof model route",
            "eligible_service_ids": [],
        },
        expected={200, 201},
    )
    route_id = _required_string(route, "resource_id")
    revision = _required_string(route, "active_revision")
    assignment = _request(
        admin,
        "PUT",
        f"/v1/admin/services/{SERVICE_ID}/assignments/{ASSIGNMENT}",
        operation="assignment publish",
        idempotency="local-live-openrouter-assignment-v1",
        document={
            "expected_revision": revision,
            "state": "active",
            "candidates": [
                {"provider_model_route_id": route_id, "attempt_timeout_ms": 30_000}
            ],
            "required_capabilities": ["chat.complete", "chat.stream"],
            "reason": "Publish the one-candidate bounded live assignment",
        },
        expected={200, 201},
    )
    if assignment.get("distribution_state") not in {"current", "distributing"}:
        _fail("The live assignment was not published.")
    return route_id


def _assert_isolation(token: str, prompt: str) -> None:
    response = _raw_create(
        token, _uuidv7(), _model_body(prompt, workspace_id=OTHER_WORKSPACE_ID)
    )
    if response.status_code not in {403, 404}:
        _fail("The live preflight did not preserve workspace isolation.")


def _run_provider_operations(
    token: str,
    prompt: str,
    request_ids: list[str],
    operations: tuple[Literal["non-stream", "stream"], ...],
) -> tuple[int, list[bytes]]:
    """Run only the closed paid operations, without an internal retry."""
    calls = 0
    returned_content: list[bytes] = []
    for expected_attempts, operation in enumerate(operations, start=1):
        request_id = _uuidv7()
        request_ids.append(request_id)
        response = _compatible_chat(
            token, request_id, prompt, stream=operation == "stream"
        )
        if operation == "stream":
            status = _read_status(token, request_id)
            evidence = _redacted_stream_evidence(status)
            parser_evidence = None
            if status.get("state") not in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
                "uncertain",
            }:
                if evidence is not None:
                    print(evidence)
                if parser_evidence is not None:
                    print(parser_evidence)
                _fail("The compatible stream ended before native terminal completion.")
            if status.get("state") != "succeeded":
                with suppress(Exception):
                    parser_evidence = _redacted_stream_parser_evidence(request_id)
            try:
                compatible_content = _compatible_stream_content(response.content)
            except LiveProofError:
                if evidence is not None:
                    print(evidence)
                if parser_evidence is not None:
                    print(parser_evidence)
                raise
        else:
            compatible_content = _assert_compatible_content(_bounded_json(response))
            status = _wait_terminal(token, request_id)
        calls = _attempt_count(tuple(request_ids))
        status_content = _verify_provider_phase(
            compatible_content,
            status,
            attempts=calls,
            expected_attempts=expected_attempts,
            phase=operation,
        )
        returned_content.extend((compatible_content, status_content))
    return calls, returned_content


def _compatible_chat(
    token: str, request_id: str, prompt: str, *, stream: bool
) -> httpx.Response:
    response = httpx.post(
        f"{BASE_URL}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "X-LLMRouter-Request-ID": request_id,
            "Content-Type": "application/json",
        },
        json={
            "model": ASSIGNMENT,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_completion_tokens": MAXIMUM_OUTPUT_UNITS,
            "stream": stream,
            "x_llmrouter_workspace_id": WORKSPACE_ID,
            "x_llmrouter_data_profile": "service-data",
            "x_llmrouter_max_cost": {
                "amount": str(MAXIMUM_REQUEST_COST),
                "currency": "USD",
            },
        },
        timeout=_http_timeout(read=PROVIDER_READ_TIMEOUT_SECONDS),
        trust_env=False,
    )
    if response.status_code != 200:
        _fail("The authenticated compatible Router request failed safely.")
    return response


def _raw_create(token: str, request_id: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{BASE_URL}/v1/model-requests",
        headers={
            "Authorization": f"Bearer {token}",
            "X-LLMRouter-Request-ID": request_id,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=_http_timeout(read=CONTROL_READ_TIMEOUT_SECONDS),
        trust_env=False,
    )


def _model_body(text: str, *, workspace_id: str = WORKSPACE_ID) -> dict[str, Any]:
    return {
        "api_version": "1",
        "data_profile": "service-data",
        "workspace_id": workspace_id,
        "assignment": ASSIGNMENT,
        "messages": [{"role": "user", "content": text}],
        "limits": {
            "attempt_timeout_ms": 30_000,
            "max_output_units": MAXIMUM_OUTPUT_UNITS,
            "max_cost": {"amount": str(MAXIMUM_REQUEST_COST), "currency": "USD"},
        },
        "output": {"format": "text", "temperature": 0},
    }


def _wait_terminal(token: str, request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        document = _read_status(token, request_id)
        if document.get("state") in {
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
            "uncertain",
        }:
            return document
        time.sleep(0.1)
    return _fail("The live request did not become terminal in the bounded time.")


def _read_status(token: str, request_id: str) -> dict[str, Any]:
    """Read one authenticated status without provider or request content output."""
    response = httpx.get(
        f"{BASE_URL}/v1/model-requests/{request_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_http_timeout(read=CONTROL_READ_TIMEOUT_SECONDS),
        trust_env=False,
    )
    if response.status_code != 200:
        _fail("The authenticated request status read failed.")
    return _bounded_json(response)


def _assert_success_content(document: dict[str, Any]) -> bytes:
    if document.get("state") != "succeeded":
        _fail("The live provider request did not succeed.")
    result = document.pop("result", None)
    outputs = result.get("outputs") if isinstance(result, dict) else None
    text = outputs[0].get("text") if isinstance(outputs, list) and outputs else None
    content = _bounded_content(text, "The native request status has no output.")
    accounting = document.get("accounting")
    if not isinstance(accounting, dict):
        _fail("The live request status did not contain bounded accounting.")
    del result, outputs, text
    return content


def _verify_provider_phase(
    compatible_content: bytes,
    status: dict[str, Any],
    *,
    attempts: int,
    expected_attempts: int,
    phase: Literal["non-stream", "stream"],
) -> bytes:
    """Print redacted evidence only after all phase checks pass."""
    status_content = _assert_success_content(status)
    if compatible_content != status_content:
        _fail("The compatible response and native status content differ.")
    if attempts != expected_attempts:
        _fail("The one-candidate live route made an unexpected provider attempt.")
    print(f"Live OpenRouter {phase} phase passed.")
    return status_content


def _assert_compatible_content(document: dict[str, Any]) -> bytes:
    choices = document.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    text = message.get("content") if isinstance(message, dict) else None
    content = _bounded_content(text, "The compatible response has no output.")
    del choices, choice, message, text
    return content


def _compatible_stream_content(stream: bytes) -> bytes:  # noqa: C901
    if len(stream) > 2_000_000:
        _fail("The compatible stream exceeded its safe size.")
    try:
        records = stream.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        _fail("The compatible stream was not valid UTF-8.")
    content_parts: list[bytes] = []
    done = False
    for record in records:
        if not record.startswith("data: "):
            continue
        payload = record.removeprefix("data: ")
        if payload == "[DONE]":
            if done:
                _fail("The compatible stream repeated terminal completion.")
            done = True
            continue
        if done:
            _fail("The compatible stream contained data after terminal completion.")
        content = _compatible_delta_content(payload)
        if content is not None:
            content_parts.append(content)
    combined = b"".join(content_parts)
    if not done or not content_parts or not combined.strip():
        _fail("The compatible stream did not contain output and terminal completion.")
    if len(combined) > MAXIMUM_RETURNED_CONTENT_BYTES:
        _fail("The compatible stream output exceeded its safe size.")
    return combined


def _compatible_delta_content(payload: str) -> bytes | None:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        _fail("The compatible stream contained invalid JSON.")
    choices = document.get("choices") if isinstance(document, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    delta = choice.get("delta") if isinstance(choice, dict) else None
    text = delta.get("content") if isinstance(delta, dict) else None
    if not isinstance(text, str) or not text:
        return None
    return _bounded_content(text, "The compatible stream has invalid output.")


def _bounded_content(value: object, empty_message: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        _fail(empty_message)
    try:
        content = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("The provider output was not valid Unicode.")
    if len(content) > MAXIMUM_RETURNED_CONTENT_BYTES:
        _fail("The provider output exceeded its safe size.")
    return content


def _accounting_cost(
    admin: httpx.Client, started_at: datetime, *, expected_requests: int = 2
) -> Decimal:
    response = admin.get(
        f"/v1/admin/services/{SERVICE_ID}/accounting/summary",
        params={
            "workspace_id": WORKSPACE_ID,
            "from": (started_at - timedelta(seconds=1)).isoformat(),
            "to": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
        },
    )
    if response.status_code != 200:
        _fail("The bounded live accounting read failed.")
    document = _bounded_json(response)
    if (
        expected_requests not in {1, 2}
        or document.get("logical_requests") != expected_requests
        or document.get("attempts") != expected_requests
    ):
        _fail("The live accounting counts are not exact.")
    try:
        return Decimal(str(document["cost"]))
    except (InvalidOperation, KeyError):
        _fail("The live accounting cost is invalid.")


def _attempt_count(request_ids: tuple[str, ...]) -> int:
    with _database() as connection:
        row = connection.execute(
            """SELECT count(*)
               FROM router.provider_attempts AS attempt
               JOIN router.logical_requests AS request
                 ON request.row_id = attempt.request_row_id
               WHERE request.request_id = ANY(%s)""",
            (list(request_ids),),
        ).fetchone()
    if row is None:
        _fail("The live provider attempt count is unavailable.")
    return int(row[0])


def _redacted_timeout_evidence(request_id: str) -> str | None:
    """Return only closed request and attempt states after a host timeout."""
    with _database() as connection:
        row = connection.execute(
            """SELECT request.state::text, count(attempt.id),
                      COALESCE(
                          array_agg(attempt.state::text ORDER BY attempt.attempt_number)
                              FILTER (WHERE attempt.id IS NOT NULL),
                          ARRAY[]::text[]
                      )
               FROM router.logical_requests AS request
               LEFT JOIN router.provider_attempts AS attempt
                 ON attempt.request_row_id = request.row_id
               WHERE request.request_id = %s
               GROUP BY request.state""",
            (request_id,),
        ).fetchone()
    if row is None:
        return None
    request_state, attempt_count, outcomes = row
    request_states = {
        "admitted",
        "running",
        "waiting_for_tool",
        "cancel_requested",
        "succeeded",
        "failed",
        "interrupted",
        "cancelled",
        "uncertain",
    }
    attempt_states = {
        "started",
        "succeeded",
        "failed",
        "interrupted",
        "cancelled",
        "uncertain",
    }
    if (
        request_state not in request_states
        or isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or not 0 <= attempt_count <= 8
        or not isinstance(outcomes, list)
        or len(outcomes) != attempt_count
        or any(outcome not in attempt_states for outcome in outcomes)
    ):
        return None
    outcome_text = ",".join(outcomes) if outcomes else "none"
    return (
        f"Live timeout evidence: request state={request_state}; "
        f"provider attempts={attempt_count}; attempt outcomes={outcome_text}."
    )


def _redacted_stream_evidence(status: dict[str, Any]) -> str | None:
    """Classify a compatible stream failure without content or identities."""
    state = status.get("state")
    if state == "succeeded":
        return (
            "Live stream evidence: native request succeeded; compatible SSE is "
            "missing valid output or terminal completion."
        )
    if state in {"failed", "cancelled", "interrupted", "uncertain"}:
        error = status.get("error")
        error_class = error.get("class") if isinstance(error, dict) else None
        affected_scope = (
            error.get("affected_scope") if isinstance(error, dict) else None
        )
        if error_class not in {
            "authentication",
            "policy",
            "budget",
            "rate_limit",
            "timeout",
            "transport",
            "provider_unavailable",
            "invalid_provider_response",
            "incompatible_request",
            "cancelled",
            "uncertain_effect",
            "router_internal",
        } or affected_scope not in {
            "attempt",
            "provider_model_route",
            "provider_instance",
            "credential",
            "assignment_candidate",
            "logical_request",
        }:
            return None
        return (
            f"Live stream evidence: native request ended as {state}; safe error "
            f"class={error_class}; affected scope={affected_scope}."
        )
    if state in {"admitted", "running", "cancel_requested"}:
        return "Live stream evidence: native request is nonterminal."
    return None


def _redacted_stream_parser_evidence(request_id: str) -> str | None:
    """Return one closed terminal parser branch from durable safe evidence."""
    with _database() as connection:
        row = connection.execute(
            """SELECT attempt.redacted_evidence->>'detail_code'
               FROM router.provider_attempts AS attempt
               JOIN router.logical_requests AS request
                 ON request.row_id = attempt.request_row_id
               WHERE request.request_id = %s
               ORDER BY attempt.attempt_number DESC
               LIMIT 1""",
            (request_id,),
        ).fetchone()
    if row is None or row[0] not in _STREAM_PARSER_DETAIL_CODES:
        return None
    return f"Live stream parser evidence: closed branch={row[0]}."


def _assert_encrypted_credential(credential_id: str) -> None:
    with _database() as connection:
        row = connection.execute(
            """SELECT ciphertext IS NOT NULL, encrypted_data_key IS NOT NULL
               FROM router.encrypted_credentials WHERE id = %s""",
            (credential_id,),
        ).fetchone()
    if row != (True, True):
        _fail("The live provider credential is not in encrypted custody.")


def _assert_local_runtime_is_live() -> None:
    docker = shutil.which("docker")
    if docker is None:
        _fail("Docker is unavailable for the live-mode check.")
    result = subprocess.run(  # noqa: S603
        [
            docker,
            "compose",
            "--project-directory",
            str(REPOSITORY_ROOT),
            "-f",
            str(REPOSITORY_ROOT / "docker-compose.dev.yml"),
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            (
                "import os; value=os.getenv('LLMROUTER_LOCAL_OPENROUTER_LIVE'); "
                "raise SystemExit(0 if value == '1' else 1)"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        env={
            name: value
            for name, value in os.environ.items()
            if name != "OPENROUTER_API_KEY"
        },
    )
    if result.returncode != 0:
        _fail("The local backend did not enable the guarded live transport.")


def _assert_safe_missing_status(token: str, challenge: str) -> None:
    response = httpx.get(
        f"{BASE_URL}/v1/model-requests/{_uuidv7()}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_http_timeout(read=CONTROL_READ_TIMEOUT_SECONDS),
        trust_env=False,
    )
    if response.status_code != 404 or challenge in response.text:
        _fail("The missing-request error was not safe.")


def _load_real_embed() -> None:
    namespace = runpy.run_path(
        str(REPOSITORY_ROOT / "scripts/local-development-e2e.py")
    )
    proof = cast("Callable[[], None]", namespace["_prove_service_scoped_embed"])
    try:
        proof()
    except Exception:  # noqa: BLE001
        _fail("The real service administration embed did not load.")


def _assert_no_sensitive_artifact(*sensitive_values: bytes) -> None:
    git = shutil.which("git")
    docker = shutil.which("docker")
    if git is None or docker is None:
        _fail("A required sensitive-artifact scan tool is unavailable.")
    paths = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.split(b"\0")
    for raw_path in paths:
        if not raw_path:
            continue
        path = REPOSITORY_ROOT / os.fsdecode(raw_path)
        if path.is_file():
            content = path.read_bytes()
            if any(value in content for value in sensitive_values):
                _fail("A live secret or provider value entered a repository artifact.")
    logs = subprocess.run(  # noqa: S603
        [
            docker,
            "compose",
            "--project-directory",
            str(REPOSITORY_ROOT),
            "-f",
            str(REPOSITORY_ROOT / "docker-compose.dev.yml"),
            "logs",
            "--no-color",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={
            name: value
            for name, value in os.environ.items()
            if name != "OPENROUTER_API_KEY"
        },
    ).stdout
    if any(value in logs for value in sensitive_values):
        _fail("A live secret or provider value entered local service logs.")


def _retire_credential(credential_id: str, revision: str) -> None:
    try:
        with _administrator_client() as admin:
            response = admin.post(
                f"/v1/admin/credentials/{credential_id}/retire",
                json={
                    "expected_revision": revision,
                    "reason": "Retire the bounded local live-test credential",
                },
            )
            _ = response.status_code
    except Exception:  # noqa: BLE001, S110
        pass


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    operation: str = "administration request",
    idempotency: str | None = None,
    document: dict[str, Any] | None = None,
    expected: frozenset[int] | set[int] = frozenset({200}),
) -> dict[str, Any]:
    headers = {} if idempotency is None else {"Idempotency-Key": idempotency}
    response = client.request(method, path, headers=headers, json=document)
    if response.status_code not in expected:
        _fail(f"The protected {operation} failed with HTTP {response.status_code}.")
    return _bounded_json(response)


def _route_price(unit: str, price_per_token: Decimal) -> dict[str, str]:
    quantity = Decimal(1) if unit == "request" else Decimal(1_000_000)
    price = price_per_token if unit == "request" else price_per_token * quantity
    return {
        "unit": unit,
        "price": format(price, "f"),
        "currency": "USD",
        "raw_source_value": f"{format(price, 'f')} USD per {quantity:f} {unit}",
        "unit_quantity": format(quantity, "f"),
    }


def _http_timeout(*, read: float) -> httpx.Timeout:
    """Set each HTTP timeout explicitly without enabling a retry."""
    return httpx.Timeout(connect=10.0, read=read, write=10.0, pool=5.0)


def _price(value: object) -> Decimal:
    if not isinstance(value, str):
        _fail("The OpenRouter model price is invalid.")
    try:
        price = Decimal(value)
    except InvalidOperation:
        _fail("The OpenRouter model price is invalid.")
    if not price.is_finite() or price < 0:
        _fail("The OpenRouter model price is invalid.")
    return price


def _bounded_json(response: httpx.Response) -> dict[str, Any]:
    if len(response.content) > 2_000_000:
        _fail("A preflight response exceeded its safe size.")
    try:
        document = response.json()
    except json.JSONDecodeError:
        _fail("A preflight response was not valid JSON.")
    if not isinstance(document, dict):
        _fail("A preflight response was not a JSON object.")
    return cast("dict[str, Any]", document)


def _required_string(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        _fail("A local administration response is incomplete.")
    return value


def _database() -> psycopg.Connection[Any]:
    password = _secret(STATE_DIRECTORY / "postgres-password")
    return psycopg.connect(
        f"postgresql://llmrouter:{password}@127.0.0.1:5434/llmrouter"
    )


def _secret(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb", closefd=True) as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            _fail("A generated local secret path is unsafe.")
        return source.read().decode("ascii").strip()


def _uuidv7() -> str:
    milliseconds = int(datetime.now(UTC).timestamp() * 1_000)
    random_bits = secrets.randbits(74)
    value = (milliseconds << 80) | (7 << 76) | ((random_bits >> 62) << 64)
    value |= (2 << 62) | (random_bits & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def _fail(message: str) -> Never:
    raise LiveProofError(message)


if __name__ == "__main__":
    main(tuple(sys.argv[1:]))
