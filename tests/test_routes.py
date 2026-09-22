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

    from lnbits.extensions.quakejs.models import ArenaInput

    for amount in (0, 49, 50.5, True, "50"):
        with pytest.raises(ValidationError):
            ArenaInput(joinAmount=amount)
    for name in ("../../etc/passwd", "aggressor;quit", "unknown"):
        with pytest.raises(ValidationError):
            ArenaInput(map=name)
    assert ArenaInput(joinAmount=50).join_amount == 50


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
