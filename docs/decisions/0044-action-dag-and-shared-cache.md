# ADR 0044: Schedule exact lifecycle actions across compatible workers and share verified caches

- Status: Accepted
- Date: 2026-09-24
- Accepted: 2026-09-25
- Decision owners: Literate AI maintainers
- Roadmap: RELEASE-INTEGRATION-002 in `docs/roadmap/active-work.md`

## Context

The 1.1 implementation can overlap whole-Component work inside static topological
layers when a caller supplies one exact worker assignment per Component. Production
CLI composition does not supply that routing, command and SSH overlap tests use local
test handlers, and every node in a layer must finish before any node in the next layer
starts. The scheduler also unions action dependencies into whole-Component edges, so a
consumer action cannot start when its own prerequisites are ready while unrelated later
provider actions continue. This does not meet the requested make/Bazel-like behavior:
use all compatible entries in private `workers.json` whenever independent SDLC work is
ready, while preserving correctness.

The repository separately has accepted-source caching, resumable local test
checkpoints, Bazel's ordinary local state, and provider-specific package outputs. These
do not establish a trustworthy shared test cache, a reusable compiler cache, or shared
package/container custody. The existing test-cache plan explicitly deferred a team-wide
cache, and its dependency-key completeness remains open. A cache hit therefore cannot
currently be treated as accepted lifecycle evidence.

[Bazel remote caching](https://bazel.build/remote/caching) natively supports a local
disk cache and shared HTTP/HTTPS or gRPC remote caches with action-cache and
content-addressed-storage namespaces. [`sccache` WebDAV
storage](https://github.com/mozilla/sccache/blob/main/docs/Webdav.md) supports a
self-hostable HTTP service, while its broader configuration also supports local disk,
Redis, and memcached backends and local-first multi-level chains. A WebDAV-capable HTTP
service can therefore be deployed on a LAN or behind an operator-configured DNS name
without requiring any cloud provider.

## Decision

### Schedule the action DAG, not whole Components

Project the exact `(Component revision, lifecycle action)` graph from the resolved
execution plans. Preserve generation, indexing, source admission, build, test,
execution, independent acceptance, link/import, package, and finalization boundaries
as distinct nodes when their contracts permit independent execution. An action becomes
ready immediately after all of its own required predecessor evidence is accepted; it
does not wait for unrelated members of a former topological layer.

Use one deterministic event-driven ready queue for the run. Canonical action identity
orders equally ready work. The scheduler maintains explicit per-worker slots and a
global cap. Omitted `--jobs` means use every safe available slot; an explicit value is
only a cap and never permission to violate worker capacity.

### Select every safe compatible worker

Read the private execution-worker catalog and fresh private observations through the
existing user-configuration adapters. Derive an identity-bound eligibility plan from
action requirements, target profile, OS/architecture, toolchain and accelerator
capabilities, health/capacity decision, transport support, and configured slot count.
Default a worker with no explicit safe capacity to one slot. Unknown, stale, unhealthy,
incompatible, or transport-incomplete workers are not eligible.

Assign ready work dynamically to idle eligible slots. Prefer a worker with an already
verified exact cache/artifact affinity, then stable worker ID, so scheduling is
reproducible without sacrificing available parallelism. Bind the selected worker,
catalog/observation identities, action, inputs, predecessors, cache decisions, and
deadline into the dispatch request and result. A changed route or stale observation
cannot resume as exact-current evidence.

Command and SSH handlers must execute a serializable action request on the selected
worker. A controller callback that runs locally is not a remote implementation.
Predecessor exports cross workers only through verified content-addressed custody.
Failure cancels transitive descendants, not independent branches. Accepted independent
work remains reusable under its exact identity.

### Use layered, provider-neutral cache configuration

Define private cache configuration with a local content-addressed baseline and an
optional HTTP(S)/WebDAV endpoint addressed by operator-configured URL or DNS name.
Configuration carries scope (`user`, `team`, `organization`, or `company`), namespace,
read/write mode, size/retention policy, TLS requirements, and credential references;
it never stores credentials or resolved private hosts in project authority.

Use separate namespaced adapters over that configuration:

- Bazel actions use Bazel's supported disk and remote-cache protocols.
- Non-Bazel supported compiler actions use `sccache`; Bazel actions do not add a
  redundant `sccache` layer unless an explicit measured policy proves it useful.
- Test results use the existing finalized receipt model only after dependency closure,
  toolchain, environment, action, and oracle identities are complete.
- Packages and containers store immutable bytes plus independently verifiable manifests
  keyed by the exact accepted product closure, provider/tool identity, target/ABI, SBOM,
  and packaging or image configuration.

Local-only configuration remains fully supported. The framework does not provision a
cloud account or require a particular server product. A shared WebDAV-capable service
is the portable baseline because it can run on the same LAN and is supported by both
Bazel-style HTTP caching and `sccache`; native provider adapters may additionally use
gRPC, Redis, memcached, or another explicitly configured backend.

### Cache hits are untrusted inputs

Every restored object, test result, package, image, manifest, and receipt is rehashed
and revalidated against the current action and complete authority before use. A cache
hit skips only the work whose exact output is proven; it never skips indexing,
validation, required execution, independent acceptance, or release verification unless
the owning contract explicitly defines a current reusable receipt for that gate.

Corruption, ambiguity, missing content, incompatible toolchains, unsafe transport,
changed scope, incomplete keys, or stale evidence is a miss or a typed refusal according
to the owning contract. Shared writers are least-privilege and may be restricted to CI;
read-only consumers remain supported. Cache availability is an optimization, never a
correctness dependency.

## Qualification and rollout

Implementation proceeds under the explicit maintainer acceptance recorded on
2026-09-25. Qualification must prove:

- a dependent action starts as soon as its own prerequisites finish while an unrelated
  slow action continues;
- interface-only consumer work can overlap later provider phases, while artifact
  consumers wait for exact accepted exports;
- all independent ready actions occupy distinct compatible worker slots, explicit
  caps are honored, and no worker is double-booked beyond declared capacity;
- incompatible, stale, unhealthy, or unreachable workers never receive work;
- command and SSH fixtures perform real remote/process actuation, byte handoff,
  cancellation, recovery, and overlap rather than invoking a local callback;
- failure cancels descendants only and exact-current recovery rejects changed routing;
- cold, warm, partial, corrupted, poisoned, unavailable, read-only, and concurrent
  local/LAN cache cases preserve identical accepted outputs;
- the second exact build demonstrates measured Bazel and/or sccache hits without
  weakening toolchain identity, and the test-cache assertion is proven rather than
  assumed;
- package and container restores verify complete manifests, payloads, modes, SBOMs,
  target/ABI and provider identities before acceptance; and
- installed-wheel Linux, macOS, and Windows workers plus hosted CI exercise the same
  configuration and fail-closed behavior.

Roll out in slices: shared scheduler primitive and deterministic fixtures; production
worker composition and real transports; local cache adapters; optional LAN endpoint;
then package/container reuse and cross-platform release qualification. Each slice keeps
uncached execution available and authoritative.

## Consequences

The controller becomes a bounded action scheduler, but not a fleet provisioner. Private
dispatchers still own provisioning, queueing, leases, priorities, and machine lifecycle.
More concurrency increases resource pressure, so worker-health admission and explicit
slot limits are part of correctness rather than optional telemetry.

Shared caching reduces rebuild time only for reproducible actions with complete keys.
It adds server security, namespace, eviction, poisoning, and credential-management
concerns; the design contains those in private configuration and verifier-owned
admission. Network cache failure may reduce performance but cannot make a valid build
or release depend on the cache being online.

## Artifact transport implementation

The HTTP artifact adapter publishes payloads before manifests using conditional
`PUT` (`If-None-Match: *`) and verifies each published entry with a bounded `GET`.
The operator provisions the namespace directories on a server that honors conditional
writes. Redirects are refused, including redirects to the same host. Private bearer
credentials are resolved outside project authority and are never included in error
text. Every read verifies the full expected manifest and the payload digest and size.
Missing partial entries are misses; conflicting or corrupt entries are typed refusals.
The local-first adapter reports remote unavailability as a miss and retains successful
local publications when the network is unavailable.

Manifest cache identity now accepts a storage projection of scope and namespace.
Access mode, credential reference, endpoint, local placement, and retention remain bound
by the full dispatch configuration, but do not change shared content identity. This
allows read-only consumers to verify writer entries and allows verified local mirrors.
Exact legacy configuration-bound manifests remain readable with their original
configuration. Product authority, provider, target/ABI, SBOM, payload identity, and
mode remain mandatory verification inputs; the storage projection grants no acceptance.
Production action composition, compiler measurements, and platform qualification remain
release-blocking work under RELEASE-INTEGRATION-003.

## Private production configuration and read-only Bazel custody

Standard rebuild loads `shared-cache.json` from the private configuration directory
reported by `litai config paths`; `LITAI_SHARED_CACHE_CONFIG` may select an absolute
alternative. An absent default file leaves caching unconfigured. Explicit missing,
unsafe, malformed, duplicate-key, or changed configuration is refused. The cache
contract binds the rebuild identity, and credential references resolve through
`env:NAME`. Bearer credentials live in a temporary private Bazel RC for each
invocation and are excluded from command arguments and diagnostics.

Bazel's remote read-only flag does not prevent disk-cache writes. Read-only builds
therefore use a disposable copy of local cache entries, independently of the shared
storage directory, with remote uploads disabled. The copy excludes expired entries
and is bounded by configured bytes, 100,000 entries, and 30 seconds; unavailable,
changing, or over-budget contents become an empty view. No writable hard links are
used. Bazel verifies restored content, and ordinary staging cleanup removes the view.
Planning without an execution workspace disables the disk cache for read-only mode.
This preserves cache correctness at the cost of copying; large-cache measurements and
shared writer retention enforcement remain qualification work.

## Cargo compiler-cache composition

Cargo compiler caching binds the selected sccache executable's content identity
into the private cache binding before rebuilding. Each build owns its foreground
server and private configuration, bounded startup, statistics, and shutdown calls;
an unavailable server falls back to the compiler. The caller's ambient sccache
settings and compiler wrappers do not select a different cache process. Credentials
remain private, and retained process output is redacted.

Rust cache keys include working-directory and Cargo-environment paths. Exact Cargo
targets therefore use stable disposable staging under the runtime object root,
protected by the existing cross-process cache lock. Staging is rebuilt from the
admitted source and removed after use. Incremental Rust compilation is disabled;
link steps are not claimed as cacheable. Read-only sccache disk storage also requires
a disposable view because the native reader updates file modification times.

Captured Cargo build observations may include strictly validated cache-tool and
configuration identities and advisory counters. These counters do not establish
acceptance or replace the exact compiler/source/dependency and independent acceptance
contracts. Cache-tool native dependency observation now binds recursive native image,
inspector, and loader-resolution evidence into cache identity and the Standard build
dependency graph. The runtime revalidates it at lifecycle entry/exit (including
artifact-cache reuse) and around each compiler-cache session with that process's
loader environment. Unsupported loader controls and identity drift refuse execution;
they are not availability fallbacks. Observation uses immutable canonical bytes and
preserves distinct cache-runtime roles when a compiler also uses the same library.
Full recursive observation has measurable per-build cost; throughput and platform/LAN
qualification remain open under RELEASE-INTEGRATION-003.

Standard native C++ command drivers share a stdlib-only compilation helper: each
translation unit produces one disposable object through the selected cache tool,
and the locked compiler links those objects without a cache wrapper. The helper
is embedded in the exact command authority, with documentation removed and bytes
compressed to preserve the existing command-token size limit. Single and multiple
entrypoints use the same boundary. Only recognized framework command drivers receive
the private wrapper; an ambient wrapper variable does not select a tool. Captured
native build observations retain the same advisory cache fields as Cargo.
