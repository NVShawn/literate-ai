# Native gRPC oracle declarations

The opt-in `literate-ai/ipc-surface-conformance-acceptance@2` document loads native
gRPC expectations for a persistent-service Component. Loading validates the declared
protobuf closure and cases. Default native lifecycle dispatch is implemented; installed
integration and live generated-surface qualification remain pending under
[IPC-SURFACE-001](../roadmap/active-work.md).
Native `@2` gRPC declarations select the default native adapter. Missing optional
dependencies refuse before socket allocation or service launch. Legacy `@1`
non-REST documents still require an explicit protocol adapter. Loading a document
is not acceptance.

Keep the verifier document under `verification/acceptance/<component>.json` (or
its named-entrypoint oracle path). Do not reference it from generator-visible
Component specifications. Existing `@1` documents retain their shape and identities;
REST authors do not need to migrate.

Version 2 keeps the common `specification_set_identity`, `surface_version`,
`compatibility` and `process` fields, and requires `protocol: "grpc"`.
`declared_schema_identity` is the SHA-256 identity of the exact binary protobuf
`FileDescriptorSet`. Every dependency must be included; global protobuf registrations
cannot supply omitted files. Declaration order may differ from dependency order.
This raw-byte identity is not a claim of cross-runtime canonical serialization.

`description_probe` has exactly two required fields:

| Field | Value |
| --- | --- |
| `descriptor_set_hex` | Complete descriptor bytes in lowercase hexadecimal, without whitespace |
| `timeout_milliseconds` | Integer from 1 through 120000 for reflection observation |

`request_cases` contains 1 through 64 objects. Every field below is required;
unknown fields refuse loading.

| Field | Value |
| --- | --- |
| `method` | Exact `/fully.qualified.Service/Method` |
| `input_type`, `output_type` | Protobuf full names matching the descriptor method |
| `cardinality` | `unary-unary`, `unary-stream`, `stream-unary`, or `stream-stream` |
| `requests`, `responses` | Ordered arrays of lowercase hexadecimal message bytes |
| `status` | Canonical gRPC status name such as `OK` or `INVALID_ARGUMENT` |
| `status_message` | Expected text, including an empty string when appropriate |
| `timeout_milliseconds` | Integer from 1 through 120000 |
| `error_details` | Array of objects with exactly `type_name` and hexadecimal `message` |

Unary requests contain exactly one message. Successful unary responses contain
exactly one message; failed unary calls declare none. Streams may be empty, and a
failed response stream may contain messages preceding the failure. Successful calls
cannot declare error details. All messages and details must decode against the
pinned descriptor closure, satisfy required fields and contain no undeclared fields.

The entire JSON document remains limited to 1 MiB, including hexadecimal expansion.
Internal per-message and descriptor bounds do not raise that public document limit.
Each message sequence has at most 1024 entries and each case at most 32 error details.
Loading native declarations requires the optional `grpc-acceptance` protobuf runtime;
REST loading does not require protobuf. Native execution must also enforce process
and per-call deadlines and prove served reflection matches the declared closure.

## Descriptor comparison boundary

The internal comparator accepts a complete immutable tuple of reflected
`FileDescriptorProto` byte strings, limited to 128 files and 4 MiB in total.
It matches the exact declared filename set and rejects missing, extra or repeated
filenames, including identical duplicates. Each parsed file is encoded
deterministically by the same protobuf runtime on both sides, then compared;
the outer filename collection is sorted, and explicit JSON names equal to the
bound runtime's implicit default are omitted from comparison. This handles protoc
populating a default `json_name` which Python reflection omits. Custom JSON names,
repeated declarations, dependencies, options and source information remain significant.
Normalization identity schema `literate-ai/grpc-descriptor-normalization@2` records
this policy; raw declared and observed byte identities remain unchanged. The rule
follows [protobuf JSON field-name semantics](https://protobuf.dev/programming-guides/json/).
Unknown wrapper fields in the original `FileDescriptorSet` refuse loading because
file-level reflection cannot account for them.

Successful comparison records the declared raw identity, the ordered raw-observation
identity, a normalized identity scoped to the protobuf version and implementation,
and sorted filenames. The comparison does not modify the declared raw identity or
claim that protobuf serialization is canonical across runtimes. This follows the
[protobuf serialization limitation](https://protobuf.dev/programming-guides/serialization-not-canonical/).
The transport must collect the complete descriptor closure from the
[gRPC reflection response](https://github.com/grpc/grpc-proto/blob/master/grpc/reflection/v1/reflection.proto)
under its deadline.

## Observation time budget

The lifecycle passes the process time remaining after readiness to
`IpcSurfaceProbe.observe_with_timeout`. Native transports must use the smaller of
that remaining budget and each declared reflection/call timeout, reducing the
remaining budget between operations. They must not restart the full process timeout
after readiness or for each RPC. The lifecycle rechecks process health and elapsed
time after observation and shuts down the service on refusal.

Existing adapters implementing only `observe` remain supported through a fallback.
That compatibility path cannot preempt a blocking adapter; the post-observation
check prevents a late result from being accepted. Native transport must implement
the budget-aware operation before its execution path is qualified.

## Native transport

`GrpcIpcSurfaceProbe` supplies loopback-only native observation through the Python
adapter API and default `@2` lifecycle dispatch. Its channels disable HTTP proxy
routing and accept only the lifecycle's `http://127.0.0.1:<port>` address form.
Readiness uses native channel readiness. Observation collects reflection descriptors
and drives unary/unary, unary/stream, stream/unary and stream/stream calls with the
remaining lifecycle budget. Installed default-dispatch qualification remains pending.

The optional `grpc-acceptance` dependency set pins gRPC, reflection and status
helpers at 1.84.0 alongside protobuf 7.36.2. Dependencies remain optional for REST.
The adapter validates response and error-detail protobuf types and compares ordered
response bytes, canonical status names, status text and typed error-detail bytes to
the oracle. These message expectations are wire-exact. Rich status metadata must
agree with the terminal gRPC status. Invalid messages and bounded-stream violations
refuse observation; mismatching valid responses produce a rejected conformance
observation. Calls and channels are cancelled or closed on exit.

Reflection may repeat a dependency across different request replies; identical
repeats are collected once, while conflicts and duplicates within one reply refuse.
Missing replies, wrong request correspondence, extra files and incomplete closures
refuse. The total reflected payload is bounded to 4 MiB. Observed RPC payloads across
all cases are bounded to 8 MiB, with existing per-message, sequence and detail limits;
channel receive/send limits and a 64 KiB metadata bound apply as well.
Raw reflected payloads are retained in `served_description_bytes`; raw response,
status and detail observations are retained in the versioned `protocol_detail` JSON.
The evidence also binds normalized reflection and per-case observation identities.

Real local fixture tests cover these paths. Default-dispatch tests launch and stop an
actual child service on acceptance, response refusal and deadline failure. Its
acceptance record retains raw reflection bytes and RPC details, whose per-case
identities can be independently recomputed. Installed default-dispatch qualification
and a live generated gRPC surface remain required for the complete release claim.
