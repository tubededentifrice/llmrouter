"""Exact accounting value and authority tests."""
# ruff: noqa: D103, FBT003, FURB157, PLR2004, SLF001

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from llmrouter_backend.accounting import (
    AccountingCorrection,
    AccountingEvent,
    AccountingSubjectKind,
    AttemptOutcome,
    CorrectionKind,
    PostgresAccountingRepository,
    PriceComponent,
    SourceSnapshot,
    UsageComponent,
    UsageDelta,
    UsageUnit,
    exact_decimal,
)
from llmrouter_backend.accounting.errors import AccountingError
from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)

NOW = datetime(2026, 8, 13, tzinfo=UTC)


class _PriceRows:
    def __init__(self, rows: list[tuple[str, str, Decimal, Decimal]]) -> None:
        self._rows = rows

    def execute(self, *_args: object, **_kwargs: object) -> _PriceRows:
        return self

    def fetchall(self) -> list[tuple[str, str, Decimal, Decimal]]:
        return self._rows


def _priced_event(price_version_id: str) -> AccountingEvent:
    return AccountingEvent(
        "event",
        "canonical",
        "request",
        "service",
        None,
        "budget",
        AccountingSubjectKind.PROVIDER_ATTEMPT,
        "attempt",
        AttemptOutcome.SUCCEEDED,
        "USD",
        (UsageComponent(UsageUnit.INPUT_TOKEN, Decimal(4)),),
        NOW,
        price_version_id=price_version_id,
    )


def test_accounting_values_reject_binary_float_and_allow_signed_corrections() -> None:
    with pytest.raises(TypeError):
        exact_decimal(0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fraction digits"):
        exact_decimal("0.0000000000000000001")
    correction = AccountingCorrection(
        "0198a080-0000-7000-8000-000000000101",
        "0198a080-0000-7000-8000-000000000102",
        CorrectionKind.PROVIDER_USAGE,
        "USD",
        Decimal("-0.125"),
        (UsageDelta(UsageUnit.INPUT_TOKEN, Decimal("-2")),),
        "provider-report",
        "The provider corrected the reported usage.",
        NOW,
    )
    assert correction.amount_delta == Decimal("-0.125")
    assert correction.usage_delta[0].quantity == Decimal("-2")


def test_price_calculation_removes_only_insignificant_fraction_zeros() -> None:
    repository = object.__new__(PostgresAccountingRepository)
    event = _priced_event("per-million")
    rows = _PriceRows(
        [("USD", "input_token", Decimal("1000000"), Decimal("0.100000000000000000"))]
    )
    assert repository._event_amount(rows, event) == Decimal("0.0000004")  # type: ignore[arg-type]

    over_scale = _PriceRows(
        [("USD", "input_token", Decimal(3), Decimal("1.000000000000000000"))]
    )
    with pytest.raises(AccountingError, match="exceeds the accounting scale"):
        repository._event_amount(over_scale, event)  # type: ignore[arg-type]


def test_source_snapshot_copies_its_price_rows() -> None:
    prices = {"model": (PriceComponent(UsageUnit.REQUEST, Decimal(1), "USD", "1"),)}
    snapshot = SourceSnapshot("catalog", NOW, SourceSnapshot.digest(prices), prices)
    prices.clear()
    assert tuple(snapshot.rows) == ("model",)
    with pytest.raises(TypeError):
        snapshot.rows["other"] = ()  # type: ignore[index]


def test_empty_reported_usage_has_one_canonical_payload_binding() -> None:
    event = AccountingEvent(
        "0198a080-0000-7000-8000-000000000103",
        "0198a080-0000-7000-8000-000000000104",
        "0198a080-0000-7000-8000-000000000105",
        "0198a080-0000-7000-8000-000000000106",
        None,
        "0198a080-0000-7000-8000-000000000107",
        AccountingSubjectKind.LOGICAL_REQUEST,
        "0198a080-0000-7000-8000-000000000105",
        AttemptOutcome.REFUSED,
        "USD",
        (),
        NOW,
        reported_amount=Decimal(0),
    )
    assert event.usage == ()
    assert len(event.canonical_payload_sha256()) == 32
    envelope = event.canonical_event("0198a080-0000-7000-8000-000000000108", 1)
    assert envelope.event_id == event.canonical_event_id
    assert envelope.payload == event.canonical_payload()
    assert replace(
        event, outcome=AttemptOutcome.UNCERTAIN
    ).canonical_payload_sha256() != (event.canonical_payload_sha256())


def test_embed_accounting_read_requires_the_exact_authority_tuple() -> None:
    scope = Scope("0198a080-0000-7000-8000-000000000001")
    valid = RequestContext(
        "request",
        PrincipalKind.EMBED,
        "user",
        AuthorityClass.SERVICE,
        AuthorityPath.EMBED,
        None,
        "accounting.read",
        scope,
        NOW,
        NOW,
        False,
    )
    PostgresAccountingRepository._require_read(valid, scope)
    for forged in (
        replace(valid, actor_kind=PrincipalKind.SERVICE),
        replace(valid, authority_class=AuthorityClass.GLOBAL_ADMINISTRATOR),
        replace(valid, authority_path=AuthorityPath.MACHINE),
        replace(valid, scope=Scope()),
        replace(valid, operation="budget.read"),
        replace(valid, mutation=True),
    ):
        with pytest.raises(AccountingError):
            PostgresAccountingRepository._require_read(forged, scope)
