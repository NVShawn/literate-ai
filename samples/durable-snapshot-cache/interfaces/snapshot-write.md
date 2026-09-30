# Durable Snapshot Write Capability 1.0

The cache provider is the sole owner of schema migration. It uses SQLite foreign keys,
WAL, `PRAGMA busy_timeout=5000`, and an explicit idempotent migration for this semantic
schema (constraint spelling and declaration order may vary, but names, type affinities,
nullability, keys, and references may not):

```sql
CREATE TABLE snapshot (
    snapshot_id INTEGER PRIMARY KEY,
    window TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('collecting', 'published', 'failed')),
    started_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE snapshot_metric (
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(snapshot_id),
    metric TEXT NOT NULL,
    value NUMERIC NOT NULL,
    PRIMARY KEY (snapshot_id, metric)
);
CREATE TABLE current_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(snapshot_id)
);
CREATE TABLE collection_window (
    window TEXT PRIMARY KEY NOT NULL,
    selected_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'failed', 'completed')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    retry_count INTEGER NOT NULL,
    retry_limit INTEGER NOT NULL,
    last_error_class TEXT,
    started_at TEXT,
    completed_at TEXT
);
```

Every table and column name above is literal and exhaustive. Do not introduce aliases
such as `window_utc`, `pointer_id`, `singleton`, `name`, `metric_name`, `value_json`, or
plural table names. Store `snapshot_metric.value` as a SQLite numeric value so a
read-only Python SQLite client receives an `int` or finite `float`; do not store a JSON
number as text. Migration leaves `current_snapshot` empty. Publication creates its sole
row with integer `id = 0`. SQLite assigns each non-null integer `snapshot_id`: the
collector omits that column when inserting a snapshot and uses `lastrowid` for all
metric and pointer writes in that attempt.

The collector consumes this already-migrated schema and SHALL NOT execute
`CREATE`, `ALTER`, or `DROP`, nor maintain a competing migration. A missing or
incompatible schema is a failure, never permission to infer or replace it.

Acquire a collection window with `BEGIN IMMEDIATE`. A non-expired lease owned by another
worker fails without upstream work; an expired lease may be reclaimed; a completed
window never runs again. Stage metrics under a `collecting` snapshot. After all expected
finite numeric metrics validate, one transaction marks the snapshot `published`, moves
the current pointer, completes the window, and clears the lease. On failure, mark the
staged snapshot `failed`, increment the same window's bounded retry count, clear the
lease, and never move the current pointer. This capability is granted only to the
collector boundary.
