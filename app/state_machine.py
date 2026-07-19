"""
Applies a single incoming (already-deduped) event to a Transaction row.

Design notes (see README > Architecture for the full writeup):

- `payment_status` always reflects the payment_* event with the LATEST
  event `timestamp` seen so far for that transaction -- not the latest to
  arrive over HTTP. This makes ingestion order-independent: a
  payment_processed event that happens to arrive before an out-of-order
  payment_initiated event with an earlier timestamp will not be clobbered.

- `settlement_status` is monotonic: once a transaction is settled it stays
  settled. Settlement events don't currently get "un-applied" -- there is no
  upstream event type for reversing a settlement in this spec.

- `has_conflict` flips permanently to True the moment both a
  payment_processed and a payment_failed event have been recorded for the
  same transaction, regardless of arrival order. This is what
  GET /reconciliation/discrepancies surfaces as CONFLICTING_STATES.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.models import Transaction

PAYMENT_EVENT_TYPES = {"payment_initiated", "payment_processed", "payment_failed"}
PAYMENT_STATUS_FROM_EVENT = {
    "payment_initiated": "initiated",
    "payment_processed": "processed",
    "payment_failed": "failed",
}


@dataclass
class IncomingEvent:
    event_type: str
    transaction_id: str
    merchant_id: str
    amount: Optional[Decimal]
    currency: Optional[str]
    timestamp: datetime


def apply_event(txn: Transaction, evt: IncomingEvent) -> Transaction:
    """Mutates and returns `txn` in place given a new, already-persisted event."""

    is_new = txn.event_count == 0

    if is_new:
        txn.transaction_id = evt.transaction_id
        txn.merchant_id = evt.merchant_id
        txn.first_event_at = evt.timestamp

    # Keep the latest amount/currency seen (events for the same transaction
    # should be consistent, but upstream systems aren't always perfect).
    if evt.amount is not None:
        txn.amount = evt.amount
    if evt.currency is not None:
        txn.currency = evt.currency

    if txn.first_event_at is None or evt.timestamp < txn.first_event_at:
        txn.first_event_at = evt.timestamp
    if txn.last_event_at is None or evt.timestamp > txn.last_event_at:
        txn.last_event_at = evt.timestamp

    if evt.event_type in PAYMENT_EVENT_TYPES:
        new_status = PAYMENT_STATUS_FROM_EVENT[evt.event_type]

        # payment_status reflects whichever payment_* event has the latest
        # timestamp seen so far -- independent of HTTP arrival order.
        if txn.last_payment_event_at is None or evt.timestamp >= txn.last_payment_event_at:
            txn.payment_status = new_status
            txn.last_payment_event_at = evt.timestamp

        # Conflict tracking is arrival-order independent and permanent: once
        # both a processed and a failed event have ever been seen for this
        # transaction, it stays flagged even if further events arrive.
        if new_status == "processed":
            txn.seen_processed = True
        elif new_status == "failed":
            txn.seen_failed = True
        if txn.seen_processed and txn.seen_failed:
            txn.has_conflict = True

    elif evt.event_type == "settled":
        txn.settlement_status = "settled"
        if txn.settled_at is None or evt.timestamp < txn.settled_at:
            txn.settled_at = evt.timestamp

    txn.event_count += 1
    return txn


def compute_overall_status(payment_status: Optional[str], settlement_status: str, has_conflict: bool) -> str:
    """Human-friendly rollup used in API responses (GET /transactions, detail view)."""
    if has_conflict:
        return "conflict"
    if payment_status == "failed" and settlement_status == "settled":
        return "discrepancy"
    if settlement_status == "settled":
        return "settled"
    if payment_status == "processed":
        return "processing_awaiting_settlement"
    if payment_status == "failed":
        return "failed"
    if payment_status == "initiated":
        return "pending"
    return "unknown"
