# Reference HTTP shared cache

The explicitly started reference service implements the artifact-byte protocol in
[accepted ADR 0044](../decisions/0044-action-dag-and-shared-cache.md).
It uses the existing local shared-cache owner for committed publication, quota,
retention and safe file access. It stores opaque bytes; a hit grants no acceptance,
build, package, release or deployment authority. Consumers still verify the current
complete manifest, payload hash and their own target/ABI/provenance requirements.

## Start an instance

Use an installed framework Python environment. Supply an existing private
`SharedCacheConfiguration` JSON file (the
`urn:literate-ai:schema:v2:shared-cache-configuration` contract), a private absolute
physical cache-root path with no symlink ancestors, and independently generated
reader and writer secrets through the environment. Credentials must be distinct,
16–8,192 printable ASCII characters; do not put them in Git, shell arguments or
the configuration JSON. The JSON's `credential_reference` is an opaque client-side
resolver reference, not a server secret. Resolve each client's reference privately.

A portable configuration example is below. Replace the example endpoint and
private resolver reference before deployment; no credential material belongs here.

```json
{
  "schema": "urn:literate-ai:schema:v2:shared-cache-configuration",
  "scope": "team",
  "namespace": "team-cache",
  "local_root_reference": "shared-cache",
  "policies": [
    {
      "schema": "urn:literate-ai:schema:v2:shared-cache-namespace-policy",
      "namespace": "package",
      "mode": "read-write"
    }
  ],
  "maximum_bytes": 1073741824,
  "retention_seconds": 604800,
  "endpoint": "https://cache.example/cache",
  "tls_required": true,
  "credential_reference": "cache/team-writer"
}
```

Use the same scope and configured namespace on all clients. A read-only client
selects `read-only` policies and its own private reader credential reference;
these permission differences preserve the shared storage identity.

```sh
python -m literate_ai.adapters.cache.http_reference \
  --configuration /private/cache-configuration.json \
  --root /private/cache-root --port 8765
```

The operator supplies `LITAI_CACHE_READER_TOKEN` before startup. Supply
`LITAI_CACHE_WRITER_TOKEN` to permit writes, or leave it absent for a read-only
service. Each configured artifact namespace's access policy independently governs
writes; a writer secret cannot bypass a read-only namespace. Writers can read;
reader secrets cannot write. Authorization is checked before path/body processing.
No credentials, request paths, payloads or raw failures enter service logs.

The listener always binds IPv4 loopback. Remote deployments require a separately
operated HTTPS reverse proxy on the shared host. Keep client `tls_required: true`,
forward the exact configured endpoint path and Authorization/If-None-Match headers,
disable redirects and shared response caching, enforce finite upstream connection,
header/body and deadline limits, and restrict reachability to the intended clients.
Do not expose the loopback HTTP port directly to a LAN. This command installs no
service, proxy, firewall rule or listener outside loopback. Run it as an unprivileged
user with a dedicated private root and one active instance per root. Stop through
the operator's foreground interrupt or supervisor; restart discards pending uploads
and preserves committed entries.

Use one instance/root/configuration/credential pair per writer group. It enforces a
group budget, not individual-machine quotas within a shared writer credential.
Separate instances and roots are required when writers require separate quotas.

## Paths and immutable publication

For endpoint `https://cache.example/cache`, configured namespace `team-cache` and
artifact namespace `package`, the exact paths are:

```text
/cache/team-cache/package/objects/<64 lowercase SHA-256 hex digits>
/cache/team-cache/package/manifests/<64 lowercase key digest>.json
```

Only configured artifact namespaces are served. Query strings, escaped path
segments, traversal, extra separators and alternate digest spellings refuse.
GET returns exact identity-encoded bytes, or 404 for absent/expired manifests.
Authentication is required for every GET and PUT. All responses use `no-store`.

Writers PUT the complete payload first, then the canonical manifest JSON. Both
requests require exactly one `If-None-Match: *` and a single bounded decimal
Content-Length. Chunked/ambiguous framing and encoded request bodies refuse.
Objects must hash to their URL digest. The manifest's namespace/key must match
its URL, its configuration must match the local owner, and its size/hash must
match the already uploaded object. Package/container target, ABI and SBOM fields
remain mandatory. Manifests without payloads refuse.

Creation returns 201. An existing identical entry returns 412, which the existing
HTTP client follows with a GET and exact byte comparison. A different manifest
under the same key refuses before eviction or replacement. Corrupt objects and
unsafe local paths refuse. Publication reuses the local owner's atomic writes
and locks; readers can observe misses during eviction and must never infer success
from status alone. Repeating an exact expired manifest can renew it through the
same local retention owner. Concurrent identical writers are idempotent while
their entries remain in the bounded cache; eviction or staging expiry can require
a fresh explicit publication attempt.

## Resource bounds

`maximum_bytes` bounds committed object-plus-manifest inventory **per configured
artifact namespace**, with the local owner's existing bounded inventory and oldest
entry eviction. It is not one aggregate quota across all configured namespaces.
Expired manifests are misses on reads; writes reclaim expired entries and objects
unreferenced by any retained manifest. Raw content-addressed object GETs can still
see such objects before reclamation. Reads do not renew retention.

Pending payloads stay in memory, never in an unbounded disk spool. Defaults are
64 MiB total pending bytes, 1,024 pending objects, 60-second pending lifetime,
64 MiB maximum individual object, four connections, and a 30-second socket lifetime.
Expiry is collected lazily on subsequent requests without renewing entries on GET.
Interrupted uploads create no pending/committed partial object. Restart loses only
pending data. Capacity pressure refuses rather than queueing more application work.

Flags expose `--pending-bytes` (up to 4 GiB), `--maximum-object-bytes` (up to 1 GiB,
also bounded by pending and committed policy), `--pending-seconds` (up to 3,600),
`--maximum-connections` (up to 32), and `--deadline-seconds` (up to 300). Manifest
requests are capped at 1 MiB. The connection bound applies before starting handler
threads; excess connections close. The socket lifetime covers trickle headers and
bodies, including authenticated requests. It does not cancel an already executing
local publication: a disconnected client must treat the result as uncertain and
re-read exact bytes.

Provision memory for pending payloads **plus** concurrent request/response buffers,
manifest buffers and the local owner's bounded inventory. Each connection can hold
an object body and copies during verification/publication; the pending byte quota
alone is not the process-memory bound. Disk peak includes local atomic-write
temporaries in addition to committed inventory and filesystem overhead. Provision
these headrooms before raising budgets for real native artifacts. This reference
buffers complete objects and is not a streaming multi-tenant cache appliance.

## Qualification boundary

Real loopback HTTP tests exercise the existing client with cold/warm/read-only,
corrupt and conflicting entries, identical concurrent writers, authentication/path/
framing refusal, committed and pending quotas, expiry, interrupted bodies and slow
headers. These tests do not establish production TLS deployment, FreeBSD unit key
coverage, remote worker transport, two-host speedups or final release qualification.
Those remain required under issues 4 and 236 and the active release roadmap.
