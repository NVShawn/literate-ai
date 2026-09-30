# ADR 0045: Construct and inspect Debian packages on compatible workers

- Status: Accepted
- Date: 2026-09-24
- Accepted: 2026-09-25
- Decision owners: Literate AI maintainers
- Roadmap: PACKAGE-APT-001 in `docs/roadmap/active-work.md`

## Context

The `package-apt` Flavor promises a deterministic Debian binary package, but the
current adapter produces a ZIP containing `DEBIAN/control`. It neither constructs a
`.deb` nor asks Debian tooling to inspect one. Its control file also declares
`Architecture: all` without proving platform independence and cannot derive installable
Debian dependency coordinates from human-facing runtime requirement names.

Package orchestration has another custody mismatch. It can select and observe a Linux
worker for the accepted build lifecycle, then returns to the controller to construct
native package bytes. A macOS or Windows controller therefore cannot truthfully satisfy
an apt request even when the selected worker has the required Debian tools. Merely
replacing the ZIP writer on the controller would preserve that contradiction.

The accepted `PackagePlan` already binds the Component lock, target, artifact graph,
logical inputs, entrypoints, runtime requirements and packager identity. Provider-native
path, architecture, dependency and tool decisions must become exact derived authority
rather than ambient choices made after the plan is accepted.

## Decision

### Derive an exact Debian projection before construction

Add a typed Debian projection that is included in the apt packager identity and whose
generated control bytes are an exact `PackageInput`. It binds:

- the source `PackagePlan` identity;
- Debian package name and semantic version;
- target architecture mapped from the accepted target and observed worker architecture
  (`x86_64` to `amd64`, `arm64` or `aarch64` to `arm64`), with no ambient fallback to
  `all`;
- every logical input to one canonical absolute package path and exact file mode;
- every entrypoint to a unique `/usr/bin/<name>` installed path;
- specification and SBOM resources beneath
  `/usr/share/doc/<package>/literate-ai/`;
- remaining product files beneath `/usr/lib/<package>/`;
- every external runtime requirement to an exact Debian package coordinate derived
  from the accepted resolved CycloneDX closure; and
- the bound Debian tool identity and reproducibility policy.

An external runtime requirement without one unambiguous accepted Debian coordinate is a
typed refusal. Display names such as `locked root language runtime` are never converted
into package names by string guessing. Duplicate, reserved, case-fold-colliding or
escaping installed paths are refused before staging.

### Bind native tooling and deterministic inputs

Introduce a `dpkg-deb` tool binding containing the resolved executable path, executable
digest, bounded version probe and fixed arguments. Recheck the executable before and
after every invocation. Construction stages only exact plan inputs beneath a fresh
directory inside worker `OBJ_DIR`, assigns declared modes, root ownership metadata and
one fixed timestamp, and invokes `dpkg-deb` with deterministic compression and
root-owner-group behavior. Locale, timezone, umask and `SOURCE_DATE_EPOCH` are fixed and
bound by the packager identity.

The staged control archive contains only the generated `control` file. Maintainer
scripts, conffile declarations and triggers are outside this first contract and are
refused. Construction never installs, publishes or executes package payloads.

### Execute where the package tools and target are valid

Local construction is allowed only when the controller itself is the selected healthy,
compatible Linux worker. Otherwise the controller sends a serializable package request
through the selected worker transport. The request binds the worker catalog and fresh
observation, Debian projection, plan, exact input blob references, deadline and output
bounds. The worker returns immutable `.deb` bytes plus bounded inspection evidence;
the controller rehashes both before admitting them to package custody.

Package dispatch uses the production command or SSH actuation boundary from ADR 0044;
a controller callback that happens to run locally is not remote evidence. Package bytes
remain below worker and controller `OBJ_DIR` roots, and credentials or private worker
locations never enter project authority.

### Inspect the complete archive without installing it

Verification starts from the recorded outer digest in a fresh directory. The bound
Debian inspection tool emits control fields plus control and filesystem tar streams.
The verifier parses those streams without installing or executing the package and
requires:

- exact package, version, architecture and dependency fields;
- no undeclared control members or maintainer scripts;
- exactly the projected payload paths, types, ownership and modes;
- exact content digests for every regular file;
- only the explicitly projected entrypoint links, when links are used; and
- no absolute, traversal, device, FIFO, socket, hard-link or unbounded archive member.

Construction and verification use separate fresh staging roots. Verification does not
trust the construction tree, construction command output or the package result's file
list in place of inspecting the returned bytes.

## Qualification and rollout

Implementation proceeds under the explicit maintainer acceptance recorded on
2026-09-25. Qualification must prove:

- byte-for-byte reproducibility across two fresh constructions with the same bound tool;
- exact `amd64` and `arm64` mapping and refusal of unknown or contradictory targets;
- complete path, mode, ownership, control-field, dependency and digest verification;
- executable entrypoints plus retained specification and resolved-SBOM resources;
- refusal of missing or changed tools, unmapped runtime dependencies, collisions,
  traversal, symlinks outside the declared mapping, maintainer scripts and tampering;
- local Linux construction and remote Linux construction from a non-Linux controller;
- bounded command output, deadline, cleanup and returned-evidence behavior; and
- installed-wheel tests followed by the complete Linux worker and hosted matrices.

Roll out in two reviewable slices: typed projection, bound local tool adapter and real
Linux evidence; then production worker dispatch and non-Linux-controller evidence. The
roadmap item remains open until both slices and independent verification are complete.

## Consequences

The apt provider becomes a real target-specific package adapter rather than a generic
metadata archive. This adds Debian-specific projection and worker execution contracts,
but keeps the provider-neutral `PackagePlan` and release result boundary intact.

Requiring exact Debian dependency coordinates may initially refuse packages whose
accepted SBOM lacks provider mapping. That refusal is more accurate than publishing a
package with guessed or omitted dependencies. Supporting additional distributions or
maintainer scripts requires a later explicit contract rather than weakening this one.
