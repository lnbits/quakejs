import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lnbits.extensions.quakejs import crud, lobby, payments, views
from lnbits.extensions.quakejs.models import EntryInput, PublicArenaInput, SettingsInput
from lnbits.extensions.quakejs.tests import test_ledger
from lnbits.extensions.quakejs.tests.test_ledger import paid, payout_invoice

arena = test_ledger.arena


def game_input(**values):
    return PublicArenaInput(
        **{
            "joinAmount": 100,
            "creatorHaircut": 10,
            "lnAddress": "creator@example.com",
            "nonce": crud.uid(),
            **values,
        }
    )


async def public_arena(owner="owner", **values):
    setting = await crud.save_settings(owner, "wallet", True, 5, True)
    game = await lobby.create_game(setting["public_id"], game_input(**values))
    run = crud.uid()
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.arenas SET run_id=:run,lease_until=:until WHERE id=:id",
            run=run,
            until=crud.now() + 60,
            id=game["id"],
        )
        await tx.execute(
            "INSERT INTO quakejs.runs(id,arena_id,worker,created_at) "
            "VALUES(:run,:arena,'test',:now)",
            run=run,
            arena=game["id"],
            now=crud.now(),
        )
    return setting, await crud.one(
        "SELECT * FROM quakejs.arenas WHERE id=:id", id=game["id"]
    )


@pytest.mark.anyio
async def test_public_creation_is_opt_in_idempotent_and_wallet_bound(arena):
    setting = await crud.settings_for("owner")
    with pytest.raises(ValueError, match="accepting"):
        await lobby.create_game(setting["public_id"], game_input())
    setting = await crud.save_settings("owner", "wallet", True, 5, True)
    data = game_input()
    results = await asyncio.gather(
        *(lobby.create_game(setting["public_id"], data) for _ in range(8))
    )
    assert len({row["id"] for row in results}) == 1
    row = await crud.one(
        "SELECT * FROM quakejs.arenas WHERE id=:id", id=results[0]["id"]
    )
    assert row["wallet_id"] == "wallet" and row["haircut"] == 5
    assert row["creator_haircut"] == 10
    with pytest.raises(ValidationError):
        game_input(walletId="stolen", haircut=0)
    with pytest.raises(ValueError, match="50%"):
        await lobby.create_game(setting["public_id"], game_input(creatorHaircut=46))
    with pytest.raises(ValueError, match="Lightning address"):
        await lobby.create_game(setting["public_id"], game_input(lnAddress=""))
    await crud.save_settings("owner", "wallet", True, 5, False)
    with pytest.raises(ValueError, match="unavailable"):
        await lobby.snapshot(setting["public_id"])


@pytest.mark.anyio
async def test_owner_closure_preserves_ledger_and_cannot_be_undone_by_payment(arena):
    setting, game = await public_arena()
    victim_token, _, _ = await paid(game)
    killer_token, _, _ = await paid(game)
    victim = await crud.allocate_life(game["id"], victim_token, game["run_id"])
    killer = await crud.allocate_life(game["id"], killer_token, game["run_id"])
    await crud.consume_death(game["run_id"], 1, victim["id"], killer["id"])
    entry = await crud.reserve_entry(
        game["id"],
        crud.uid(),
        EntryInput(lnAddress="late@example.com", nonce=crud.uid()),
    )
    outbox = await crud.all_rows("SELECT * FROM quakejs.payouts")
    creator_outbox = await crud.all_rows("SELECT * FROM quakejs.creator_payouts")
    assert outbox and creator_outbox
    with pytest.raises(views.HTTPException) as error:
        await views.close_game(
            game["id"], SimpleNamespace(wallet=SimpleNamespace(user="other-owner"))
        )
    assert error.value.status_code == 404
    assert (await crud.public_state(game["id"]))["game"]["status"] == "active"
    key = SimpleNamespace(wallet=SimpleNamespace(user="owner"))
    assert await views.close_game(game["id"], key) == {"success": True}
    assert await views.close_game(game["id"], key) == {"success": True}
    listed = await views.list_games(page=1, rows_per_page=1, key=key)
    assert listed["total"] == 1  # The original fixture arena remains active.
    assert [row["id"] for row in listed["games"]] == [arena["id"]]
    archived = await views.list_games(rows_per_page=10, include_closed=True, key=key)
    assert archived["total"] == 2
    assert game["id"] in {row["id"] for row in archived["games"]}
    other = await views.list_games(
        rows_per_page=10,
        include_closed=True,
        key=SimpleNamespace(wallet=SimpleNamespace(user="other-owner")),
    )
    assert other == {"games": [], "total": 0}
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
    await crud.settle_entry(payment)
    state = await crud.public_state(game["id"], victim_token)
    assert state["game"]["status"] == "closed" and not state["canJoin"]
    assert state["player"]["livesRemaining"] == 4
    assert game["id"] not in {
        row["id"] for row in (await lobby.snapshot(setting["public_id"]))["games"]
    }
    recorded = await crud.one(
        "SELECT * FROM quakejs.entries WHERE id=:id", id=entry["id"]
    )
    assert recorded["status"] == "paid" and recorded["remaining"] == 5
    assert await crud.all_rows("SELECT * FROM quakejs.payouts") == outbox
    assert (
        await crud.all_rows("SELECT * FROM quakejs.creator_payouts") == creator_outbox
    )
    assert await payments.claim_payout()  # Closure does not block earned payouts.
    with pytest.raises(ValueError, match="unavailable"):
        await crud.allocate_life(game["id"], victim_token, game["run_id"])
    with pytest.raises(ValueError, match="unavailable"):
        await crud.reserve_entry(
            game["id"],
            crud.uid(),
            EntryInput(lnAddress="new@example.com", nonce=crud.uid()),
        )
    from lnbits.extensions.quakejs.server import Manager

    manager = Manager()
    with pytest.raises(ValueError, match="lease ended"):
        await manager.renew(SimpleNamespace(last_lease=0), game["id"])


@pytest.mark.anyio
async def test_one_frag_creates_two_bounded_transfers_and_preserves_respawn(
    arena, monkeypatch
):
    setting, game = await public_arena()
    victim_token, _, _ = await paid(game)
    killer_token, _, _ = await paid(game)
    victim = await crud.allocate_life(game["id"], victim_token, game["run_id"])
    killer = await crud.allocate_life(game["id"], killer_token, game["run_id"])
    await asyncio.gather(
        *(
            crud.consume_death(game["run_id"], 1, victim["id"], killer["id"])
            for _ in range(4)
        )
    )
    winner = (await crud.all_rows("SELECT * FROM quakejs.payouts"))[0]
    creator = (await crud.all_rows("SELECT * FROM quakejs.creator_payouts"))[0]
    assert winner["amount"] == 17 and creator["amount"] == 2
    assert winner["ln_address"] == "winner@example.com"
    assert creator["ln_address"] == "creator@example.com"
    assert (await crud.public_state(game["id"], victim_token))["player"][
        "livesRemaining"
    ] == 4
    await crud.allocate_life(game["id"], victim_token, game["run_id"])
    claims = [await payments.claim_payout(), await payments.claim_payout()]
    creator = next(row for row in claims if row["_table"] == "creator_payouts")
    winner = next(row for row in claims if row["_table"] == "payouts")
    calls = []

    async def invoice(*args):
        return payout_invoice(2000)

    async def missing(*args, **kwargs):
        return None

    async def send(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            is_out=True,
            success=True,
            amount=-2000,
            wallet_id=creator["wallet_id"],
            payment_hash=creator["payment_hash"],
            extra=kwargs["extra"],
        )

    monkeypatch.setattr(payments, "get_pr_from_lnurl", invoice)
    monkeypatch.setattr(payments, "get_standalone_payment", missing)
    monkeypatch.setattr(payments, "pay_invoice", send)
    await payments.process_payout(creator)
    await payments.process_payout(creator)
    assert len(calls) == 1 and creator["status"] == "paid"
    assert calls[0]["extra"]["quakejs_kind"] == "creator"
    assert not (await lobby.snapshot(setting["public_id"]))["scoreboard"]
    # The two outboxes cannot reserve the same provider invoice.
    with pytest.raises(ValueError, match="already used"):
        await payments.update_payout(
            winner,
            "prepared",
            payment_hash=creator["payment_hash"],
            bolt11=creator["bolt11"],
        )
    async with crud.transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.payouts SET status='paid' WHERE id=:id", id=winner["id"]
        )
    board = (await lobby.snapshot(setting["public_id"]))["scoreboard"]
    assert board == [{"address": "winner@example.com", "sats": 17, "kills": 1}]


@pytest.mark.anyio
async def test_empty_game_expiration_preserves_lives_invoices_and_admin_games(
    arena, monkeypatch
):
    setting, game = await public_arena(creatorHaircut=0, lnAddress="")
    clock = crud.now()
    monkeypatch.setattr(crud, "now", lambda: clock)
    clock += 599
    assert not await lobby.expire_game(game["id"])
    assert game["id"] in {
        row["id"] for row in (await lobby.snapshot(setting["public_id"]))["games"]
    }
    clock += 1
    # Admin games never auto-expire.
    assert not await lobby.expire_game(arena["id"])
    assert await lobby.expire_game(game["id"])
    assert (await crud.public_state(game["id"]))["game"]["status"] == "closed"
    assert game["id"] not in {
        row["id"] for row in (await lobby.snapshot(setting["public_id"]))["games"]
    }
    _, funded = await public_arena()
    token, _, _ = await paid(funded)
    clock += 601
    assert not await lobby.expire_game(funded["id"])
    assert (await crud.public_state(funded["id"], token))["player"][
        "livesRemaining"
    ] == 5
    assert funded["id"] in {
        row["id"] for row in (await lobby.snapshot(setting["public_id"]))["games"]
    }
    _, pending = await public_arena()
    clock += 601
    await crud.reserve_entry(
        pending["id"],
        crud.uid(),
        EntryInput(lnAddress="paid@example.com", nonce=crud.uid()),
    )
    assert not await lobby.expire_game(pending["id"])
    clock += 301
    assert await lobby.expire_game(pending["id"])


@pytest.mark.anyio
async def test_maintenance_removes_expired_public_game_and_notifies_lobby(
    arena, monkeypatch
):
    setting, game = await public_arena(creatorHaircut=0, lnAddress="")
    clock = crud.now() + 600
    monkeypatch.setattr(crud, "now", lambda: clock)
    refreshed = asyncio.Event()
    monkeypatch.setattr(lobby, "listeners", {"owner": {refreshed}})
    task = asyncio.create_task(lobby.maintenance())
    try:
        await asyncio.wait_for(refreshed.wait(), 2)
        result = await lobby.snapshot(setting["public_id"])
        assert game["id"] not in {row["id"] for row in result["games"]}
        assert arena["id"] in {row["id"] for row in result["games"]}
        recorded = await crud.one(
            "SELECT * FROM quakejs.arenas WHERE id=:id", id=game["id"]
        )
        assert recorded["active"] == 0 and recorded["lobby_hidden"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_playing_game_resets_idle_clock_and_world_deaths_have_no_creator_fee(
    arena, monkeypatch
):
    _, game = await public_arena()
    token, _, _ = await paid(game)
    life = await crud.allocate_life(game["id"], token, game["run_id"])
    assert not await lobby.expire_game(game["id"])
    await crud.consume_death(game["run_id"], 1, life["id"], "")
    assert not await crud.all_rows("SELECT * FROM quakejs.creator_payouts")
    assert not await lobby.expire_game(game["id"])
    row = await crud.one("SELECT * FROM quakejs.arenas WHERE id=:id", id=game["id"])
    assert row["idle_since"] is not None


@pytest.mark.anyio
async def test_lobby_is_owner_scoped_and_exposes_only_public_fields(arena):
    setting, game = await public_arena()
    other, other_game = await public_arena("other-owner")
    result = await lobby.snapshot(setting["public_id"])
    assert other_game["id"] not in {row["id"] for row in result["games"]}
    assert game["id"] in {row["id"] for row in result["games"]}
    encoded = json.dumps(result)
    for forbidden in (
        "wallet_id",
        "walletId",
        "owner_id",
        "public_nonce",
        "creator_ln_address",
        "payment_hash",
        "bolt11",
        "adminkey",
    ):
        assert forbidden not in encoded
    assert "creator@example.com" not in encoded
    assert (await crud.public_state(game["id"]))["lobbyUrl"].endswith(
        setting["public_id"]
    )
    assert other["public_id"] != setting["public_id"]


@pytest.mark.parametrize("exception", [RuntimeError, ValueError])
def test_public_errors_do_not_echo_internal_secrets(monkeypatch, exception):
    app = FastAPI()
    app.include_router(views.router, prefix="/quakejs")
    views.limits.clear()

    async def broken(*args):
        raise exception("adminkey=DO-NOT-EXPOSE wallet_id=private")

    monkeypatch.setattr(lobby, "create_game", broken)
    monkeypatch.setattr(lobby, "snapshot", broken)
    monkeypatch.setattr(lobby, "setting_for", broken)
    with TestClient(app) as client:
        payload = game_input().dict(by_alias=True)
        result = client.post("/quakejs/api/v1/lobby/example/games", json=payload)
        assert result.status_code == 503 and "DO-NOT-EXPOSE" not in result.text
        result = client.get("/quakejs/api/v1/lobby/example")
        assert result.status_code == 503 and "DO-NOT-EXPOSE" not in result.text
        result = client.get("/quakejs/lobby/example")
        assert result.status_code == 503 and "DO-NOT-EXPOSE" not in result.text
        result = client.post(
            "/quakejs/api/v1/lobby/example/games",
            json=payload,
            headers={"origin": "https://evil.example"},
        )
        assert result.status_code == 403


def test_lobby_websocket_is_public_scoped_bounded_and_closes_when_disabled(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    app = FastAPI()
    app.include_router(views.router, prefix="/quakejs")
    views.limits.clear()
    enabled = True

    async def setting(public_id):
        if not enabled:
            raise ValueError("unavailable")
        assert public_id == "public-test"
        return {"id": "private-owner"}

    async def snapshot(public_id, page):
        await setting(public_id)
        return {"games": [], "page": page, "scoreboard": []}

    monkeypatch.setattr(lobby, "setting_for", setting)
    monkeypatch.setattr(lobby, "snapshot", snapshot)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/quakejs/api/v1/lobby/public-test/ws",
                headers={"origin": "https://evil.example"},
            ):
                pass
        with client.websocket_connect(
            "/quakejs/api/v1/lobby/public-test/ws",
            headers={"origin": "http://testserver"},
        ) as ws:
            assert ws.receive_json()["page"] == 1
            ws.send_json({"type": "page", "page": 2})
            assert ws.receive_json()["page"] == 2
            assert len(lobby.listeners["private-owner"]) == 1
            enabled = False
            ws.send_json({"type": "page", "page": 1})
            assert ws.receive()["type"] == "websocket.close"
    assert not lobby.listeners
    assert not views.socket_slots


@pytest.mark.anyio
async def test_creator_payment_timeout_is_reconciled_without_resending(
    arena, monkeypatch
):
    _, game = await public_arena()
    tokens = [(await paid(game))[0] for _ in range(2)]
    lives = [
        await crud.allocate_life(game["id"], token, game["run_id"]) for token in tokens
    ]
    await crud.consume_death(game["run_id"], 1, lives[0]["id"], lives[1]["id"])
    claimed = [await payments.claim_payout(), await payments.claim_payout()]
    row = next(item for item in claimed if item["_table"] == "creator_payouts")
    await payments.update_payout(
        row, "prepared", bolt11="synthetic-invoice", payment_hash="creator-hash"
    )
    sends = []

    async def send(**kwargs):
        sends.append(kwargs)
        raise asyncio.TimeoutError()

    async def lookup(*args, **kwargs):
        return SimpleNamespace(
            is_out=True,
            success=True,
            pending=False,
            failed=False,
            wallet_id=row["wallet_id"],
            amount=-2000,
            payment_hash=row["payment_hash"],
            extra={
                "tag": "quakejs",
                "quakejs_payout": row["id"],
                "quakejs_victim": row["victim_id"],
                "quakejs_kind": "creator",
            },
        )

    monkeypatch.setattr(payments, "pay_invoice", send)
    monkeypatch.setattr(payments, "get_standalone_payment", lookup)
    await payments.process_payout(row)
    assert row["status"] == "pending"
    await crud.allocate_life(game["id"], tokens[0], game["run_id"])
    await payments.process_payout(row)
    assert row["status"] == "paid" and len(sends) == 1
    assert (await crud.public_state(game["id"], tokens[1]))["won"] == 0


@pytest.mark.anyio
async def test_public_game_limit_cannot_be_bypassed_by_concurrent_creators(arena):
    setting = await crud.save_settings("owner", "wallet", True, 5, True)
    for _ in range(49):
        await lobby.create_game(setting["public_id"], game_input())
    results = await asyncio.gather(
        *(lobby.create_game(setting["public_id"], game_input()) for _ in range(5)),
        return_exceptions=True
    )
    assert sum(isinstance(row, dict) for row in results) == 1
    assert sum(isinstance(row, ValueError) for row in results) == 4


@pytest.mark.parametrize("fee", [True, -1, 51, 99, 101, 1.5, "10"])
def test_creator_fee_must_be_an_integer_percentage(fee):
    with pytest.raises(ValidationError):
        game_input(creatorHaircut=fee)


@pytest.mark.parametrize("admin_fee,creator_fee", [(0, 50), (5, 45), (50, 0)])
@pytest.mark.anyio
async def test_combined_fee_limit_is_enforced_in_creation_transaction(
    arena, admin_fee, creator_fee
):
    setting = await crud.save_settings("owner", "wallet", True, admin_fee, True)
    game = await lobby.create_game(
        setting["public_id"], game_input(creatorHaircut=creator_fee)
    )
    assert game["haircut"] + game["creatorHaircut"] == 50
    assert game["prizePerKill"] == 10
    before = await crud.one("SELECT COUNT(*) AS n FROM quakejs.arenas")
    with pytest.raises(ValueError):
        await lobby.create_game(
            setting["public_id"], game_input(creatorHaircut=creator_fee + 1)
        )
    assert await crud.one("SELECT COUNT(*) AS n FROM quakejs.arenas") == before


@pytest.mark.parametrize("fee", [True, -1, 51, 99, 1.5, "50"])
@pytest.mark.anyio
async def test_admin_fee_limit_rejects_invalid_settings_without_saving(arena, fee):
    with pytest.raises(ValidationError):
        SettingsInput(walletId="wallet", haircut=fee)
    before = await crud.settings_for("owner")
    with pytest.raises(ValueError):
        await crud.save_settings("owner", "wallet", True, fee, True)
    assert await crud.settings_for("owner") == before


@pytest.mark.anyio
async def test_upgrade_preserves_existing_finances_and_resumes_after_interruption(
    tmp_path, monkeypatch
):
    from lnbits.db import Database
    from lnbits.extensions.quakejs.migrations import (
        m007_native_arena_ledger,
        m009_public_lobbies,
    )
    from lnbits.settings import settings

    monkeypatch.setattr(settings, "lnbits_data_folder", str(tmp_path))
    database = Database("ext_quakejs")
    try:
        await m007_native_arena_ledger(database)
        await database.execute(
            "INSERT INTO quakejs.settings_native VALUES('owner','wallet',1,5,123)"
        )
        await database.execute(
            "INSERT INTO quakejs.payouts "
            "(id,arena_id,victim_id,killer_id,player_id,wallet_id,ln_address,"
            "amount,status,bolt11,payment_hash,created_at,updated_at) "
            "VALUES('payout','arena','victim','killer','player','wallet',"
            "'winner@example.com',9,'pending','invoice','hash',123,123)"
        )
        # Simulate stopping after the first DDL statement was committed.
        await database.execute(
            "ALTER TABLE quakejs.settings_native ADD COLUMN "
            "allow_public_creation INTEGER NOT NULL DEFAULT 0"
        )
        await m009_public_lobbies(database)
        setting = await database.fetchone("SELECT * FROM quakejs.settings_native")
        assert not setting["allow_public_creation"]
        assert setting["wallet_id"] == "wallet" and setting["haircut"] == 5
        public_id = setting["public_id"]
        assert len(public_id) == 48
        await m009_public_lobbies(database)
        assert (
            await database.fetchone("SELECT public_id FROM quakejs.settings_native")
        )["public_id"] == public_id
        payout = await database.fetchone("SELECT * FROM quakejs.payouts")
        assert payout["status"] == "pending" and payout["amount"] == 9
        claim = await database.fetchone("SELECT * FROM quakejs.payout_invoice_claims")
        assert claim["payment_hash"] == "hash" and claim["payout_id"] == "payout"
    finally:
        await database.engine.dispose()


@pytest.mark.anyio
async def test_late_payment_restores_expired_public_game_without_losing_lives(
    arena, monkeypatch
):
    setting, game = await public_arena()
    token = crud.uid()
    entry = await crud.reserve_entry(
        game["id"], token, EntryInput(lnAddress="player@example.com", nonce=crud.uid())
    )
    clock = crud.now()
    monkeypatch.setattr(crud, "now", lambda: clock + 601)
    assert await lobby.expire_game(game["id"])
    await crud.settle_entry(
        SimpleNamespace(
            success=True,
            is_in=True,
            extra={"tag": "quakejs", "quakejs_entry": entry["id"]},
            wallet_id="wallet",
            amount=100000,
            payment_hash=crud.uid(),
            bolt11="test-invoice",
        )
    )
    state = await crud.public_state(game["id"], token)
    assert state["player"]["livesRemaining"] == 5
    assert state["game"]["status"] == "active"
    assert game["id"] in {
        row["id"] for row in (await lobby.snapshot(setting["public_id"]))["games"]
    }


@pytest.mark.anyio
async def test_server_capacity_is_persistent_admin_only_and_keeps_running_games(
    arena, monkeypatch
):
    from lnbits.extensions.quakejs.models import ServerSettingsInput
    from lnbits.extensions.quakejs.server import Manager

    manager = Manager()
    monkeypatch.setattr(views, "manager", manager)
    monkeypatch.setattr(views.lnbits_settings, "super_user", "server-owner")
    monkeypatch.setattr(views.lnbits_settings, "lnbits_admin_users", [])
    key = SimpleNamespace(wallet=SimpleNamespace(user="regular-owner"))
    assert await crud.server_capacity() == 4
    with pytest.raises(views.HTTPException) as error:
        await views.put_server_settings(ServerSettingsInput(maxMatches=12), key)
    assert error.value.status_code == 403
    assert await crud.server_capacity() == 4
    key.wallet.user = "server-owner"
    result = await views.put_server_settings(ServerSettingsInput(maxMatches=12), key)
    assert result["server"]["maxMatches"] == 12
    assert manager.max_matches == 12
    assert await crud.server_capacity() == 12
    manager.matches = {"existing-one": object(), "existing-two": object()}
    await views.put_server_settings(ServerSettingsInput(maxMatches=1), key)
    assert manager.max_matches == 1
    assert len(manager.matches) == 2
    # Re-running the schema initialization never resets an administrator's cap.
    from lnbits.extensions.quakejs.migrations import m010_server_capacity

    await m010_server_capacity(crud.db)
    assert await crud.server_capacity() == 1


@pytest.mark.parametrize("value", [0, 33, True, "8", 8.5])
def test_server_capacity_requires_bounded_integer(value):
    from lnbits.extensions.quakejs.models import ServerSettingsInput

    with pytest.raises(ValidationError):
        ServerSettingsInput(maxMatches=value)


@pytest.mark.anyio
@pytest.mark.parametrize("address", ["", "creator@example.com"])
async def test_zero_creator_fee_never_queues_a_creator_payment(arena, address):
    _, game = await public_arena(creatorHaircut=0, lnAddress=address)
    victim_token, _, _ = await paid(game)
    killer_token, _, _ = await paid(game)
    victim = await crud.allocate_life(game["id"], victim_token, game["run_id"])
    killer = await crud.allocate_life(game["id"], killer_token, game["run_id"])
    await crud.consume_death(game["run_id"], 1, victim["id"], killer["id"])
    assert not await crud.all_rows("SELECT * FROM quakejs.creator_payouts")
    payout = await payments.claim_payout()
    assert payout["_table"] == "payouts"
    assert payout["amount"] == 19  # 100 / 5, less only the 5% admin fee, rounded down.
    assert payout["ln_address"] == "winner@example.com"
    assert await payments.claim_payout() is None


@pytest.mark.anyio
async def test_closure_during_invoice_creation_does_not_return_payment_request(
    arena, monkeypatch
):
    started, release = asyncio.Event(), asyncio.Event()
    captured = {}

    async def provider(**values):
        captured.update(values)
        started.set()
        await release.wait()
        return SimpleNamespace(
            success=False,
            is_in=True,
            payment_hash="delayed-hash",
            bolt11="delayed-invoice",
        )

    monkeypatch.setattr(payments, "create_invoice", provider)
    token = crud.uid()
    task = asyncio.create_task(
        payments.create_entry(
            arena["id"],
            token,
            EntryInput(lnAddress="late@example.com", nonce=crud.uid()),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        await views.close_game(
            arena["id"], SimpleNamespace(wallet=SimpleNamespace(user="owner"))
        )
        release.set()
        with pytest.raises(ValueError, match="closed. Do not pay"):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    entry = await crud.one(
        "SELECT * FROM quakejs.entries WHERE id=:id", id=captured["external_id"]
    )
    assert entry["status"] == "pending" and entry["bolt11"] == "delayed-invoice"
    state = await crud.public_state(arena["id"], token)
    assert state["game"]["status"] == "closed" and not state["canJoin"]
    assert "invoice" not in state


@pytest.mark.anyio
@pytest.mark.parametrize("payment_first", [False, True])
async def test_concurrent_admin_closure_and_settlement_stay_closed(
    arena, payment_first
):
    entry = await crud.reserve_entry(
        arena["id"],
        crud.uid(),
        EntryInput(lnAddress="late@example.com", nonce=crud.uid()),
    )
    payment = SimpleNamespace(
        success=True,
        is_in=True,
        extra={"tag": "quakejs", "quakejs_entry": entry["id"]},
        wallet_id="wallet",
        amount=entry["amount"] * 1000,
        payment_hash=crud.uid(),
        bolt11="late-invoice",
    )
    operations = [
        views.close_game(
            arena["id"], SimpleNamespace(wallet=SimpleNamespace(user="owner"))
        ),
        crud.settle_entry(payment),
    ]
    if payment_first:
        operations.reverse()
    await asyncio.gather(*operations)
    state = await crud.public_state(arena["id"])
    assert state["game"]["status"] == "closed" and not state["canJoin"]
    recorded = await crud.one(
        "SELECT * FROM quakejs.entries WHERE id=:id", id=entry["id"]
    )
    assert recorded["status"] == "paid" and recorded["remaining"] == 5
