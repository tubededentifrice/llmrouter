"""Prove the complete localhost MVP without a paid provider call."""
# ruff: noqa: B006, EM101, EM102, INP001, PLR0913, PLR0915, PLR2004, PT018, S101, TRY003

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast

import httpx
import psycopg

if TYPE_CHECKING:
    from collections.abc import Callable

SERVICE_ID = "0198a080-0000-7000-8000-000000000101"
WORKSPACE_ID = "0198a080-0000-7000-8000-000000000102"
OTHER_WORKSPACE_ID = "0198a080-0000-7000-8000-000000000103"
BASE_URL = "http://127.0.0.1:8010"
ADMIN_ORIGIN = "http://127.0.0.1:5174"
ADMIN_BASE_URL = ADMIN_ORIGIN
STATE_DIRECTORY = Path(__file__).resolve().parents[1] / ".local-development"
STATE_PATH = STATE_DIRECTORY / "e2e-state.json"


def main() -> None:
    """Run the selected restart-safe proof phase."""
    phase = sys.argv[1] if len(sys.argv) == 2 else "prepare"
    if phase == "prepare":
        _prepare()
    elif phase == "resume":
        _resume()
    else:
        raise SystemExit("Use prepare or resume.")


def _prepare() -> None:
    admin_session = _secret(STATE_DIRECTORY / "administrator-session")
    admin_csrf = _secret(STATE_DIRECTORY / "administrator-csrf")
    data_token = _secret(STATE_DIRECTORY / "data-plane-token")
    admin = httpx.Client(
        base_url=ADMIN_BASE_URL,
        headers={
            "Cookie": f"__Host-llmrouter-local-admin={admin_session}",
            "Origin": ADMIN_ORIGIN,
            "X-CSRF-Token": admin_csrf,
        },
        timeout=10,
        trust_env=False,
    )
    state = _request(admin, "GET", f"/v1/admin/services/{SERVICE_ID}/state")
    assert state["service_id"] == SERVICE_ID
    credentials = _request(admin, "GET", "/v1/admin/credentials?limit=100")
    if credentials["items"]:
        credential = credentials["items"][0]
    else:
        credential = _request(
            admin,
            "POST",
            "/v1/admin/credentials",
            idempotency="local-e2e-credential-v1",
            json={
                "owner_scope": "global",
                "provider_catalog_id": "openai_compatible.v1",
                "secret": secrets.token_urlsafe(32),
                "safe_label": "Local deterministic OpenRouter",
            },
            expected={200, 201},
        )
    credential_id = str(credential["credential_id"])
    providers = _request(
        admin,
        "GET",
        f"/v1/admin/services/{SERVICE_ID}/provider-instances?limit=100",
    )
    provider = next(
        (
            item
            for item in providers["items"]
            if item["display_name"] == "Local OpenRouter"
        ),
        None,
    )
    if provider is None:
        provider = _request(
            admin,
            "POST",
            f"/v1/admin/services/{SERVICE_ID}/provider-instances",
            idempotency="local-e2e-provider-v1",
            json={
                "provider_catalog_id": "openai_compatible.v1",
                "display_name": "Local OpenRouter",
                "endpoint": "https://openrouter.ai/api/v1",
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
                "reason": "Create the deterministic local provider",
                "eligible_service_ids": [],
            },
            expected={200, 201},
        )
    provider_id_value = provider.get(
        "resource_id", provider.get("provider_instance_id")
    )
    assert isinstance(provider_id_value, str)
    provider_id = provider_id_value
    routes = _request(
        admin,
        "GET",
        f"/v1/admin/services/{SERVICE_ID}/provider-model-routes?limit=100",
    )
    revision = str(routes["configuration_revision"])
    fallback_route = next(
        (
            item
            for item in routes["items"]
            if item["wire_model"] == "local/missing-model"
        ),
        None,
    )
    if fallback_route is None:
        fallback_route = _request(
            admin,
            "POST",
            f"/v1/admin/services/{SERVICE_ID}/provider-model-routes",
            idempotency="local-e2e-fallback-route-v1",
            json={
                "provider_instance_id": provider_id,
                "canonical_model_id": "0198a080-0000-7000-8000-000000000120",
                "wire_model": "local/missing-model",
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
                    _price("input_token", "0.10"),
                    _price("output_token", "0.20"),
                    _price("request", "0.001"),
                ],
                "synchronization_schedule": "0 0 * * 0",
                "stale_after_seconds": 1_209_600,
                "state": "active",
                "expected_revision": revision,
                "reason": "Create the deterministic failed fallback route",
                "eligible_service_ids": [],
            },
            expected={200, 201},
        )
        revision = str(fallback_route["active_revision"])
    fallback_route_id_value = fallback_route.get(
        "resource_id", fallback_route.get("provider_model_route_id")
    )
    assert isinstance(fallback_route_id_value, str)
    fallback_route_id = fallback_route_id_value
    if isinstance(fallback_route.get("provider_instance_id"), str):
        provider_id = fallback_route["provider_instance_id"]
    route = next(
        (
            item
            for item in routes["items"]
            if item["wire_model"] == "deepseek/deepseek-v4-flash"
        ),
        None,
    )
    if route is None:
        route = _request(
            admin,
            "POST",
            f"/v1/admin/services/{SERVICE_ID}/provider-model-routes",
            idempotency="local-e2e-deepseek-route-v1",
            json={
                "provider_instance_id": provider_id,
                "canonical_model_id": "0198a080-0000-7000-8000-000000000120",
                "wire_model": "deepseek/deepseek-v4-flash",
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
                    _price("input_token", "0.10"),
                    _price("output_token", "0.20"),
                    _price("request", "0.001"),
                ],
                "synchronization_schedule": "0 0 * * 0",
                "stale_after_seconds": 1_209_600,
                "state": "active",
                "expected_revision": revision,
                "reason": "Create the deterministic DeepSeek route",
                "eligible_service_ids": [],
            },
            expected={200, 201},
        )
        revision = str(route["active_revision"])
    route_id_value = route.get("resource_id", route.get("provider_model_route_id"))
    assert isinstance(route_id_value, str)
    route_id = route_id_value
    assignments = _request(
        admin,
        "GET",
        f"/v1/admin/services/{SERVICE_ID}/assignments?limit=100",
    )
    assignment = next(
        (item for item in assignments["items"] if item["name"] == "general"),
        None,
    )
    if assignment is None:
        assignment = _request(
            admin,
            "PUT",
            f"/v1/admin/services/{SERVICE_ID}/assignments/general",
            idempotency="local-e2e-assignment-v1",
            json={
                "expected_revision": revision,
                "state": "active",
                "candidates": [
                    {
                        "provider_model_route_id": fallback_route_id,
                        "attempt_timeout_ms": 30_000,
                    },
                    {
                        "provider_model_route_id": route_id,
                        "attempt_timeout_ms": 30_000,
                    },
                ],
                "required_capabilities": ["chat.complete", "chat.stream"],
                "reason": "Publish the deterministic fallback chain",
            },
            expected={200, 201},
        )
        assert assignment["distribution_state"] in {"current", "distributing"}

    request_id = _uuidv7()
    body = _model_body("Return the deterministic response.")
    receipt = _create(data_token, request_id, body)
    assert receipt["request_id"] == request_id
    replay = _create(data_token, request_id, body)
    assert replay["request_id"] == request_id
    conflict = _raw_create(data_token, request_id, _model_body("Changed input."))
    assert conflict.status_code == 409
    status = _wait_terminal(data_token, request_id)
    assert status["state"] == "succeeded"
    assert status["result"]["outputs"][0]["text"] == "local response"
    events = _events(data_token, request_id)
    assert "event: output.delta" in events and "event: request.terminal" in events

    wrong_scope = _raw_create(
        data_token,
        _uuidv7(),
        _model_body("Wrong workspace.", workspace_id=str(uuid.uuid4())),
    )
    assert wrong_scope.status_code in {403, 404}
    host_token = _secret(STATE_DIRECTORY / "example-host-token")
    wrong_permission = _raw_create(
        host_token, _uuidv7(), _model_body("Permission boundary.")
    )
    assert wrong_permission.status_code in {401, 403}
    missing = httpx.get(
        f"{BASE_URL}/v1/model-requests/{_uuidv7()}",
        headers={"Authorization": f"Bearer {data_token}"},
        timeout=5,
    )
    assert missing.status_code == 404 and "local response" not in missing.text

    cancel_id = _uuidv7()
    _create(data_token, cancel_id, _model_body("Wait for cancellation."))
    _wait_attempt_started(cancel_id, candidate_ordinal=2)
    cancelled = httpx.post(
        f"{BASE_URL}/v1/model-requests/{cancel_id}/cancel",
        headers={
            "Authorization": f"Bearer {data_token}",
            "Content-Type": "application/json",
        },
        json={"reason": "The deterministic proof cancels this request."},
        timeout=10,
    )
    assert cancelled.status_code == 200, cancelled.text[:500]
    assert cancelled.json()["state"] in {"cancel_requested", "cancelled", "uncertain"}
    cancelled_status = _wait_terminal(data_token, cancel_id)
    assert cancelled_status["state"] in {"cancelled", "uncertain"}

    recovery_id = _admit_restart_recovery(data_token)
    _write_state(
        {
            "cancel_id": cancel_id,
            "recovery_id": recovery_id,
            "successful_id": request_id,
        }
    )
    print("The deterministic API proof is ready for restart.")


def _resume() -> None:
    data_token = _secret(STATE_DIRECTORY / "data-plane-token")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    recovery_id = state["recovery_id"]
    replay = _create(data_token, recovery_id, _model_body("Wait for restart recovery."))
    assert replay["request_id"] == recovery_id
    recovered = _wait_terminal(data_token, recovery_id)
    assert recovered["state"] in {"failed", "uncertain", "interrupted"}

    admin_session = _secret(STATE_DIRECTORY / "administrator-session")
    end = datetime.now(UTC) + timedelta(seconds=1)
    start = end - timedelta(days=1)
    summary = httpx.get(
        f"{ADMIN_BASE_URL}/v1/admin/services/{SERVICE_ID}/accounting/summary",
        headers={"Cookie": f"__Host-llmrouter-local-admin={admin_session}"},
        params={
            "workspace_id": WORKSPACE_ID,
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
        timeout=10,
    )
    assert summary.status_code == 200
    accounting = summary.json()
    assert accounting["logical_requests"] >= 1
    assert accounting["attempts"] >= 2
    assert Decimal(accounting["cost"]) > 0

    with psycopg_connect() as connection:
        row = connection.execute(
            """SELECT count(*), count(*) FILTER (WHERE ciphertext IS NOT NULL)
               FROM router.encrypted_credentials"""
        ).fetchone()
        assert row is not None and row[0] >= 1 and row[0] == row[1]
        content = connection.execute(
            """SELECT count(*), count(*) FILTER (
                       WHERE wire_data LIKE '%Return the deterministic response.%'
                   )
               FROM router.execution_stream_events
               WHERE event_name = 'output.delta'"""
        ).fetchone()
        assert content is not None and content[0] >= 2 and content[1] == 0
        fallback = connection.execute(
            """SELECT candidate_ordinal, state
               FROM router.provider_attempts
               WHERE request_row_id = (
                   SELECT row_id FROM router.logical_requests WHERE request_id = %s
               )
               ORDER BY candidate_ordinal""",
            (state["successful_id"],),
        ).fetchall()
        assert fallback == [(1, "failed"), (2, "succeeded")]
        cancelled_accounting = connection.execute(
            """SELECT count(*)
               FROM router.accounting_facts AS fact
               JOIN router.provider_attempts AS attempt
                 ON attempt.id = fact.subject_id
                AND attempt.request_row_id = fact.request_row_id
               WHERE fact.request_row_id = (
                   SELECT row_id FROM router.logical_requests WHERE request_id = %s
               )
                 AND fact.subject_kind = 'provider_attempt'
                 AND fact.outcome = 'failed'
                 AND attempt.candidate_ordinal = 2""",
            (state["cancel_id"],),
        ).fetchone()
        assert cancelled_accounting == (1,)
    _prove_service_scoped_embed()
    _prove_global_administration()
    print(
        "The deterministic API, accounting, persistence, recovery, and embed proof "
        "passed."
    )


def _prove_service_scoped_embed() -> None:
    """Prove embed origin, scope, and user-switch isolation."""
    wrong_origin = httpx.post(
        "http://127.0.0.1:5176/api/context",
        headers={"Origin": "http://127.0.0.1:5999"},
        json={"action": "switch_user"},
        timeout=5,
    )
    assert wrong_origin.status_code == 403
    with _CdpBrowser() as browser:
        browser.navigate("http://127.0.0.1:5176")
        browser.wait_for_text("The host authorized this exact Router scope.")
        old_frame = browser.wait_for_frame("/service-administration")
        old_text = browser.wait_for_frame_text(
            old_frame, SERVICE_ID, WORKSPACE_ID, "Configuration", "Providers"
        )
        assert SERVICE_ID in old_text
        assert WORKSPACE_ID in old_text
        assert "Configuration" in old_text
        assert "Providers" in old_text and "Routes" in old_text

        browser.click_button("Switch user")
        browser.wait_for_text("The host authorized this exact Router scope.")
        new_frame = browser.wait_for_frame(
            "/service-administration", excluded_id=old_frame["id"]
        )
        assert new_frame["id"] != old_frame["id"]
        assert old_frame["id"] not in {item["id"] for item in browser.frames()}
        new_text = browser.wait_for_frame_text(
            new_frame, SERVICE_ID, WORKSPACE_ID, "Configuration"
        )
        assert SERVICE_ID in new_text
        assert WORKSPACE_ID in new_text
        assert "Configuration" in new_text


def _prove_global_administration() -> None:
    """Prove deterministic global administration data and secret controls."""
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    successful_request_id = str(state["successful_id"])
    administration_url = f"http://127.0.0.1:5174/?view=global&service_id={SERVICE_ID}"
    with _CdpBrowser() as browser:
        browser.navigate(administration_url)
        browser.wait_for_text("Activate administrator session")
        browser.activate_administrator(secrets.token_urlsafe(24))
        browser.wait_for_text("The local administrator session was not activated.")
        assert browser.evaluate(
            """(() => {
              const input = document.querySelector(
                'input[name="local-administrator-secret"]'
              );
              return input instanceof HTMLInputElement && input.value === "";
            })()"""
        )
        admin_secret = _secret(STATE_DIRECTORY / "administrator-session")
        browser.activate_administrator(admin_secret)
        browser.wait_for_text("Effective configuration")
        browser.click_button("Effective configuration")
        try:
            browser.wait_for_text("Local OpenRouter")
        except AssertionError:
            browser.navigate(administration_url)
            browser.wait_for_text("Effective configuration")
            browser.click_button("Effective configuration")
            browser.wait_for_text("Local OpenRouter")
        admin_text = browser.body_text()
        assert "LLM Router" in admin_text and "Administration" in admin_text
        assert "Global administrator" in admin_text
        assert "Local OpenRouter" in admin_text
        assert "deepseek/deepseek-v4-flash" in admin_text
        assert browser.evaluate(
            """(() => {
              return [...document.querySelectorAll('input[type="password"]')]
                .every((input) => input.value === "");
            })()"""
        )
        assert not browser.evaluate(
            f"document.body.innerText.includes({json.dumps(admin_secret)})"
        )
        browser.select_labeled_option("Provider instance", "Local OpenRouter")
        browser.select_labeled_option("Supported model", "DeepSeek V4 Flash")
        browser.fill_labeled_input("Provider model name", "browser/named-catalog-proof")
        browser.fill_labeled_input("Input price", "0.11")
        browser.fill_labeled_input("Output price", "0.22")
        browser.click_button("Publish route")
        browser.wait_for_text("Model route published at")
        browser.wait_for_text("browser/named-catalog-proof")
        browser.click_button("Budgets")
        browser.wait_for_text("Budget summary")
        budget_text = browser.body_text()
        for label in (
            "Hard limit",
            "Warning threshold",
            "Reserved",
            "Used",
            "Corrected",
            "Remaining",
            "Enforcement",
            "Reset period",
            "Revision",
        ):
            assert label in budget_text
        old_hard_limit = _displayed_usd(browser.definition_value("Hard limit"))
        new_hard_limit = Decimal(6) if old_hard_limit == Decimal(5) else Decimal(5)
        new_warning_threshold = new_hard_limit - Decimal("1.5")
        old_budget_revision = browser.definition_value("Revision")
        assert old_budget_revision.isdecimal()
        browser.fill_labeled_input("Hard limit", str(new_hard_limit))
        browser.fill_labeled_input("Warning threshold", str(new_warning_threshold))
        browser.click_button("Save budget")
        browser.wait_for_text("Budget revision")
        browser.wait_until(
            lambda: (
                _displayed_usd(browser.definition_value("Hard limit")) == new_hard_limit
                and _displayed_usd(browser.definition_value("Warning threshold"))
                == new_warning_threshold
                and browser.definition_value("Revision") != old_budget_revision
            ),
            "The committed budget did not refresh with its new revision.",
        )
        browser.click_button("Assignments")
        browser.wait_for_text("general")
        assignment_text = browser.body_text()
        assert "general" in assignment_text
        assert "Primary" in assignment_text and "Fallback 1" in assignment_text
        assert "active" in assignment_text
        browser.click_button("Budgets")
        browser.wait_for_text("Budget summary")
        assert _displayed_usd(browser.definition_value("Hard limit")) == new_hard_limit
        assert (
            _displayed_usd(browser.definition_value("Warning threshold"))
            == new_warning_threshold
        )
        assert browser.definition_value("Revision") != old_budget_revision
        browser.navigate(
            "http://127.0.0.1:5174/?view=global"
            f"&service_id={SERVICE_ID}&workspace_id={WORKSPACE_ID}"
        )
        browser.wait_for_text("Effective configuration")
        browser.click_button("Requests")
        browser.wait_for_text(successful_request_id)
        browser.track_request_detail_reads(successful_request_id)
        browser.click_request_detail(successful_request_id)
        browser.wait_for_text("Ordered provider attempts")
        assert browser.active_element_label() == "Logical request detail"
        detail_text = browser.body_text()
        assert SERVICE_ID in detail_text and WORKSPACE_ID in detail_text
        assert "No retry. Router used the next fallback." in detail_text
        assert (
            "The attempt succeeded. Router did not use another fallback." in detail_text
        )
        attempt_rows = browser.request_attempt_rows()
        assert len(attempt_rows) == 2
        assert "failed" in attempt_rows[0]
        assert "succeeded" in attempt_rows[1]
        assert "Return the deterministic response." not in detail_text
        assert "local response" not in detail_text
        browser.set_viewport(390, 844)
        assert browser.focus_region("Ordered provider attempts table")
        request_reads = browser.request_detail_reads()
        browser.click_button("Refresh detail")
        browser.wait_until(
            lambda: browser.request_detail_reads() > request_reads,
            "The request detail refresh did not start a new HTTP read.",
        )
        browser.wait_until(
            lambda: (
                "Ordered provider attempts" in browser.body_text()
                and browser.active_element_label() == "Logical request detail"
            ),
            "The refreshed request detail did not become active.",
        )
        assert browser.request_attempt_rows() == attempt_rows
        browser.click_button("Back to requests")
        browser.wait_for_text(successful_request_id)
        assert browser.active_element_label() == f"View request {successful_request_id}"
        assert browser.focus_region("Logical requests table")
        browser.click_button("Diagnostics")
        browser.wait_for_text("Safe route diagnostic")
        browser.select_labeled_option_containing(
            "Provider-model route", "deepseek/deepseek-v4-flash"
        )
        diagnostic_route_id = browser.labeled_select_value("Provider-model route")
        assert diagnostic_route_id != ""
        browser.click_button("Run diagnostic")
        browser.wait_until(
            lambda: any(
                state in browser.body_text()
                for state in ("Diagnostic active", "Diagnostic succeeded")
            ),
            "The diagnostic did not become active or succeed.",
        )
        for _attempt in range(30):
            if "Diagnostic succeeded" in browser.body_text():
                break
            browser.wait_for_enabled_button("Refresh diagnostic status")
            browser.click_button("Refresh diagnostic status")
            browser.wait_until(
                lambda: (
                    "Diagnostic succeeded" in browser.body_text()
                    or browser.button_is_enabled("Refresh diagnostic status")
                ),
                "The diagnostic status refresh did not finish.",
            )
        browser.wait_for_text("Diagnostic succeeded")
        assert browser.definition_value("Service") == SERVICE_ID
        assert browser.definition_value("Workspace") == WORKSPACE_ID
        assert browser.definition_value("Route") == diagnostic_route_id
        diagnostic_revision = browser.definition_value("Route revision")
        assert diagnostic_revision != ""
        diagnostic_text = browser.body_text()
        for safe_phase in (
            "authorization",
            "route eligibility",
            "admission",
            "provider",
            "accounting",
        ):
            assert safe_phase in diagnostic_text.lower()
        assert "Reply only with OK" not in diagnostic_text
        assert "local response" not in diagnostic_text
        assert admin_secret not in diagnostic_text
        browser.set_viewport(390, 844)
        assert browser.has_no_horizontal_overflow()
        assert browser.focus_labeled_control("Provider-model route")
        _prove_diagnostic_database(
            diagnostic_route_id=diagnostic_route_id,
            diagnostic_revision=diagnostic_revision,
        )
        browser.click_button("Open global tasks")
        browser.wait_for_text("Run LLM Router")
        browser.click_button("Audit events")
        browser.wait_for_text("Security and administration activity")
        browser.wait_until(
            lambda: (
                browser.evaluate(
                    'document.querySelectorAll(".audit-event-card").length'
                )
                not in {None, 0}
            ),
            "The global audit page did not load its safe events.",
        )
        audit_text = browser.body_text()
        assert "permitted" in audit_text
        assert SERVICE_ID in audit_text
        assert WORKSPACE_ID in audit_text
        assert admin_secret not in audit_text
        assert "Return the deterministic response." not in audit_text
        assert "local response" not in audit_text
        browser.set_viewport(390, 844)
        assert browser.has_no_horizontal_overflow()
        assert browser.focus_labeled_control("From (UTC)")
        browser.click_button("Apply range")
        browser.wait_until(
            lambda: browser.active_element_label() == "Audit event results",
            "The refreshed audit results did not receive focus.",
        )
        browser.click_button("Provider credentials")
        browser.wait_for_text("Store OpenRouter credential")
        credential_count = browser.evaluate(
            'document.querySelectorAll("table tbody tr").length'
        )
        assert isinstance(credential_count, int)
        credential_ids = browser.evaluate(
            """[...document.querySelectorAll("table tbody tr small")]
              .map((item) => item.textContent?.trim())"""
        )
        assert isinstance(credential_ids, list)
        browser.fill_labeled_input("Safe label", "Browser lifecycle proof")
        browser.fill_labeled_input(
            "Provider secret", secrets.token_urlsafe(32), secret=True
        )
        browser.click_button("Store credential")
        browser.wait_for_text("The write-only OpenRouter credential was stored.")
        browser.wait_until(
            lambda: (
                browser.evaluate('document.querySelectorAll("table tbody tr").length')
                == credential_count + 1
            ),
            "The new credential did not become selectable after refresh.",
        )
        assert browser.evaluate(
            """(() => [...document.querySelectorAll('input[type="password"]')]
              .every((input) => input.value === ""))()"""
        )
        new_credential_ids = browser.evaluate(
            """[...document.querySelectorAll("table tbody tr small")]
              .map((item) => item.textContent?.trim())"""
        )
        assert isinstance(new_credential_ids, list)
        created_ids = [
            item
            for item in new_credential_ids
            if isinstance(item, str) and item not in credential_ids
        ]
        assert len(created_ids) == 1
        created_id = created_ids[0]
        browser.change_credential(created_id, "replace", secrets.token_urlsafe(32))
        browser.wait_for_text("The credential was replaced.")
        browser.change_credential(created_id, "disable")
        browser.wait_for_text("The credential was disabled.")
        browser.change_credential(created_id, "retire")
        browser.wait_for_text("The credential was retired.")
        browser.click_button("Sign out")
        browser.wait_for_text("Activate administrator session")


def _prove_diagnostic_database(
    *, diagnostic_route_id: str, diagnostic_revision: str
) -> None:
    """Prove exact audit, execution, and accounting for the browser diagnostic."""
    with psycopg_connect() as connection:
        authorization = connection.execute(
            """SELECT diagnostic_authorization.request_id::text,
                      diagnostic_authorization.route_configuration_revision_id::text,
                      grant_audit.action, use_audit.action
               FROM router.diagnostic_route_authorizations AS diagnostic_authorization
               JOIN router.diagnostic_route_grants AS diagnostic_grant
                 ON diagnostic_grant.grant_id = diagnostic_authorization.grant_id
               JOIN router.audit_events AS grant_audit
                 ON grant_audit.event_id = diagnostic_grant.creation_audit_event_id
               JOIN router.audit_events AS use_audit
                 ON use_audit.event_id = diagnostic_authorization.use_audit_event_id
               WHERE diagnostic_authorization.service_id = %s
                 AND diagnostic_authorization.workspace_id = %s
                 AND diagnostic_authorization.exact_route_id = %s
               ORDER BY diagnostic_authorization.authorized_at DESC
               LIMIT 1""",
            (SERVICE_ID, WORKSPACE_ID, diagnostic_route_id),
        ).fetchone()
        assert authorization is not None
        request_id, route_revision, grant_action, use_action = authorization
        assert route_revision == diagnostic_revision
        assert grant_action == "diagnostic.grant.create"
        assert use_action == "diagnostic.route.use"
        execution = connection.execute(
            """SELECT request.state,
                      count(DISTINCT attempt.id),
                      count(DISTINCT fact.event_id) FILTER (
                          WHERE fact.subject_kind = 'provider_attempt'
                      )
               FROM router.logical_requests AS request
               JOIN router.provider_attempts AS attempt
                 ON attempt.request_row_id = request.row_id
               LEFT JOIN router.accounting_facts AS fact
                 ON fact.request_row_id = request.row_id
               WHERE request.service_id = %s
                 AND request.workspace_id = %s
                 AND request.request_id = %s
                 AND attempt.provider_model_route_id = %s
               GROUP BY request.state""",
            (SERVICE_ID, WORKSPACE_ID, request_id, diagnostic_route_id),
        ).fetchone()
        assert execution is not None
        assert execution[0] == "succeeded"
        assert execution[1] == 1
        assert execution[2] >= 1
        leaked_audit = connection.execute(
            """SELECT count(*)
               FROM router.audit_events
               WHERE event_id IN (
                   SELECT diagnostic_grant.creation_audit_event_id
                   FROM router.diagnostic_route_grants AS diagnostic_grant
                   JOIN router.diagnostic_route_authorizations
                     AS diagnostic_authorization
                     ON diagnostic_authorization.grant_id = diagnostic_grant.grant_id
                   WHERE diagnostic_authorization.request_id = %s
                   UNION ALL
                   SELECT diagnostic_authorization.use_audit_event_id
                   FROM router.diagnostic_route_authorizations
                     AS diagnostic_authorization
                   WHERE diagnostic_authorization.request_id = %s
               )
                 AND (safe_details::text LIKE '%%Reply only with OK%%'
                      OR safe_details::text LIKE '%%local response%%')""",
            (request_id, request_id),
        ).fetchone()
        assert leaked_audit == (0,)


def _displayed_usd(value: str) -> Decimal:
    """Parse one displayed USD value without depending on decimal scale."""
    if not value.endswith(" USD"):
        raise AssertionError("The displayed budget currency is not USD.")
    return Decimal(value.removesuffix(" USD"))


class _CdpBrowser:
    """Control one local headless browser without an added browser dependency."""

    def __init__(self) -> None:
        self._profile = Path(tempfile.mkdtemp(prefix="llmrouter-live-browser-"))
        self._port = _unused_loopback_port()
        self._process = subprocess.Popen(  # noqa: S603
            [
                "/usr/bin/google-chrome",
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--remote-allow-origins=http://127.0.0.1",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={self._port}",
                f"--user-data-dir={self._profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._socket: _WebSocket | None = None
        try:
            endpoint = self._debugging_endpoint()
            self._socket = _WebSocket(endpoint)
            self._next_id = 0
            self._contexts: dict[str, int] = {}
            self.command("Page.enable")
            self.command("Runtime.enable")
        except Exception:
            self._stop()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_error: object) -> None:
        self._stop()

    def _stop(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                shutil.rmtree(self._profile)
            except FileNotFoundError:
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
            else:
                return

    def _debugging_endpoint(self) -> str:
        for _attempt in range(100):
            try:
                response = httpx.get(
                    f"http://127.0.0.1:{self._port}/json/list",
                    timeout=1,
                    trust_env=False,
                )
                if response.status_code == 200:
                    targets = response.json()
                    page = next(item for item in targets if item["type"] == "page")
                    return str(page["webSocketDebuggerUrl"])
            except httpx.HTTPError, StopIteration:
                pass
            time.sleep(0.1)
        raise AssertionError("The local browser debugging endpoint did not start.")

    def command(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert self._socket is not None
        self._next_id += 1
        identity = self._next_id
        message: dict[str, Any] = {"id": identity, "method": method}
        if params is not None:
            message["params"] = params
        self._socket.send(json.dumps(message, separators=(",", ":")))
        while True:
            document = json.loads(self._socket.receive())
            self._record_context(document)
            if document.get("id") != identity:
                continue
            if "error" in document:
                raise AssertionError("The local browser command failed safely.")
            return cast("dict[str, Any]", document.get("result", {}))

    def _record_context(self, document: dict[str, Any]) -> None:
        if document.get("method") == "Runtime.executionContextCreated":
            context = document["params"]["context"]
            auxiliary = context.get("auxData", {})
            frame_id = auxiliary.get("frameId")
            if auxiliary.get("isDefault") is True and isinstance(frame_id, str):
                self._contexts[frame_id] = int(context["id"])
        elif document.get("method") == "Runtime.executionContextDestroyed":
            destroyed = document["params"].get("executionContextId")
            self._contexts = {
                frame: context
                for frame, context in self._contexts.items()
                if context != destroyed
            }
        elif document.get("method") == "Runtime.executionContextsCleared":
            self._contexts.clear()

    def navigate(self, url: str) -> None:
        self.command("Page.navigate", {"url": url})
        self.wait_until(
            lambda: self.evaluate("document.readyState") == "complete",
            "The browser page did not load.",
        )

    def evaluate(self, expression: str, context_id: int | None = None) -> object:
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        result = self.command("Runtime.evaluate", params)
        if "exceptionDetails" in result:
            raise AssertionError("The local browser expression failed safely.")
        return result["result"].get("value")

    def body_text(self) -> str:
        value = self.evaluate("document.body.innerText")
        return value if isinstance(value, str) else ""

    def wait_for_text(self, value: str) -> None:
        self.wait_until(
            lambda: value in self.body_text(),
            "The expected local browser state did not load.",
        )

    def click_button(self, label: str) -> None:
        clicked = self.evaluate(
            f"""(() => {{
              const button = [...document.querySelectorAll("button")]
                .find((item) => item.textContent?.trim() === {json.dumps(label)});
              if (button === undefined) return false;
              button.click();
              return true;
            }})()"""
        )
        assert clicked is True

    def fill_labeled_input(
        self, label: str, value: str, *, secret: bool = False
    ) -> None:
        filled = self.evaluate(
            f"""(() => {{
              const control = [...document.querySelectorAll("label")]
                .find((item) => item.textContent?.includes({json.dumps(label)}))
                ?.querySelector("input");
              if (!(control instanceof HTMLInputElement)) return false;
              if ({json.dumps(secret)} && control.type !== "password") return false;
              const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, "value"
              )?.set;
              setter?.call(control, {json.dumps(value)});
              control.dispatchEvent(new Event("input", {{ bubbles: true }}));
              return true;
            }})()"""
        )
        assert filled is True

    def select_labeled_option(self, label: str, visible_text: str) -> None:
        """Select one option by its displayed name and send a React change event."""
        selected = self.evaluate(
            f"""(() => {{
              const control = [...document.querySelectorAll("label")]
                .find((item) => item.textContent?.includes({json.dumps(label)}))
                ?.querySelector("select");
              if (!(control instanceof HTMLSelectElement)) return false;
              const option = [...control.options].find(
                (item) => item.textContent?.trim() === {json.dumps(visible_text)}
              );
              if (option === undefined) return false;
              const setter = Object.getOwnPropertyDescriptor(
                HTMLSelectElement.prototype, "value"
              )?.set;
              setter?.call(control, option.value);
              control.dispatchEvent(new Event("change", {{ bubbles: true }}));
              return true;
            }})()"""
        )
        assert selected is True

    def select_labeled_option_containing(self, label: str, text: str) -> None:
        """Select one option that contains the specified visible text."""
        selected = self.evaluate(
            f"""(() => {{
              const control = [...document.querySelectorAll("label")]
                .find((item) => item.textContent?.includes({json.dumps(label)}))
                ?.querySelector("select");
              if (!(control instanceof HTMLSelectElement)) return false;
              const option = [...control.options].find(
                (item) => item.textContent?.includes({json.dumps(text)})
              );
              if (option === undefined) return false;
              const setter = Object.getOwnPropertyDescriptor(
                HTMLSelectElement.prototype, "value"
              )?.set;
              setter?.call(control, option.value);
              control.dispatchEvent(new Event("change", {{ bubbles: true }}));
              return true;
            }})()"""
        )
        assert selected is True

    def labeled_select_value(self, label: str) -> str:
        """Read the current value from one labeled select control."""
        value = self.evaluate(
            f"""(() => {{
              const control = [...document.querySelectorAll("label")]
                .find((item) => item.textContent?.includes({json.dumps(label)}))
                ?.querySelector("select");
              return control instanceof HTMLSelectElement ? control.value : "";
            }})()"""
        )
        return value if isinstance(value, str) else ""

    def button_is_enabled(self, label: str) -> bool:
        """Report whether one exact visible button is enabled."""
        value = self.evaluate(
            f"""(() => {{
              const button = [...document.querySelectorAll("button")]
                .find((item) => item.textContent?.trim() === {json.dumps(label)});
              return button instanceof HTMLButtonElement && !button.disabled;
            }})()"""
        )
        return value is True

    def wait_for_enabled_button(self, label: str) -> None:
        """Wait for one exact visible button to accept input."""
        self.wait_until(
            lambda: self.button_is_enabled(label),
            "The expected local browser action did not become available.",
        )

    def focus_labeled_control(self, label: str) -> bool:
        """Focus one labeled form control and confirm keyboard access."""
        value = self.evaluate(
            f"""(() => {{
              const control = [...document.querySelectorAll("label")]
                .find((item) => item.textContent?.includes({json.dumps(label)}))
                ?.querySelector("input, select, textarea, button");
              if (!(control instanceof HTMLElement)) return false;
              control.focus();
              return document.activeElement === control;
            }})()"""
        )
        return value is True

    def has_no_horizontal_overflow(self) -> bool:
        """Confirm that the current viewport has no page-level overflow."""
        value = self.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        return value is True

    def definition_value(self, label: str) -> str:
        """Read one value from the visible definition list."""
        value = self.evaluate(
            f"""(() => {{
              const term = [...document.querySelectorAll("dt")]
                .find((item) => item.textContent?.trim() === {json.dumps(label)});
              const description = term?.parentElement?.querySelector("dd");
              return description?.textContent?.trim() ?? "";
            }})()"""
        )
        return value if isinstance(value, str) else ""

    def click_request_detail(self, request_id: str) -> None:
        """Open one logical request from its exact table row."""
        clicked = self.evaluate(
            f"""(() => {{
              const row = [...document.querySelectorAll("table tbody tr")]
                .find((item) => item.querySelector("strong")?.textContent?.trim()
                  === {json.dumps(request_id)});
              const button = [...(row?.querySelectorAll("button") ?? [])]
                .find((item) => item.textContent?.trim() === "View request");
              if (button === undefined) return false;
              button.click();
              return true;
            }})()"""
        )
        assert clicked is True

    def track_request_detail_reads(self, request_id: str) -> None:
        """Count exact request-detail fetches in this local browser page."""
        installed = self.evaluate(
            f"""(() => {{
              performance.clearResourceTimings();
              globalThis.__llmrouterDetailTarget = "/model-requests/{request_id}";
              return true;
            }})()"""
        )
        assert installed is True

    def request_detail_reads(self) -> int:
        """Return the count of tracked exact request-detail fetches."""
        value = self.evaluate(
            """performance.getEntriesByType("resource")
              .filter((entry) => entry.name.includes(
                globalThis.__llmrouterDetailTarget ?? ""
              )).length"""
        )
        if not isinstance(value, int):
            raise TypeError("The request-detail read count is invalid.")
        return value

    def set_viewport(self, width: int, height: int) -> None:
        """Set one local phone-size browser viewport."""
        self.command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": True,
            },
        )

    def active_element_label(self) -> str:
        """Read the accessible label of the active browser element."""
        value = self.evaluate(
            "document.activeElement?.getAttribute('aria-label') ?? ''"
        )
        return value if isinstance(value, str) else ""

    def focus_region(self, label: str) -> bool:
        """Focus one named scroll region and confirm keyboard access."""
        value = self.evaluate(
            f"""(() => {{
              const region = document.querySelector(
                `[role="region"][aria-label={json.dumps(label)}]`
              );
              if (!(region instanceof HTMLElement) || region.tabIndex !== 0) {{
                return false;
              }}
              region.focus();
              return document.activeElement === region;
            }})()"""
        )
        return value is True

    def request_attempt_rows(self) -> list[str]:
        """Read the ordered provider-attempt rows from request detail."""
        value = self.evaluate(
            """(() => {
              const heading = [...document.querySelectorAll("h3")]
                .find((item) => item.textContent?.trim()
                  === "Ordered provider attempts");
              const table = heading?.parentElement?.querySelector("table");
              return [...(table?.querySelectorAll("tbody tr") ?? [])]
                .map((row) => row.textContent?.trim() ?? "");
            })()"""
        )
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise AssertionError("The ordered request attempts are not available.")
        return value

    def change_credential(
        self,
        credential_id: str,
        action: str,
        replacement_secret: str | None = None,
    ) -> None:
        if action not in {"replace", "disable", "retire"}:
            raise AssertionError("The browser credential action is invalid.")
        changed = self.evaluate(
            f"""(() => {{
              const rows = [...document.querySelectorAll("table tbody tr")];
              const row = rows.find((item) =>
                item.querySelector("small")?.textContent?.trim()
                  === {json.dumps(credential_id)}
              );
              if (!(row instanceof HTMLTableRowElement)) return false;
              if ({json.dumps(replacement_secret)} !== null) {{
                const input = row.querySelector('input[type="password"]');
                if (!(input instanceof HTMLInputElement)) return false;
                const setter = Object.getOwnPropertyDescriptor(
                  HTMLInputElement.prototype, "value"
                )?.set;
                setter?.call(input, {json.dumps(replacement_secret)});
                input.dispatchEvent(new Event("input", {{ bubbles: true }}));
              }}
              const button = [...row.querySelectorAll("button")]
                .find((item) => item.textContent?.trim().toLowerCase()
                  === {json.dumps(action)});
              if (button === undefined) return false;
              button.click();
              return true;
            }})()"""
        )
        assert changed is True

    def activate_administrator(self, secret: str) -> None:
        activated = self.evaluate(
            f"""(() => {{
              const input = document.querySelector(
                'input[name="local-administrator-secret"]'
              );
              if (!(input instanceof HTMLInputElement)) return false;
              const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, "value"
              )?.set;
              setter?.call(input, {json.dumps(secret)});
              input.dispatchEvent(new Event("input", {{ bubbles: true }}));
              const form = input.closest("form");
              form?.querySelector("button")?.click();
              return true;
            }})()"""
        )
        assert activated is True

    def frames(self) -> list[dict[str, str]]:
        tree = self.command("Page.getFrameTree")["frameTree"]
        result: list[dict[str, str]] = []

        def collect(node: dict[str, Any]) -> None:
            frame = node["frame"]
            result.append({"id": str(frame["id"]), "url": str(frame["url"])})
            for child in node.get("childFrames", []):
                collect(child)

        collect(tree)
        return result

    def wait_for_frame(
        self, path: str, *, excluded_id: str | None = None
    ) -> dict[str, str]:
        selected: dict[str, str] | None = None

        def find() -> bool:
            nonlocal selected
            selected = next(
                (
                    frame
                    for frame in self.frames()
                    if path in frame["url"] and frame["id"] != excluded_id
                ),
                None,
            )
            return selected is not None

        self.wait_until(find, "The authorized Router frame did not load.")
        assert selected is not None
        return selected

    def frame_text(self, frame: dict[str, str]) -> str:
        context_id: int | None = None

        def find_context() -> bool:
            nonlocal context_id
            context_id = self._contexts.get(frame["id"])
            if context_id is None:
                self.command("Runtime.evaluate", {"expression": "true"})
                context_id = self._contexts.get(frame["id"])
            return context_id is not None

        self.wait_until(find_context, "The Router frame context did not load.")
        assert context_id is not None
        value = self.evaluate("document.body.innerText", context_id)
        return value if isinstance(value, str) else ""

    def wait_for_frame_text(self, frame: dict[str, str], *values: str) -> str:
        text = ""

        def loaded() -> bool:
            nonlocal text
            text = self.frame_text(frame)
            return all(value in text for value in values)

        self.wait_until(loaded, "The bounded Router frame state did not load.")
        return text

    @staticmethod
    def wait_until(check: Callable[[], bool], message: str) -> None:
        for _attempt in range(150):
            try:
                if check():
                    return
            except AssertionError, KeyError:
                pass
            time.sleep(0.1)
        raise AssertionError(message)


class _WebSocket:
    """Use the small RFC 6455 subset that Chrome CDP needs."""

    def __init__(self, endpoint: str) -> None:
        url = httpx.URL(endpoint)
        if url.scheme != "ws" or url.host != "127.0.0.1" or url.port is None:
            raise AssertionError("The browser debugging endpoint is not loopback.")
        self._socket = socket.create_connection((url.host, url.port), timeout=10)
        self._socket.settimeout(10)
        key = b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {url.raw_path.decode()} HTTP/1.1\r\n"
            f"Host: {url.host}:{url.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(self._socket.recv(4096))
        header, remaining = bytes(response).split(b"\r\n\r\n", 1)
        if not header.startswith(b"HTTP/1.1 101 "):
            raise AssertionError("The browser debugging socket did not upgrade.")
        self._buffer = bytearray(remaining)

    def close(self) -> None:
        self._socket.close()

    def send(self, value: str) -> None:
        self._send_frame(1, value.encode())

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 65_535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(header)

    def receive(self) -> str:
        fragments = bytearray()
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length)
            if opcode == 8:
                raise AssertionError("The browser debugging socket closed.")
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode in {0, 1}:
                fragments.extend(payload)
                if first & 0x80:
                    return fragments.decode()

    def _read(self, length: int) -> bytes:
        while len(self._buffer) < length:
            part = self._socket.recv(max(4096, length - len(self._buffer)))
            if not part:
                raise AssertionError("The browser debugging socket closed.")
            self._buffer.extend(part)
        result = bytes(self._buffer[:length])
        del self._buffer[:length]
        return result


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def psycopg_connect() -> psycopg.Connection[Any]:
    """Open the local database without displaying its generated password."""
    password = _secret(STATE_DIRECTORY / "postgres-password")
    return psycopg.connect(
        f"postgresql://llmrouter:{password}@127.0.0.1:5434/llmrouter"
    )


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    idempotency: str | None = None,
    json: dict[str, Any] | None = None,
    expected: set[int] = {200},
) -> dict[str, Any]:
    headers = {} if idempotency is None else {"Idempotency-Key": idempotency}
    response = client.request(method, path, headers=headers, json=json)
    if response.status_code not in expected:
        raise AssertionError(
            f"{method} {path} failed with {response.status_code}: {response.text[:500]}"
        )
    return cast("dict[str, Any]", response.json())


def _create(token: str, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
    response = _raw_create(token, request_id, body)
    assert response.status_code in {200, 201}, response.text[:500]
    return cast("dict[str, Any]", response.json())


def _raw_create(token: str, request_id: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{BASE_URL}/v1/model-requests",
        headers={
            "Authorization": f"Bearer {token}",
            "X-LLMRouter-Request-ID": request_id,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=10,
    )


def _wait_terminal(token: str, request_id: str) -> dict[str, Any]:
    for _attempt in range(100):
        response = httpx.get(
            f"{BASE_URL}/v1/model-requests/{request_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        assert response.status_code == 200, response.text[:500]
        document = cast("dict[str, Any]", response.json())
        if document["state"] in {
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
            "uncertain",
        }:
            return document
        time.sleep(0.1)
    raise AssertionError("The model request did not become terminal.")


def _wait_attempt_started(request_id: str, *, candidate_ordinal: int) -> None:
    for _attempt in range(100):
        with psycopg_connect() as connection:
            started = connection.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM router.provider_attempts
                       WHERE request_row_id = (
                           SELECT row_id FROM router.logical_requests
                           WHERE request_id = %s
                       ) AND candidate_ordinal = %s AND state = 'started'
                   )""",
                (request_id, candidate_ordinal),
            ).fetchone()
        if started == (True,):
            return
        time.sleep(0.1)
    raise AssertionError("The cancellable provider attempt did not start.")


def _admit_restart_recovery(data_token: str) -> str:
    """Admit work and cross the durable no-repeat boundary before SIGKILL."""
    recovery_id = _uuidv7()
    recovery_body = _model_body("Wait for restart recovery.")
    _create(data_token, recovery_id, recovery_body)
    _wait_attempt_dispatched(recovery_id, candidate_ordinal=2)
    return recovery_id


def _wait_attempt_dispatched(request_id: str, *, candidate_ordinal: int) -> None:
    """Wait until exact provider dispatch evidence is durable."""
    for _attempt in range(100):
        with psycopg_connect() as connection:
            dispatched = connection.execute(
                """SELECT EXISTS (
                       SELECT 1
                       FROM router.routing_attempt_dispatches AS dispatch
                       JOIN router.provider_attempts AS attempt
                         ON attempt.id = dispatch.attempt_id
                       WHERE attempt.request_row_id = (
                           SELECT row_id FROM router.logical_requests
                           WHERE request_id = %s
                       ) AND attempt.candidate_ordinal = %s
                   )""",
                (request_id, candidate_ordinal),
            ).fetchone()
        if dispatched == (True,):
            return
        time.sleep(0.1)
    raise AssertionError("The restart provider attempt did not become dispatched.")


def _events(token: str, request_id: str) -> str:
    response = httpx.get(
        f"{BASE_URL}/v1/model-requests/{request_id}/events",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream; llmrouter-stream=1",
        },
        timeout=10,
    )
    assert response.status_code == 200
    return response.text


def _model_body(text: str, *, workspace_id: str = WORKSPACE_ID) -> dict[str, Any]:
    return {
        "api_version": "1",
        "data_profile": "service-data",
        "workspace_id": workspace_id,
        "assignment": "general",
        "messages": [{"role": "user", "content": text}],
        "limits": {"attempt_timeout_ms": 30_000, "max_output_units": 128},
        "output": {"format": "text"},
    }


def _price(unit: str, price: str) -> dict[str, str]:
    quantity = "1" if unit == "request" else "1000000"
    return {
        "unit": unit,
        "price": price,
        "currency": "USD",
        "raw_source_value": f"{price} USD per {quantity} {unit}",
        "unit_quantity": quantity,
    }


def _uuidv7() -> str:
    milliseconds = int(datetime.now(UTC).timestamp() * 1000)
    random_bits = secrets.randbits(74)
    value = (milliseconds << 80) | (7 << 76) | ((random_bits >> 62) << 64)
    value |= (2 << 62) | (random_bits & ((1 << 62) - 1))
    return str(uuid.UUID(int=value))


def _secret(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb", closefd=True) as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AssertionError("A local test secret path is unsafe.")
        return source.read().decode("ascii").strip()


def _write_state(value: dict[str, Any]) -> None:
    descriptor = os.open(
        STATE_PATH,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(value, output, sort_keys=True, separators=(",", ":"))


if __name__ == "__main__":
    main()
