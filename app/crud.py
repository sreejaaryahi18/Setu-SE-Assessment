from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Merchant, Transaction, PaymentEvent
from app.schemas import EventIn
from app.state_machine import apply_event, IncomingEvent, compute_overall_status
from app.timeutils import to_naive_utc, utc_now_naive


def get_or_create_merchant(db: Session, merchant_id: str, merchant_name: Optional[str]) -> Merchant:
    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        merchant = Merchant(merchant_id=merchant_id, merchant_name=merchant_name or merchant_id)
        db.add(merchant)
        db.flush()
    elif merchant_name and merchant.merchant_name != merchant_name:
        merchant.merchant_name = merchant_name
    return merchant


def ingest_event(db: Session, event: EventIn) -> tuple[PaymentEvent, Transaction, bool]:
    """
    Idempotent event ingestion.

    Idempotency mechanism: `payment_events.event_id` has a UNIQUE constraint.
    We optimistically check for an existing row first (cheap, avoids a wasted
    transaction rollback in the common non-duplicate case) and additionally
    guard the insert with the DB constraint itself as a safety net against
    races between the check and the insert.

    Returns (event_row, transaction_row, was_duplicate).
    """
    existing = db.query(PaymentEvent).filter(PaymentEvent.event_id == event.event_id).one_or_none()
    if existing is not None:
        txn = db.get(Transaction, existing.transaction_id)
        return existing, txn, True

    get_or_create_merchant(db, event.merchant_id, event.merchant_name)

    event_timestamp = to_naive_utc(event.timestamp)

    txn = db.get(Transaction, event.transaction_id)
    if txn is None:
        txn = Transaction(
            transaction_id=event.transaction_id,
            merchant_id=event.merchant_id,
            settlement_status="unsettled",
            event_count=0,
        )
        db.add(txn)

    db_event = PaymentEvent(
        event_id=event.event_id,
        transaction_id=event.transaction_id,
        merchant_id=event.merchant_id,
        event_type=event.event_type.value,
        amount=event.amount,
        currency=event.currency,
        event_timestamp=event_timestamp,
    )
    db.add(db_event)

    apply_event(
        txn,
        IncomingEvent(
            event_type=event.event_type.value,
            transaction_id=event.transaction_id,
            merchant_id=event.merchant_id,
            amount=event.amount,
            currency=event.currency,
            timestamp=event_timestamp,
        ),
    )

    try:
        db.commit()
    except IntegrityError:
        # Lost a race against a concurrent request with the same event_id.
        db.rollback()
        existing = db.query(PaymentEvent).filter(PaymentEvent.event_id == event.event_id).one()
        txn = db.get(Transaction, existing.transaction_id)
        return existing, txn, True

    db.refresh(db_event)
    db.refresh(txn)
    return db_event, txn, False


def transaction_to_dict(txn: Transaction, merchant_name: Optional[str] = None) -> dict:
    return {
        "transaction_id": txn.transaction_id,
        "merchant_id": txn.merchant_id,
        "merchant_name": merchant_name,
        "amount": txn.amount,
        "currency": txn.currency,
        "payment_status": txn.payment_status,
        "settlement_status": txn.settlement_status,
        "overall_status": compute_overall_status(txn.payment_status, txn.settlement_status, txn.has_conflict),
        "has_conflict": txn.has_conflict,
        "first_event_at": txn.first_event_at,
        "last_event_at": txn.last_event_at,
        "settled_at": txn.settled_at,
        "event_count": txn.event_count,
    }


ALLOWED_SORT_FIELDS = {
    "last_event_at": Transaction.last_event_at,
    "first_event_at": Transaction.first_event_at,
    "amount": Transaction.amount,
    "created_at": Transaction.created_at,
}


def list_transactions(
    db: Session,
    merchant_id: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "last_event_at",
    sort_order: str = "desc",
):
    query = db.query(Transaction, Merchant.merchant_name).join(
        Merchant, Transaction.merchant_id == Merchant.merchant_id
    )

    if merchant_id:
        query = query.filter(Transaction.merchant_id == merchant_id)

    if status:
        if status in ("initiated", "processed", "failed"):
            query = query.filter(Transaction.payment_status == status)
        elif status in ("settled", "unsettled"):
            query = query.filter(Transaction.settlement_status == status)
        elif status == "conflict":
            query = query.filter(Transaction.has_conflict.is_(True))
        elif status == "discrepancy":
            query = query.filter(
                Transaction.payment_status == "failed",
                Transaction.settlement_status == "settled",
            )
        elif status == "processing_awaiting_settlement":
            query = query.filter(
                Transaction.payment_status == "processed",
                Transaction.settlement_status == "unsettled",
            )
        elif status == "pending":
            query = query.filter(Transaction.payment_status == "initiated")
        # unrecognized status values simply return no extra filter narrowing

    if date_from:
        query = query.filter(Transaction.last_event_at >= to_naive_utc(date_from))
    if date_to:
        query = query.filter(Transaction.last_event_at <= to_naive_utc(date_to))

    total = query.count()

    sort_col = ALLOWED_SORT_FIELDS.get(sort_by, Transaction.last_event_at)
    sort_col = sort_col.asc() if sort_order == "asc" else sort_col.desc()
    query = query.order_by(sort_col)

    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    items = [transaction_to_dict(txn, merchant_name) for txn, merchant_name in rows]
    return items, total, page, page_size


def get_transaction_detail(db: Session, transaction_id: str):
    row = (
        db.query(Transaction, Merchant.merchant_name)
        .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
        .filter(Transaction.transaction_id == transaction_id)
        .one_or_none()
    )
    if row is None:
        return None
    txn, merchant_name = row
    events = (
        db.query(PaymentEvent)
        .filter(PaymentEvent.transaction_id == transaction_id)
        .order_by(PaymentEvent.event_timestamp.asc())
        .all()
    )
    data = transaction_to_dict(txn, merchant_name)
    data["events"] = events
    return data


def reconciliation_summary(db: Session, group_by: str = "merchant"):
    """
    All aggregation happens in SQL via GROUP BY + conditional SUM (CASE WHEN),
    never by pulling rows into Python and looping.
    """
    settled_count = func.sum(case((Transaction.settlement_status == "settled", 1), else_=0))
    unsettled_count = func.sum(case((Transaction.settlement_status == "unsettled", 1), else_=0))
    initiated_count = func.sum(case((Transaction.payment_status == "initiated", 1), else_=0))
    processed_count = func.sum(case((Transaction.payment_status == "processed", 1), else_=0))
    failed_count = func.sum(case((Transaction.payment_status == "failed", 1), else_=0))
    discrepancy_count = func.sum(
        case(
            (
                (Transaction.has_conflict.is_(True))
                | ((Transaction.payment_status == "failed") & (Transaction.settlement_status == "settled")),
                1,
            ),
            else_=0,
        )
    )
    total_amount = func.coalesce(func.sum(Transaction.amount), 0)
    txn_count = func.count(Transaction.transaction_id)

    if group_by == "merchant":
        group_col = Merchant.merchant_name
        query = (
            db.query(
                group_col.label("group"),
                txn_count,
                total_amount,
                initiated_count,
                processed_count,
                failed_count,
                settled_count,
                unsettled_count,
                discrepancy_count,
            )
            .select_from(Transaction)
            .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
            .group_by(group_col)
            .order_by(group_col)
        )
    elif group_by == "status":
        group_col = Transaction.payment_status
        query = (
            db.query(
                group_col.label("group"),
                txn_count,
                total_amount,
                initiated_count,
                processed_count,
                failed_count,
                settled_count,
                unsettled_count,
                discrepancy_count,
            )
            .select_from(Transaction)
            .group_by(group_col)
            .order_by(group_col)
        )
    else:  # date
        group_col = func.date(Transaction.last_event_at)
        query = (
            db.query(
                group_col.label("group"),
                txn_count,
                total_amount,
                initiated_count,
                processed_count,
                failed_count,
                settled_count,
                unsettled_count,
                discrepancy_count,
            )
            .select_from(Transaction)
            .group_by(group_col)
            .order_by(group_col)
        )

    rows = query.all()
    return [
        {
            "group": str(r.group) if r.group is not None else "unknown",
            "transaction_count": r[1],
            "total_amount": r[2],
            "initiated_count": r[3],
            "processed_count": r[4],
            "failed_count": r[5],
            "settled_count": r[6],
            "unsettled_count": r[7],
            "discrepancy_count": r[8],
        }
        for r in rows
    ]


def reconciliation_discrepancies(
    db: Session,
    discrepancy_type: Optional[str] = None,
    merchant_id: Optional[str] = None,
    min_age_hours: int = 6,
    page: int = 1,
    page_size: int = 20,
):
    """
    Discrepancy categories (all computed via SQL WHERE clauses against the
    denormalized Transaction snapshot -- see README > Reconciliation Logic):

      PROCESSED_NOT_SETTLED   payment processed, still unsettled after
                              `min_age_hours` (a normal in-flight settlement
                              becomes a discrepancy once it's stale).
      SETTLED_BUT_FAILED      settlement recorded for a payment that failed.
      SETTLED_NO_PAYMENT      settled with no payment_processed/failed ever
                              observed (orphan settlement).
      CONFLICTING_STATES      both payment_processed and payment_failed were
                              seen for the same transaction (duplicate/
                              out-of-order events led to contradictory state).
    """
    cutoff = utc_now_naive() - timedelta(hours=min_age_hours)

    processed_not_settled = (
        (Transaction.payment_status == "processed")
        & (Transaction.settlement_status == "unsettled")
        & (Transaction.last_event_at <= cutoff)
    )
    settled_but_failed = (Transaction.payment_status == "failed") & (Transaction.settlement_status == "settled")
    settled_no_payment = (Transaction.payment_status.is_(None)) & (Transaction.settlement_status == "settled")
    conflicting_states = Transaction.has_conflict.is_(True)

    type_filters = {
        "PROCESSED_NOT_SETTLED": processed_not_settled,
        "SETTLED_BUT_FAILED": settled_but_failed,
        "SETTLED_NO_PAYMENT": settled_no_payment,
        "CONFLICTING_STATES": conflicting_states,
    }

    if discrepancy_type and discrepancy_type in type_filters:
        combined = type_filters[discrepancy_type]
    else:
        combined = processed_not_settled | settled_but_failed | settled_no_payment | conflicting_states

    query = (
        db.query(Transaction, Merchant.merchant_name)
        .join(Merchant, Transaction.merchant_id == Merchant.merchant_id)
        .filter(combined)
    )
    if merchant_id:
        query = query.filter(Transaction.merchant_id == merchant_id)

    total = query.count()

    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)
    rows = (
        query.order_by(Transaction.last_event_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = []
    for txn, merchant_name in rows:
        if txn.has_conflict:
            dtype, reason = "CONFLICTING_STATES", "Both a payment_processed and a payment_failed event were recorded for this transaction."
        elif txn.payment_status == "failed" and txn.settlement_status == "settled":
            dtype, reason = "SETTLED_BUT_FAILED", "A settlement was recorded even though the payment failed."
        elif txn.payment_status is None and txn.settlement_status == "settled":
            dtype, reason = "SETTLED_NO_PAYMENT", "Settlement recorded with no corresponding payment event ever received."
        else:
            dtype, reason = (
                "PROCESSED_NOT_SETTLED",
                f"Payment processed but not settled within {min_age_hours}h.",
            )
        items.append(
            {
                "transaction_id": txn.transaction_id,
                "merchant_id": txn.merchant_id,
                "merchant_name": merchant_name,
                "amount": txn.amount,
                "currency": txn.currency,
                "payment_status": txn.payment_status,
                "settlement_status": txn.settlement_status,
                "has_conflict": txn.has_conflict,
                "last_event_at": txn.last_event_at,
                "discrepancy_type": dtype,
                "reason": reason,
            }
        )

    return items, total, page, page_size
