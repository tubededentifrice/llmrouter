"""Exact hierarchical budget value and authority tests."""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from llmrouter_backend.authority import (
    Audience,
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import (
    BudgetError,
    BudgetScopeKind,
    BudgetTarget,
    Money,
    PostgresBudgetRepository,
    ReservationResult,
    ReservationState,
)

NOW = datetime(2026, 8, 13, tzinfo=UTC)
SERVICE = "0198a080-0000-7000-8000-000000000001"
WORKSPACE = "0198a080-0000-7000-8000-000000000003"


def test_money_rejects_float_and_keeps_exact_decimal() -> None:
    assert Money(Decimal("0.100000000000000001"), "USD").amount == Decimal(
        "0.100000000000000001"
    )
    with pytest.raises(TypeError):
        Money(0.1, "USD")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="currency"):
        Money(Decimal(1), "usd")


def test_budget_targets_have_closed_scope_shapes() -> None:
    assert BudgetTarget(BudgetScopeKind.GLOBAL).service_id is None
    assert BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE, WORKSPACE).workspace_id == (
        WORKSPACE
    )
    with pytest.raises(ValueError, match="scope kind"):
        BudgetTarget(BudgetScopeKind.WORKSPACE, SERVICE)
    with pytest.raises(ValueError, match="scope kind"):
        BudgetTarget(BudgetScopeKind.HOST_CEILING, SERVICE, WORKSPACE)


def test_replayed_reservation_does_not_permit_an_external_effect() -> None:
    created = ReservationResult(
        ReservationState.RESERVED,
        "request",
        "candidate",
        "USD",
        "reservation",
    )
    assert created.external_effects_permitted
    assert not replace(created, replayed=True).external_effects_permitted


def test_host_ceiling_rejects_human_and_embed_authority_before_database() -> None:
    valid = RequestContext(
        "request",
        PrincipalKind.SERVICE,
        SERVICE,
        AuthorityClass.SERVICE,
        AuthorityPath.MACHINE,
        Audience.BUDGET_AUTHORITY,
        "budget_ceiling.write",
        Scope(SERVICE, WORKSPACE),
        NOW,
        None,
        mutation=True,
    )
    for forged in (
        replace(
            valid,
            actor_kind=PrincipalKind.ADMINISTRATOR,
            actor_id="human",
            authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR,
            authority_path=AuthorityPath.GLOBAL_ADMINISTRATION,
            machine_audience=None,
        ),
        replace(
            valid,
            actor_kind=PrincipalKind.EMBED,
            actor_id="embed",
            authority_path=AuthorityPath.EMBED,
            machine_audience=None,
        ),
        replace(valid, machine_audience=Audience.CONFIGURATION),
        replace(valid, scope=Scope(SERVICE)),
    ):
        with pytest.raises(BudgetError):
            PostgresBudgetRepository("postgresql://unused").put_host_ceiling(
                forged,
                service_id=SERVICE,
                workspace_id=WORKSPACE,
                amount=Decimal(1),
                currency="USD",
                expected_revision=None,
                idempotency_key="host-ceiling-test-key",
                reason="Test ceiling.",
                now=NOW,
            )
