"""Public lobbies contain only explicitly selected public fields, never wallet keys."""

import asyncio

from loguru import logger

from . import crud
from .models import MAPS, MAX_PLAYERS, MAX_TOTAL_HAIRCUT

listeners = {}


class LobbyError(ValueError):
    """An intentionally public validation message, containing no internal data."""


def changed(owner):
    for event in tuple(listeners.get(owner, ())):
        event.set()


async def setting_for(public_id):
    row = await crud.one(
        "SELECT * FROM quakejs.settings_native WHERE "
        "public_id=:id AND allow_public_creation=1",
        id=public_id,
    )
    if not row:
        raise LobbyError("This public lobby is unavailable.")
    return row


async def create_game(public_id, data):
    async with crud.transaction() as tx:
        setting = await tx.one(
            "UPDATE quakejs.settings_native SET "
            "updated_at=updated_at WHERE public_id=:id "
            "AND allow_public_creation=1 RETURNING *",
            id=public_id,
        )
        if not setting or not setting["enabled"]:
            raise LobbyError("This lobby is not accepting new games.")
        owner = setting["id"]
        existing = await tx.one(
            "SELECT * FROM quakejs.arenas WHERE owner_id=:owner "
            "AND public_nonce=:nonce",
            owner=owner,
            nonce=data.nonce,
        )
        if existing:
            return crud.public_arena(existing)
        if data.creator_haircut + setting["haircut"] > MAX_TOTAL_HAIRCUT:
            raise LobbyError("The admin and creator fees cannot total more than 50%.")
        if data.creator_haircut and not data.ln_address:
            raise LobbyError("Enter a Lightning address to receive your creator fee.")
        count = await tx.one(
            "SELECT COUNT(*) AS n FROM quakejs.arenas WHERE "
            "owner_id=:owner AND is_public=1 "
            "AND (active=1 OR created_at>:recent)",
            owner=owner,
            recent=crud.now() - 3600,
        )
        if count["n"] >= 50:
            raise LobbyError(
                "This lobby has reached its game-creation limit. Try again later."
            )
        arena_id = crud.uid()
        await tx.execute(
            "INSERT INTO quakejs.arenas "
            "(id,owner_id,wallet_id,name,map,entry_amount,haircut,"
            "created_at,is_public,creator_haircut,creator_ln_address,"
            "public_nonce,idle_since) "
            "VALUES(:id,:owner,:wallet,:name,:map,:amount,:fee,:now,"
            "1,:creator_fee,:address,:nonce,:now)",
            id=arena_id,
            owner=owner,
            wallet=setting["wallet_id"],
            name=data.name,
            map=data.map,
            amount=data.join_amount,
            fee=setting["haircut"],
            now=crud.now(),
            creator_fee=data.creator_haircut,
            address=data.ln_address,
            nonce=data.nonce,
        )
        arena = await tx.one("SELECT * FROM quakejs.arenas WHERE id=:id", id=arena_id)
    changed(owner)
    return crud.public_arena(arena)


async def snapshot(public_id, page=1):
    setting = await setting_for(public_id)
    owner = setting["id"]
    games = await crud.all_rows(
        "SELECT a.*, (SELECT COUNT(*) FROM quakejs.lives l WHERE l.arena_id=a.id "
        "AND l.status='alive' AND l.connected_until>:now) AS players_count "
        "FROM quakejs.arenas a WHERE a.owner_id=:owner AND "
        "a.active=1 AND a.lobby_hidden=0 "
        "ORDER BY a.is_public ASC,a.created_at DESC,a.id DESC "
        "LIMIT 20 OFFSET :offset",
        owner=owner,
        now=crud.now(),
        offset=(page - 1) * 20,
    )
    count = await crud.one(
        "SELECT COUNT(*) AS n FROM quakejs.arenas WHERE "
        "owner_id=:owner AND active=1 AND lobby_hidden=0",
        owner=owner,
    )
    scoreboard = await crud.all_rows(
        "SELECT p.ln_address AS address,SUM(p.amount) AS sats,COUNT(*) AS kills "
        "FROM quakejs.payouts p JOIN quakejs.arenas a ON a.id=p.arena_id "
        "WHERE a.owner_id=:owner AND p.status='paid' "
        "GROUP BY p.ln_address ORDER BY sats DESC,address ASC LIMIT 10",
        owner=owner,
    )
    return {
        "games": [crud.public_arena(game, game["players_count"]) for game in games],
        "total": count["n"],
        "page": page,
        "maps": MAPS,
        "adminHaircut": setting["haircut"],
        "canCreate": bool(setting["enabled"]),
        "maxPlayers": MAX_PLAYERS,
        "scoreboard": scoreboard,
    }


async def expire_game(arena_id):
    async with crud.transaction() as tx:
        arena = await tx.lock_arena(arena_id, active=False)
        if not arena["is_public"] or not arena["active"]:
            return False
        playing = await tx.one(
            "SELECT COUNT(*) AS n FROM quakejs.lives WHERE "
            "arena_id=:id AND status='alive' AND "
            "connected_until>:now",
            id=arena_id,
            now=crud.now(),
        )
        if playing["n"]:
            await tx.execute(
                "UPDATE quakejs.arenas SET idle_since=NULL WHERE id=:id", id=arena_id
            )
            return False
        if arena["idle_since"] is None:
            await tx.execute(
                "UPDATE quakejs.arenas SET idle_since=:now WHERE id=:id",
                now=crud.now(),
                id=arena_id,
            )
            return False
        if arena["idle_since"] > crud.now() - 600:
            return False
        outstanding = await tx.one(
            "SELECT COUNT(*) AS n FROM quakejs.entries WHERE arena_id=:id AND "
            "(remaining>0 OR (status IN ('creating','pending') AND expires_at>:now))",
            id=arena_id,
            now=crud.now(),
        )
        if outstanding["n"]:
            return False
        # Keep financial records and pending payouts; removal is from admission
        # and the public listing. No live engine is started by this operation.
        await tx.execute(
            "UPDATE quakejs.arenas SET active=0,lobby_hidden=1 WHERE id=:id",
            id=arena_id,
        )
    changed(arena["owner_id"])
    return True


async def maintenance():
    cursor = ""
    while True:
        try:
            rows = await crud.all_rows(
                "SELECT id FROM quakejs.arenas WHERE is_public=1 "
                "AND active=1 AND id>:cursor ORDER BY id LIMIT 100",
                cursor=cursor,
            )
            cursor = rows[-1]["id"] if rows else ""
            for row in rows:
                try:
                    await expire_game(row["id"])
                except Exception:
                    logger.warning("QuakeJS deferred public game expiration.")
        except Exception:
            logger.warning("QuakeJS will retry lobby maintenance.")
        await asyncio.sleep(5)
