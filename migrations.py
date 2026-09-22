"""Separate tables preserve the prior WASM records in ext_quakejs.

The WASM schema reached version 6. Starting at 7 works for both new installs
and an explicit replacement of that extension; old financial rows are untouched.
"""


async def m007_native_arena_ledger(db):
    tables = [
        """settings_native (
            id TEXT PRIMARY KEY, wallet_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0, haircut INTEGER NOT NULL,
            updated_at BIGINT NOT NULL
        )""",
        """arenas (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, wallet_id TEXT NOT NULL,
            name TEXT NOT NULL, map TEXT NOT NULL, entry_amount INTEGER NOT NULL,
            haircut INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            revision INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL,
            run_id TEXT NOT NULL DEFAULT '', worker TEXT NOT NULL DEFAULT '',
            lease_until BIGINT NOT NULL DEFAULT 0
        )""",
        """participants (
            id TEXT PRIMARY KEY, arena_id TEXT NOT NULL, token_hash TEXT NOT NULL,
            name TEXT NOT NULL, ln_address TEXT NOT NULL, created_at BIGINT NOT NULL,
            UNIQUE(arena_id, token_hash)
        )""",
        """entries (
            id TEXT PRIMARY KEY, arena_id TEXT NOT NULL, player_id TEXT NOT NULL,
            nonce TEXT NOT NULL, wallet_id TEXT NOT NULL, amount INTEGER NOT NULL,
            haircut INTEGER NOT NULL, ln_address TEXT NOT NULL,
            status TEXT NOT NULL, payment_hash TEXT UNIQUE, bolt11 TEXT,
            remaining INTEGER NOT NULL DEFAULT 0 CHECK(remaining BETWEEN 0 AND 5),
            created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
            UNIQUE(player_id, nonce)
        )""",
        """lives (
            id TEXT PRIMARY KEY, arena_id TEXT NOT NULL, player_id TEXT NOT NULL,
            entry_id TEXT NOT NULL, run_id TEXT NOT NULL,
            status TEXT NOT NULL, slot INTEGER NOT NULL,
            connected_until BIGINT NOT NULL DEFAULT 0,
            created_at BIGINT NOT NULL, died_at BIGINT,
            killer_id TEXT NOT NULL DEFAULT ''
        )""",
        """runs (
            id TEXT PRIMARY KEY, arena_id TEXT NOT NULL, worker TEXT NOT NULL,
            created_at BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
            event_sequence BIGINT NOT NULL DEFAULT 0
        )""",
        "payouts (\n            id TEXT PRIMARY KEY, arena_id TEXT NOT NULL, "
        "victim_id TEXT NOT NULL UNIQUE,\n            killer_id TEXT NOT NULL, "
        "player_id TEXT NOT NULL,\n            wallet_id TEXT NOT NULL, "
        "ln_address TEXT NOT NULL,\n            amount INTEGER NOT NULL "
        "CHECK(amount >= 0), status TEXT NOT NULL,\n            bolt11 TEXT, "
        "payment_hash TEXT UNIQUE, attempts INTEGER NOT NULL DEFAULT 0,"
        "\n            next_attempt BIGINT NOT NULL DEFAULT 0, claimed_until "
        "BIGINT NOT NULL DEFAULT 0,\n            claim TEXT NOT NULL DEFAULT '',"
        " error TEXT NOT NULL DEFAULT '',\n            created_at BIGINT NOT "
        "NULL, updated_at BIGINT NOT NULL\n        )",
    ]
    for definition in tables:
        await db.execute(f"CREATE TABLE IF NOT EXISTS quakejs.{definition}")
    for name, table, columns in [
        ("arena_owner_idx", "arenas", "owner_id"),
        ("entry_status_idx", "entries", "status, expires_at"),
        ("entry_player_idx", "entries", "player_id, remaining"),
        ("life_arena_idx", "lives", "arena_id, status"),
        ("life_player_idx", "lives", "player_id, status"),
        ("payout_work_idx", "payouts", "status, next_attempt"),
    ]:
        # SQLite qualifies the index, PostgreSQL qualifies the table.
        target = table if db.type == "SQLITE" else "quakejs." + table
        index = "quakejs." + name if db.type == "SQLITE" else name
        await db.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {target} ({columns})")


async def m008_retry_unprepared_payouts(db):
    # The old datetime/float expiry comparison rejected valid invoices before
    # persisting them. No payment could have been sent from these rows.
    # Never reset prepared, sending, pending or any row with a stored invoice.
    await db.execute(
        "UPDATE quakejs.payouts SET status='queued',attempts=0,next_attempt=0,error='' "
        "WHERE status IN ('queued','failed') AND payment_hash IS NULL "
        "AND bolt11 IS NULL AND error='Unable to obtain a valid payout invoice.'"
    )
