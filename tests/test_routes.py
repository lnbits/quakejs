from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lnbits.extensions.quakejs import crud, views
from lnbits.extensions.quakejs.models import SettingsInput


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_websocket_rejects_cross_origin_and_fake_kills(monkeypatch):
    app = FastAPI()
    app.include_router(views.router, prefix="/quakejs")

    async def public(*args):
        return {"game": {"id": "arena"}, "player": None}

    monkeypatch.setattr(crud, "public_state", public)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/quakejs/api/v1/ws/arena", headers={"origin": "https://evil.example"}
            ):
                pass
        with client.websocket_connect(
            "/quakejs/api/v1/ws/arena", headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json({"token": crud.uid()})
            assert ws.receive_json()["type"] == "state"
            ws.send_json({"type": "admit"})
            assert ws.receive_json()["message"] == "A paid entry is required."
            ws.send_json({"type": "kill", "victim": "forged", "amount": 1000000})
            assert ws.receive()["type"] == "websocket.close"
        assert (
            client.post("/quakejs/api/v1/public/arena/winner", json={}).status_code
            == 404
        )
        assert client.post(
            "/quakejs/api/v1/games", json={"name": "Unauthorized"}
        ).status_code in (401, 403)


@pytest.mark.anyio
async def test_settings_require_ownership_and_send_permission(monkeypatch):
    async def wallets(*args):
        return [
            SimpleNamespace(
                id="own",
                is_lightning_shared_wallet=False,
                can_send_payments=False,
                can_receive_payments=True,
            )
        ]

    monkeypatch.setattr(views, "get_wallets", wallets)
    key = SimpleNamespace(wallet=SimpleNamespace(user="alice"))
    for wallet in ("other-owner", "own"):
        with pytest.raises(views.HTTPException) as error:
            await views.put_settings(SettingsInput(walletId=wallet, enabled=True), key)
        assert error.value.status_code == 403


def test_minimum_entry_fee_and_map_allowlist():
    from pydantic import ValidationError

    from lnbits.extensions.quakejs.models import MAPS, ArenaInput, PublicArenaInput

    for amount in (0, 49, 50, 99, 100.5, True, "100"):
        with pytest.raises(ValidationError):
            ArenaInput(joinAmount=amount)
        with pytest.raises(ValidationError):
            PublicArenaInput(joinAmount=amount, nonce="a" * 48)
    for name in ("../../etc/passwd", "aggressor;quit", "unknown"):
        with pytest.raises(ValidationError):
            ArenaInput(map=name)
    assert ArenaInput(joinAmount=100).join_amount == 100
    assert PublicArenaInput(joinAmount=100, nonce="a" * 48).join_amount == 100
    maps = ["aggressor", "oa_dm7", "oa_minia", "czest1dm", "oa_shine", "kaos2"]
    assert [item["value"] for item in MAPS] == maps
    for name in maps:
        assert ArenaInput(map=name).map == name
        assert PublicArenaInput(map=name, nonce="a" * 48).map == name
    for name in ("oa_dm1", "oa_dm2"):
        with pytest.raises(ValidationError):
            ArenaInput(map=name)


def test_rate_limit_storage_is_bounded(monkeypatch):
    monkeypatch.setattr(views, "limits", {})
    for index in range(8192):
        views.limit(("player", index), 1, 60)
    for index in range(8192, 8200):
        with pytest.raises(ValueError, match="busy"):
            views.limit(("player", index), 1, 60)
    assert len(views.limits) == 8192


@pytest.mark.parametrize("global_limit,ip_limit", [(1, 32), (512, 1)])
def test_incomplete_websocket_handshakes_reserve_capacity(
    monkeypatch, global_limit, ip_limit
):
    app = FastAPI()
    app.include_router(views.router, prefix="/quakejs")
    monkeypatch.setattr(views, "limits", {})
    monkeypatch.setattr(views, "socket_slots", {})
    monkeypatch.setattr(views, "MAX_SOCKETS", global_limit)
    monkeypatch.setattr(views, "MAX_SOCKETS_PER_IP", ip_limit)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/quakejs/api/v1/ws/arena", headers={"origin": "http://testserver"}
        ):
            # No hello: the socket already occupies capacity while waiting.
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/quakejs/api/v1/ws/arena", headers={"origin": "http://testserver"}
                ):
                    pass
            assert len(views.socket_slots) == 1
    assert not views.socket_slots


@pytest.mark.anyio
async def test_invoice_request_does_not_start_an_engine(monkeypatch):
    from unittest.mock import AsyncMock

    from lnbits.extensions.quakejs.models import EntryInput

    token = crud.uid()
    start = AsyncMock(side_effect=AssertionError("Unpaid engine startup"))
    create = AsyncMock(return_value={"paymentRequest": "test-invoice"})
    monkeypatch.setattr(views.manager, "ensure", start)
    monkeypatch.setattr(views.manager, "check_available", lambda _: None)
    monkeypatch.setattr(views.manager, "matches", {})
    monkeypatch.setattr(views.manager, "notify", AsyncMock())
    monkeypatch.setattr(views, "create_entry", create)
    result = await views.invoice(
        SimpleNamespace(client=SimpleNamespace(host="invoice-test")),
        "arena",
        EntryInput(lnAddress="player@example.com", nonce=crud.uid()),
        "Bearer " + token,
    )
    assert result["paymentRequest"] == "test-invoice"
    start.assert_not_awaited()
    create.assert_awaited_once()


@pytest.mark.parametrize("token", [None, 48, ["a"] * 48, {}, "a" * 47, "z" * 48])
def test_malformed_player_tokens_are_rejected(token):
    with pytest.raises(ValueError, match="Invalid player session"):
        crud.token_hash(token)


def test_deep_websocket_json_is_rejected():
    with pytest.raises(ValueError, match="Invalid WebSocket"):
        views.message_object("[" * 2000 + "0" + "]" * 2000)


def test_extracted_binary_permissions_and_integrity(tmp_path, monkeypatch):
    import hashlib
    import json

    from lnbits.extensions.quakejs import server

    root = tmp_path / "bin" / "linux-x86_64"
    root.mkdir(parents=True)
    binary = root / "ioq3ded"
    binary.write_bytes(b"test executable")
    binary.chmod(0o644)
    (root / "manifest.json").write_text(
        json.dumps({"sha256": hashlib.sha256(binary.read_bytes()).hexdigest()})
    )
    monkeypatch.setattr(server, "ROOT", tmp_path)
    assert server.prepare_binary() == binary
    assert binary.stat().st_mode & 0o777 == 0o744
    binary.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity"):
        server.prepare_binary()


@pytest.mark.parametrize("exception", [ValueError, RuntimeError])
def test_player_http_errors_redact_secrets(monkeypatch, exception):
    from unittest.mock import AsyncMock

    app = FastAPI(debug=True)
    app.include_router(views.router, prefix="/quakejs")
    monkeypatch.setattr(views, "limits", {})
    broken = AsyncMock(side_effect=exception("adminkey=DO-NOT-EXPOSE"))
    monkeypatch.setattr(crud, "public_state", broken)
    monkeypatch.setattr(views, "create_entry", broken)
    monkeypatch.setattr(views.manager, "check_available", lambda _: None)
    monkeypatch.setattr(views.manager, "matches", {})
    token = crud.uid()
    headers = {"Authorization": "Bearer " + token}
    with TestClient(app) as client:
        result = client.get("/quakejs/api/v1/public/arena", headers=headers)
        assert result.status_code == 503
        assert "DO-NOT-EXPOSE" not in result.text
        result = client.post(
            "/quakejs/api/v1/public/arena/entry",
            headers=headers,
            json={"lnAddress": "player@example.com", "nonce": crud.uid()},
        )
        assert result.status_code == 503
        assert "DO-NOT-EXPOSE" not in result.text


@pytest.mark.anyio
async def test_admission_errors_are_redacted_and_do_not_echo_tokens(monkeypatch):
    from unittest.mock import AsyncMock

    connection = views.Connection(None, "arena", crud.uid())
    monkeypatch.setattr(views, "limits", {})
    monkeypatch.setattr(
        crud, "public_state", AsyncMock(return_value={"player": {"livesRemaining": 5}})
    )
    monkeypatch.setattr(
        views.manager, "ensure", AsyncMock(side_effect=ValueError("private wallet key"))
    )
    await connection.message({"type": "admit"})
    message = connection.queue.get_nowait()
    assert message["type"] == "error"
    assert "paid lives are saved" in message["message"]
    assert "private wallet key" not in message["message"]


def test_invoice_limit_applies_across_arenas(monkeypatch):
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(views.router, prefix="/quakejs")
    monkeypatch.setattr(views, "limits", {})
    monkeypatch.setattr(views.manager, "check_available", lambda _: None)
    monkeypatch.setattr(views.manager, "matches", {})
    monkeypatch.setattr(views.manager, "notify", AsyncMock())
    create = AsyncMock(return_value={"paymentRequest": "fixture"})
    monkeypatch.setattr(views, "create_entry", create)
    with TestClient(app) as client:
        for n in range(13):
            response = client.post(
                f"/quakejs/api/v1/public/arena-{n}/entry",
                headers={"Authorization": "Bearer " + crud.uid()},
                json={"lnAddress": "player@example.com", "nonce": crud.uid()},
            )
            assert response.status_code == (200 if n < 12 else 409)
    assert create.await_count == 12


def test_public_html_errors_are_generic_even_in_debug_mode(monkeypatch):
    from unittest.mock import AsyncMock

    app = FastAPI(debug=True)
    app.include_router(views.router, prefix="/quakejs")
    monkeypatch.setattr(
        crud, "one", AsyncMock(side_effect=RuntimeError("database-password=secret"))
    )
    with TestClient(app) as client:
        for path in ("/games/arena", "/games/arena/share.jpg"):
            response = client.get("/quakejs" + path)
            assert response.status_code == 503
            assert "secret" not in response.text
            assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["player", "lobby", "admission"])
async def test_disconnect_waits_for_inflight_ledger_work(monkeypatch, kind):
    import asyncio

    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    interrupted = []

    async def ledger(*args):
        started.set()
        try:
            await release.wait()
            completed.set()
            return {"game": {"id": "arena"}, "player": None}
        except asyncio.CancelledError:
            interrupted.append(True)
            raise

    if kind == "player":
        monkeypatch.setattr(crud, "public_state", ledger)
        connection = views.Connection(None, "arena", crud.uid())
        operation = connection.states()
    elif kind == "lobby":
        monkeypatch.setattr(views.lobby, "snapshot", ledger)
        connection = views.LobbyConnection(None, "public")
        connection.event.set()
        operation = connection.write()
    else:
        from unittest.mock import AsyncMock

        socket = SimpleNamespace(
            receive=AsyncMock(
                return_value={"type": "websocket.receive", "text": '{"type":"admit"}'}
            )
        )
        connection = views.Connection(socket, "arena", crud.uid())
        monkeypatch.setattr(connection, "message", ledger)
        operation = connection.read()
    task = asyncio.create_task(operation)
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not interrupted
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert completed.is_set()
    assert not interrupted
