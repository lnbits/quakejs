import asyncio

from lnbits.extensions.quakejs import crud
from lnbits.extensions.quakejs.migrations import m007_native_arena_ledger
from lnbits.extensions.quakejs.models import MAPS, ArenaInput
from lnbits.extensions.quakejs.server import manager


async def main():
    await m007_native_arena_ledger(crud.db)
    await crud.save_settings("test-owner", "test-wallet", True, 5)
    manager.directory.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(manager.loop())
    await asyncio.sleep(0.1)
    try:
        for map_item in MAPS:
            arena = await crud.create_arena(
                "test-owner", ArenaInput(map=map_item["value"])
            )
            match = await manager.ensure(arena["id"])
            print("READY", map_item["value"], match.run, flush=True)
            await match.stop()
            manager.matches.pop(arena["id"], None)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


asyncio.run(main())
