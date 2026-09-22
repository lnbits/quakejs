import asyncio

from fastapi import APIRouter
from loguru import logger

from lnbits.task_manager import task_manager

from .crud import db, settle_entry
from .lobby import maintenance
from .payments import payout_loop, reconcile_entries
from .server import manager
from .views import router

quakejs_ext = APIRouter(prefix="/quakejs", tags=["QuakeJS"])
quakejs_ext.include_router(router)
quakejs_static_files = [{"path": "/quakejs/static", "name": "quakejs_static"}]
_tasks = []
_listener = None


async def paid(payment):
    try:
        arena = await settle_entry(payment)
        if arena:
            await manager.notify(arena)
    except Exception:
        logger.warning("QuakeJS deferred an invoice event for reconciliation.")


def quakejs_start():
    global _listener
    if _tasks:
        return
    _listener = task_manager.register_invoice_listener(paid, "quakejs")
    _tasks.extend(
        asyncio.create_task(job)
        for job in (
            manager.loop(),
            payout_loop(manager.notify),
            reconcile_entries(manager.notify),
            maintenance(),
        )
    )


async def quakejs_stop():
    global _listener
    for task in _tasks:
        task.cancel()
    if _listener:
        if _listener in task_manager.tasks:
            task_manager.cancel_task(_listener)
        else:
            _listener.task.cancel()
        await asyncio.gather(_listener.task, return_exceptions=True)
        _listener = None
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()


__all__ = ["db", "quakejs_ext", "quakejs_static_files"]
