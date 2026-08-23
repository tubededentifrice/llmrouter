"""Tests for generated Python contracts and runtime validation."""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from llmrouter_client import ContractValidationError, validate_contract
from llmrouter_client.generated_models import CONTRACT_SCHEMA_NAMES, CONTRACT_SCHEMAS

ROOT = Path(__file__).parents[3]
EXPECTED_SCHEMA_COUNT = 156
FIXTURES = {
    "ContractManifest": "contract-manifest.json",
    "ServiceToken": "service-token.json",
    "Workspace": "workspace.json",
    "Attachment": "attachment.json",
    "ModelRequest": "model-request.json",
    "EffectiveConfiguration": "effective-configuration.json",
    "AdministratorGrant": "administration-grant.json",
    "Health": "health.json",
    "BusinessToolCall": "business-tool-call.json",
    "EmbedAdministrationSnapshot": "embed-administration-snapshot.json",
}


def load_fixture(filename: str) -> dict[str, Any]:
    """Load one accepted strict JSON fixture."""
    path = ROOT / "docs/api/fixtures" / filename
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(("schema_name", "filename"), FIXTURES.items())
def test_every_valid_fixture_round_trips(schema_name: str, filename: str) -> None:
    """Each accepted fixture validates and survives a JSON round trip."""
    fixture = load_fixture(filename)
    validated = validate_contract(schema_name, fixture)
    assert json.loads(json.dumps(validated)) == fixture


@pytest.mark.parametrize(("schema_name", "filename"), FIXTURES.items())
def test_every_fixture_rejects_an_unknown_field(
    schema_name: str, filename: str
) -> None:
    """Each closed fixture schema rejects an unknown sibling field."""
    fixture = load_fixture(filename)
    fixture["unknown_contract_field"] = True
    with pytest.raises(ContractValidationError, match="unknown_contract_field"):
        validate_contract(schema_name, fixture)


def test_validator_enforces_numeric_and_string_constraints() -> None:
    """Runtime validation enforces limits that static types cannot express."""
    request = load_fixture("model-request.json")
    request["limits"]["attempt_timeout_ms"] = 120001
    with pytest.raises(ContractValidationError, match="maximum"):
        validate_contract("ModelRequest", request)

    attachment = load_fixture("attachment.json")
    attachment["sha256"] = "short"
    with pytest.raises(ContractValidationError, match="does not match"):
        validate_contract("Attachment", attachment)

    tool_call = load_fixture("business-tool-call.json")
    tool_call["deadline"] = "not-a-time"
    with pytest.raises(ContractValidationError, match="date-time"):
        validate_contract("BusinessToolCall", tool_call)


def test_budget_writes_use_one_numeric_revision_contract() -> None:
    """Both budget write contracts reject opaque nonnumeric revisions."""
    common = {
        "hard_limit": {"amount": "100", "currency": "USD"},
        "warning_threshold": {"amount": "80", "currency": "USD"},
        "reset_period": "monthly",
        "expected_revision": "revision-one",
    }
    with pytest.raises(ContractValidationError, match="does not match"):
        validate_contract("PutBudgetLimit", {"scope": "service", **common})
    with pytest.raises(ContractValidationError, match="does not match"):
        validate_contract(
            "PutSelectedBudgetLimit",
            {
                "hard_limit": "100",
                "currency": "USD",
                "warning_threshold": "80",
                "reset_period": "monthly",
                "expected_revision": "revision-one",
            },
        )


def test_generated_schema_catalog_is_complete() -> None:
    """The generated catalog contains the accepted fixture schemas."""
    assert set(FIXTURES) < CONTRACT_SCHEMA_NAMES
    assert len(CONTRACT_SCHEMA_NAMES) == EXPECTED_SCHEMA_COUNT
    with pytest.raises(ContractValidationError, match="Unknown contract schema"):
        validate_contract("NotAContract", {})


def test_administrator_request_status_forbids_retained_result_content() -> None:
    """Administrator request status has no retained result field."""
    status = cast("dict[str, Any]", CONTRACT_SCHEMAS["AdministrationRequestStatus"])
    properties = cast("dict[str, Any]", status["properties"])
    assert status["additionalProperties"] is False
    assert "result" not in properties
    page = cast("dict[str, Any]", CONTRACT_SCHEMAS["AdministrationRequestStatusPage"])
    page_properties = cast("dict[str, Any]", page["properties"])
    items = cast("dict[str, Any]", page_properties["items"])
    item_schema = cast("dict[str, Any]", items["items"])
    assert item_schema["$ref"] == "#/components/schemas/AdministrationRequestStatus"
    embed = cast("dict[str, Any]", CONTRACT_SCHEMAS["EmbedRequestStatus"])
    assert embed["$ref"] == "#/components/schemas/AdministrationRequestStatus"


def test_identity_and_exact_route_negative_cases() -> None:
    """Identity and diagnostic routing constraints fail at runtime."""
    with pytest.raises(ContractValidationError, match="does not match"):
        validate_contract("UuidV7", "0198a5b0-1234-6abc-8def-0123456789ab")
    with pytest.raises(ContractValidationError, match="date-time"):
        validate_contract("Timestamp", "not-a-time")

    request = load_fixture("model-request.json")
    request["exact_route"] = "route-1"
    with pytest.raises(ContractValidationError):
        validate_contract("ModelRequest", request)

    workspace = load_fixture("workspace.json")
    del workspace["workspace_id"]
    with pytest.raises(ContractValidationError, match="required"):
        validate_contract("Workspace", workspace)
    workspace = load_fixture("workspace.json")
    workspace["state"] = "unknown"
    with pytest.raises(ContractValidationError, match="not one of"):
        validate_contract("Workspace", workspace)
