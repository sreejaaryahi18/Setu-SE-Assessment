from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud
from app.schemas import EventIn, EventIngestResponse

router = APIRouter(tags=["events"])


@router.post("/events", response_model=EventIngestResponse, status_code=status.HTTP_201_CREATED)
def ingest_event(event: EventIn, db: Session = Depends(get_db)):
    db_event, txn, was_duplicate = crud.ingest_event(db, event)

    merchant_name = txn.merchant.merchant_name if txn.merchant else None
    txn_dict = crud.transaction_to_dict(txn, merchant_name)

    return EventIngestResponse(
        event_id=event.event_id,
        transaction_id=event.transaction_id,
        duplicate=was_duplicate,
        message=(
            "Event already ingested previously; no state change applied."
            if was_duplicate
            else "Event ingested."
        ),
        transaction=txn_dict,
    )
