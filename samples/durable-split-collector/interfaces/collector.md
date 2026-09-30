# Durable Snapshot Collector Capability 1.0

The provider is the portfolio's only process permitted to receive an upstream URL or
credential and the only process permitted to use the snapshot write capability. It
acquires a database-backed lease before one bounded upstream request, stages a complete
window, and atomically advances the current pointer only after validation.

The process accepts one JSON object as specified by the Component and returns only
non-secret status, window, owner, metric count, and retry count. `lease-held` and
`already-complete` outcomes do not contact upstream. Failures preserve the prior current
snapshot and update the existing window's bounded retry state. The returned owner is
always the invocation owner for every status. In particular, a `lease-held` result does
not substitute or reveal the owner of the existing live lease.
