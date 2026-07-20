# Setu Payment Reconciliation Service

A backend service that ingests payment lifecycle events, maintains transaction
and reconciliation state, and exposes reporting APIs for operations teams --
built with **FastAPI + SQLAlchemy + SQLite** (swappable to Postgres via one
env var).

> This README covers local setup and deployment.

**Live deployment:** https://setu-se-assessment.onrender.com
**Interactive API docs:** https://setu-se-assessment.onrender.com/docs

> Note: this runs on Render's free tier, which spins down after 15 minutes of inactivity. If it's been idle, the first request can have a delay of 50 seconds or more to wake back up, subsequent requests are fast. This is expected behavior, not an error.

---

## 1. Quick Start (local)

Requires Python 3.10+.

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) copy the env template -- defaults already work out of the box
cp .env.example .env

# 4. Seed the database with the provided sample data (10,355 official events)
python scripts/seed_db.py --reset

# 5. Run the server
uvicorn app.main:app --reload
```

The API is now live at `http://127.0.0.1:8000`, with interactive docs at
`http://127.0.0.1:8000/docs` (Swagger UI) and `http://127.0.0.1:8000/redoc`.

Run the test suite:

```bash
pytest -v
```

A ready-to-run Postman collection is in `postman/Setu_Reconciliation.postman_collection.json`
(import into Postman and set the `base_url` variable).

---

## 2. Architecture Overview

```
                POST /events
                     |
                     v
          +----------------------+        writes           +--------------------+
          |  payment_events      | -----------------------> |  transactions       |
          |  (append-only log,   |   state machine derives  |  (denormalized,     |
          |   source of truth)   |   current snapshot        |   query-optimized   |
          +----------------------+                           |   current state)    |
                     ^                                        +--------------------+
                     | event_id UNIQUE constraint                       |
                     | = idempotency guard                              |
                     |                                                   v
              duplicate submissions                          GET /transactions
              rejected, never reapplied                      GET /transactions/{id}
                                                               GET /reconciliation/summary
                                                               GET /reconciliation/discrepancies
```

**Two tables carry the real weight:**

- **`payment_events`** -- an append-only log of every event ever received.
  This is the source of truth and is never mutated or deleted. `event_id`
  has a `UNIQUE` constraint, which is the idempotency mechanism (see §5).
- **`transactions`** -- a denormalized, continuously-updated snapshot of each
  transaction's *current* state (`payment_status`, `settlement_status`,
  `has_conflict`, timestamps, amount). Every `POST /events` call updates this
  row via the state machine in `app/state_machine.py`.

Keeping a denormalized snapshot table is the single biggest design decision
in this service. Without it, every `GET /transactions` or
`/reconciliation/*` call would need to re-derive "what's the current status
of this transaction?" by scanning and folding over its entire event history
-- fine at 10 events, unworkable at scale. With it, every read endpoint is a
single indexed `WHERE`/`GROUP BY` against a small, flat table, and the
event log stays fully intact underneath for audit/replay if ever needed.

A third table, **`merchants`**, is upserted on first sight of a
`merchant_id` (from the event payload's `merchant_id` / `merchant_name`).

### Request flow for `POST /events`

1. Check if `event_id` already exists in `payment_events`. If yes: return
   `duplicate: true` immediately, no state change (fast path).
2. Otherwise, upsert the merchant, get-or-create the transaction row, insert
   the new event row, and run `apply_event()` (the state machine) against the
   transaction snapshot.
3. Commit. If a concurrent request raced us and inserted the same
   `event_id` first, the `UNIQUE` constraint throws `IntegrityError`; we
   catch it, roll back, and treat it as a duplicate (see §5 for why this
   matters).

### Request flow for reads

`GET /transactions`, `/transactions/{id}`, `/reconciliation/summary`, and
`/reconciliation/discrepancies` all query the `transactions` table (joined to
`merchants` for the name) using SQLAlchemy-built `WHERE`, `GROUP BY`,
`ORDER BY`, `LIMIT`/`OFFSET` -- filtering, sorting, pagination, and
aggregation all happen in SQL. `/transactions/{id}` additionally pulls the
full ordered event history from `payment_events` for that one transaction.

---

## 3. Data Model

```
merchants
+-- merchant_id       (PK)
+-- merchant_name
+-- created_at

transactions                                  payment_events
+-- transaction_id        (PK)                +-- id                  (PK, autoincrement)
+-- merchant_id            (FK -> merchants)   +-- event_id            (UNIQUE -- idempotency key)
+-- amount                                     +-- transaction_id      (FK -> transactions, indexed)
+-- currency                                   +-- merchant_id         (FK -> merchants, indexed)
+-- payment_status    initiated|processed|failed
+-- settlement_status  unsettled|settled       +-- event_type
+-- has_conflict       bool                    +-- amount / currency
+-- seen_processed / seen_failed  bool         +-- event_timestamp     (indexed)
+-- first_event_at / last_event_at (indexed)   +-- received_at
+-- last_payment_event_at
+-- settled_at
+-- event_count
+-- created_at / updated_at
```

**Indexes** (see `app/models.py` for the exact `Index(...)` declarations):

| Index | Supports |
|---|---|
| `payment_events.event_id` (UNIQUE) | idempotency check on every `POST /events` |
| `payment_events(transaction_id, event_timestamp)` | ordered event history in `GET /transactions/{id}` |
| `transactions.merchant_id` | merchant filtering |
| `transactions(merchant_id, payment_status)` | combined merchant + status filtering |
| `transactions(merchant_id, last_event_at)` | merchant filter + date-range/sort |
| `transactions(settlement_status, payment_status)` | reconciliation discrepancy queries |
| `transactions.last_event_at` | date-range filtering and default sort |

---

## 4. Reconciliation Logic

### `GET /reconciliation/summary?group_by=merchant|status|date`

Runs one `GROUP BY` query with conditional `SUM(CASE WHEN ...)` aggregates
(`app/crud.py::reconciliation_summary`) to return, per group: transaction
count, total amount, and a breakdown by payment/settlement status and
discrepancy count.

### `GET /reconciliation/discrepancies`

Four discrepancy categories, all expressed as SQL `WHERE` predicates against
the denormalized `transactions` table:

| Type | Condition | Meaning |
|---|---|---|
| `PROCESSED_NOT_SETTLED` | `payment_status = processed AND settlement_status = unsettled AND last_event_at <= now() - min_age_hours` | Payment succeeded but settlement hasn't landed after a grace period |
| `SETTLED_BUT_FAILED` | `payment_status = failed AND settlement_status = settled` | Settlement was recorded for a payment that failed |
| `SETTLED_NO_PAYMENT` | `payment_status IS NULL AND settlement_status = settled` | Orphan settlement -- no payment event was ever received |
| `CONFLICTING_STATES` | `has_conflict = true` | Both a `payment_processed` and a `payment_failed` event were recorded for the same transaction (contradictory upstream delivery) |

`min_age_hours` (default `6`, configurable via `DISCREPANCY_MIN_AGE_HOURS` or
per-request query param) exists because "processed but not yet settled" is
completely normal for a few hours -- flagging it instantly would make every
in-flight transaction look broken. See §6 for how this interacts with the
sample data's dates.

Filter to one category with `?type=PROCESSED_NOT_SETTLED` (etc.), or omit
`type` to get the union of all four.

---

## 5. Idempotency & Ordering

**Idempotency mechanism:** `payment_events.event_id` has a `UNIQUE`
constraint. On `POST /events`:

1. We first check if the `event_id` already exists (cheap read, avoids an
   unnecessary failed-insert in the common case).
2. If a duplicate somehow still races past that check (concurrent requests
   with the same `event_id`), the `UNIQUE` constraint itself rejects the
   second insert, we catch `IntegrityError`, roll back, and treat it as
   what it is: a duplicate.

Either way, a duplicate submission never re-runs the state machine, never
inserts a second event row, and returns `duplicate: true` with the
transaction's current (unchanged) state.

**Out-of-order delivery:** upstream systems don't guarantee events arrive in
the order they happened. `payment_status` is therefore derived from
whichever `payment_*` event has the **latest business `timestamp`** seen so
far for that transaction -- not whichever arrived over HTTP last. A
`payment_processed` event with timestamp `10:05` that happens to be POSTed
*before* a `payment_initiated` event with timestamp `10:00` still resolves
correctly to `processed`. This is covered by
`test_out_of_order_events_still_resolve_by_business_timestamp` in the test
suite.

**Conflicting states:** if a transaction ever receives *both* a
`payment_processed` and a `payment_failed` event (in either order), it's
flagged `has_conflict = true` permanently, surfaced via
`overall_status = "conflict"` and the `CONFLICTING_STATES` discrepancy
category. This is the "duplicate events causing conflicting state
transitions" case from the assignment brief.

---

## 6. Sample Data

`data/sample_events.json` is the **official sample dataset provided by Setu**
(fetched from the hiring-assignments repo): **10,355 events across 3,800
transactions and 5 merchants** (QuickMart, FreshBasket, UrbanEats, TechBazaar,
StyleHub), spanning Jan 8 -- Apr 8, 2026.

A few things worth knowing about it, verified by direct inspection before
seeding:

- **190 events are exact verbatim duplicates** (same `event_id` and payload
  repeated) -- these are real idempotency test cases, and `seed_db.py`
  correctly reports "190 duplicates skipped" when loading it.
- The file is in strict chronological order, so it doesn't itself exercise
  out-of-order delivery -- that scenario (and the two discrepancy categories
  below) are instead covered by synthetic cases in `tests/test_api.py`.
- It contains meaningful volumes of two of the four discrepancy categories:
  `PROCESSED_NOT_SETTLED` (380 transactions) and `SETTLED_BUT_FAILED` (95
  transactions). It does **not** contain any `CONFLICTING_STATES` or
  `SETTLED_NO_PAYMENT` cases -- I confirmed this by directly scanning the
  file's event types per transaction, not by a detection bug. Those two
  categories are still fully implemented and are verified by
  `test_conflicting_states_detected` and `test_orphan_settlement_is_a_discrepancy`
  in the test suite using synthetic events.

**`scripts/seed_db.py`** loads this file by calling the exact same
`crud.ingest_event()` function that `POST /events` uses -- so the seeded
data exercises the real idempotency and state-machine logic (including the
190 verbatim duplicates) rather than being bulk-inserted around it.

### Optional: generating additional synthetic data

`scripts/generate_sample_data.py` is also included and can generate a larger,
synthetic dataset that additionally covers `CONFLICTING_STATES` and
`SETTLED_NO_PAYMENT` at volume, plus out-of-order delivery, if you want to see
all four discrepancy categories populated from seed data rather than only from
the test suite:

```bash
python scripts/generate_sample_data.py   # writes data/sample_events.json
python scripts/seed_db.py --reset
```

This will overwrite `data/sample_events.json` with the synthetic set -- the
official file is not modified on disk by this script, so you can re-fetch it
if you want to switch back.

> **Why does `/reconciliation/discrepancies` show `PROCESSED_NOT_SETTLED`
> results out of the box?** The sample data is dated Jan-Apr 2026, and
> `min_age_hours` compares against the real current time. Since today is well
> past those dates, every unsettled-but-processed transaction is already
> "stale" by definition -- which is intentional, so you can see the
> discrepancy endpoint return real results immediately without needing to
> wait or fake the clock.

---

## 7. API Documentation

Full interactive docs (with schemas and try-it-out) are auto-generated at
`/docs` once the server is running. Summary below.

### `POST /events`

Ingests one payment lifecycle event.

```json
{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type": "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": 15248.29,
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}
```

`event_type` must be one of `payment_initiated`, `payment_processed`,
`payment_failed`, `settled` (422 otherwise). Returns `201` with:

```json
{
  "event_id": "...",
  "transaction_id": "...",
  "duplicate": false,
  "message": "Event ingested.",
  "transaction": { "...": "current transaction snapshot" }
}
```

### `GET /transactions`

Query params: `merchant_id`, `status`, `date_from`, `date_to`, `page`
(default 1), `page_size` (default 20, max 100), `sort_by`
(`last_event_at`|`first_event_at`|`amount`|`created_at`), `sort_order`
(`asc`|`desc`).

`status` accepts: `initiated`, `processed`, `failed` (payment status),
`settled`, `unsettled` (settlement status), `pending`,
`processing_awaiting_settlement`, `discrepancy`, `conflict` (overall rollup
statuses). `date_from`/`date_to` filter on `last_event_at`.

Returns `{ items, total, page, page_size, total_pages }`.

### `GET /transactions/{transaction_id}`

Returns the transaction snapshot, merchant name, and full event history
(ordered oldest -> newest). `404` if not found.

### `GET /reconciliation/summary`

Query param: `group_by` = `merchant` (default) | `status` | `date`. Returns
an array of per-group aggregates (counts by payment/settlement status, total
amount, discrepancy count).

### `GET /reconciliation/discrepancies`

Query params: `type` (one of the four categories in §4, omit for all),
`merchant_id`, `min_age_hours` (default 6), `page`, `page_size`. Returns
`{ items, total, page, page_size, total_pages }`, each item tagged with
`discrepancy_type` and a human-readable `reason`.

---

## 8. Deployment

**Live URL:** https://setu-se-assessment.onrender.com
**Swagger docs:** https://setu-se-assessment.onrender.com/docs

Deployed on [Render](https://render.com)'s free tier, connected directly to
this GitHub repo with auto-deploy on every push to `main`.

### Configuration used

| Setting | Value |
|---|---|
| Runtime | Python (native, no Docker) |
| Python version | `3.11` (pinned via `.python-version` in repo root -- Render's current default of 3.14 doesn't yet have prebuilt wheels for some pinned dependencies, so the build fails without this) |
| Build Command | `pip install -r requirements.txt && python scripts/seed_db.py --reset` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/health` |
| Instance Type | Free |
| Environment Variables | none set -- the app's built-in defaults (SQLite, 6h discrepancy threshold) are used as-is |

### Why the database is re-seeded on every build

Render's free tier does not provide a persistent disk -- the filesystem is
reset on every new deploy. Rather than have the live service occasionally
serve an empty database, the seed script is deliberately made part of the
**Build Command**, not a separate manual step: every deploy freshly seeds
all 10,355 sample events via the real ingestion path, so the live URL always
reflects a fully-populated, freshly reconciled dataset immediately after
each deploy finishes.

### Known limitation: cold starts

Free Render web services spin down after 15 minutes of no traffic. The
first request after a period of inactivity can take 30-60 seconds to
respond while the instance wakes up; subsequent requests are fast (well
under a second). This is a free-tier platform behavior, not an application
bug -- upgrading to a paid instance type removes it entirely.


---

## 9. Assumptions & Tradeoffs

- **SQLite for local dev, Postgres-ready for production.** The schema uses
  only portable SQLAlchemy types, and the entire DB connection is one
  `DATABASE_URL` env var. Swapping to Postgres is a one-line change plus
  `pip install psycopg2-binary` -- nothing else in the codebase assumes
  SQLite. SQLite was chosen for local dev specifically so a reviewer can run
  this in minutes with zero external services.
- **Denormalized snapshot over event-sourcing-only.** I chose to maintain a
  continuously-updated `transactions` table rather than computing status
  on-the-fly from the event log on every read. This trades a bit of write-side
  complexity (the state machine) for read performance and much simpler SQL
  everywhere else -- and it's the right tradeoff for a service whose primary
  job is answering "what's the current state" and "what's inconsistent," not
  replaying history.
- **`settlement_status` is monotonic.** Once settled, always settled -- there's
  no event type in the spec for reversing a settlement, so I didn't invent
  state for it. A real-world "settlement reversed" event would just add a
  new transition.
- **Staleness-based discrepancy threshold (`min_age_hours`).** "Processed but
  not settled" is normal for a while; I treat it as a discrepancy only after
  a configurable grace period rather than immediately. Default is 6 hours,
  overridable per-request.
- **`date_from`/`date_to` filter on `last_event_at`,** not `first_event_at` --
  chosen because ops teams reconciling "what happened yesterday" usually care
  about the most recent activity on a transaction, not when it first started.
- **Conflict detection is permanent once triggered.** A transaction that ever
  saw both `processed` and `failed` stays flagged, even if, hypothetically,
  more events arrive later. I didn't build an "un-flagging" path since the
  spec doesn't define what would resolve such a conflict; a real system would
  need a manual-resolution workflow, which felt out of scope here.
- **No auth/rate-limiting.** Out of scope for this assignment; would add
  API-key auth and per-merchant rate limits before any real deployment.
- **No Alembic migrations.** `Base.metadata.create_all()` is used for
  simplicity given the fixed schema and 3-day time box. A real production
  service would use Alembic for versioned migrations.
- **What I'd do differently with more time:** add a background job to
  proactively re-flag discrepancies on a schedule rather than only computing
  them at query time (functionally equivalent today since detection is
  cheap, but a scheduled sweep + alerting would matter at higher volume);
  add bulk/batch event ingestion for high-throughput producers; add
  structured audit logging of every ingested event for compliance.

## 10. AI Tool Disclosure

This service (schema design, FastAPI implementation, state machine,
reconciliation SQL, sample data generator, tests, and this README) was built
with Claude (Anthropic) as a pair-programming assistant, with all code run
and verified locally (tests executed, server booted, endpoints exercised via
curl) before being included here.

## 11. Project Structure

```
setu-reconciliation-service/
+-- app/
|   +-- main.py              # FastAPI app, lifespan, router registration
|   +-- database.py          # SQLAlchemy engine/session
|   +-- models.py            # ORM models + indexes
|   +-- schemas.py           # Pydantic request/response schemas
|   +-- state_machine.py     # event -> transaction-snapshot derivation logic
|   +-- crud.py              # idempotent ingestion + SQL-driven queries
|   +-- timeutils.py         # naive-UTC datetime convention helpers
|   +-- routers/
|       +-- events.py
|       +-- transactions.py
|       +-- reconciliation.py
+-- scripts/
|   +-- generate_sample_data.py
|   +-- seed_db.py
+-- tests/
|   +-- test_api.py
+-- postman/
|   +-- Setu_Reconciliation.postman_collection.json
+-- data/
|   +-- sample_events.json
+-- requirements.txt
+-- .env.example
+-- README.md
```
