# ADR 0055: Address remote workers through explicit reachability commands

- Status: Accepted
- Accepted: 2026-11-02; directing user explicitly accepted both ADRs for 1.4
- Date: 2026-11-01
- Decision owners: directing user and literate-ai maintainers
- Roadmap: [COMMAND-WORKER-001](../roadmap/active-work.md#command-worker-001-address-workers-through-reviewed-reachability-commands); governed by [ADR 0006](0006-significant-feature-request-governance.md)

## Context

A machine is not necessarily addressed by a host, port and user. Operators reach
workers through SSH aliases and jump hosts, scheduler allocations, or authenticated
organization connectors. Requiring an invented SSH endpoint for those routes loses
the operator's actual addressing authority.

The existing v1 SSH worker supports a named SSH-compatible executable, but still
requires a literal `username@host`. That executable must also work with SCP. The
existing `command` worker is different: it consumes a typed dispatch request and
returns a typed dispatch result. It is not a remote shell command prefix. Registering
a connector as that dispatcher would not provide working probes, alignment or file
custody.

## Decision

Introduce an opt-in v2 remote worker descriptor with an explicit reachability
contract, without changing v1 worker field sets, identities or dispatch semantics.
Preserve local, SSH and structured-command workers. Do not synthesize SSH endpoints
or automatically migrate registrations.

The remote descriptor names the existing workspace, platform requirements, exact
lifecycle launcher and slot policy, together with these operator-private commands:

- `execute`: a literal argument vector containing exactly one complete
  `{remote_command}` argument. Substitute the platform-specific receiver command
  into that argument without shell evaluation on the controller. The operator
  explicitly supplies any required remote shell invocation in the vector.
- `upload`: a literal argument vector with exactly one complete `{local_file}` and
  one complete `{remote_file}` argument, transferring one staged regular file.
- `download`: the same placeholders, transferring one exact remote evidence bundle
  into a new controller-owned regular file.

Each vector has 1–128 arguments, each at most 4096 characters, with a literal
executable and no NUL, carriage return, newline, partial or unknown placeholder.
Only the three named placeholders are supported, and only in their specified
operation. Do not interpolate environment variables, templates or shell fragments.
Connector identity, scope and routing arguments are private operator configuration,
bound into the worker's canonical identity. Never commit actual destinations,
accounts, allocation identifiers, tokens or organization-specific command vectors.

All three operations are required for an execution-ready remote registration. A
successful execute-only diagnostic must report missing transfer capability rather
than qualify the worker. An operator may supply a private adapter when a connector
does not expose file transfer directly; the framework must not guess a direct SCP
route or assume a JSON command-result channel preserves arbitrary file bytes.

Use one provider-neutral transport owner for connectivity, capabilities, storage,
resource health, alignment, bootstrap, lifecycle execution, cancellation and
cleanup. Existing SSH behavior remains an adapter to that owner. Every operation
uses the same exact selected registration and remaining deadline. Bound process
trees, concurrent operations, output, diagnostics, staging bytes and evidence
bytes; a connector's own timeout does not replace controller-enforced termination.

Keep existing source archives, materialization identities, accepted-source checks,
receiver validation, evidence extraction and independent acceptance. Transfer exit
zero alone establishes no source or artifact identity. Preserve file permissions,
private staging, destination collision checks, digest verification and cleanup
ownership. Never replay an uncertain mutation automatically after reconnect.

Read-only discovery may try multiple explicitly selected connector scopes. Record
which scope actually resolved the target; an internal miss must not imply an
external miss. Authentication errors are distinct from missing, destroyed or
unreachable instances. Use the chosen scope explicitly for subsequent operations;
do not change the operator's global connector default. Discovery cannot authorize
provisioning, starting, deleting or resizing a machine.

## Acceptance and execution order

After explicit acceptance, record the implementation work and its release target
through the roadmap CLI before source changes. Implement the v2 descriptor and
strict codec, shared execution/transfer owner, all worker callers, then operator
CLI and documentation. Preserve complete v1 compatibility controls.

Required evidence includes:

1. Real command-addressed worker registration, exact nonce handshake, native
   platform/storage observation and read-only alignment, using a private catalog.
   Try both applicable organization scopes and distinguish authentication failure
   from target absence. No placeholder or fake endpoint counts as this evidence.
2. Upload, receiver execution and download through the same declared route; exact
   source and artifact custody; a real native Component lifecycle and retained
   diagnostics on failure. No direct-network fallback.
3. Scheduler/jump routing fixtures, argument and path quoting, unknown/partial
   placeholders, executable substitution, configuration drift, missing transfer,
   destination collisions, corrupted/truncated transfer, oversized output and
   evidence, stale receiver bindings, timeout and descendant cleanup controls.
4. All connectivity, probe, health, alignment, staging and cleanup callers use the
   same transport. A passing handshake cannot bypass missing tools, disk floors,
   provider credentials, model verification or platform-specific readiness.
5. Native POSIX qualification first; native Windows evidence before claiming a
   Windows connector supported. Installed non-editable CLI evidence, exact-head
   CI and integrated release gates remain required for the scope claimed.

## Consequences

Operators can describe the commands that actually reach their workers, while
framework code remains independent of specific connectors and schedulers. This
requires coordinated execution and file-transfer support rather than loosening
the current endpoint parser. Existing structured-command dispatch remains useful
for external systems implementing the full typed protocol. A new registration
does not establish readiness, native qualification or release completion.
