# Durable Snapshot Read Capability 1.0

Open the supplied SQLite database with URI `mode=ro`, enable
`PRAGMA query_only=ON`, and set `PRAGMA busy_timeout=5000`. Readers resolve the one
`current_snapshot` pointer, require its target status to be `published`, and return only
that snapshot's metrics ordered by metric name. A `collecting`, `failed`, or aborted
snapshot is never current.

The read projection uses these exact table and column names:

- `current_snapshot(id, snapshot_id)`;
- `snapshot(snapshot_id, window, status, started_at, published_at)`;
- `snapshot_metric(snapshot_id, metric, value)`; and
- `collection_window(window, selected_at, state, lease_owner, lease_expires_at,
  retry_count, retry_limit, last_error_class, started_at, completed_at)`.

All `snapshot_id` columns use SQLite `INTEGER` affinity and the cache migration assigns
the key. Windows, timestamps, states, owners, error classes, and metric names use
`TEXT`; retry values use `INTEGER`; metric values use `NUMERIC`. Before the first
publication `current_snapshot` has no row. After publication it has exactly one row
with integer `id = 0` and a non-null snapshot reference.

Readers SHALL use `snapshot_metric.metric` as the metric name and
`snapshot_metric.value` as its finite JSON number. They SHALL NOT guess aliases,
plural table names, or an alternative cache schema.

Read freshness/progress from the current snapshot plus the newest `collection_window`:
window, publish time, collection state, retry count, lease owner, and lease expiry.
This interface grants no migration, INSERT, UPDATE, DELETE, transaction, upstream, or
credential authority. Any attempted write through the reader connection must fail.
