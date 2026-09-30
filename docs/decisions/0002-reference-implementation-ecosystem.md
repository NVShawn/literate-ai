# ADR 0002: Use Python for the Reference Kernel and Isolate Node Tooling

- Status: Accepted; amended for CycloneDX validation
- Date: 2026-08-02
- Decision owners: literate-ai maintainers
- Supersedes: the ambiguous root-level npm scaffold

## Context

The first architecture commit placed `package.json` and `package-lock.json` at the
repository root solely to pin and execute the OpenSpec CLI. That layout made the
repository look like a Node.js framework even though the domain design proposed Python
for OVA-compatible extraction. It also made a contributor-only npm dependency tree look
like part of the framework's runtime supply chain.

literate-ai needs a conservative implementation ecosystem. OVA's reusable vertical
slice, schemas, orchestration, tests, and compatibility boundary are Python. The portable
value lies in versioned wire contracts and lifecycle semantics, not in requiring every
consumer to run one language runtime.

## Decision

The initial reference kernel SHALL be a Python 3.11+ distribution named `literate-ai`
with import package `literate_ai`. It admits one exactly pinned runtime capability:
`cyclonedx-python-lib` with its strict JSON-validation extra, behind the dependency
adapter. This implements the standards boundary for every generated source and resolved
SBOM without teaching the neutral domain about a Python library. Future runtime
dependencies require an explicit decision covering maintenance health,
security history, release cadence, license, transitive size, replacement strategy, and
which architectural port contains them.

Node.js SHALL NOT be required to install, import, or run the framework kernel. The pinned
OpenSpec CLI remains contributor tooling under `tools/openspec/`, with its own private
package manifest, lockfile, installation target, and ignored `node_modules`. Repository
validation may require this tool; framework runtime does not.

Portable schemas use JSON Schema and canonical serialization independently of Python.
Optional TypeScript may implement a future console/presentation adapter. Security-sensitive
CAS, sandbox, or execution helpers may later use Rust or platform-native code behind
versioned ports. Neither choice changes domain or wire-contract ownership.

## Dependency admission policy

For each proposed runtime dependency, record:

- the exact capability that the standard library or existing code cannot reasonably
  provide;
- active-maintenance and release evidence;
- direct and transitive package count;
- vulnerability, provenance, and license posture;
- version/support policy and upgrade owner;
- isolation boundary and failure behavior; and
- a removal or replacement path.

Dependencies SHALL be direct and intentional. Framework adapters may use pinned extras;
the domain package cannot import them. Lockfiles are mandatory for contributor and release
environments but do not convert tooling dependencies into runtime dependencies.

## Consequences

- OVA extraction can reuse its strongest Python contracts and tests with minimal semantic
  translation.
- The framework starts with a small and inspectable runtime supply chain and validates
  dependency evidence against the official CycloneDX 1.7 JSON schema.
- Consumers in other languages can implement the same schemas and ports.
- Contributors still need Node.js for strict OpenSpec validation until a suitably stable
  standalone validator exists.
- Python cannot by itself provide every desired sandbox guarantee; native helpers may be
  added behind audited ports rather than embedded into domain code.

## Validation

- `pyproject.toml` declares Python 3.11+ and exactly pins the CycloneDX library's
  JSON-validation extra.
- the build backend is an exact reviewed pin rather than an unconstrained resolver input.
- `make install` follows [ADR 0032](0032-tuple-specific-native-install-sboms.md): it
  proves or explicitly installs tuple-specific native prerequisites, then installs the
  package and its pinned Python dependency closure in a private environment.
- `make tools-install` installs npm dependencies only beneath `tools/openspec/`.
- Python import/unit checks run independently of Node.js.
- dependency-direction tests will prevent domain imports of optional adapters/UI/native
  helpers.
