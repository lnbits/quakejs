"""Durable payout outbox. No game task waits for a Lightning payment.

Persist the exact invoice before sending. After an ambiguous send, reconcile
that payment hash and NEVER obtain another invoice or resend automatically.
"""

import asyncio
import re
from time import time

import httpx
from anyio import fail_after
from bolt11 import decode
from lnurl import LnurlResponseException
from loguru import logger

from lnbits.core.crud import get_standalone_payment
from lnbits.core.services import create_invoice, get_pr_from_lnurl, pay_invoice
from lnbits.core.services.payments import check_payment_status

from . import crud
from .models import PublicError

PAYOUT_INVOICE_TIMEOUT = 15
ENTRY_INVOICE_TIMEOUT = 20


def invoice_error_detail(error):
    """Report known failures without logging provider URLs, invoices or secrets."""
    if isinstance(error, (TimeoutError, httpx.TimeoutException)) or isinstance(
        error.__cause__, httpx.TimeoutException
    ):
        return "Lightning address provider timed out."
    if isinstance(error, httpx.RequestError):
        return "Could not connect to the Lightning address provider."
    message = str(error)
    if isinstance(error, LnurlResponseException):
        limits = re.fullmatch(
            r"Amount (\d{1,18}) not in range (\d{1,18}) - (\d{1,18})", message
        )
        if limits:
            amount, minimum, maximum = limits.groups()
            return (
                f"Provider amount limit: requested {amount} msat; "
                f"allowed {minimum}-{maximum} msat."
            )
        known = {
            "LNURL request failed.",
            "LNURL request target could not be resolved.",
            "LNURL redirect was not allowed.",
            "LNURL request target resolves to a private or non-global IP address.",
            "LNURL request target is not allowed over HTTP.",
            "Invalid LNURL response.",
            "Invalid LNURL response. Expected LnurlPayResponse.",
            "Invalid LNURL-pay response.",
            "Invalid invoice in LNURL response.",
            "LNURL service returned an invalid invoice amount.",
            "LNURL response is too large.",
        }
        if message in known:
            return message
        return "Lightning address provider rejected the LNURL request."
    if isinstance(error, ValueError) and message in {
        "Invalid payout invoice.",
        "Payout invoice was already used.",
    }:
        return message
    return "Unable to obtain a valid payout invoice."


async def create_entry(arena_id, token, data):
    entry = await crud.reserve_entry(arena_id, token, data)
    if entry.get("_new"):
        try:
            with fail_after(ENTRY_INVOICE_TIMEOUT):
                payment = await create_invoice(
                    wallet_id=entry["wallet_id"],
                    amount=entry["amount"],
                    memo="QuakeJS: five lives",
                    expiry=300,
                    extension="quakejs",
                    extra={"tag": "quakejs", "quakejs_entry": entry["id"]},
                    external_id=entry["id"],
                )
            await crud.record_invoice(entry["id"], payment)
            await crud.settle_entry(payment)
        except Exception:
            # Creating an invoice can succeed upstream before the call fails.
            # Preserve the reserved entry for reconciliation, not a second invoice.
            logger.warning(
                "QuakeJS entry invoice needs reconciliation: {}", entry["id"]
            )
            raise PublicError(
                "Invoice creation is pending. Retry shortly; do not create "
                "another entry."
            ) from None
        entry = await crud.one(
            "SELECT * FROM quakejs.entries WHERE id=:id", id=entry["id"]
        )
    if entry["expires_at"] <= crud.now() and entry["status"] != "paid":
        raise PublicError("Invoice expired. Create a new invoice for five lives.")
    if not entry.get("bolt11"):
        raise PublicError("Your invoice is being prepared. Please retry shortly.")
    # Invoice creation awaits a provider outside the arena lock. An owner may
    # close the game meanwhile; retain the invoice for reconciliation, not display.
    arena = await crud.one(
        "SELECT active FROM quakejs.arenas WHERE id=:id", id=arena_id
    )
    if not arena or not arena["active"]:
        raise PublicError("This arena has been closed. Do not pay its invoice.")
    return {
        "playerToken": token,
        "paymentHash": entry["payment_hash"],
        "paymentRequest": entry["bolt11"],
        "expiresAt": entry["expires_at"],
        "nonce": entry["nonce"],
    }


def outbox_table(row):
    table = row.get("_table", "payouts")
    if table not in ("payouts", "creator_payouts"):
        raise ValueError("Invalid payout kind.")
    return table


async def claim_payout():
    claim = crud.uid()
    async with crud.transaction() as tx:
        candidate = await tx.one(
            "SELECT id,kind FROM ("
            "SELECT id,created_at,next_attempt,'payouts' AS kind "
            "FROM quakejs.payouts WHERE "
            "status IN ('queued','prepared','sending','pending') "
            "AND next_attempt<=:now AND claimed_until<=:now "
            "UNION ALL SELECT id,created_at,next_attempt,'creator_payouts' AS "
            "kind FROM quakejs.creator_payouts WHERE "
            "status IN ('queued','prepared','sending','pending') "
            "AND next_attempt<=:now AND claimed_until<=:now"
            ") AS ready ORDER BY next_attempt,created_at,id LIMIT 1",
            now=crud.now(),
        )
        if not candidate:
            return None
        table = outbox_table({"_table": candidate["kind"]})
        row = await tx.one(
            f"UPDATE quakejs.{table} SET claim=:claim,claimed_until=:until "  # noqa: S608 - allowlisted table
            "WHERE id=:id AND claimed_until<=:now RETURNING *",
            id=candidate["id"],
            claim=claim,
            until=crud.now() + 90,
            now=crud.now(),
        )
        if row:
            row["_table"] = table
        return row


async def update_payout(row, status, **values):
    allowed = {"bolt11", "payment_hash", "attempts", "next_attempt", "error"}
    if not set(values) <= allowed:
        raise ValueError("Invalid payout update.")
    table = outbox_table(row)
    fields = {key: values.get(key, row[key]) for key in allowed}
    async with crud.transaction() as tx:
        if status == "prepared" and fields["payment_hash"]:
            await tx.execute(
                "INSERT INTO "
                "quakejs.payout_invoice_claims(payment_hash,payout"
                "_id) VALUES(:hash,:id) "
                "ON CONFLICT(payment_hash) DO NOTHING",
                hash=fields["payment_hash"],
                id=row["id"],
            )
            owner = await tx.one(
                "SELECT payout_id FROM "
                "quakejs.payout_invoice_claims WHERE "
                "payment_hash=:hash",
                hash=fields["payment_hash"],
            )
            if owner["payout_id"] != row["id"]:
                raise ValueError("Payout invoice was already used.")
        result = await tx.execute(
            f"UPDATE quakejs.{table} SET status=:status,updated_at=:now,"  # noqa: S608 - allowlisted table
            "bolt11=:bolt11,payment_hash=:payment_hash,attempts=:attempts,"
            "next_attempt=:next_attempt,error=:error WHERE id=:id AND claim=:claim",
            status=status,
            now=crud.now(),
            id=row["id"],
            claim=row["claim"],
            **fields,
        )
        if result.rowcount != 1:
            raise RuntimeError("Payout lease was lost.")
    row.update(status=status, **values)


async def process_payout(row):
    if row["status"] == "queued":
        # Fetching an invoice cannot transfer money and can safely be retried.
        try:
            # End provider lookups before the worker's overall deadline so their
            # failures count towards the bounded invoice-preparation retries.
            with fail_after(PAYOUT_INVOICE_TIMEOUT):
                pr = await get_pr_from_lnurl(row["ln_address"], row["amount"] * 1000)
            invoice = decode(pr)
            if (
                invoice.amount_msat != row["amount"] * 1000
                or invoice.date + invoice.expiry <= time() + 10
            ):
                raise ValueError("Invalid payout invoice.")
            # LNbits may return an existing outgoing payment for a reused hash,
            # including one from another wallet. Never reuse a provider invoice.
            existing = await get_standalone_payment(invoice.payment_hash)
            if existing and not (existing.is_in and existing.pending):
                raise ValueError("Payout invoice was already used.")
            await update_payout(
                row, "prepared", bolt11=pr, payment_hash=invoice.payment_hash, error=""
            )
        except Exception as error:
            detail = invoice_error_detail(error)
            logger.warning(
                "QuakeJS payout invoice preparation failed: {} ({}): {}",
                row["id"],
                type(error).__name__,
                detail,
            )
            attempts = row["attempts"] + 1
            await update_payout(
                row,
                "failed" if attempts >= 8 else "queued",
                attempts=attempts,
                next_attempt=crud.now() + min(3600, 15 * 2 ** min(attempts, 8)),
                error=detail,
            )
            return
    if row["status"] == "prepared":
        # Committed before calling LNbits. A crash after this point is uncertain.
        await update_payout(row, "sending")
        try:
            payment = await pay_invoice(
                wallet_id=row["wallet_id"],
                payment_request=row["bolt11"],
                max_sat=row["amount"],
                description=(
                    "QuakeJS creator fee"
                    if outbox_table(row) == "creator_payouts"
                    else "QuakeJS frag payout"
                ),
                tag="quakejs",
                external_id=row["id"],
                extra={
                    "tag": "quakejs",
                    "quakejs_payout": row["id"],
                    "quakejs_victim": row["victim_id"],
                    "quakejs_kind": (
                        "creator" if outbox_table(row) == "creator_payouts" else "kill"
                    ),
                },
            )
            if not matches_payout(payment, row):
                await update_payout(
                    row,
                    "failed",
                    error="Payment identity mismatch. Owner review required.",
                )
                return
            await update_payout(
                row,
                "paid" if payment.success else "pending",
                next_attempt=crud.now() + 15,
            )
        except Exception:
            await update_payout(
                row,
                "pending",
                next_attempt=crud.now() + 15,
                error="Payment outcome needs reconciliation.",
            )
        return
    if row["status"] in ("sending", "pending"):
        await reconcile_payout(row)


def matches_payout(payment, row):
    return (
        payment.is_out
        and (
            outbox_table(row) != "creator_payouts"
            or payment.extra.get("quakejs_kind") == "creator"
        )
        and payment.wallet_id == row["wallet_id"]
        and payment.payment_hash == row["payment_hash"]
        and payment.amount == -row["amount"] * 1000
        and payment.extra.get("tag") == "quakejs"
        and payment.extra.get("quakejs_payout") == row["id"]
        and payment.extra.get("quakejs_victim") == row["victim_id"]
    )


async def reconcile_payout(row):
    payment = await get_standalone_payment(
        row["payment_hash"], wallet_id=row["wallet_id"]
    )
    if payment and not matches_payout(payment, row):
        await update_payout(
            row, "failed", error="Payment identity mismatch. Owner review required."
        )
        return
    if payment:
        if payment.pending:
            try:
                status = await check_payment_status(payment)
                if status.success:
                    await update_payout(row, "paid", error="")
                    return
                payment = await get_standalone_payment(
                    row["payment_hash"], wallet_id=row["wallet_id"]
                )
            except Exception:
                logger.debug("QuakeJS will retry a payment status check.")
        if payment and matches_payout(payment, row) and payment.success:
            await update_payout(row, "paid", error="")
            return
        if payment and payment.failed:
            await update_payout(
                row,
                "failed",
                error="Lightning payment failed. No automatic resend.",
            )
            return
    await update_payout(row, "pending", next_attempt=crud.now() + 30)


async def payout_loop(notify):
    while True:
        row = None
        try:
            row = await claim_payout()
            if not row:
                await asyncio.sleep(1)
                continue
            with fail_after(60):
                await process_payout(row)
            await notify(row["arena_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "QuakeJS payout worker deferred an operation for reconciliation."
            )
            await asyncio.sleep(1)
        finally:
            if row:
                try:
                    # A timeout outside process_payout must also yield the queue
                    # to other recipients. Never reset a possibly sent payment.
                    async with crud.transaction() as tx:
                        await tx.execute(
                            f"UPDATE quakejs.{outbox_table(row)} "  # noqa: S608 - allowlisted table
                            "SET claimed_until=0,next_attempt=CASE WHEN "
                            "status IN ('queued','prepared','sending','pending') "
                            "AND next_attempt<=:now THEN :retry ELSE next_attempt END "
                            "WHERE id=:id AND claim=:claim",
                            id=row["id"],
                            claim=row["claim"],
                            now=crud.now(),
                            retry=crud.now() + 30,
                        )
                except Exception:
                    # An unavailable DB must not kill the payout worker. The
                    # lease expires naturally and exact-hash recovery is safe.
                    logger.warning("QuakeJS payout lease will expire for recovery.")


async def reconcile_entries(notify):
    # WebSocket/invoice events are primary; this recovers missed notifications.
    from lnbits.core.db import db as core_db

    cursor = ""
    while True:
        try:
            rows = await crud.all_rows(
                "SELECT * FROM quakejs.entries WHERE status IN "
                "('creating','pending') AND id>:cursor ORDER BY id LIMIT 100",
                cursor=cursor,
            )
        except Exception:
            logger.warning("QuakeJS will retry entry reconciliation.")
            await asyncio.sleep(15)
            continue
        cursor = rows[-1]["id"] if rows else ""
        for row in rows:
            try:
                payment = None
                if row["payment_hash"]:
                    payment = await get_standalone_payment(
                        row["payment_hash"], incoming=True, wallet_id=row["wallet_id"]
                    )
                else:
                    from lnbits.core.models import Payment

                    payment = await core_db.fetchone(
                        "SELECT * FROM apipayments WHERE external_id=:id AND "
                        "wallet_id=:wallet AND amount>0",
                        {"id": row["id"], "wallet": row["wallet_id"]},
                        Payment,
                    )
                    if payment:
                        await crud.record_invoice(row["id"], payment)
                if payment and payment.success:
                    arena = await crud.settle_entry(payment)
                    if arena:
                        await notify(arena)
            except Exception:
                logger.warning("QuakeJS entry reconciliation deferred: {}", row["id"])
        await asyncio.sleep(15)
