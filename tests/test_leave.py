"""Refund accounting and the shipped engine's revocation barrier."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from lnbits.extensions.quakejs import crud, leaving, payments
from lnbits.extensions.quakejs.migrations import m012_leave_refunds
from lnbits.extensions.quakejs.models import EntryInput, PublicError
from lnbits.extensions.quakejs.server import Manager, Match
from lnbits.extensions.quakejs.tests.test_ledger import arena, paid, payout_invoice

__all__ = ["arena"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def manager_for_test():
    manager = Manager()
    manager.running = True
    return manager


@asynccontextmanager
async def engine(arena, tmp_path):
    manager = manager_for_test()
    manager.directory.mkdir(parents=True, exist_ok=True)
    manager.worker_lock = (tmp_path / "leave-worker.lock").open("a+b")
    match = Match(manager, arena, arena["run_id"])
    manager.matches[arena["id"]] = match
    try:
        await match.start()
        yield manager, match
    finally:
        await match.stop()
        manager.worker_lock.close()


def connection(token):
    return SimpleNamespace(token=token, life=None, match=None, offer=lambda _: None)


@pytest.mark.anyio
async def test_leave_retries_settlement_replay_and_admission_cannot_restore_lives(
    arena,
):
    token, entry, payment = await paid(arena)
    manager = manager_for_test()

    async def leave():
        player = await leaving.request_leave(arena["id"], token)
        await manager.finish_leave(player)

    await asyncio.gather(*(leave() for _ in range(8)), crud.settle_entry(payment))
    state = await crud.public_state(arena["id"], token)
    assert state["leaveState"] == "left"
    assert state["player"]["livesRemaining"] == 0
    assert state["refund"] == {"estimate": 0, "pending": 47, "paid": 0, "failed": 0}
    assert state["won"] == state["pendingWinnings"] == 0
    rows = await crud.all_rows("SELECT * FROM quakejs.refund_payouts")
    assert len(rows) == 1 and rows[0]["victim_id"] == entry["id"]
    assert rows[0]["ln_address"] == entry["ln_address"]
    with pytest.raises(PublicError, match="leaving"):
        await crud.allocate_life(arena["id"], token, arena["run_id"])
    with pytest.raises(PublicError, match="left"):
        await crud.reserve_entry(
            arena["id"],
            token,
            EntryInput(lnAddress="evil@example.com", nonce=crud.uid()),
        )
    anonymous = await crud.public_state(arena["id"])
    assert "refund" not in anonymous and "leaveState" not in anonymous


@pytest.mark.anyio
async def test_refund_transaction_rolls_back_all_lives_and_outbox_on_failure(
    arena, monkeypatch
):
    token, _, _ = await paid(arena)
    player = await leaving.request_leave(arena["id"], token)
    original = crud.Tx.execute

    async def fail_after_queue(self, sql, **values):
        if "SET remaining=0" in sql:
            raise RuntimeError("simulated crash")
        return await original(self, sql, **values)

    with monkeypatch.context() as patch:
        patch.setattr(crud.Tx, "execute", fail_after_queue)
        with pytest.raises(RuntimeError, match="simulated"):
            await leaving.finish_leave(player["id"])
    assert not await crud.all_rows("SELECT * FROM quakejs.refund_payouts")
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 5
    await manager_for_test().recover_leaves()
    assert (await crud.public_state(arena["id"], token))["refund"]["pending"] == 47


@pytest.mark.anyio
async def test_late_invoice_settlement_is_refunded_and_never_reopens_session(arena):
    token = crud.uid()
    old = await crud.reserve_entry(
        arena["id"],
        token,
        EntryInput(lnAddress="original@example.com", nonce=crud.uid()),
    )
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.entries SET expires_at=0 WHERE id=:id", id=old["id"]
        )
    _, _, _ = await paid(arena, token)
    player = await leaving.request_leave(arena["id"], token)
    await manager_for_test().finish_leave(player)
    payment = SimpleNamespace(
        success=True,
        is_in=True,
        extra={"tag": "quakejs", "quakejs_entry": old["id"]},
        wallet_id="wallet",
        amount=50_000,
        payment_hash=crud.uid(),
        bolt11="late",
    )
    await asyncio.gather(*(crud.settle_entry(payment) for _ in range(5)))
    state = await crud.public_state(arena["id"], token)
    assert state["leaveState"] == "left" and state["player"]["livesRemaining"] == 0
    refunds = await crud.all_rows("SELECT * FROM quakejs.refund_payouts")
    assert len(refunds) == 2 and sum(r["amount"] for r in refunds) == 94
    assert {r["ln_address"] for r in refunds} == {
        "original@example.com",
        "winner@example.com",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("creator_fee", [0, 10, 45])
async def test_refund_uses_remaining_value_and_both_haircuts(arena, creator_fee):
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.arenas SET is_public=1,creator_haircut=:fee,"
            "creator_ln_address='creator@example.com',entry_amount=100 WHERE id=:id",
            fee=creator_fee,
            id=arena["id"],
        )
    token, entry, _ = await paid(arena)
    life = await crud.allocate_life(arena["id"], token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, life["id"], "")
    player = await leaving.request_leave(arena["id"], token)
    await leaving.finish_leave(player["id"])
    refund = (await crud.all_rows("SELECT * FROM quakejs.refund_payouts"))[0]
    assert refund["amount"] == 4 * 100 * (95 - creator_fee) // 500
    creators = await crud.all_rows("SELECT * FROM quakejs.creator_payouts")
    assert len(creators) == int(creator_fee > 0)
    if creator_fee:
        assert creators[0]["amount"] == 4 * 100 * creator_fee // 500
        assert creators[0]["victim_id"] == entry["id"]
    assert not await crud.all_rows("SELECT * FROM quakejs.payouts")


@pytest.mark.anyio
async def test_native_leave_revokes_without_forfeit_and_blocks_racing_respawn(
    arena, tmp_path
):
    token, _, _ = await paid(arena)
    other_token, _, _ = await paid(arena)
    async with engine(arena, tmp_path) as (manager, match):
        peer = connection(token)
        life = await match.attach(peer)
        player = await leaving.request_leave(arena["id"], token)
        results = await asyncio.gather(
            manager.finish_leave(player),
            match.attach(connection(token)),
            return_exceptions=True,
        )
        assert results[0] is None and isinstance(results[1], PublicError)
        await match.detach(peer)
        await match.control(bytes([7, life["slot"]]))
        await match.drain_journal()
        state = await crud.public_state(arena["id"], token)
        assert state["refund"]["pending"] == 47
        assert state["player"]["livesRemaining"] == 0
        assert not match.journal.read_text()
        assert (await match.attach(connection(other_token)))["slot"] == life["slot"]


@pytest.mark.anyio
async def test_native_disconnect_after_leave_request_does_not_spend_current_life(
    arena, tmp_path
):
    token, _, _ = await paid(arena)
    async with engine(arena, tmp_path) as (manager, match):
        peer = connection(token)
        await match.attach(peer)
        player = await leaving.request_leave(arena["id"], token)
        await asyncio.gather(match.detach(peer), manager.finish_leave(player))
        assert (await crud.public_state(arena["id"], token))["refund"]["pending"] == 47
        assert not match.journal.read_text()


@pytest.mark.anyio
async def test_kill_in_journal_before_leave_gets_paid_once_and_reduces_refund(
    arena, tmp_path
):
    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    async with engine(arena, tmp_path) as (manager, match):
        victim = await match.attach(connection(victim_token))
        killer = await match.attach(connection(killer_token))
        # A journaled frag is deliberately left unreplayed until the leave barrier.
        event = {"sequence": 1, "victim": victim["id"], "killer": killer["id"]}
        match.journal.write_text(json.dumps(event) + "\n")
        player = await leaving.request_leave(arena["id"], victim_token)
        await manager.finish_leave(player)
        await manager.finish_leave(player)
        state = await crud.public_state(arena["id"], victim_token)
        assert state["refund"]["pending"] == 38  # four lives, not five
        payouts = await crud.all_rows("SELECT * FROM quakejs.payouts")
        assert len(payouts) == 1 and payouts[0]["amount"] == 9
        assert not await crud.all_rows(
            "SELECT * FROM quakejs.lives WHERE player_id=:id AND status='alive'",
            id=player["id"],
        )


@pytest.mark.anyio
async def test_failed_engine_barrier_stops_engine_before_recovery_refunds(
    arena, tmp_path, monkeypatch
):
    token, _, _ = await paid(arena)
    async with engine(arena, tmp_path) as (manager, match):
        await match.attach(connection(token))
        player = await leaving.request_leave(arena["id"], token)

        async def broken(_):
            raise TimeoutError("missing ACK")

        monkeypatch.setattr(match, "control", broken)
        with pytest.raises(TimeoutError):
            await manager.finish_leave(player)
        assert match.process.returncode is not None
        assert not await crud.all_rows("SELECT * FROM quakejs.refund_payouts")
        await manager.recover_leaves()
        assert (await crud.public_state(arena["id"], token))["refund"]["pending"] == 47


@pytest.mark.anyio
async def test_unrecovered_live_run_blocks_refund_then_recovers_journal(
    arena, tmp_path
):
    token, _, _ = await paid(arena)
    life = await crud.allocate_life(arena["id"], token, arena["run_id"])
    player = await leaving.request_leave(arena["id"], token)
    manager = manager_for_test()
    manager.directory.mkdir(parents=True, exist_ok=True)
    with pytest.raises(PublicError, match="recovering"):
        await manager.finish_leave(player)
    assert not await crud.all_rows("SELECT * FROM quakejs.refund_payouts")
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.arenas SET lease_until=0 WHERE id=:id", id=arena["id"]
        )
    journal = manager.directory / (arena["run_id"] + ".jsonl")
    journal.write_text(
        json.dumps({"sequence": 1, "victim": life["id"], "killer": ""}) + "\n"
    )
    await manager.recover_leaves()
    assert (await crud.public_state(arena["id"], token))["refund"]["pending"] == 38


@pytest.mark.anyio
async def test_refund_ambiguous_payment_reconciles_without_resending(
    arena, monkeypatch
):
    token, _, _ = await paid(arena)
    player = await leaving.request_leave(arena["id"], token)
    await manager_for_test().finish_leave(player)
    row = await payments.claim_payout()
    assert row["_table"] == "refund_payouts"
    calls = []

    async def invoice(*args):
        return payout_invoice(amount=47_000)

    async def lookup(*args, **kwargs):
        return None

    async def send(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("uncertain")

    monkeypatch.setattr(payments, "get_pr_from_lnurl", invoice)
    monkeypatch.setattr(payments, "get_standalone_payment", lookup)
    monkeypatch.setattr(payments, "pay_invoice", send)
    await payments.process_payout(row)
    await payments.process_payout(row)
    assert len(calls) == 1 and row["status"] == "pending"
    assert calls[0]["extra"]["quakejs_kind"] == "refund"
    payment = SimpleNamespace(
        is_out=True,
        success=True,
        pending=False,
        failed=False,
        wallet_id="wallet",
        amount=-47_000,
        payment_hash=row["payment_hash"],
        extra=calls[0]["extra"],
    )

    async def settled(*args, **kwargs):
        return payment

    monkeypatch.setattr(payments, "get_standalone_payment", settled)
    await payments.process_payout(row)
    assert len(calls) == 1 and row["status"] == "paid"
    state = await crud.public_state(arena["id"], token)
    assert state["won"] == 0 and state["refund"]["paid"] == 47
    payment.extra = {**payment.extra, "quakejs_kind": "kill"}
    assert not payments.matches_payout(payment, row)


@pytest.mark.anyio
async def test_leave_migration_resumes_and_preserves_money(arena):
    token, _, _ = await paid(arena)
    await m012_leave_refunds(crud.db)
    await m012_leave_refunds(crud.db)
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 5


@pytest.mark.anyio
async def test_failed_refund_never_restores_spendable_lives(arena, monkeypatch):
    token, _, _ = await paid(arena)
    await manager_for_test().finish_leave(
        await leaving.request_leave(arena["id"], token)
    )
    row = await payments.claim_payout()
    row["attempts"] = 7

    async def broken(*args):
        raise ValueError("provider response with private data")

    monkeypatch.setattr(payments, "get_pr_from_lnurl", broken)
    await payments.process_payout(row)
    state = await crud.public_state(arena["id"], token)
    assert state["refund"]["failed"] == 47
    assert state["player"]["livesRemaining"] == 0 and state["leaveState"] == "left"
    stored = await crud.one(
        "SELECT * FROM quakejs.refund_payouts WHERE id=:id", id=row["id"]
    )
    assert stored["error"] == "Unable to obtain a valid payout invoice."
    assert not await payments.claim_payout()


@pytest.mark.anyio
async def test_refund_route_authentication_isolation_and_redacted_errors(
    arena, monkeypatch
):
    from fastapi import HTTPException, Request

    from lnbits.extensions.quakejs import views

    manager = manager_for_test()
    monkeypatch.setattr(views, "manager", manager)
    monkeypatch.setattr(views, "limits", {})
    token, _, _ = await paid(arena)
    request = Request({"type": "http", "client": ("127.0.0.1", 1234)})
    for auth, code in (
        (None, 401),
        ("Bearer invalid", 401),
        ("Bearer " + crud.uid(), 409),
    ):
        with pytest.raises(HTTPException) as error:
            await views.leave_game(request, arena["id"], auth)
        assert error.value.status_code == code
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 5

    async def failure(*args):
        raise RuntimeError("wallet-key-DO-NOT-EXPOSE")

    monkeypatch.setattr(manager, "finish_leave", failure)
    with pytest.raises(HTTPException) as error:
        await views.leave_game(request, arena["id"], "Bearer " + token)
    assert error.value.status_code == 503 and "DO-NOT-EXPOSE" not in error.value.detail
    assert (await crud.public_state(arena["id"], token))["leaveState"] == "leaving"
    await manager_for_test().recover_leaves()
    assert (await crud.public_state(arena["id"], token))["leaveState"] == "left"


@pytest.mark.anyio
async def test_last_life_can_die_while_leave_is_requested(arena):
    token, _, _ = await paid(arena)
    for sequence in range(1, 6):
        life = await crud.allocate_life(arena["id"], token, arena["run_id"])
        await crud.consume_death(arena["run_id"], sequence, life["id"], "")
    player = await leaving.request_leave(arena["id"], token)
    await leaving.finish_leave(player["id"])
    state = await crud.public_state(arena["id"], token)
    assert state["leaveState"] == "left" and state["refund"]["pending"] == 0
    assert not await crud.all_rows("SELECT * FROM quakejs.refund_payouts")


@pytest.mark.anyio
async def test_crash_after_retirement_never_spends_a_refunded_life(arena):
    token, _, payment = await paid(arena)
    life = await crud.allocate_life(arena["id"], token, arena["run_id"])
    player = await leaving.request_leave(arena["id"], token)
    with pytest.raises(PublicError, match="recovering"):
        await leaving.finish_leave(player["id"])
    # Model a completed engine revocation barrier, then a repeated/stale frag.
    await leaving.finish_leave(player["id"], revoked_run=arena["run_id"])
    with pytest.raises(ValueError, match="no funded entry"):
        await crud.consume_death(arena["run_id"], 1, life["id"], "")
    await crud.settle_entry(payment)
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 0
    assert not await crud.all_rows("SELECT * FROM quakejs.payouts")
