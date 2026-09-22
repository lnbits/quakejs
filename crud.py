"""Explicit transactions; never use Connection.execute's per-statement commit."""

import hashlib
import secrets
from contextlib import asynccontextmanager
from time import time, time_ns

from sqlalchemy import text

from lnbits.db import Database

from .models import MAX_PLAYERS, prize_per_kill

db = Database("ext_quakejs")


def uid():
    return secrets.token_hex(24)


def now():
    return int(time())


def token_hash(token: str):
    if (
        not isinstance(token, str)
        or len(token) != 48
        or any(c not in "0123456789abcdef" for c in token)
    ):
        raise ValueError("Invalid player session.")
    return hashlib.sha256(token.encode()).hexdigest()


class Tx:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, sql, **values):
        return await self.connection.execute(text(sql), values)

    async def one(self, sql, **values):
        result = await self.execute(sql, **values)
        row = result.mappings().first()
        return dict(row) if row else None

    async def all(self, sql, **values):
        result = await self.execute(sql, **values)
        return [dict(row) for row in result.mappings().all()]

    async def lock_arena(self, arena_id, active=True):
        row = await self.one(
            "UPDATE quakejs.arenas SET revision=revision+1 WHERE id=:id RETURNING *",
            id=arena_id,
        )
        if not row or (active and not row["active"]):
            raise ValueError("Arena is unavailable.")
        return row


@asynccontextmanager
async def transaction(database=None):
    database = database or db
    async with database.connect() as connection:
        raw = connection.conn
        if database.type == "SQLITE":
            # LNbits attaches the same database file under its extension name.
            # BEGIN IMMEDIATE locks both aliases and deadlocks against itself.
            # Acquire the attached database's write lock before any reads.
            await raw.exec_driver_sql("BEGIN")
            await raw.exec_driver_sql(
                "UPDATE quakejs.settings_native SET updated_at=updated_at WHERE id=''"
            )
        else:
            await raw.begin()
        try:
            yield Tx(raw)
            await raw.commit()
        except BaseException:
            await raw.rollback()
            raise


async def one(sql, **values):
    async with db.connect() as connection:
        return await Tx(connection.conn).one(sql, **values)


async def all_rows(sql, **values):
    async with db.connect() as connection:
        return await Tx(connection.conn).all(sql, **values)


async def settings_for(owner):
    return await one("SELECT * FROM quakejs.settings_native WHERE id=:id", id=owner)


async def save_settings(owner, wallet_id, enabled, haircut):
    async with transaction() as tx:
        await tx.execute(
            "INSERT INTO "
            "quakejs.settings_native(id,wallet_id,enabled,haircut,updated_at)"
            "\n            VALUES(:id,:wallet,:enabled,:fee,:now) ON "
            "CONFLICT(id) DO UPDATE SET\n            "
            "wallet_id=:wallet,enabled=:enabled,haircut=:fee,updated_at=:now",
            id=owner,
            wallet=wallet_id,
            enabled=int(enabled),
            fee=haircut,
            now=now(),
        )
    return await settings_for(owner)


async def create_arena(owner, data):
    async with transaction() as tx:
        settings = await tx.one(
            "SELECT * FROM quakejs.settings_native WHERE id=:id", id=owner
        )
        if not settings or not settings["enabled"]:
            raise ValueError("Enable QuakeJS and select a wallet first.")
        arena_id = uid()
        await tx.execute(
            "INSERT INTO "
            "quakejs.arenas(id,owner_id,wallet_id,name,map,entry_amount,haircut,created_at)"
            "\n            "
            "VALUES(:id,:owner,:wallet,:name,:map,:amount,:fee,:now)",
            id=arena_id,
            owner=owner,
            wallet=settings["wallet_id"],
            name=data.name,
            map=data.map,
            amount=data.join_amount,
            fee=settings["haircut"],
            now=now(),
        )
        return await tx.one("SELECT * FROM quakejs.arenas WHERE id=:id", id=arena_id)


async def participant(tx, arena_id, token):
    return await tx.one(
        "SELECT * FROM quakejs.participants WHERE arena_id=:arena AND "
        "token_hash=:token",
        arena=arena_id,
        token=token_hash(token),
    )


async def reserve_entry(arena_id, token, data):
    """Reserve an invoice slot without holding a transaction during a wallet call."""
    async with transaction() as tx:
        arena = await tx.lock_arena(arena_id)
        setting = await tx.one(
            "SELECT * FROM quakejs.settings_native WHERE id=:id", id=arena["owner_id"]
        )
        if not setting or not setting["enabled"]:
            raise ValueError("This arena is not accepting new entries.")
        player = await participant(tx, arena_id, token)
        if player:
            existing = await tx.one(
                "SELECT * FROM quakejs.entries WHERE player_id=:id AND nonce=:nonce",
                id=player["id"],
                nonce=data.nonce,
            )
            if existing:
                return existing
            remaining = await tx.one(
                "SELECT SUM(remaining) AS n FROM quakejs.entries WHERE player_id=:id",
                id=player["id"],
            )
            if remaining["n"]:
                raise ValueError(
                    "You already have unused lives. Respawn instead of paying again."
                )
            existing = await tx.one(
                """SELECT * FROM quakejs.entries WHERE player_id=:id
                AND status IN ('creating','pending') AND expires_at>:now""",
                id=player["id"],
                now=now(),
            )
            if existing:
                return existing
        occupied = await tx.one(
            "SELECT COUNT(DISTINCT player_id) AS n FROM (\n            SELECT "
            "player_id FROM quakejs.lives WHERE arena_id=:arena AND "
            "status='alive'\n            UNION SELECT "
            "player_id FROM quakejs.entries WHERE arena_id=:arena AND status IN"
            " ('creating','pending') AND expires_at>:now\n            ) AS "
            "occupied",
            arena=arena_id,
            now=now(),
        )
        if occupied["n"] >= MAX_PLAYERS:
            raise ValueError("This arena is full. Try again when a slot opens.")
        if not player:
            player = {"id": uid()}
            await tx.execute(
                "INSERT INTO "
                "quakejs.participants(id,arena_id,token_hash,name,ln_address,created_at)"
                "\n                VALUES(:id,:arena,:token,:name,:address,:now)",
                id=player["id"],
                arena=arena_id,
                token=token_hash(token),
                name=data.name,
                address=data.ln_address,
                now=now(),
            )
        entry_id = uid()
        await tx.execute(
            "INSERT INTO "
            "quakejs.entries(id,arena_id,player_id,nonce,wallet_id,amount,haircut,ln_address,status,created_at,expires_at)"
            "\n            "
            "VALUES(:id,:arena,:player,:nonce,:wallet,:amount,:fee,:address,'creating',:now,:expires)",
            id=entry_id,
            arena=arena_id,
            player=player["id"],
            nonce=data.nonce,
            wallet=arena["wallet_id"],
            amount=arena["entry_amount"],
            fee=arena["haircut"],
            address=data.ln_address,
            now=time_ns(),
            expires=now() + 300,
        )
        entry = await tx.one("SELECT * FROM quakejs.entries WHERE id=:id", id=entry_id)
        entry["_new"] = True
        return entry


async def record_invoice(entry_id, payment):
    async with transaction() as tx:
        await tx.execute(
            "UPDATE quakejs.entries SET "
            "payment_hash=:hash,bolt11=:invoice,status='pending'\n            "
            "WHERE id=:id AND status='creating' AND payment_hash IS NULL",
            id=entry_id,
            hash=payment.payment_hash,
            invoice=payment.bolt11,
        )


async def settle_entry(payment):
    if (
        not payment.success
        or not payment.is_in
        or payment.extra.get("tag") != "quakejs"
    ):
        return None
    entry_id = payment.extra.get("quakejs_entry")
    async with transaction() as tx:
        entry = await tx.one("SELECT * FROM quakejs.entries WHERE id=:id", id=entry_id)
        if not entry:
            return None
        await tx.lock_arena(entry["arena_id"], active=False)
        entry = await tx.one("SELECT * FROM quakejs.entries WHERE id=:id", id=entry_id)
        # Only the core payment record supplies wallet, amount and settlement.
        if (
            payment.wallet_id != entry["wallet_id"]
            or payment.amount != entry["amount"] * 1000
        ):
            raise ValueError("Entry payment does not match its wallet and amount.")
        if entry["payment_hash"] and entry["payment_hash"] != payment.payment_hash:
            raise ValueError("Entry payment hash mismatch.")
        if entry["status"] == "paid":
            return entry["arena_id"]
        if entry["status"] not in ("creating", "pending"):
            raise ValueError("Entry payment requires owner review.")
        await tx.execute(
            "UPDATE quakejs.entries SET "
            "status='paid',remaining=5,payment_hash=:hash,bolt11=:invoice"
            "\n            WHERE id=:id AND status IN ('creating','pending')",
            id=entry_id,
            hash=payment.payment_hash,
            invoice=payment.bolt11,
        )
        # A payment may have settled before expiry but its notification arrived
        # after the owner closed the arena. Honor those already purchased lives.
        await tx.execute(
            "UPDATE quakejs.arenas SET active=1 WHERE id=:id", id=entry["arena_id"]
        )
        await tx.execute(
            "UPDATE quakejs.participants SET ln_address=:address WHERE id=:id",
            id=entry["player_id"],
            address=entry["ln_address"],
        )
        return entry["arena_id"]


async def consume_death(run_id, sequence, victim_id, killer_id):
    """Only the private engine journal calls this; no public kill-report API."""
    async with transaction() as tx:
        run = await tx.one("SELECT * FROM quakejs.runs WHERE id=:id", id=run_id)
        if not run:
            raise ValueError("Unknown server run.")
        await tx.lock_arena(run["arena_id"], active=False)
        run = await tx.one("SELECT * FROM quakejs.runs WHERE id=:id", id=run_id)
        if sequence <= run["event_sequence"]:
            return run["arena_id"]
        if sequence != run["event_sequence"] + 1:
            raise ValueError("Game journal has a gap.")
        victim = await tx.one("SELECT * FROM quakejs.lives WHERE id=:id", id=victim_id)
        if (
            not victim
            or victim["run_id"] != run_id
            or victim["arena_id"] != run["arena_id"]
        ):
            raise ValueError("Death does not belong to this server run.")
        if victim["status"] != "dead":
            entry = await tx.one(
                "SELECT * FROM quakejs.entries WHERE id=:id", id=victim["entry_id"]
            )
            if not entry or entry["remaining"] <= 0:
                raise ValueError("Consumed life has no funded entry.")
            await tx.execute(
                "UPDATE quakejs.entries SET remaining=remaining-1 WHERE id=:id "
                "AND remaining>0",
                id=entry["id"],
            )
            await tx.execute(
                "UPDATE quakejs.lives SET "
                "status='dead',died_at=:now,killer_id=:killer,connected_until=0"
                "\n                WHERE id=:id",
                id=victim_id,
                now=now(),
                killer=killer_id,
            )
            killer = await tx.one(
                "SELECT * FROM quakejs.lives WHERE id=:id AND arena_id=:arena "
                "AND run_id=:run",
                id=killer_id,
                arena=run["arena_id"],
                run=run_id,
            )
            if (
                killer
                and killer_id != victim_id
                and killer["player_id"] != victim["player_id"]
            ):
                destination = await tx.one(
                    "SELECT ln_address FROM quakejs.entries WHERE id=:id",
                    id=killer["entry_id"],
                )
                # Exact integer arithmetic: floor((entry / 5) * (1 - fee/100)).
                amount = prize_per_kill(entry["amount"], entry["haircut"])
                await tx.execute(
                    "INSERT INTO "
                    "quakejs.payouts(id,arena_id,victim_id,killer_id,player_id,wallet_id,ln_address,amount,status,created_at,updated_at)"
                    "\n                    "
                    "VALUES(:id,:arena,:victim,:killer,:player,:wallet,:address,:amount,:status,:now,:now)"
                    "\n                    ON CONFLICT(victim_id) DO NOTHING",
                    id=uid(),
                    arena=run["arena_id"],
                    victim=victim_id,
                    killer=killer_id,
                    player=killer["player_id"],
                    wallet=entry["wallet_id"],
                    address=destination["ln_address"],
                    amount=amount,
                    status="queued" if amount else "withheld",
                    now=now(),
                )
        await tx.execute(
            "UPDATE quakejs.runs SET event_sequence=:seq WHERE id=:id",
            id=run_id,
            seq=sequence,
        )
        return run["arena_id"]


async def allocate_life(arena_id, token, run_id):
    async with transaction() as tx:
        arena = await tx.lock_arena(arena_id)
        if arena["run_id"] != run_id or arena["lease_until"] <= now():
            raise ValueError("Game server is restarting. Please retry.")
        player = await participant(tx, arena_id, token)
        if not player:
            raise ValueError("A paid entry is required.")
        current = await tx.one(
            "SELECT * FROM quakejs.lives WHERE player_id=:id AND status IN "
            "('alive','left') ORDER BY created_at DESC LIMIT 1",
            id=player["id"],
        )
        if current and current["status"] == "alive" and current["run_id"] == run_id:
            return current
        entry = await tx.one(
            "SELECT * FROM quakejs.entries WHERE player_id=:id AND "
            "status='paid' AND remaining>0 ORDER BY created_at LIMIT 1",
            id=player["id"],
        )
        if not entry:
            raise ValueError("No lives left. Pay for another five lives.")
        occupied = await tx.all(
            "SELECT slot FROM quakejs.lives WHERE arena_id=:id AND " "status='alive'",
            id=arena_id,
        )
        used = {r["slot"] for r in occupied}
        slot = next((s for s in range(1, MAX_PLAYERS + 1) if s not in used), None)
        if slot is None:
            raise ValueError("Arena full. Your remaining lives are saved.")
        if current:
            await tx.execute(
                "UPDATE quakejs.lives SET "
                "status='alive',run_id=:run,slot=:slot,connected_until=:until "
                "WHERE id=:id",
                run=run_id,
                slot=slot,
                until=now() + 30,
                id=current["id"],
            )
            life_id = current["id"]
        else:
            life_id = uid()
            await tx.execute(
                "INSERT INTO "
                "quakejs.lives(id,arena_id,player_id,entry_id,run_id,status,slot,connected_until,created_at)"
                "\n                "
                "VALUES(:id,:arena,:player,:entry,:run,'alive',:slot,:until,:now)",
                id=life_id,
                arena=arena_id,
                player=player["id"],
                entry=entry["id"],
                run=run_id,
                slot=slot,
                until=now() + 30,
                now=time_ns(),
            )
        return await tx.one("SELECT * FROM quakejs.lives WHERE id=:id", id=life_id)


def public_arena(arena, count=0):
    return {
        "id": arena["id"],
        "name": arena["name"],
        "map": arena["map"],
        "joinAmount": arena["entry_amount"],
        "haircut": arena["haircut"],
        "prizePerKill": prize_per_kill(arena["entry_amount"], arena["haircut"]),
        "playersCount": count,
        "maxPlayers": MAX_PLAYERS,
        "status": "active" if arena["active"] else "closed",
        "createdAt": arena["created_at"],
    }


async def public_state(arena_id, token=""):
    async with db.connect() as connection:
        tx = Tx(connection.conn)
        arena = await tx.one("SELECT * FROM quakejs.arenas WHERE id=:id", id=arena_id)
        if not arena:
            raise ValueError("Arena not found.")
        roster = await tx.all(
            "SELECT l.id,p.name FROM quakejs.lives l JOIN quakejs.participants "
            "p ON p.id=l.player_id\n            WHERE l.arena_id=:id AND "
            "l.status='alive' AND l.connected_until>:now",
            id=arena_id,
            now=now(),
        )
        result = {
            "game": public_arena(arena, len(roster)),
            "players": [dict(r, status="alive") for r in roster],
            "player": None,
            "won": 0,
            "pendingWinnings": 0,
            "failedWinnings": 0,
            "canJoin": bool(arena["active"]) and len(roster) < MAX_PLAYERS,
        }
        player = await participant(tx, arena_id, token) if token else None
        if not player:
            return result
        entry = await tx.one(
            "SELECT * FROM quakejs.entries WHERE player_id=:id ORDER BY "
            "CASE WHEN status='paid' AND remaining>0 THEN 0 ELSE 1 END, "
            "created_at DESC,id DESC LIMIT 1",
            id=player["id"],
        )
        life = await tx.one(
            "SELECT * FROM quakejs.lives WHERE player_id=:id ORDER BY "
            "created_at DESC,id DESC LIMIT 1",
            id=player["id"],
        )
        balance = await tx.one(
            "SELECT COALESCE(SUM(remaining),0) AS n FROM quakejs.entries WHERE "
            "player_id=:id AND status='paid'",
            id=player["id"],
        )
        winnings = await tx.all(
            "SELECT status, SUM(amount) AS n FROM quakejs.payouts WHERE "
            "player_id=:id GROUP BY status",
            id=player["id"],
        )
        amounts = {row["status"]: row["n"] for row in winnings}
        result["won"] = amounts.get("paid", 0)
        result["pendingWinnings"] = sum(
            amounts.get(status, 0)
            for status in ("queued", "prepared", "sending", "pending")
        )
        result["failedWinnings"] = amounts.get("failed", 0)
        if not entry or entry["status"] != "paid":
            if entry and entry["expires_at"] <= now():
                result["invoiceExpired"] = True
            elif entry and entry["status"] == "pending":
                result["invoice"] = {
                    "paymentHash": entry["payment_hash"],
                    "paymentRequest": entry["bolt11"],
                    "expiresAt": entry["expires_at"],
                }
            return result
        state = "dead" if not balance["n"] else "left"
        if life and life["status"] == "alive" and life["connected_until"] > now():
            state = "alive"
        elif life and life["status"] == "dead":
            state = "dead"
        result["player"] = {
            "id": life["id"] if life else player["id"],
            "name": player["name"],
            "status": state,
            "livesRemaining": balance["n"],
            "entryAmount": entry["amount"],
            "paidAmount": entry["amount"] / 5,
            "slot": life["slot"] if life else 0,
            "autoAdmit": balance["n"] > 0
            and (not life or (life["entry_id"] != entry["id"])),
            "payoutStatus": "",
            "killerId": "",
            "payoutAmount": 0,
        }
        return result
