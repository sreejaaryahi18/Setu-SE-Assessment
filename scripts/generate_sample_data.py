"""
Generates data/sample_events.json: a realistic stream of payment lifecycle
events across 5 merchants, covering:

  - clean successful flows        (initiated -> processed -> settled)
  - clean failures                (initiated -> failed)
  - pending settlement            (initiated -> processed, no settlement yet)
  - discrepancy: settled failure  (initiated -> failed -> settled)   [bug case]
  - discrepancy: orphan settle    (settled with no payment event at all)
  - conflicting states            (initiated -> processed -> failed, or vice versa)
  - duplicate events              (same event_id resubmitted verbatim)
  - out-of-order delivery         (events shuffled relative to their timestamps)

Run: python scripts/generate_sample_data.py
Produces ~10,000+ events at data/sample_events.json
"""

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from faker import Faker

fake = Faker()
random.seed(42)
Faker.seed(42)

MERCHANTS = [
    ("merchant_1", "UrbanThreads"),
    ("merchant_2", "FreshBasket"),
    ("merchant_3", "GadgetHive"),
    ("merchant_4", "PixelPantry"),
    ("merchant_5", "SwiftRide"),
]

CURRENCY = "INR"
TARGET_TRANSACTIONS = 4200  # yields 10k+ events given multi-event flows + duplicates/noise

START_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
END_DATE = datetime(2026, 3, 31, tzinfo=timezone.utc)


def random_timestamp():
    delta = END_DATE - START_DATE
    seconds = random.randint(0, int(delta.total_seconds()))
    return START_DATE + timedelta(seconds=seconds)


def make_event(event_type, transaction_id, merchant_id, merchant_name, amount, ts):
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "transaction_id": transaction_id,
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "amount": amount,
        "currency": CURRENCY,
        "timestamp": ts.isoformat(),
    }


def build_transaction_events(scenario, merchant_id, merchant_name):
    transaction_id = str(uuid.uuid4())
    amount = round(random.uniform(50, 50000), 2)
    t0 = random_timestamp()
    events = []

    def add(event_type, offset_minutes):
        events.append(make_event(event_type, transaction_id, merchant_id, merchant_name, amount, t0 + timedelta(minutes=offset_minutes)))

    if scenario == "clean_success":
        add("payment_initiated", 0)
        add("payment_processed", random.randint(1, 5))
        add("settled", random.randint(60, 24 * 60))

    elif scenario == "clean_failure":
        add("payment_initiated", 0)
        add("payment_failed", random.randint(1, 10))

    elif scenario == "pending_settlement":
        add("payment_initiated", 0)
        add("payment_processed", random.randint(1, 5))
        # intentionally no settlement event -> will surface as
        # PROCESSED_NOT_SETTLED once older than the discrepancy age threshold

    elif scenario == "settled_despite_failure":
        add("payment_initiated", 0)
        add("payment_failed", random.randint(1, 5))
        add("settled", random.randint(60, 300))  # upstream settlement bug

    elif scenario == "orphan_settlement":
        add("settled", 0)  # settlement with no matching payment event ever received

    elif scenario == "conflicting_states":
        add("payment_initiated", 0)
        if random.random() < 0.5:
            add("payment_processed", 5)
            add("payment_failed", 10)  # contradicts the processed event
        else:
            add("payment_failed", 5)
            add("payment_processed", 10)  # contradicts the failed event

    elif scenario == "duplicate_heavy":
        add("payment_initiated", 0)
        add("payment_processed", 3)
        add("settled", 120)
        # duplicate a couple of the events verbatim (same event_id) to test idempotency
        dup_targets = random.sample(events, k=min(2, len(events)))
        for d in dup_targets:
            events.append(dict(d))  # exact duplicate, same event_id

    return events


def main():
    all_events = []
    scenario_weights = {
        "clean_success": 0.45,
        "clean_failure": 0.18,
        "pending_settlement": 0.15,
        "settled_despite_failure": 0.06,
        "orphan_settlement": 0.04,
        "conflicting_states": 0.06,
        "duplicate_heavy": 0.06,
    }
    scenarios = list(scenario_weights.keys())
    weights = list(scenario_weights.values())

    for _ in range(TARGET_TRANSACTIONS):
        merchant_id, merchant_name = random.choice(MERCHANTS)
        scenario = random.choices(scenarios, weights=weights, k=1)[0]
        all_events.extend(build_transaction_events(scenario, merchant_id, merchant_name))

    # Simulate real-world out-of-order delivery: shuffle overall arrival order
    # (each event still carries its true business `timestamp`).
    random.shuffle(all_events)

    out_path = Path(__file__).resolve().parent.parent / "data" / "sample_events.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_events, f, indent=2)

    print(f"Wrote {len(all_events)} events to {out_path}")
    print("Scenario mix (by transaction, approx):")
    for s, w in scenario_weights.items():
        print(f"  {s}: {w*100:.0f}%")


if __name__ == "__main__":
    main()
