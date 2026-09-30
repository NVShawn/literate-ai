# Playback Controller Public Interface 1.0

The portable `run` entrypoint accepts exactly one positional argument: a JSON object
with exactly one field, `events`, whose value is an array of strings applied in order
from the chart's initial configuration. The callable surface SHALL preserve that
one-object boundary in every language. In Python the public callable is
`main(request)`, not `main(events)`.

The provider SHALL start powered on with transport `stopped` and audio `audible`, then
apply every event using the locked W3C SCXML 1.0 chart. The observable transitions are:

- `play` moves transport `stopped` to `playing`;
- `mute` moves audio `audible` to `muted`;
- `power` leaves the parallel `on` region for `off` and records deep history `last`;
- `resume` from `off` reenters through history `last`.

An event with no enabled transition leaves the configuration unchanged. The provider
SHALL NOT invent unmute, pause, or other events.

The result is one JSON object with exactly these fields and no others:

| Field | Type | Meaning |
| --- | --- | --- |
| `active` | array of strings | Sorted unique active leaf-state identifiers after every event |
| `event_count` | integer | Length of `events` |
| `powered` | boolean | False exactly when `off` is active; otherwise true |

For example, `["play"]` produces active states `["audible", "playing"]` with `powered`
true. `["play", "mute", "power", "resume"]` restores active states
`["muted", "playing"]` through deep history with `powered` true. These examples
illustrate the general chart semantics and SHALL NOT be hard-coded as special cases.
