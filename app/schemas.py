from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict


class EventType(str, Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


class EventIn(BaseModel):
    event_id: str = Field(..., description="Globally unique ID for this event. Used as the idempotency key.")
    event_type: EventType
    transaction_id: str
    merchant_id: str
    merchant_name: Optional[str] = Field(
        None, description="Used to create/refresh the merchant record on first sight."
    )
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    timestamp: datetime = Field(..., description="Time the event actually occurred upstream.")


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    merchant_id: str
    merchant_name: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    payment_status: Optional[str] = None
    settlement_status: str
    overall_status: str
    has_conflict: bool
    first_event_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    event_count: int


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    event_type: str
    transaction_id: str
    merchant_id: str
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    event_timestamp: datetime
    received_at: Optional[datetime] = None


class EventIngestResponse(BaseModel):
    event_id: str
    transaction_id: str
    duplicate: bool
    message: str
    transaction: TransactionOut


class TransactionDetailOut(TransactionOut):
    events: List[EventOut] = []


class PaginatedTransactions(BaseModel):
    items: List[TransactionOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class ReconciliationSummaryRow(BaseModel):
    group: str
    transaction_count: int
    total_amount: Decimal
    initiated_count: int
    processed_count: int
    failed_count: int
    settled_count: int
    unsettled_count: int
    discrepancy_count: int


class DiscrepancyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    merchant_id: str
    merchant_name: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    payment_status: Optional[str] = None
    settlement_status: str
    has_conflict: bool
    last_event_at: Optional[datetime] = None
    discrepancy_type: str
    reason: str


class PaginatedDiscrepancies(BaseModel):
    items: List[DiscrepancyOut]
    total: int
    page: int
    page_size: int
    total_pages: int
