from sqlalchemy import (
    Column,
    String,
    Numeric,
    DateTime,
    Boolean,
    Integer,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Merchant(Base):
    __tablename__ = "merchants"

    merchant_id = Column(String, primary_key=True)
    merchant_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    transactions = relationship("Transaction", back_populates="merchant")


class Transaction(Base):
    """
    Denormalized, query-optimized snapshot of a transaction's current state.

    This table is derived entirely from payment_events (the source of truth /
    append-only log). Every /events POST updates this row via the state
    machine in app/state_machine.py so that GET /transactions and the
    reconciliation endpoints can filter, sort, and aggregate purely in SQL
    without re-scanning the full event history on every request.
    """

    __tablename__ = "transactions"

    transaction_id = Column(String, primary_key=True)
    merchant_id = Column(String, ForeignKey("merchants.merchant_id"), nullable=False, index=True)

    amount = Column(Numeric(18, 2), nullable=True)
    currency = Column(String(8), nullable=True)

    # Derived from the latest (by event timestamp) payment_initiated /
    # payment_processed / payment_failed event seen for this transaction.
    payment_status = Column(String, nullable=True, index=True)  # initiated | processed | failed

    # Set once a `settled` event is seen. Monotonic: never reverts to unsettled.
    settlement_status = Column(String, nullable=False, default="unsettled", index=True)  # unsettled | settled

    # True if this transaction has ever received BOTH a payment_processed and a
    # payment_failed event -- i.e. conflicting terminal payment states, usually
    # caused by out-of-order or duplicate upstream event delivery.
    has_conflict = Column(Boolean, nullable=False, default=False, index=True)

    # Internal bookkeeping to detect has_conflict correctly regardless of
    # event arrival order (payment_status only reflects the latest-by-timestamp
    # event, so we can't infer "ever seen" from it alone).
    seen_processed = Column(Boolean, nullable=False, default=False)
    seen_failed = Column(Boolean, nullable=False, default=False)

    first_event_at = Column(DateTime(timezone=True), nullable=True)
    last_event_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_payment_event_at = Column(DateTime(timezone=True), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)

    event_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    merchant = relationship("Merchant", back_populates="transactions")
    events = relationship(
        "PaymentEvent",
        back_populates="transaction",
        order_by="PaymentEvent.event_timestamp",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # Supports GET /transactions?merchant_id=...&status=... plus sorting by date
        Index("ix_txn_merchant_payment_status", "merchant_id", "payment_status"),
        Index("ix_txn_merchant_last_event", "merchant_id", "last_event_at"),
        Index("ix_txn_settlement_payment", "settlement_status", "payment_status"),
    )


class PaymentEvent(Base):
    """
    Append-only event log -- the source of truth. Never mutated or deleted.
    `event_id` is the idempotency key: a UNIQUE constraint means a duplicate
    submission of the same event_id is rejected at the DB layer and never
    reapplied to transaction state.
    """

    __tablename__ = "payment_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String, nullable=False)
    transaction_id = Column(String, ForeignKey("transactions.transaction_id"), nullable=False, index=True)
    merchant_id = Column(String, ForeignKey("merchants.merchant_id"), nullable=False, index=True)

    event_type = Column(String, nullable=False)  # payment_initiated | payment_processed | payment_failed | settled
    amount = Column(Numeric(18, 2), nullable=True)
    currency = Column(String(8), nullable=True)

    event_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now())

    transaction = relationship("Transaction", back_populates="events")

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_payment_events_event_id"),
        Index("ix_events_txn_timestamp", "transaction_id", "event_timestamp"),
    )
