import asyncio
import json
import time
from collections import deque
from urllib.parse import urlsplit

from anyio import fail_after
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from lnbits.core.crud import get_wallets
from lnbits.core.models import WalletTypeInfo
from lnbits.core.views.generic import index as lnbits_index
from lnbits.decorators import check_user_exists, require_admin_key
from lnbits.helpers import template_renderer

from . import crud
from .models import MAPS, ArenaInput, EntryInput, SettingsInput
from .payments import create_entry
from .server import manager
from .share import HEIGHT, WIDTH, render_share_image


class PrivateJSONResponse(JSONResponse):
    def __init__(self, *args, **kwargs):
        headers = kwargs.pop("headers", None) or {}
        super().__init__(
            *args, headers={**headers, "Cache-Control": "no-store"}, **kwargs
        )


router = APIRouter(default_response_class=PrivateJSONResponse)
renderer = template_renderer(["quakejs/templates"])
limits = {}
socket_slots = {}
MAX_SOCKETS = 512
MAX_SOCKETS_PER_IP = 32
share_render_slots = asyncio.Semaphore(2)


def limit(key, maximum, period):
    current = time.monotonic()
    if key not in limits:
        if len(limits) >= 4096:
            for candidate in list(limits):
                if not limits[candidate] or limits[candidate][-1] < current - 600:
                    del limits[candidate]
            if len(limits) >= 8192:
                raise ValueError("Server busy. Please retry shortly.")
        limits[key] = deque()
    bucket = limits[key]
    while bucket and bucket[0] < current - period:
        bucket.popleft()
    if len(bucket) >= maximum:
        raise ValueError("Too many requests. Please wait before retrying.")
    bucket.append(current)


def message_object(text):
    try:
        data = json.loads(text)
    except RecursionError:
        raise ValueError("Invalid WebSocket message.") from None
    if not isinstance(data, dict):
        raise ValueError("Invalid WebSocket message.")
    return data


def session(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "A player session is required.")
    token = authorization[7:]
    try:
        crud.token_hash(token)
    except ValueError as error:
        raise HTTPException(401, str(error)) from None
    return token


router.add_api_route(
    "/",
    methods=["GET"],
    endpoint=lnbits_index,
    dependencies=[Depends(check_user_exists)],
)


@router.get("/games/{arena_id}", name="quakejs_public_page")
async def public_page(request: Request, arena_id: str):
    arena = await crud.one("SELECT * FROM quakejs.arenas WHERE id=:id", id=arena_id)
    if not arena:
        raise HTTPException(404, "Arena not found.")
    game = crud.public_arena(arena)
    title = f"FIGHT ME IN QUAKE · SATS FOR KILLS · {game['joinAmount']} SATS TO JOIN"
    description = (
        f"{game['joinAmount']} sats buys 5 lives. "
        f"Earn {game['prizePerKill']} sats per kill after the "
        f"{game['haircut']}% arena fee. Join {game['name']}."
    )
    return renderer.TemplateResponse(
        "quakejs/public.html",
        {
            "request": request,
            "game": game,
            "share_title": title,
            "share_description": description,
            "share_url": str(request.url_for("quakejs_public_page", arena_id=arena_id)),
            "share_image": str(
                request.url_for("quakejs_share_image", arena_id=arena_id)
            )
            + f"?v=1-{game['joinAmount']}",
            "share_width": WIDTH,
            "share_height": HEIGHT,
        },
        headers={
            "Content-Security-Policy": (
                "frame-ancestors 'self'; object-src 'none'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/games/{arena_id}/share.jpg", name="quakejs_share_image")
async def share_image(arena_id: str):
    arena = await crud.one(
        "SELECT entry_amount FROM quakejs.arenas WHERE id=:id", id=arena_id
    )
    if not arena:
        raise HTTPException(404, "Arena not found.")
    async with share_render_slots:
        content = await asyncio.to_thread(render_share_image, arena["entry_amount"])
    return Response(
        content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


def display_settings(row):
    return (
        {
            "walletId": row["wallet_id"],
            "enabled": bool(row["enabled"]),
            "haircut": row["haircut"],
        }
        if row
        else {"walletId": "", "enabled": False, "haircut": 5}
    )


@router.get("/api/v1/settings")
async def get_settings(key: WalletTypeInfo = Depends(require_admin_key)):
    return {
        "settings": display_settings(await crud.settings_for(key.wallet.user)),
        "maps": MAPS,
    }


@router.put("/api/v1/settings")
async def put_settings(
    data: SettingsInput, key: WalletTypeInfo = Depends(require_admin_key)
):
    wallets = await get_wallets(key.wallet.user)
    wallet = next((w for w in wallets if w.id == data.wallet_id), None)
    if (
        not wallet
        or wallet.is_lightning_shared_wallet
        or not wallet.can_send_payments
        or not wallet.can_receive_payments
    ):
        raise HTTPException(
            403, "Choose a Lightning wallet you own with send and receive permissions."
        )
    return {
        "settings": display_settings(
            await crud.save_settings(
                key.wallet.user, wallet.source_wallet_id, data.enabled, data.haircut
            )
        )
    }


@router.get("/api/v1/games")
async def list_games(
    page: int = 1,
    rows_per_page: int = Query(default=10, alias="rowsPerPage"),
    key: WalletTypeInfo = Depends(require_admin_key),
):
    size = min(100, max(1, rows_per_page))
    rows = await crud.all_rows(
        "SELECT * FROM quakejs.arenas WHERE owner_id=:owner ORDER BY created_at"
        " DESC,id DESC LIMIT :limit OFFSET :offset",
        owner=key.wallet.user,
        limit=size,
        offset=max(0, page - 1) * size,
    )
    count = await crud.one(
        "SELECT COUNT(*) AS n FROM quakejs.arenas WHERE owner_id=:owner",
        owner=key.wallet.user,
    )
    games = []
    for row in rows:
        active = await crud.one(
            "SELECT COUNT(*) AS n FROM quakejs.lives WHERE arena_id=:id AND "
            "status='alive' AND connected_until>:now",
            id=row["id"],
            now=crud.now(),
        )
        games.append(crud.public_arena(row, active["n"]))
    return {"games": games, "total": count["n"]}


@router.post("/api/v1/games")
async def new_game(data: ArenaInput, key: WalletTypeInfo = Depends(require_admin_key)):
    try:
        return {
            "game": crud.public_arena(await crud.create_arena(key.wallet.user, data))
        }
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.delete("/api/v1/games/{arena_id}")
async def close_game(arena_id: str, key: WalletTypeInfo = Depends(require_admin_key)):
    async with crud.transaction() as tx:
        row = await tx.one(
            "SELECT * FROM quakejs.arenas WHERE id=:id AND owner_id=:owner",
            id=arena_id,
            owner=key.wallet.user,
        )
        if not row:
            raise HTTPException(404, "Arena not found.")
        await tx.lock_arena(arena_id, active=False)
        outstanding = await tx.one(
            "SELECT COUNT(*) AS n FROM quakejs.entries WHERE arena_id=:id AND "
            "(remaining>0 OR (status IN ('creating','pending') AND "
            "expires_at>:now))",
            id=arena_id,
            now=crud.now(),
        )
        if outstanding["n"]:
            raise HTTPException(
                409,
                "This arena still has unused paid lives or live invoices. "
                "Disable new entries in Settings and let those lives finish "
                "before closing it.",
            )
        await tx.execute("UPDATE quakejs.arenas SET active=0 WHERE id=:id", id=arena_id)
    await manager.notify(arena_id)
    return {"success": True}


@router.get("/api/v1/games/{arena_id}/payouts")
async def payouts(arena_id: str, key: WalletTypeInfo = Depends(require_admin_key)):
    row = await crud.one(
        "SELECT id FROM quakejs.arenas WHERE id=:id AND owner_id=:owner",
        id=arena_id,
        owner=key.wallet.user,
    )
    if not row:
        raise HTTPException(404, "Arena not found.")
    return {
        "payouts": await crud.all_rows(
            "SELECT id,amount,status,error,payment_hash,created_at FROM "
            "quakejs.payouts WHERE arena_id=:id ORDER BY created_at DESC LIMIT "
            "100",
            id=arena_id,
        )
    }


@router.get("/api/v1/public/{arena_id}")
async def get_public(arena_id: str, authorization: str | None = Header(default=None)):
    try:
        return await crud.public_state(
            arena_id, session(authorization) if authorization else ""
        )
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/api/v1/public/{arena_id}/entry")
async def invoice(
    request: Request,
    arena_id: str,
    data: EntryInput,
    authorization: str | None = Header(default=None),
):
    token = session(authorization)
    try:
        limit(
            ("invoice", request.client.host if request.client else "", arena_id), 12, 60
        )
        # An unpaid visitor must not be able to occupy a native engine slot.
        manager.check_available(arena_id)
        if (
            arena_id not in manager.matches
            and len(manager.matches) >= manager.max_matches
        ):
            raise ValueError("The game server is at capacity. Try again later.")
        result = await create_entry(arena_id, token, data)
        await manager.notify(arena_id)
        return result
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


class Connection:
    def __init__(self, websocket, arena_id, token):
        self.websocket, self.arena_id, self.token = websocket, arena_id, token
        self.queue = asyncio.Queue(maxsize=128)
        self.dirty = asyncio.Event()
        self.dirty.set()
        self.match = None
        self.life = None
        self.overflow = False

    def offer(self, message):
        if self.queue.full():
            # Close a slow reader; don't retain stale gameplay or lose state silently.
            self.overflow = True
        else:
            self.queue.put_nowait(message)

    async def write(self):
        while True:
            message = await self.queue.get()
            if self.overflow:
                await self.websocket.close(code=1013)
                return
            with fail_after(3):
                if isinstance(message, bytes):
                    await self.websocket.send_bytes(message)
                else:
                    await self.websocket.send_json(message)

    async def states(self):
        while True:
            try:
                await asyncio.wait_for(self.dirty.wait(), 15)
            except asyncio.TimeoutError:
                self.dirty.set()
            self.dirty.clear()
            self.offer(
                {
                    "type": "state",
                    "data": await crud.public_state(self.arena_id, self.token),
                }
            )
            await asyncio.sleep(0.05)

    async def read(self):
        start, packets, total = time.monotonic(), 0, 0
        while True:
            message = await asyncio.wait_for(self.websocket.receive(), 30)
            if message["type"] == "websocket.disconnect":
                return
            payload = message.get("bytes")
            size = len(payload) if payload is not None else len(message.get("text", ""))
            current = time.monotonic()
            if current - start >= 1:
                start, packets, total = current, 0, 0
            packets += 1
            total += size
            if size > 16384 or packets > 160 or total > 131072:
                await self.websocket.close(code=1008)
                return
            if payload is not None:
                if self.match and self.life:
                    await self.match.send(bytes([2, self.life["slot"]]) + payload)
                continue
            data = message_object(message.get("text", "{}"))
            await self.message(data)

    async def message(self, data):
        if data.get("type") == "ping":
            self.offer({"type": "pong", "id": data.get("id")})
        elif data.get("type") == "admit":
            try:
                limit(("admit", self.token), 10, 10)
                state = await crud.public_state(self.arena_id, self.token)
                if not state["player"] or state["player"]["livesRemaining"] <= 0:
                    raise ValueError("A paid entry is required.")
                match = await manager.ensure(self.arena_id)
                life = await match.attach(self)
                self.offer({"type": "admitted", "life": life["id"]})
                await manager.notify(self.arena_id)
            except ValueError as error:
                self.offer({"type": "error", "message": str(error)})
        elif data.get("type") == "refresh":
            limit(("refresh", self.token), 5, 10)
            self.dirty.set()
        else:
            await self.websocket.close(code=1008)
            return


@router.websocket("/api/v1/ws/{arena_id}")
async def websocket(websocket: WebSocket, arena_id: str):
    origin = urlsplit(websocket.headers.get("origin", ""))
    if origin.netloc != websocket.headers.get("host") or origin.scheme not in (
        "http",
        "https",
    ):
        await websocket.close(code=1008)
        return
    ip = websocket.client.host if websocket.client else ""
    connection = None
    tasks = []
    reservation = None
    try:
        limit(("connect", ip), 30, 60)
        # Reserve before the first await, including incomplete handshakes.
        if (
            len(socket_slots) >= MAX_SOCKETS
            or sum(host == ip for host in socket_slots.values()) >= MAX_SOCKETS_PER_IP
        ):
            await websocket.close(code=1013)
            return
        reservation = object()
        socket_slots[reservation] = ip
        await websocket.accept()
        hello = await asyncio.wait_for(websocket.receive_text(), 5)
        if len(hello) > 256:
            raise ValueError("Invalid player session.")
        data = message_object(hello)
        token = data.get("token", "")
        crud.token_hash(token)
        await crud.public_state(arena_id, token)
        connection = Connection(websocket, arena_id, token)
        manager.connections.add(connection)
        tasks = [
            asyncio.create_task(fn())
            for fn in (connection.read, connection.write, connection.states)
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (
        ValueError,
        TypeError,
        KeyError,
        asyncio.TimeoutError,
        WebSocketDisconnect,
        RuntimeError,
    ):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if connection:
                manager.connections.discard(connection)
                if connection.match:
                    await connection.match.detach(connection)
                await manager.notify(arena_id)
        finally:
            socket_slots.pop(reservation, None)
            try:
                await websocket.close()
            except RuntimeError:
                pass
