import os
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.schemas import ReconciliationSummaryRow, PaginatedDiscrepancies

router = APIRouter(tags=["reconciliation"])

DEFAULT_MIN_AGE_HOURS = int(os.getenv("DISCREPANCY_MIN_AGE_HOURS", "6"))


@router.get("/reconciliation/summary", response_model=List[ReconciliationSummaryRow])
def summary(
    group_by: str = Query("merchant", description="merchant | date | status"),
    db: Session = Depends(get_db),
):
    if group_by not in ("merchant", "date", "status"):
        group_by = "merchant"
    return crud.reconciliation_summary(db, group_by=group_by)


@router.get("/reconciliation/discrepancies", response_model=PaginatedDiscrepancies)
def discrepancies(
    type: Optional[str] = Query(
        None,
        alias="type",
        description="PROCESSED_NOT_SETTLED | SETTLED_BUT_FAILED | SETTLED_NO_PAYMENT | CONFLICTING_STATES",
    ),
    merchant_id: Optional[str] = None,
    min_age_hours: int = Query(
        DEFAULT_MIN_AGE_HOURS,
        ge=0,
        description="How long a processed-but-unsettled transaction must sit before it counts as a discrepancy.",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    items, total, page, page_size = crud.reconciliation_discrepancies(
        db,
        discrepancy_type=type,
        merchant_id=merchant_id,
        min_age_hours=min_age_hours,
        page=page,
        page_size=page_size,
    )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return PaginatedDiscrepancies(items=items, total=total, page=page, page_size=page_size, total_pages=total_pages)
