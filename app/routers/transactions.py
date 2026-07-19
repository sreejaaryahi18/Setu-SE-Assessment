from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.schemas import PaginatedTransactions, TransactionDetailOut

router = APIRouter(tags=["transactions"])


@router.get("/transactions", response_model=PaginatedTransactions)
def list_transactions(
    merchant_id: Optional[str] = None,
    status: Optional[str] = Query(
        None,
        description=(
            "One of: initiated, processed, failed, settled, unsettled, pending, "
            "processing_awaiting_settlement, discrepancy, conflict"
        ),
    ),
    date_from: Optional[datetime] = Query(None, description="Filters on last_event_at >= date_from"),
    date_to: Optional[datetime] = Query(None, description="Filters on last_event_at <= date_to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("last_event_at", description="last_event_at | first_event_at | amount | created_at"),
    sort_order: str = Query("desc", description="asc | desc"),
    db: Session = Depends(get_db),
):
    items, total, page, page_size = crud.list_transactions(
        db,
        merchant_id=merchant_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return PaginatedTransactions(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)


@router.get("/transactions/{transaction_id}", response_model=TransactionDetailOut)
def get_transaction(transaction_id: str, db: Session = Depends(get_db)):
    data = crud.get_transaction_detail(db, transaction_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Transaction '{transaction_id}' not found.")
    return data
