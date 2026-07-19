"""
Loads data/sample_events.json into the database by calling the exact same
crud.ingest_event() function the POST /events endpoint uses -- so the seeded
data exercises the real idempotency + state-machine logic (including the
verbatim duplicate event_ids present in the sample data) rather than being
bulk-inserted directly.

Run: python scripts/seed_db.py [--reset]
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, Base, engine
from app import crud
from app.schemas import EventIn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Drop and recreate all tables before seeding.")
    parser.add_argument(
        "--file",
        default=str(Path(__file__).resolve().parent.parent / "data" / "sample_events.json"),
    )
    args = parser.parse_args()

    if args.reset:
        print("Dropping and recreating all tables...")
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with open(args.file) as f:
        events = json.load(f)

    db = SessionLocal()
    ingested = 0
    duplicates = 0
    errors = 0
    start = time.time()

    for i, raw in enumerate(events, start=1):
        try:
            event = EventIn(**raw)
            _, _, was_duplicate = crud.ingest_event(db, event)
            if was_duplicate:
                duplicates += 1
            else:
                ingested += 1
        except Exception as e:
            errors += 1
            print(f"  [warn] failed to ingest event {i} ({raw.get('event_id')}): {e}")

        if i % 1000 == 0:
            print(f"  ...processed {i}/{len(events)}")

    db.close()
    elapsed = time.time() - start

    print("\nSeed complete.")
    print(f"  Total events in file : {len(events)}")
    print(f"  Newly ingested       : {ingested}")
    print(f"  Duplicates skipped   : {duplicates}")
    print(f"  Errors               : {errors}")
    print(f"  Elapsed              : {elapsed:.1f}s")


if __name__ == "__main__":
    main()
