# ADR 0041: Qualify Production Containment Through Trusted Phase Runners

- Status: Accepted
- Date: 2026-09-11
- Approval: Maintainer explicitly accepted ADRs 0040 and 0041 in the development conversation on 2026-09-12.
- Decision owners: literate-ai maintainers
- Release target: 1.1; the scope expansion below is explicitly accepted
- Roadmap: [SEC-360](../roadmap/active-work.md#sec-360-establish-production-containment)

## Context

The maintainer's all-outstanding-work directive includes SEC-360's Linux production
reference, VM-isolated enterprise workers, phase separation and signed enforcement
evidence. Accepted [ADR 0039](0039-1.1-capability-boundaries.md) instead bounds 1.1
claims to explicitly authorized local development and requires another scope decision
before production containment becomes a release requirement. Neither closing SEC-360
as unsupported nor silently implementing a different security boundary resolves this.

The maintainer accepted expanding 1.1 to the complete SEC-360 obligation through
the design below. This supersedes ADR 0039's local-development-only release scope;
SEC-360 remains open until implementation and acceptance are complete. Design
approval does not provision hosts, install privileged services, select accounts or
set model budgets. Execution and publication retain their applicable authorization.

Inspected at `873426f2f350f9b6c641aaa1c14b2a1ad5eb006f`:

- `src/literate_ai/security/isolation/contracts.py` defines ordered levels and exact requests,
  policies, observations and decisions. `IsolationObservation` is caller-supplied;
  it is not an authenticated attestation.
- `src/literate_ai/security/isolation/evaluation.py::evaluate_isolation_policy` evaluates those
  reports without launching a process or granting execution authority.
- The [existing threat model](../architecture/production-containment-threat-model.md)
  already specifies controls, phase separation and the OPS-300 dependency. Reuse it;
  do not create a second control vocabulary or claim its experimental schemas are
  already published.
- [Current user guidance](../user/security.md) accurately describes host execution
  and same-user limitations. The narrower source-observation sandbox is not a
  production generation/build/test backend.

## Decision

### Separate the trusted launcher from untrusted work

Use an operator-owned runner outside the generated process's administrative and
filesystem authority. The coding agent must not hold the runner's control socket,
signing material or unrestricted host shell. A wrapper in the same broadly privileged
agent session cannot satisfy this boundary. Host administrators, kernels, hypervisors
and the provisioner's control plane remain explicitly trusted.

Extend the existing authorization and OPS-300 evidence chain, rather than introducing
another permission system. Before launch, bind the exact subject, stage, policy,
runner/runtime/image, target OS/architecture, toolchain, input/output closure, resource
and egress policies, actor and expiry. The trusted launcher validates and consumes
the scoped grant; changed, expired, revoked or replayed grants refuse before launch.
Post-run observations remain distinct from authorization. An observation marked
`reported-sufficient` alone cannot advance a receipt or authorize another process.

### Deliver the existing platform obligations in order

The Linux reference uses a maintained, explicitly pinned OCI-compatible runtime
through a narrow runner adapter, not a new handwritten namespace launcher. Its
reviewed profile must enforce the existing read-only input, separate output,
environment, credential, device, network, resource and process-tree controls.
Namespaces, mount policy and seccomp are complementary controls: the Linux kernel
documentation explicitly distinguishes syscall filtering from a complete sandbox.
See [seccomp guidance](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
and the [OCI Linux configuration contract](https://github.com/opencontainers/runtime-spec/blob/main/config-linux.md).

Resource limits use an operator-provisioned, verified cgroup-v2 hierarchy together
with bounded output storage and wall time; absence of delegation or a required
controller is unsupported, not permission to omit its budget. Runtime and kernel
versions must be pinned in the qualification matrix, not inferred from a runtime
name. See the [kernel's cgroup-v2 interface](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).

Then qualify independently provisioned VM runners for enterprise macOS and Windows
execution, as SEC-360 already requires. The guest OS, image and toolchain must match
the claimed target: a Linux guest on a macOS host is not native macOS execution.
Linux success does not close the VM or enterprise-review acceptance gates. No host
kernel changes, runtime installation or VM provisioning occur implicitly.

### Keep every phase's privileges independent

Acquisition and resolution use approved endpoint policies and return immutable
inputs. Generation receives only its declared authority, fresh outputs and restricted
provider access; private acceptance material stays in a separate verifier context.
Build, generated tests and application execution receive no provider or release
credentials and deny network unless the phase's explicitly reviewed contract requires
restricted access. Grants and writable volumes never carry over implicitly.

For credentialed phases, distinguish narrowly brokered task authentication from
ambient credentials. Keep reusable secrets in the trusted broker, outside generated
code, prompts, logs and artifacts. A provider which cannot operate within the reviewed
broker/egress boundary is unsupported for production until qualified; its existing
explicit local-development support does not change. Key names are metadata, not
authentication or execution authority.

### Authenticate evidence and stop safely

OPS-300 binds the runner's trusted identity to the exact grant, runtime profile,
inputs, observed controls, exit status, output identities and cleanup result. Verify
issuer trust, expiry/revocation, freshness and complete retained bytes before receipt
promotion. Runtime success without authenticated evidence is unqualified.

Cancellation, timeout or connection loss terminates the owned execution tree and
preserves attributable failure evidence. Cleanup failure quarantines that runner for
operator recovery; do not reuse it or report a clean run. Revocation handling and
retention rules must be explicit operational policy, not a post-run boolean.

## Implementation and acceptance

First integrate and freeze the public isolation schemas and APP-240/OPS-300 authority
bindings. Next qualify Linux phase execution; then the required VM platforms and
provider bindings; finally bind their signed matrix to release gates. Update the
existing SEC-360 checklist only after approval, retaining every unfinished outcome.

Use independent, disposable, non-destructive conformance checks for input immutability,
output custody, environment/device exclusion, denied/restricted egress, finite resource
budgets, process cleanup and evidence authentication. Include both legitimate work and
refusal cases; a backend which rejects everything is not qualified. Run on each actual
claimed platform. Mocks, configuration inspection and local-report recomputation do
not replace enforcement evidence. The enterprise profile also requires independent
security review, incident/revocation procedures and retention evidence.

## Alternatives and consequences

Keeping 1.1 local-development-only is a defensible smaller release, but would require
the maintainer to amend the all-outstanding-work scope explicitly; it cannot silently
close or defer SEC-360. Reusing only the existing observation sandbox would omit phases,
authentication and platform obligations. A VM-only first implementation simplifies
kernel separation but does not fulfill the requested Linux OS-sandbox reference by
itself. Building our own low-level sandbox would increase the trusted code and review
burden without removing the need for platform primitives and operational ownership.

The proposed path adds deployment and review costs and may delay 1.1 materially.
Existing local workflows remain explicitly uncontained. A failed production request
never falls back to them. Rollback disables the new backend and invalidates its current
evidence; it neither changes published schema meanings nor downgrades a requested
security level. No release-ready claim follows from acceptance of this ADR alone.
