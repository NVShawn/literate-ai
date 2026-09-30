# Durable Snapshot API Capability 1.0

The provider is an independently deployed, read-only HTTP service. It exposes:

- `GET /health`, returning a successful readiness response; and
- `GET /snapshot`, returning canonical JSON with keys `snapshot`, `freshness`, and
  `progress`.

When present, `snapshot` contains string `window`, UTC string `published_at`, and an
ordered `metrics` object whose values are finite JSON numbers. `freshness` names the
current window and publish time. `progress` contains the current collection window,
state, retry count, and lease expiry, with nulls when no collection exists.

The consumer supplies only the service base URL. This capability grants no cache path,
write operation, upstream URL, or credential. A missing snapshot is represented
explicitly and never causes the API to collect.
