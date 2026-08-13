"""Durable node-local budget allowance tests."""
# ruff: noqa: D103, FBT003, PLR2004

from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from llmrouter_backend.authority import (
    AuthorityClass,
    AuthorityPath,
    PrincipalKind,
    RequestContext,
    Scope,
)
from llmrouter_backend.budgets import (
    AllowanceBatch,
    AllowanceLease,
    BudgetError,
    BudgetErrorCode,
    SqliteAllowanceWallet,
)

NOW = datetime(2026, 9, 15, 10, tzinfo=UTC)
NODE = "0198a080-0000-7000-8000-000000000301"
BATCH = "0198a080-0000-7000-8000-000000000302"
LEASE_A = "0198a080-0000-7000-8000-000000000303"
LEASE_B = "0198a080-0000-7000-8000-000000000304"
SCOPE_A = "0198a080-0000-7000-8000-000000000305"
SCOPE_B = "0198a080-0000-7000-8000-000000000306"
AUTHORITY = "budget-authority"


def _authority(*, actor_id: str = AUTHORITY) -> RequestContext:
    return RequestContext(
        "allowance-install",
        PrincipalKind.SYSTEM,
        actor_id,
        AuthorityClass.SYSTEM,
        AuthorityPath.MACHINE,
        None,
        "budget.allowance.install",
        Scope(),
        NOW,
        None,
        True,
    )


def _batch(*, generation: int = 1, batch_id: str = BATCH) -> AllowanceBatch:
    leases = tuple(
        AllowanceLease(
            lease_id,
            batch_id,
            scope_id,
            NODE,
            generation,
            "USD",
            Decimal(10),
            Decimal(2),
            NOW,
            NOW + timedelta(minutes=2),
            NOW + timedelta(minutes=3),
        )
        for lease_id, scope_id in ((LEASE_A, SCOPE_A), (LEASE_B, SCOPE_B))
    )
    return AllowanceBatch(
        batch_id,
        BATCH,
        NODE,
        generation,
        None,
        None,
        None,
        "USD",
        (SCOPE_A, SCOPE_B),
        leases,
    )


def test_wallet_restart_keeps_consumption_and_finalization(tmp_path: object) -> None:
    path = tmp_path / "allowance.sqlite"  # type: ignore[operator]
    first = SqliteAllowanceWallet(path, owner_node_id=NODE, authority_id=AUTHORITY)
    first.install(_authority(), _batch())
    first.consume(
        BATCH,
        Decimal(4),
        service_id=None,
        workspace_id=None,
        assignment_id=None,
        now=NOW,
    )

    restarted = SqliteAllowanceWallet(path, owner_node_id=NODE, authority_id=AUTHORITY)
    debit = restarted.consume(
        BATCH,
        Decimal(3),
        service_id=None,
        workspace_id=None,
        assignment_id=None,
        now=NOW + timedelta(seconds=1),
    )
    assert dict(debit.consumed_by_lease) == {LEASE_A: Decimal(7), LEASE_B: Decimal(7)}
    state = restarted.state(BATCH, now=NOW + timedelta(seconds=1))
    assert state.current
    assert not state.finalized
    assert {item.remaining_amount for item in state.scopes} == {Decimal(3)}
    assert {item.maximum_correction_risk for item in state.scopes} == {Decimal(2)}
    final = restarted.final(BATCH)
    assert {(item.used_amount, item.returned_amount) for item in final} == {
        (Decimal(7), Decimal(3))
    }
    assert restarted.final(BATCH) == final
    final_state = restarted.state(BATCH, now=NOW + timedelta(seconds=1))
    assert final_state.finalized
    assert not final_state.current
    with pytest.raises(BudgetError) as error:
        restarted.consume(
            BATCH,
            Decimal(1),
            service_id=None,
            workspace_id=None,
            assignment_id=None,
            now=NOW + timedelta(seconds=2),
        )
    assert error.value.code is BudgetErrorCode.STALE_ALLOWANCE


def test_wallet_finalization_and_consumption_serialize(tmp_path: object) -> None:
    wallet = SqliteAllowanceWallet(
        tmp_path / "allowance.sqlite",  # type: ignore[operator]
        owner_node_id=NODE,
        authority_id=AUTHORITY,
    )
    wallet.install(_authority(), _batch())

    def consume() -> str:
        try:
            wallet.consume(
                BATCH,
                Decimal(1),
                service_id=None,
                workspace_id=None,
                assignment_id=None,
                now=NOW,
            )
        except BudgetError:
            return "closed"
        return "used"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        consume_result = executor.submit(consume)
        final_result = executor.submit(wallet.final, BATCH)
    final = final_result.result()
    if consume_result.result() == "used":
        assert all(item.used_amount == 1 for item in final)
    else:
        assert all(item.used_amount == 0 for item in final)
    assert all(item.used_amount + item.returned_amount == 10 for item in final)


def test_wallet_rejects_preissue_and_frozen_clock_after_restart(
    tmp_path: object,
) -> None:
    path = tmp_path / "allowance.sqlite"  # type: ignore[operator]
    wallet = SqliteAllowanceWallet(path, owner_node_id=NODE, authority_id=AUTHORITY)
    with pytest.raises(BudgetError) as unauthorized:
        wallet.install(_authority(actor_id="untrusted-node"), _batch())
    assert unauthorized.value.code is BudgetErrorCode.INSUFFICIENT_SCOPE
    wallet.install(_authority(), _batch())
    with pytest.raises(BudgetError) as early:
        wallet.consume(
            BATCH,
            Decimal(1),
            service_id=None,
            workspace_id=None,
            assignment_id=None,
            now=NOW - timedelta(seconds=1),
        )
    assert early.value.code is BudgetErrorCode.STALE_ALLOWANCE
    wallet.consume(
        BATCH,
        Decimal(1),
        service_id=None,
        workspace_id=None,
        assignment_id=None,
        now=NOW + timedelta(seconds=1),
    )
    restarted = SqliteAllowanceWallet(path, owner_node_id=NODE, authority_id=AUTHORITY)
    with pytest.raises(BudgetError) as frozen:
        restarted.consume(
            BATCH,
            Decimal(1),
            service_id=None,
            workspace_id=None,
            assignment_id=None,
            now=NOW + timedelta(seconds=1),
        )
    assert frozen.value.code is BudgetErrorCode.STALE_ALLOWANCE
    with pytest.raises(BudgetError) as backward:
        restarted.consume(
            BATCH,
            Decimal(1),
            service_id=None,
            workspace_id=None,
            assignment_id=None,
            now=NOW,
        )
    assert backward.value.code is BudgetErrorCode.STALE_ALLOWANCE
    restarted.consume(
        BATCH,
        Decimal(1),
        service_id=None,
        workspace_id=None,
        assignment_id=None,
        now=NOW + timedelta(seconds=2),
    )


def test_wallet_fences_old_generation_and_invalid_batches(tmp_path: object) -> None:
    wallet = SqliteAllowanceWallet(
        tmp_path / "allowance.sqlite",  # type: ignore[operator]
        owner_node_id=NODE,
        authority_id=AUTHORITY,
    )
    old = _batch()
    wallet.install(_authority(), old)
    new = _batch(
        generation=2,
        batch_id="0198a080-0000-7000-8000-000000000307",
    )
    new = replace(
        new,
        lineage_id=old.lineage_id,
        leases=tuple(
            replace(
                lease,
                lease_id=f"0198a080-0000-7000-8000-0000000003{index:02d}",
            )
            for index, lease in enumerate(new.leases, 8)
        ),
    )
    wallet.install(_authority(), new)
    with pytest.raises(BudgetError) as wrong_scope:
        wallet.consume(
            new.batch_id,
            Decimal(1),
            service_id="0198a080-0000-7000-8000-000000000399",
            workspace_id=None,
            assignment_id=None,
            now=NOW,
        )
    assert wrong_scope.value.code is BudgetErrorCode.STALE_ALLOWANCE
    wrong_scope_state = wallet.state(new.batch_id, now=NOW)
    assert all(item.remaining_amount == 10 for item in wrong_scope_state.scopes)
    with pytest.raises(BudgetError) as error:
        wallet.consume(
            BATCH,
            Decimal(1),
            service_id=None,
            workspace_id=None,
            assignment_id=None,
            now=NOW,
        )
    assert error.value.code is BudgetErrorCode.STALE_ALLOWANCE
    wallet.consume(
        new.batch_id,
        Decimal(1),
        service_id=None,
        workspace_id=None,
        assignment_id=None,
        now=NOW,
    )

    with pytest.raises(BudgetError) as changed_replay:
        wallet.install(
            _authority(),
            replace(
                new,
                leases=(
                    replace(new.leases[0], issued_amount=Decimal(9)),
                    new.leases[1],
                ),
            ),
        )
    assert changed_replay.value.code is BudgetErrorCode.STALE_ALLOWANCE

    with pytest.raises(ValueError, match="do not match"):
        replace(new, leases=(new.leases[0], replace(new.leases[1], currency="EUR")))
