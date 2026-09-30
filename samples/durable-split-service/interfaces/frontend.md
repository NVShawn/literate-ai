# Durable Split Frontend Capability 1.0

This language-neutral contract describes the independently deployable browser boundary.
The frontend server consumes only an operator-supplied base URL implementing
`literate-ai.durable-snapshot-api`. Its browser document fetches the same-origin
`/api/snapshot` path; the server may proxy that path to the supplied API base URL.

The frontend SHALL expose `GET /health` and `GET /`, bind only loopback for conformance,
and render semantic `main` and `region` landmarks. It SHALL NOT receive a database path,
an upstream URL, or an upstream credential, and SHALL NOT open SQLite or contact an
upstream source. Restarting it cannot start or invoke collection.
