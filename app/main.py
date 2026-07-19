from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import Base, engine
from app import models  # noqa: F401  (ensures models are registered on Base before create_all)
from app.routers import events, transactions, reconciliation


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Setu Payment Reconciliation Service",
    description=(
        "Ingests payment lifecycle events, maintains transaction/reconciliation "
        "state, and exposes reporting APIs for operations teams."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


app.include_router(events.router)
app.include_router(transactions.router)
app.include_router(reconciliation.router)
