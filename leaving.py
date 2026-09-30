"""Retire a player session before queuing refunds; never send inside a DB lock."""

from . import crud
from .models import PublicError


async def request_leave(arena_id, token):
    async with crud.transaction() as tx:
        await tx.lock_arena(arena_id, active=False)
        player = await crud.participant(tx, arena_id, token)
        if not player:
            raise PublicError("A paid player session is required.")
        if not player["leave_state"]:
            funded = await tx.one(
                "SELECT id FROM quakejs.entries WHERE player_id=:id "
                "AND status='paid' LIMIT 1",
                id=player["id"],
            )
            if not funded:
                raise PublicError("There are no paid lives to refund.")
            await tx.execute(
                "UPDATE quakejs.participants SET leave_state='leaving' WHERE id=:id",
                id=player["id"],
            )
            player["leave_state"] = "leaving"
        return player


def refund_amounts(entry, arena):
    creator = arena["creator_haircut"] if arena["is_public"] else 0
    value = entry["remaining"] * entry["amount"]
    return value * (100 - entry["haircut"] - creator) // 500, value * creator // 500


async def refund_entry(tx, entry, arena):
    """Caller owns the arena write lock and has revoked any engine admission."""
    if entry["status"] != "paid" or entry["remaining"] <= 0:
        return
    amount, creator = refund_amounts(entry, arena)
    # Unique entry identity plus zeroing its balance in this same transaction
    # makes retries and duplicate invoice notifications unable to refund twice.
    await tx.execute(
        "INSERT INTO quakejs.refund_payouts "
        "(id,arena_id,victim_id,player_id,wallet_id,ln_address,amount,status,"
        "created_at,updated_at) "
        "VALUES(:id,:arena,:entry,:player,:wallet,:address,:amount,:status,:now,:now)",
        id=crud.uid(),
        arena=entry["arena_id"],
        entry=entry["id"],
        player=entry["player_id"],
        wallet=entry["wallet_id"],
        address=entry["ln_address"],
        amount=amount,
        status="queued" if amount else "withheld",
        now=crud.now(),
    )
    if creator:
        await tx.execute(
            "INSERT INTO quakejs.creator_payouts "
            "(id,arena_id,victim_id,killer_id,wallet_id,ln_address,amount,status,"
            "created_at,updated_at) "
            "VALUES(:id,:arena,:entry,'',:wallet,:address,:amount,'queued',:now,:now)",
            id=crud.uid(),
            arena=entry["arena_id"],
            entry=entry["id"],
            wallet=entry["wallet_id"],
            address=arena["creator_ln_address"],
            amount=creator,
            now=crud.now(),
        )
    await tx.execute(
        "UPDATE quakejs.entries SET remaining=0 WHERE id=:id", id=entry["id"]
    )


async def finish_leave(player_id, revoked_run=""):
    async with crud.transaction() as tx:
        player = await tx.one(
            "SELECT * FROM quakejs.participants WHERE id=:id", id=player_id
        )
        arena = await tx.lock_arena(player["arena_id"], active=False)
        player = await tx.one(
            "SELECT * FROM quakejs.participants WHERE id=:id", id=player_id
        )
        if player["leave_state"] == "left":
            return
        if player["leave_state"] != "leaving":
            raise PublicError("A leave request is required.")
        unsafe = await tx.one(
            "SELECT id FROM quakejs.lives WHERE player_id=:id AND status='alive' "
            "AND run_id!=:run LIMIT 1",
            id=player_id,
            run=revoked_run,
        )
        if unsafe:
            raise PublicError("Your game is recovering. The leave request is saved.")
        entries = await tx.all(
            "SELECT * FROM quakejs.entries WHERE player_id=:id "
            "AND status='paid' AND remaining>0",
            id=player_id,
        )
        for entry in entries:
            await refund_entry(tx, entry, arena)
        await tx.execute(
            "UPDATE quakejs.lives SET status='refunded',connected_until=0,died_at=:now "
            "WHERE player_id=:id AND status IN ('alive','left')",
            id=player_id,
            now=crud.now(),
        )
        await tx.execute(
            "UPDATE quakejs.participants SET leave_state='left' WHERE id=:id",
            id=player_id,
        )


async def refund_state(tx, player, arena):
    entries = await tx.all(
        "SELECT * FROM quakejs.entries WHERE player_id=:id "
        "AND status='paid' AND remaining>0",
        id=player["id"],
    )
    rows = await tx.all(
        "SELECT status,SUM(amount) AS n FROM quakejs.refund_payouts "
        "WHERE player_id=:id GROUP BY status",
        id=player["id"],
    )
    totals = {row["status"]: row["n"] for row in rows}
    return {
        "estimate": sum(refund_amounts(entry, arena)[0] for entry in entries),
        "paid": totals.get("paid", 0),
        "pending": sum(
            totals.get(s, 0) for s in ("queued", "prepared", "sending", "pending")
        ),
        "failed": totals.get("failed", 0),
    }
