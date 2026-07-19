"""
Run with: pytest -v

Uses a throwaway SQLite file (test_setu.db) so it never touches the dev DB
you seed for manual exploration.
"""

import os
import sys
from pathlib import Path

TEST_DB_PATH = Path(__file__).resolve().parent / "test_setu.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH}"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def post_event(**overrides):
    payload = {
        "event_id": "evt-1",
        "event_type": "payment_initiated",
        "transaction_id": "txn-1",
        "merchant_id": "merchant_1",
        "merchant_name": "UrbanThreads",
        "amount": 100.0,
        "currency": "INR",
        "timestamp": "2026-01-01T10:00:00+00:00",
    }
    payload.update(overrides)
    return client.post("/events", json=payload)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ingest_new_event_creates_transaction():
    r = post_event()
    assert r.status_code == 201
    body = r.json()
    assert body["duplicate"] is False
    assert body["transaction"]["payment_status"] == "initiated"
    assert body["transaction"]["overall_status"] == "pending"
    assert body["transaction"]["event_count"] == 1


def test_duplicate_event_id_is_idempotent():
    r1 = post_event()
    r2 = post_event()  # exact same event_id + payload
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["duplicate"] is False
    assert r2.json()["duplicate"] is True
    # event_count must not increase from the duplicate
    assert r2.json()["transaction"]["event_count"] == 1

    detail = client.get("/transactions/txn-1").json()
    assert detail["event_count"] == 1
    assert len(detail["events"]) == 1


def test_full_lifecycle_reaches_settled():
    post_event(event_id="e1", event_type="payment_initiated", timestamp="2026-01-01T10:00:00+00:00")
    post_event(event_id="e2", event_type="payment_processed", timestamp="2026-01-01T10:05:00+00:00")
    post_event(event_id="e3", event_type="settled", timestamp="2026-01-01T11:00:00+00:00")

    detail = client.get("/transactions/txn-1").json()
    assert detail["payment_status"] == "processed"
    assert detail["settlement_status"] == "settled"
    assert detail["overall_status"] == "settled"
    assert detail["event_count"] == 3
    assert [e["event_type"] for e in detail["events"]] == [
        "payment_initiated",
        "payment_processed",
        "settled",
    ]


def test_out_of_order_events_still_resolve_by_business_timestamp():
    # payment_processed (later timestamp) arrives over HTTP BEFORE
    # payment_initiated (earlier timestamp) -- final payment_status should
    # still reflect "processed" since it has the later true timestamp.
    post_event(event_id="e2", event_type="payment_processed", timestamp="2026-01-01T10:05:00+00:00")
    post_event(event_id="e1", event_type="payment_initiated", timestamp="2026-01-01T10:00:00+00:00")

    detail = client.get("/transactions/txn-1").json()
    assert detail["payment_status"] == "processed"


def test_conflicting_states_detected():
    post_event(event_id="e1", event_type="payment_initiated", timestamp="2026-01-01T10:00:00+00:00")
    post_event(event_id="e2", event_type="payment_processed", timestamp="2026-01-01T10:05:00+00:00")
    post_event(event_id="e3", event_type="payment_failed", timestamp="2026-01-01T10:10:00+00:00")

    detail = client.get("/transactions/txn-1").json()
    assert detail["has_conflict"] is True
    assert detail["overall_status"] == "conflict"

    r = client.get("/reconciliation/discrepancies", params={"type": "CONFLICTING_STATES"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["transaction_id"] == "txn-1"


def test_settled_but_failed_is_a_discrepancy():
    post_event(event_id="e1", event_type="payment_initiated", timestamp="2026-01-01T10:00:00+00:00")
    post_event(event_id="e2", event_type="payment_failed", timestamp="2026-01-01T10:05:00+00:00")
    post_event(event_id="e3", event_type="settled", timestamp="2026-01-01T11:00:00+00:00")

    r = client.get("/reconciliation/discrepancies", params={"type": "SETTLED_BUT_FAILED"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["discrepancy_type"] == "SETTLED_BUT_FAILED"


def test_processed_not_settled_respects_min_age_hours():
    post_event(event_id="e1", event_type="payment_initiated", timestamp="2026-01-01T10:00:00+00:00")
    post_event(event_id="e2", event_type="payment_processed", timestamp="2026-01-01T10:05:00+00:00")

    # min_age_hours=0 -> should show up immediately regardless of wall-clock time
    r = client.get(
        "/reconciliation/discrepancies",
        params={"type": "PROCESSED_NOT_SETTLED", "min_age_hours": 0},
    )
    assert r.json()["total"] == 1

    # An absurdly high min_age_hours in the future should exclude it
    r2 = client.get(
        "/reconciliation/discrepancies",
        params={"type": "PROCESSED_NOT_SETTLED", "min_age_hours": 999999},
    )
    assert r2.json()["total"] == 0


def test_orphan_settlement_is_a_discrepancy():
    post_event(
        event_id="e1",
        transaction_id="txn-orphan",
        event_type="settled",
        timestamp="2026-01-01T10:00:00+00:00",
    )
    r = client.get("/reconciliation/discrepancies", params={"type": "SETTLED_NO_PAYMENT"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["transaction_id"] == "txn-orphan"


def test_transaction_not_found_returns_404():
    r = client.get("/transactions/does-not-exist")
    assert r.status_code == 404


def test_list_transactions_filters_by_merchant_and_status():
    post_event(event_id="e1", transaction_id="t1", merchant_id="merchant_1", event_type="payment_initiated")
    post_event(event_id="e2", transaction_id="t2", merchant_id="merchant_2", event_type="payment_failed")

    r = client.get("/transactions", params={"merchant_id": "merchant_1"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["merchant_id"] == "merchant_1"

    r2 = client.get("/transactions", params={"status": "failed"})
    body2 = r2.json()
    assert body2["total"] == 1
    assert body2["items"][0]["transaction_id"] == "t2"


def test_list_transactions_pagination():
    for i in range(5):
        post_event(
            event_id=f"e{i}",
            transaction_id=f"t{i}",
            timestamp=f"2026-01-0{i+1}T10:00:00+00:00",
        )

    r = client.get("/transactions", params={"page": 1, "page_size": 2, "sort_by": "last_event_at", "sort_order": "asc"})
    body = r.json()
    assert body["total"] == 5
    assert body["total_pages"] == 3
    assert len(body["items"]) == 2
    assert body["items"][0]["transaction_id"] == "t0"


def test_reconciliation_summary_group_by_merchant():
    post_event(event_id="e1", transaction_id="t1", merchant_id="merchant_1", amount=100)
    post_event(event_id="e2", transaction_id="t2", merchant_id="merchant_1", amount=200)
    post_event(event_id="e3", transaction_id="t3", merchant_id="merchant_2", merchant_name="FreshBasket", amount=50)

    r = client.get("/reconciliation/summary", params={"group_by": "merchant"})
    rows = {row["group"]: row for row in r.json()}
    assert rows["UrbanThreads"]["transaction_count"] == 2
    assert float(rows["UrbanThreads"]["total_amount"]) == 300.0


def test_invalid_event_type_is_rejected():
    r = post_event(event_type="not_a_real_event_type")
    assert r.status_code == 422
