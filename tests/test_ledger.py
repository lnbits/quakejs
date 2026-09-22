"""Real SQLite transaction tests; all Lightning calls use test doubles."""

import asyncio
from types import SimpleNamespace

import pytest
from bolt11 import Bolt11, Tag, TagChar, Tags, encode

from lnbits.db import Database
from lnbits.extensions.quakejs import crud, payments
from lnbits.extensions.quakejs.migrations import (
    m007_native_arena_ledger,
    m009_public_lobbies,
    m010_server_capacity,
    m011_admin_arena_closure,
)
from lnbits.extensions.quakejs.models import ArenaInput, EntryInput
from lnbits.settings import settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def arena(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lnbits_data_folder", str(tmp_path))
    database = Database("ext_quakejs")
    monkeypatch.setattr(crud, "db", database)
    await m007_native_arena_ledger(database)
    await m009_public_lobbies(database)
    await m010_server_capacity(database)
    await m011_admin_arena_closure(database)
    await crud.save_settings("owner", "wallet", True, 5)
    row = await crud.create_arena("owner", ArenaInput(joinAmount=100))
    # Keep exercising existing 50-sat games after raising the creation minimum.
    row["entry_amount"] = 50
    run = crud.uid()
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.arenas SET entry_amount=50,run_id=:run,"
            "lease_until=:until WHERE id=:id",
            id=row["id"],
            run=run,
            until=crud.now() + 300,
        )
        await tx.execute(
            (
                "INSERT INTO quakejs.runs(id,arena_id,worker,created_at) "
                "VALUES(:run,:arena,'test',:now)"
            ),
            run=run,
            arena=row["id"],
            now=crud.now(),
        )
    row["run_id"] = run
    yield row
    await database.engine.dispose()


async def paid(arena, token=None):
    token = token or crud.uid()
    entry = await crud.reserve_entry(
        arena["id"], token, EntryInput(lnAddress="winner@example.com", nonce=crud.uid())
    )
    payment = SimpleNamespace(
        success=True,
        is_in=True,
        extra={"tag": "quakejs", "quakejs_entry": entry["id"]},
        wallet_id="wallet",
        amount=entry["amount"] * 1000,
        payment_hash=crud.uid(),
        bolt11="test-invoice",
    )
    await crud.settle_entry(payment)
    return token, entry, payment


def payout_invoice(amount=9000, expiry=300):
    return encode(
        Bolt11(
            currency="bc",
            date=crud.now(),
            amount_msat=amount,
            tags=Tags(
                [
                    Tag(TagChar.payment_hash, "01" * 32),
                    Tag(TagChar.payment_secret, "02" * 32),
                    Tag(TagChar.description, "QuakeJS test payout"),
                    Tag(TagChar.expire_time, expiry),
                ]
            ),
        ),
        private_key="03" * 32,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "entry_amount,fee,expected",
    [
        (50, 5, 9),
        (50, 0, 10),
        (100, 5, 19),
        (50, 100, 0),
        (54, 5, 10),
    ],
)
async def test_displayed_prize_matches_funded_frag(arena, entry_amount, fee, expected):
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.arenas SET entry_amount=:amount,haircut=:fee WHERE id=:id",
            amount=entry_amount,
            fee=fee,
            id=arena["id"],
        )
    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    state = await crud.public_state(arena["id"], killer_token)
    payout = await crud.one(
        "SELECT * FROM quakejs.payouts WHERE victim_id=:id", id=victim["id"]
    )
    assert state["game"]["joinAmount"] == entry_amount
    assert state["game"]["prizePerKill"] == payout["amount"] == expected


@pytest.mark.anyio
async def test_exact_payment_and_duplicate_events(arena):
    token, entry, payment = await paid(arena)
    life = await crud.allocate_life(arena["id"], token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, life["id"], "")
    await asyncio.gather(*(crud.settle_entry(payment) for _ in range(12)))
    row = await crud.one(
        "SELECT remaining FROM quakejs.entries WHERE id=:id", id=entry["id"]
    )
    assert row["remaining"] == 4
    payment.amount = 49_000
    with pytest.raises(ValueError, match="wallet and amount"):
        await crud.settle_entry(payment)
    payment.amount = 50_000
    payment.wallet_id = "other-wallet"
    with pytest.raises(ValueError, match="wallet and amount"):
        await crud.settle_entry(payment)


@pytest.mark.anyio
async def test_five_lives_payout_outage_does_not_block(arena):
    token, _entry, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    for sequence in range(1, 6):
        victim = await crud.allocate_life(arena["id"], token, arena["run_id"])
        await asyncio.gather(
            *(
                crud.consume_death(
                    arena["run_id"], sequence, victim["id"], killer["id"]
                )
                for _ in range(8)
            )
        )
        state = await crud.public_state(arena["id"], token)
        assert state["player"]["livesRemaining"] == 5 - sequence
    payouts = await crud.all_rows("SELECT * FROM quakejs.payouts")
    assert len(payouts) == 5
    assert sum(p["amount"] for p in payouts) == 45
    assert all(p["status"] == "queued" for p in payouts)
    with pytest.raises(ValueError, match="No lives"):
        await crud.allocate_life(arena["id"], token, arena["run_id"])
    await paid(arena, token)
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 5


@pytest.mark.anyio
async def test_unpaid_forgery_and_admission_capacity(arena):
    with pytest.raises(ValueError, match="paid entry"):
        await crud.allocate_life(arena["id"], crud.uid(), arena["run_id"])
    # Paid balances can outlive a disconnect; the live engine still has eight slots.
    players = [(await paid(arena))[0] for _ in range(9)]
    results = await asyncio.gather(
        *(crud.allocate_life(arena["id"], token, arena["run_id"]) for token in players),
        return_exceptions=True,
    )
    admitted = [r for r in results if isinstance(r, dict)]
    assert len(admitted) == 8
    assert {r["slot"] for r in admitted} == set(range(1, 9))
    assert sum(isinstance(r, ValueError) for r in results) == 1


@pytest.mark.anyio
async def test_expired_heartbeats_do_not_reassign_live_engine_slots(arena):
    players = [(await paid(arena))[0] for _ in range(9)]
    lives = [
        await crud.allocate_life(arena["id"], token, arena["run_id"])
        for token in players[:8]
    ]
    async with crud.transaction() as tx:
        await tx.execute("UPDATE quakejs.lives SET connected_until=0")
    with pytest.raises(ValueError, match="full"):
        await crud.allocate_life(arena["id"], players[8], arena["run_id"])
    with pytest.raises(ValueError, match="full"):
        await paid(arena)
    assert (await crud.allocate_life(arena["id"], players[0], arena["run_id"]))[
        "id"
    ] == lives[0]["id"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "foreign", [None, "wallet", "payout", "victim", "amount", "direction", "hash"]
)
async def test_payout_cannot_claim_an_unrelated_payment(arena, monkeypatch, foreign):
    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    row = await payments.claim_payout()
    await payments.update_payout(
        row, "prepared", payment_hash="payout-hash", bolt11="test"
    )
    record = SimpleNamespace(
        wallet_id=row["wallet_id"],
        payment_hash=row["payment_hash"],
        amount=-row["amount"] * 1000,
        is_out=True,
        pending=False,
        success=True,
        failed=False,
        extra={
            "tag": "quakejs",
            "quakejs_payout": row["id"],
            "quakejs_victim": row["victim_id"],
        },
    )
    if foreign in ("payout", "victim"):
        record.extra["quakejs_" + foreign] = "unrelated"
    elif foreign:
        field, value = {
            "wallet": ("wallet_id", "other-wallet"),
            "amount": ("amount", -1000),
            "direction": ("is_out", False),
            "hash": ("payment_hash", "other-hash"),
        }[foreign]
        setattr(record, field, value)

    async def unrelated(*args, **kwargs):
        return record

    monkeypatch.setattr(payments, "pay_invoice", unrelated)
    monkeypatch.setattr(payments, "get_standalone_payment", unrelated)
    await payments.process_payout(row)
    expected = "failed" if foreign else "paid"
    assert row["status"] == expected
    await payments.update_payout(row, "pending")
    await payments.reconcile_payout(row)
    assert row["status"] == expected
    assert (await crud.public_state(arena["id"], killer_token))["won"] == (
        0 if foreign else row["amount"]
    )


@pytest.mark.anyio
async def test_reused_provider_invoice_is_not_sent(arena, monkeypatch):
    from unittest.mock import AsyncMock

    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    row = await payments.claim_payout()
    monkeypatch.setattr(
        payments, "get_pr_from_lnurl", AsyncMock(return_value=payout_invoice())
    )
    monkeypatch.setattr(
        payments,
        "get_standalone_payment",
        AsyncMock(return_value=SimpleNamespace(is_in=False)),
    )
    send = AsyncMock()
    monkeypatch.setattr(payments, "pay_invoice", send)
    await payments.process_payout(row)
    send.assert_not_awaited()
    assert row["status"] == "queued"
    assert row["payment_hash"] is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reason", ["minimum", "bad_response", "http", "private", "provider_error"]
)
async def test_lnurl_failure_details_preserve_unsent_payout(arena, monkeypatch, reason):
    from unittest.mock import AsyncMock

    from lnurl import LnurlResponseException

    from lnbits.core.services import lnurl

    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    row = await payments.claim_payout()
    callbacks = []

    async def provider(url, **kwargs):
        if reason == "http":
            raise LnurlResponseException("LNURL request failed.")
        if reason == "private":
            raise LnurlResponseException(
                "LNURL request target resolves to a private or non-global IP address."
            )
        if reason == "provider_error":
            return {
                "status": "ERROR",
                "reason": "private-key=secret https://provider.invalid/?token=secret",
            }
        if kwargs.get("params"):
            callbacks.append(url)
            return {"pr": "not-a-valid-invoice", "routes": []}
        return {
            "tag": "payRequest",
            "callback": "https://example.com/callback",
            "minSendable": 10000 if reason == "minimum" else 1000,
            "maxSendable": 1000000,
            "metadata": '[["text/plain","QuakeJS test payout"]]',
        }

    monkeypatch.setattr(lnurl, "_request_lnurl_json", provider)
    send = AsyncMock()
    monkeypatch.setattr(payments, "pay_invoice", send)
    await payments.process_payout(row)
    assert row["status"] == "queued"
    assert row["attempts"] == 1 and row["next_attempt"] > crud.now()
    assert row["payment_hash"] is None and row["bolt11"] is None
    assert "secret" not in row["error"] and "https://" not in row["error"]
    send.assert_not_awaited()
    if reason == "minimum":
        assert (
            row["error"]
            == "Provider amount limit: requested 9000 msat; allowed 10000-1000000 msat."
        )
        assert not callbacks
    elif reason == "bad_response":
        assert row["error"] == "Invalid LNURL response."
    elif reason == "http":
        assert row["error"] == "LNURL request failed."
    elif reason == "private":
        assert "blocked" in row["error"] or "private" in row["error"]
    else:
        assert row["error"] == "Lightning address provider rejected the LNURL request."
    state = await crud.public_state(arena["id"], killer_token)
    assert state["pendingWinnings"] == 9 and state["won"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("existing_incoming", [False, True])
@pytest.mark.parametrize(
    "amount,expiry,valid", [(9000, 300, True), (9000, 1, False), (8000, 300, False)]
)
async def test_real_lnurl_invoice_through_payout_and_balance(
    arena, monkeypatch, existing_incoming, amount, expiry, valid
):
    from unittest.mock import AsyncMock

    from lnbits.core.models import Payment
    from lnbits.core.services import lnurl

    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    state = await crud.public_state(arena["id"], killer_token)
    assert state["pendingWinnings"] == 9 and state["won"] == 0
    row = await payments.claim_payout()
    invoice = payout_invoice(amount, expiry)

    async def provider(url, **kwargs):
        if kwargs.get("params"):
            assert kwargs["params"]["amount"] == 9000
            return {"pr": invoice, "routes": []}
        return {
            "tag": "payRequest",
            "callback": "https://example.com/callback",
            "minSendable": 1000,
            "maxSendable": 1000000,
            "metadata": '[["text/plain","QuakeJS test payout"]]',
        }

    monkeypatch.setattr(lnurl, "_request_lnurl_json", provider)
    monkeypatch.setattr(
        payments,
        "get_standalone_payment",
        AsyncMock(
            return_value=(
                SimpleNamespace(is_in=True, pending=True) if existing_incoming else None
            )
        ),
    )

    async def send(**kwargs):
        return Payment(
            checking_id="outgoing-test",
            payment_hash="01" * 32,
            wallet_id=kwargs["wallet_id"],
            amount=-9000,
            fee=0,
            bolt11=invoice,
            status="success",
            extra=kwargs["extra"],
        )

    send_mock = AsyncMock(side_effect=send)
    monkeypatch.setattr(payments, "pay_invoice", send_mock)
    await payments.process_payout(row)
    state = await crud.public_state(arena["id"], killer_token)
    assert row["status"] == ("paid" if valid else "queued")
    assert state["won"] == (9 if valid else 0)
    assert state["pendingWinnings"] == (0 if valid else 9)
    assert send_mock.await_count == int(valid)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,has_invoice,retry",
    [
        ("queued", False, True),
        ("failed", False, True),
        ("pending", False, False),
        ("sending", False, False),
        ("failed", True, False),
    ],
)
async def test_recover_only_unsent_invoice_failures(arena, status, has_invoice, retry):
    from lnbits.extensions.quakejs.migrations import m008_retry_unprepared_payouts

    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    row = await payments.claim_payout()
    await payments.update_payout(
        row,
        status,
        attempts=8,
        next_attempt=crud.now() + 3600,
        bolt11="stored-invoice" if has_invoice else None,
        payment_hash="stored-hash" if has_invoice else None,
        error="Unable to obtain a valid payout invoice.",
    )
    await m008_retry_unprepared_payouts(crud.db)
    updated = await crud.one("SELECT * FROM quakejs.payouts WHERE id=:id", id=row["id"])
    assert updated["attempts"] == (0 if retry else 8)
    assert updated["status"] == ("queued" if retry else status)
    assert updated["payment_hash"] == row["payment_hash"]


@pytest.mark.anyio
async def test_old_run_cleanup_preserves_replacement_lease(arena, tmp_path):
    from lnbits.extensions.quakejs.server import Manager, Match

    manager = SimpleNamespace(directory=tmp_path, worker="test-worker")
    old = Match(manager, arena, arena["run_id"])
    old.journal.touch()
    replacement = crud.uid()
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.arenas SET worker=:worker,run_id=:run,lease_until=:until "
            "WHERE id=:id",
            worker=manager.worker,
            run=replacement,
            until=crud.now() + 45,
            id=arena["id"],
        )
    with pytest.raises(ValueError, match="lease ended"):
        await Manager.renew(manager, old, arena["id"])
    await old.stop()
    current = await crud.one(
        "SELECT * FROM quakejs.arenas WHERE id=:id", id=arena["id"]
    )
    assert current["run_id"] == replacement
    assert current["lease_until"] > crud.now()


@pytest.mark.anyio
async def test_only_one_invoice_is_created_for_concurrent_clicks(arena, monkeypatch):
    calls = []

    async def invoice(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.02)
        return SimpleNamespace(
            success=False, is_in=True, payment_hash="test-hash", bolt11="test-invoice"
        )

    monkeypatch.setattr(payments, "create_invoice", invoice)
    token = crud.uid()
    data = EntryInput(lnAddress="player@example.com", nonce=crud.uid())
    await asyncio.gather(
        *(payments.create_entry(arena["id"], token, data) for _ in range(10)),
        return_exceptions=True,
    )
    assert len(calls) == 1
    assert len(await crud.all_rows("SELECT * FROM quakejs.entries")) == 1


@pytest.mark.anyio
async def test_journal_rejects_unknown_life_run_and_gaps(arena):
    token, _, _ = await paid(arena)
    life = await crud.allocate_life(arena["id"], token, arena["run_id"])
    with pytest.raises(ValueError, match="gap"):
        await crud.consume_death(arena["run_id"], 2, life["id"], "")
    with pytest.raises(ValueError, match="does not belong"):
        await crud.consume_death(arena["run_id"], 1, crud.uid(), "")
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 5


@pytest.mark.anyio
async def test_uncertain_payout_is_never_sent_again(arena, monkeypatch):
    token, _, _ = await paid(arena)
    other, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], other, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    row = await payments.claim_payout()
    await payments.update_payout(
        row, "prepared", bolt11="test-invoice", payment_hash="outgoing-hash"
    )
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)
        raise TimeoutError("Outcome unknown")

    async def lookup(*args, **kwargs):
        return None

    monkeypatch.setattr(payments, "pay_invoice", send)
    monkeypatch.setattr(payments, "get_standalone_payment", lookup)
    await payments.process_payout(row)
    assert row["status"] == "pending"
    await payments.process_payout(row)
    await payments.process_payout(row)
    assert len(sent) == 1
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 4


@pytest.mark.anyio
async def test_transaction_rollback_and_competing_claims(arena):
    token, _, _ = await paid(arena)
    other, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], other, arena["run_id"])
    with pytest.raises(RuntimeError):
        async with crud.transaction() as tx:
            await tx.execute("UPDATE quakejs.entries SET remaining=0")
            raise RuntimeError("Simulated crash")
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 5
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    claims = await asyncio.gather(*(payments.claim_payout() for _ in range(10)))
    assert sum(row is not None for row in claims) == 1


@pytest.mark.anyio
async def test_database_lock_protects_independent_connections(arena):
    """Separate Database objects have separate asyncio locks, like separate workers."""
    second = Database("ext_quakejs")
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with crud.transaction() as tx:
            await tx.execute(
                "UPDATE quakejs.settings_native SET haircut=haircut+1 WHERE id='owner'"
            )
            first_entered.set()
            await asyncio.sleep(0.15)
            assert not second_entered.is_set()

    async def competitor():
        await first_entered.wait()
        async with crud.transaction(second) as tx:
            second_entered.set()
            row = await tx.one(
                "SELECT haircut FROM quakejs.settings_native WHERE id='owner'"
            )
            assert row["haircut"] == 6
            await tx.execute(
                "UPDATE quakejs.settings_native SET haircut=haircut+1 WHERE id='owner'"
            )

    await asyncio.gather(first(), competitor())
    assert (await crud.settings_for("owner"))["haircut"] == 7
    await second.engine.dispose()


@pytest.mark.anyio
async def test_fsynced_journal_recovery_is_idempotent(arena, tmp_path):
    import json

    from lnbits.extensions.quakejs.server import Match

    token, _, _ = await paid(arena)
    other, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], other, arena["run_id"])
    journal = tmp_path / (arena["run_id"] + ".jsonl")
    journal.write_text(
        json.dumps({"sequence": 1, "victim": victim["id"], "killer": killer["id"]})
        + "\n"
    )
    for _ in range(3):
        # Each fresh reader models a process restarting before saving its offset.
        match = Match(SimpleNamespace(directory=tmp_path), arena, arena["run_id"])
        assert await match.replay()
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 4
    assert len(await crud.all_rows("SELECT * FROM quakejs.payouts")) == 1
    journal.write_text('{"sequence":2')
    with pytest.raises(RuntimeError, match="Incomplete game journal"):
        await Match(
            SimpleNamespace(directory=tmp_path), arena, arena["run_id"]
        ).replay()


@pytest.mark.anyio
async def test_pending_invoices_reserve_capacity(arena):
    for _ in range(8):
        await crud.reserve_entry(
            arena["id"],
            crud.uid(),
            EntryInput(lnAddress="player@example.com", nonce=crud.uid()),
        )
    with pytest.raises(ValueError, match="full"):
        await crud.reserve_entry(
            arena["id"],
            crud.uid(),
            EntryInput(lnAddress="player@example.com", nonce=crud.uid()),
        )


@pytest.mark.anyio
@pytest.mark.parametrize("recovery", ["stop", "restart"])
async def test_long_journal_recovers_unsettled_tail(
    arena, tmp_path, monkeypatch, recovery
):
    import json

    from lnbits.extensions.quakejs.server import Manager, Match

    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    # Model a long, already settled prefix plus one fsynced but unsettled death.
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.runs SET event_sequence=512 WHERE id=:id",
            id=arena["run_id"],
        )
        if recovery == "restart":
            await tx.execute(
                "UPDATE quakejs.arenas SET lease_until=0 WHERE id=:id", id=arena["id"]
            )
    journal = tmp_path / (arena["run_id"] + ".jsonl")
    events = [
        {"sequence": sequence, "victim": crud.uid(), "killer": ""}
        for sequence in range(1, 513)
    ]
    events.append({"sequence": 513, "victim": victim["id"], "killer": killer["id"]})
    journal.write_text("".join(json.dumps(event) + "\n" for event in events))
    manager = Manager()
    manager.directory = tmp_path
    manager.running = True

    async def fake_start(self):
        self.journal.touch()

    if recovery == "restart":
        monkeypatch.setattr(Match, "start", fake_start)
        match = await manager.ensure(arena["id"])
        await match.stop()
    else:
        match = Match(manager, arena, arena["run_id"])
        await match.stop()
        await match.stop()
    run = await crud.one("SELECT * FROM quakejs.runs WHERE id=:id", id=arena["run_id"])
    assert run["event_sequence"] == 513
    assert run["status"] == ("recovered" if recovery == "restart" else "stopped")
    assert (await crud.public_state(arena["id"], victim_token))["player"][
        "livesRemaining"
    ] == 4
    payouts = await crud.all_rows("SELECT * FROM quakejs.payouts")
    assert len(payouts) == 1 and payouts[0]["amount"] == 9


@pytest.mark.anyio
async def test_a_stale_payout_worker_cannot_send(arena, monkeypatch):
    token, _, _ = await paid(arena)
    other, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], other, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    row = await payments.claim_payout()
    await payments.update_payout(
        row, "prepared", bolt11="fixture", payment_hash="fixture-hash"
    )
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.payouts SET claim='new-worker' WHERE id=:id", id=row["id"]
        )

    async def forbidden(**kwargs):
        pytest.fail("Stale worker attempted a payment")

    monkeypatch.setattr(payments, "pay_invoice", forbidden)
    with pytest.raises(RuntimeError, match="lease was lost"):
        await payments.process_payout(row)


@pytest.mark.anyio
async def test_expired_invoice_can_be_replaced_and_late_payment_is_preserved(arena):
    token = crud.uid()
    old = await crud.reserve_entry(
        arena["id"], token, EntryInput(lnAddress="player@example.com", nonce=crud.uid())
    )
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.entries SET status='pending',expires_at=0,"
            "payment_hash='old-hash',bolt11='old-invoice' WHERE id=:id",
            id=old["id"],
        )
    state = await crud.public_state(arena["id"], token)
    assert state["invoiceExpired"] and "invoice" not in state
    await crud.reserve_entry(
        arena["id"], token, EntryInput(lnAddress="player@example.com", nonce=crud.uid())
    )
    payment = SimpleNamespace(
        success=True,
        is_in=True,
        extra={"tag": "quakejs", "quakejs_entry": old["id"]},
        wallet_id="wallet",
        amount=50_000,
        payment_hash="old-hash",
        bolt11="old-invoice",
    )
    await crud.settle_entry(payment)
    assert (await crud.public_state(arena["id"], token))["player"][
        "livesRemaining"
    ] == 5


@pytest.mark.anyio
async def test_native_eighth_peer_and_capacity(arena, tmp_path):
    """The shipped engine accepts slot eight and can reuse it after departure."""
    from lnbits.extensions.quakejs.server import Manager, Match

    players = [(await paid(arena))[0] for _ in range(9)]
    packets = asyncio.Queue()
    connections = [
        SimpleNamespace(token=token, life=None, match=None, offer=packets.put_nowait)
        for token in players
    ]
    manager = Manager()
    manager.directory.mkdir(parents=True, exist_ok=True)
    manager.worker_lock = (tmp_path / "test-worker.lock").open("a+b")
    match = Match(manager, arena, arena["run_id"])
    try:
        await match.start()
        for slot, connection in enumerate(connections[:8], 1):
            assert (await match.attach(connection))["slot"] == slot
        with pytest.raises(ValueError, match="full"):
            await match.attach(connections[8])
        await match.send(bytes([2, 8]) + b"\xff\xff\xff\xffgetinfo capacity")
        response = await asyncio.wait_for(packets.get(), 3)
        assert b"infoResponse" in response
        assert b"\\sv_maxclients\\8" in response
        await match.detach(connections[7])
        state = await crud.public_state(arena["id"], players[7])
        assert state["game"]["maxPlayers"] == 8
        assert state["player"]["livesRemaining"] == 4
        assert not await crud.all_rows("SELECT * FROM quakejs.payouts")
        assert (await match.attach(connections[8]))["slot"] == 8
    finally:
        await match.stop()
        manager.worker_lock.close()


@pytest.mark.anyio
async def test_native_disconnect_journal_and_server_restart(arena, tmp_path):
    """Run the shipped confined engine; use only synthetic funded entries."""
    from lnbits.extensions.quakejs.server import Manager, Match

    manager = Manager()
    manager.directory.mkdir(parents=True, exist_ok=True)
    manager.worker_lock = (tmp_path / "test-worker.lock").open("a+b")
    match = Match(manager, arena, arena["run_id"])
    token, _, _ = await paid(arena)
    connection = SimpleNamespace(
        token=token, life=None, match=None, offer=lambda _: None
    )
    try:
        await match.start()
        first = await match.attach(connection)
        duplicate = SimpleNamespace(token=token, life=None, match=None)
        with pytest.raises(ValueError, match="another tab"):
            await match.attach(duplicate)
        await match.detach(duplicate)
        await match.detach(connection)
        await match.detach(connection)
        # Repeating a private forfeiture cannot consume an already ended life.
        await match.control(bytes([7, first["slot"]]))
        await match.replay()
        state = await crud.public_state(arena["id"], token)
        assert state["player"]["livesRemaining"] == 4
        assert not await crud.all_rows("SELECT * FROM quakejs.payouts")
        second = await match.attach(connection)
        assert first["id"] != second["id"]
        await match.stop()
        state = await crud.public_state(arena["id"], token)
        assert state["player"]["livesRemaining"] == 4
        assert state["player"]["status"] == "left"
        assert len(match.journal.read_text().splitlines()) == 1
    finally:
        if not match.closed:
            await match.stop()
        manager.worker_lock.close()


@pytest.mark.anyio
async def test_late_payment_reopens_closed_arena_for_funded_lives(arena):
    token = crud.uid()
    entry = await crud.reserve_entry(
        arena["id"], token, EntryInput(lnAddress="player@example.com", nonce=crud.uid())
    )
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.arenas SET active=0 WHERE id=:id", id=arena["id"]
        )
    payment = SimpleNamespace(
        success=True,
        is_in=True,
        extra={"tag": "quakejs", "quakejs_entry": entry["id"]},
        wallet_id="wallet",
        amount=50_000,
        payment_hash=crud.uid(),
        bolt11="test-invoice",
    )
    await crud.settle_entry(payment)
    state = await crud.public_state(arena["id"], token)
    assert state["game"]["status"] == "active"
    assert state["player"]["livesRemaining"] == 5


@pytest.mark.anyio
async def test_bad_journal_does_not_stop_other_matches(arena, monkeypatch):
    from lnbits.extensions.quakejs.server import Manager

    manager = Manager()
    visited = asyncio.Event()

    async def corrupt():
        raise RuntimeError("incomplete journal")

    async def healthy():
        visited.set()
        return False

    async def noop(*args):
        return None

    manager.matches = {
        "bad": SimpleNamespace(
            lock=asyncio.Lock(), replay=corrupt, stop=corrupt, peers={}
        ),
        "good": SimpleNamespace(
            lock=asyncio.Lock(),
            replay=healthy,
            stop=noop,
            peers={},
            closed=False,
            last_used=crud.now(),
        ),
    }
    monkeypatch.setattr(manager, "renew", noop)
    task = asyncio.create_task(manager.loop())
    try:
        await asyncio.wait_for(visited.wait(), 2)
        assert manager.running
        assert "good" in manager.matches
        assert "bad" not in manager.matches
        with pytest.raises(ValueError, match="owner review"):
            await manager.ensure("bad")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["rejected", "timeout"])
async def test_bad_address_exhausts_retries_without_blocking_respawn(
    arena, monkeypatch, failure
):
    from unittest.mock import AsyncMock

    from lnurl import LnurlResponseException

    victim_token, _, _ = await paid(arena)
    killer_token, _, _ = await paid(arena)
    victim = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    killer = await crud.allocate_life(arena["id"], killer_token, arena["run_id"])
    await crud.consume_death(arena["run_id"], 1, victim["id"], killer["id"])
    row = await payments.claim_payout()

    async def provider(*args):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise LnurlResponseException("private provider error with token=secret")

    monkeypatch.setattr(payments, "PAYOUT_INVOICE_TIMEOUT", 0.01)
    monkeypatch.setattr(payments, "get_pr_from_lnurl", provider)
    send = AsyncMock()
    monkeypatch.setattr(payments, "pay_invoice", send)
    for _ in range(8):
        await payments.process_payout(row)
    assert row["status"] == "failed"
    assert row["attempts"] == 8
    assert "secret" not in row["error"]
    if failure == "timeout":
        assert row["error"] == "Lightning address provider timed out."
    send.assert_not_awaited()
    assert (await crud.public_state(arena["id"], killer_token))["won"] == 0
    respawn = await crud.allocate_life(arena["id"], victim_token, arena["run_id"])
    assert respawn["status"] == "alive"
    assert (await crud.public_state(arena["id"], victim_token))["player"][
        "livesRemaining"
    ] == 4


@pytest.mark.anyio
async def test_unexpected_payout_failure_yields_to_other_recipients(arena, monkeypatch):
    tokens = [(await paid(arena))[0] for _ in range(3)]
    lives = [
        await crud.allocate_life(arena["id"], token, arena["run_id"])
        for token in tokens
    ]
    await crud.consume_death(arena["run_id"], 1, lives[0]["id"], lives[2]["id"])
    await crud.consume_death(arena["run_id"], 2, lives[1]["id"], lives[2]["id"])
    calls, completed = [], asyncio.Event()

    async def process(row):
        calls.append(row["id"])
        if len(calls) == 1:
            raise TimeoutError("fixture: provider operation exceeded worker deadline")
        await payments.update_payout(row, "paid")

    async def notify(_):
        completed.set()

    monkeypatch.setattr(payments, "process_payout", process)
    task = asyncio.create_task(payments.payout_loop(notify))
    try:
        await asyncio.wait_for(completed.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(calls) == 2 and calls[0] != calls[1]
    failed = await crud.one("SELECT * FROM quakejs.payouts WHERE id=:id", id=calls[0])
    assert failed["status"] == "queued"  # No transfer was attempted.
    assert failed["claimed_until"] == 0
    assert failed["next_attempt"] > crud.now()
    # If another retry and a fresh payout are due together, the fresh work wins.
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.payouts SET next_attempt=1 WHERE id=:id", id=calls[0]
        )
        await tx.execute(
            "UPDATE quakejs.payouts SET status='queued',next_attempt=0,"
            "claimed_until=0 WHERE id=:id",
            id=calls[1],
        )
    assert (await payments.claim_payout())["id"] == calls[1]
