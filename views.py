import asyncio
import json
import time
from collections import deque
from urllib.parse import urlsplit

from anyio import fail_after
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from loguru import logger
from starlette.responses import JSONResponse, Response

from lnbits.core.crud import get_wallets
from lnbits.core.models import WalletTypeInfo
from lnbits.core.views.generic import index as lnbits_index
from lnbits.decorators import check_user_exists, require_admin_key
from lnbits.helpers import template_renderer
from lnbits.settings import settings as lnbits_settings

from . import crud, lobby
from .models import (
    MAPS,
    ArenaInput,
    EntryInput,
    PublicArenaInput,
    PublicError,
    ServerSettingsInput,
    SettingsInput,
)
from .payments import create_entry
from .server import manager
from .share import HEIGHT, WIDTH, render_share_image


class PrivateJSONResponse(JSONResponse):
    def __init__(self, *args, **kwargs):
        headers = kwargs.pop("headers", None) or {}
        super().__init__(
            *args, headers={**headers, "Cache-Control": "no-store"}, **kwargs
        )


class SafePublicRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        path = self.path.removeprefix("/quakejs")
        public = path.startswith(
            ("/games/", "/lobby/", "/api/v1/public/", "/api/v1/lobby/")
        )

        async def guarded(request):
            try:
                response = await handler(request)
            except (HTTPException, RequestValidationError):
                raise
            except Exception:
                if not public:
                    raise
                logger.warning("QuakeJS public request failed; retry is available.")
                response = PrivateJSONResponse(
                    {"detail": "QuakeJS is temporarily unavailable. Retry shortly."},
                    status_code=503,
                )
            if request.method == "HEAD":
                # Keep GET metadata, including its Content-Length, but no body.
                return Response(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    background=response.background,
                )
            return response

        return guarded


router = APIRouter(
    default_response_class=PrivateJSONResponse, route_class=SafePublicRoute
)
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
                raise PublicError("Server busy. Please retry shortly.")
        limits[key] = deque()
    bucket = limits[key]
    while bucket and bucket[0] < current - period:
        bucket.popleft()
    if len(bucket) >= maximum:
        raise PublicError("Too many requests. Please wait before retrying.")
    bucket.append(current)


async def finish_on_cancel(operation):
    """Finish short ledger work before disconnect cleanup starts using the DB.

    Cancelling a socket must not interrupt SQLite's worker while it holds a
    cursor/transaction, or leave a partially completed paid admission behind.
    """
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


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
    except PublicError as error:
        raise HTTPException(401, str(error)) from None
    return token


router.add_api_route(
    "/",
    methods=["GET"],
    endpoint=lnbits_index,
    dependencies=[Depends(check_user_exists)],
)


@router.head("/games/{arena_id}", include_in_schema=False)
@router.get("/games/{arena_id}", name="quakejs_public_page")
async def public_page(request: Request, arena_id: str):
    arena = await crud.one("SELECT * FROM quakejs.arenas WHERE id=:id", id=arena_id)
    if not arena:
        raise HTTPException(404, "Arena not found.")
    if not arena["active"]:
        setting = await crud.settings_for(arena["owner_id"])
        lobby_url = (
            "/quakejs/lobby/" + setting["public_id"]
            if setting and setting["allow_public_creation"] and setting["public_id"]
            else ""
        )
        return renderer.TemplateResponse(
            "quakejs/closed.html",
            {"request": request, "name": arena["name"], "lobby_url": lobby_url},
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )
    game = crud.public_arena(arena)
    title = f"FIGHT ME IN QUAKE · SATS FOR KILLS · {game['joinAmount']} SATS TO JOIN"
    fee_label = f"{game['haircut']}% arena fee"
    if game["creatorHaircut"]:
        fee_label += f" and {game['creatorHaircut']}% creator fee"
    description = (
        f"{game['joinAmount']} sats buys 5 lives. "
        f"Earn {game['prizePerKill']} sats per kill after the "
        f"{fee_label}. Join {game['name']}."
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
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "frame-ancestors 'self'; object-src 'none'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.head("/games/{arena_id}/share.jpg", include_in_schema=False)
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
            "allowPublicCreation": bool(row.get("allow_public_creation")),
            "publicLobbyUrl": (
                "/quakejs/lobby/" + row["public_id"] if row.get("public_id") else ""
            ),
        }
        if row
        else {
            "walletId": "",
            "enabled": False,
            "haircut": 5,
            "allowPublicCreation": False,
            "publicLobbyUrl": "",
        }
    )


@router.get("/api/v1/settings")
async def get_settings(key: WalletTypeInfo = Depends(require_admin_key)):
    return {
        "settings": display_settings(await crud.settings_for(key.wallet.user)),
        "maps": MAPS,
        "server": {
            "maxMatches": manager.max_matches,
            "activeMatches": len(manager.matches),
            "canManage": lnbits_settings.is_admin_user(key.wallet.user),
        },
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
    saved = await crud.save_settings(
        key.wallet.user,
        wallet.source_wallet_id,
        data.enabled,
        data.haircut,
        data.allow_public_creation,
    )
    lobby.changed(key.wallet.user)
    return {"settings": display_settings(saved)}


@router.put("/api/v1/server-settings")
async def put_server_settings(
    data: ServerSettingsInput, key: WalletTypeInfo = Depends(require_admin_key)
):
    if not lnbits_settings.is_admin_user(key.wallet.user):
        raise HTTPException(403, "Only a server admin can change match capacity.")
    try:
        await finish_on_cancel(manager.set_capacity(data.max_matches))
    except Exception:
        raise HTTPException(
            503, "Match capacity could not be saved. Retry shortly."
        ) from None
    return {
        "server": {
            "maxMatches": manager.max_matches,
            "activeMatches": len(manager.matches),
            "canManage": True,
        }
    }


@router.get("/api/v1/games")
async def list_games(
    page: int = 1,
    rows_per_page: int = Query(default=10, alias="rowsPerPage"),
    include_closed: bool = False,
    key: WalletTypeInfo = Depends(require_admin_key),
):
    size = min(100, max(1, rows_per_page))
    rows = await crud.all_rows(
        "SELECT * FROM quakejs.arenas WHERE owner_id=:owner "
        "AND (:include_closed=1 OR active=1) ORDER BY created_at"
        " DESC,id DESC LIMIT :limit OFFSET :offset",
        owner=key.wallet.user,
        include_closed=int(include_closed),
        limit=size,
        offset=max(0, page - 1) * size,
    )
    count = await crud.one(
        "SELECT COUNT(*) AS n FROM quakejs.arenas WHERE owner_id=:owner "
        "AND (:include_closed=1 OR active=1)",
        owner=key.wallet.user,
        include_closed=int(include_closed),
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
        game = crud.public_arena(await crud.create_arena(key.wallet.user, data))
        lobby.changed(key.wallet.user)
        return {"game": game}
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
        await tx.execute(
            "UPDATE quakejs.arenas SET active=0,admin_closed=1,lobby_hidden=1 "
            "WHERE id=:id",
            id=arena_id,
        )
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
            "SELECT id,kind,amount,status,error,payment_hash,created_at FROM "
            "(SELECT id,'kill' AS kind,amount,status,error,payment_hash,"
            "created_at,arena_id FROM quakejs.payouts "
            "UNION ALL SELECT id,'creator' AS kind,amount,status,error,"
            "payment_hash,created_at,arena_id FROM quakejs.creator_payouts) "
            "AS transfers "
            "WHERE arena_id=:id ORDER BY created_at DESC LIMIT "
            "100",
            id=arena_id,
        )
    }


@router.get("/api/v1/public/{arena_id}")
async def get_public(
    request: Request, arena_id: str, authorization: str | None = Header(default=None)
):
    try:
        limit(("game-read", request.client.host if request.client else ""), 60, 60)
    except PublicError:
        raise HTTPException(429, "Too many requests. Please retry shortly.") from None
    try:
        return await crud.public_state(
            arena_id, session(authorization) if authorization else ""
        )
    except PublicError as error:
        raise HTTPException(404, str(error)) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503, "Arena information is temporarily unavailable."
        ) from None


@router.post("/api/v1/public/{arena_id}/entry")
async def invoice(
    request: Request,
    arena_id: str,
    data: EntryInput,
    authorization: str | None = Header(default=None),
):
    token = session(authorization)
    try:
        # Bound invoice work across all arenas, not just each chosen arena.
        limit(("invoice", request.client.host if request.client else ""), 12, 60)
        # An unpaid visitor must not be able to occupy a native engine slot.
        manager.check_available(arena_id)
        if (
            arena_id not in manager.matches
            and len(manager.matches) >= manager.max_matches
        ):
            raise PublicError("The game server is at capacity. Try again later.")
        result = await create_entry(arena_id, token, data)
        await manager.notify(arena_id)
        return result
    except PublicError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(
            503, "Entry preparation is temporarily unavailable. Retry shortly."
        ) from None


class Connection:
    def __init__(self, websocket, arena_id, token):
        self.websocket, self.arena_id, self.token = websocket, arena_id, token
        self.queue = asyncio.Queue(maxsize=128)
        self.dirty = asyncio.Event()
        self.dirty.set()
        self.match = None
        self.life = None
        self.overflow = False

    async def detach(self):
        manager.connections.discard(self)
        try:
            if self.match:
                await self.match.detach(self)
            await manager.notify(self.arena_id)
        except Exception:
            logger.warning("QuakeJS disconnect cleanup deferred to journal recovery.")

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
                    "data": await finish_on_cancel(
                        crud.public_state(self.arena_id, self.token)
                    ),
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
            await finish_on_cancel(self.message(data))

    async def message(self, data):
        if data.get("type") == "ping":
            self.offer({"type": "pong", "id": data.get("id")})
        elif data.get("type") == "admit":
            try:
                limit(("admit", self.token), 10, 10)
                state = await crud.public_state(self.arena_id, self.token)
                if not state["player"] or state["player"]["livesRemaining"] <= 0:
                    raise PublicError("A paid entry is required.")
                match = await manager.ensure(self.arena_id)
                life = await match.attach(self)
                self.offer({"type": "admitted", "life": life["id"]})
                await manager.notify(self.arena_id)
            except PublicError as error:
                self.offer({"type": "error", "message": str(error)})
            except Exception:
                self.offer(
                    {
                        "type": "error",
                        "message": (
                            "Arena admission is temporarily unavailable. "
                            "Your paid lives are saved."
                        ),
                    }
                )
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
        await finish_on_cancel(crud.public_state(arena_id, token))
        connection = Connection(websocket, arena_id, token)
        manager.connections.add(connection)
        tasks = [
            asyncio.create_task(fn())
            for fn in (connection.read, connection.write, connection.states)
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except Exception:
        # Never allow framework debug responses to expose a failed query or token.
        logger.debug("QuakeJS player connection closed.")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if connection:
                await connection.detach()
        finally:
            socket_slots.pop(reservation, None)
            try:
                await websocket.close()
            except RuntimeError:
                pass


@router.head("/lobby/{public_id}", include_in_schema=False)
@router.get("/lobby/{public_id}", name="quakejs_public_lobby")
async def public_lobby(request: Request, public_id: str):
    try:
        await lobby.setting_for(public_id)
    except lobby.LobbyError:
        raise HTTPException(404, "This public lobby is unavailable.") from None
    except Exception:
        raise HTTPException(
            503, "The public lobby is temporarily unavailable."
        ) from None
    return renderer.TemplateResponse(
        "quakejs/lobby.html",
        {
            "request": request,
            "public_id": public_id,
            "share_url": str(
                request.url_for("quakejs_public_lobby", public_id=public_id)
            ),
            "share_image": str(request.base_url) + "quakejs/static/share/lobby.png?v=1",
        },
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "frame-ancestors 'self'; object-src 'none'; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/v1/lobby/{public_id}")
async def lobby_state(
    request: Request, public_id: str, page: int = Query(1, ge=1, le=100000)
):
    try:
        limit(("lobby-read", request.client.host if request.client else ""), 60, 60)
    except ValueError:
        raise HTTPException(429, "Too many requests. Please retry shortly.") from None
    try:
        return await lobby.snapshot(public_id, page)
    except lobby.LobbyError as error:
        raise HTTPException(400, str(error)) from None
    except Exception:
        raise HTTPException(
            503, "The public lobby is temporarily unavailable."
        ) from None


@router.post("/api/v1/lobby/{public_id}/games")
async def public_new_game(request: Request, public_id: str, data: PublicArenaInput):
    # This is deliberately unauthenticated; no wallet key is accepted or needed.
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Cross-origin game creation is not allowed.")
    try:
        limit(("public-create", request.client.host if request.client else ""), 5, 60)
    except ValueError:
        raise HTTPException(429, "Too many requests. Please retry shortly.") from None
    try:
        return {"game": await lobby.create_game(public_id, data)}
    except lobby.LobbyError as error:
        raise HTTPException(400, str(error)) from None
    except Exception:
        raise HTTPException(
            503, "Game creation is temporarily unavailable. Retry shortly."
        ) from None


class LobbyConnection:
    def __init__(self, websocket, public_id):
        self.websocket = websocket
        self.public_id = public_id
        self.event = asyncio.Event()
        self.page = 1

    async def write(self):
        while True:
            try:
                await asyncio.wait_for(self.event.wait(), 15)
            except asyncio.TimeoutError:
                pass
            self.event.clear()
            data = await finish_on_cancel(lobby.snapshot(self.public_id, self.page))
            with fail_after(5):
                await self.websocket.send_json(data)
            await asyncio.sleep(1)

    async def read(self):
        while True:
            text = await asyncio.wait_for(self.websocket.receive_text(), 45)
            if len(text) > 100:
                raise ValueError("Invalid lobby message.")
            limit(("lobby-message", self), 15, 30)
            data = message_object(text)
            if data.get("type") == "ping":
                continue
            if (
                data.get("type") != "page"
                or type(data.get("page")) is not int
                or not 1 <= data["page"] <= 100000
            ):
                raise ValueError("Invalid lobby message.")
            self.page = data["page"]
            self.event.set()


@router.websocket("/api/v1/lobby/{public_id}/ws")
async def lobby_socket(websocket: WebSocket, public_id: str):
    origin = urlsplit(websocket.headers.get("origin", ""))
    if origin.netloc != websocket.headers.get("host") or origin.scheme not in (
        "http",
        "https",
    ):
        await websocket.close(code=1008)
        return
    ip = websocket.client.host if websocket.client else ""
    reservation = None
    connection = LobbyConnection(websocket, public_id)
    event = connection.event
    owner = None
    tasks = []
    try:
        limit(("connect", ip), 30, 60)
        if (
            len(socket_slots) >= MAX_SOCKETS
            or sum(host == ip for host in socket_slots.values()) >= MAX_SOCKETS_PER_IP
        ):
            await websocket.close(code=1013)
            return
        reservation = object()
        socket_slots[reservation] = ip
        setting = await finish_on_cancel(lobby.setting_for(public_id))
        owner = setting["id"]
        lobby.listeners.setdefault(owner, set()).add(event)
        await websocket.accept()
        event.set()

        tasks = [
            asyncio.create_task(connection.write()),
            asyncio.create_task(connection.read()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except Exception:
        # Never echo DB failures, provider errors, or request contents to visitors.
        logger.debug("QuakeJS public lobby connection closed.")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        socket_slots.pop(reservation, None)
        limits.pop(("lobby-message", connection), None)
        if owner:
            lobby.listeners.get(owner, set()).discard(event)
            if not lobby.listeners.get(owner):
                lobby.listeners.pop(owner, None)
        try:
            await websocket.close(code=1008)
        except RuntimeError:
            pass
