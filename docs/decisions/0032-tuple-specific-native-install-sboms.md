# ADR 0032: Drive Native Installation from Tuple-Specific SBOMs

- Status: Accepted
- Date: 2026-09-01 (accepted by operator direction during 0.9.0 P0 closure)
- Decision owners: literate-ai maintainers
- Release target: 0.9.0
- Roadmap: [HOST-INSTALL-SBOM-001](../roadmap/active-work.md#x-host-install-sbom-001-make-native-cli-installation-sbom-driven)
- Amends: [ADR 0002](0002-reference-implementation-ecosystem.md) installation
  validation and [ADR 0030](0030-executable-cross-layer-authority.md) host-platform
  policy

## Context

Before this decision, `make install` proved only that a compatible Python interpreter
was already on `PATH`. It then created a private virtual environment and let `pip`
install the distribution's pinned Python requirements. Installation from a Git checkout
also used Git, and Debian Python requires its separately packaged `venv` support, but
neither prerequisite had a declarative native-package contract. Contributor and sample
bootstrap prose contained package commands, but that prose was not the CLI installation
flow and could drift by operating system.

A single installer containing every operating-system, architecture, accelerator, and
package-manager branch would move the drift into code. It would also make a new RPM or
FreeBSD port require changing orchestration even when its package manager supports the
same query/install shape.

There is one unavoidable bootstrap boundary: GNU Make and a Python 3.11+ interpreter
must exist before a Make recipe or Python dependency resolver can execute. The installer
cannot truthfully promise to install the interpreter that is already executing it.

## Decision

### Separate stage zero, native prerequisites, and Python distribution dependencies

GNU Make and the compatible interpreter selected by `PYTHON` or PATH discovery are
stage-zero prerequisites. `make install` then proves that the tuple's native package
manager records the required Python support package, venv support where separately
packaged, and Git, and that the executing Python and discovered Git satisfy the declared
version ranges. The private environment's Python package closure remains authoritative
in `pyproject.toml` and the repository lock and is installed by `pip`; it is not
duplicated as native packages with different distro versions.

### Use one CycloneDX document per supported tuple

The repository stores CycloneDX 1.7 host realization SBOMs beneath
`flavors/os-<family>/host-install/<os>/<architecture>/<accelerator>.cdx.json`. Each
document declares:

- the exact normalized OS, CPU architecture, and accelerator tuple;
- package-manager identity plus local query, pre-install, install, and elevation
  commands;
- native package identifiers, required version ranges, runtime probes, and reasons;
- OS-owned PATH entries needed to re-probe a newly installed package in the same
  installer process;
- integrity-pinned managed-archive locations, extraction rules, executable placement,
  and the relative paths of every runtime companion required for a usable install; and
- an explicit dependency graph from the installation root to every package.

ADR 0033 subsequently separated logical ownership from these realization leaves.
`flavors/os-base/toolchain.cdx.json` owns universal capability contracts, and selected
language, package, build, accelerator, and concrete OS Flavors may contribute sibling
`host-toolchain.cdx.json` mix-ins. This ADR's tuple leaves map the resulting logical
capability IDs to platform-native packages or integrity-pinned managed artifacts.

Core CLI installation has no GPU dependency, so its accelerator coordinate is `none`.
GPU toolkits remain selected by Flavor and worker compatibility authority. A future
GPU-dependent installer adds a distinct coordinate rather than changing `none`.

The initial supported set is Debian-family Linux on x86-64 and ARM64 through APT,
macOS on x86-64 and ARM64 through Homebrew, and Windows x86-64 through WinGet. Windows
ARM64, RPM-family Linux, FreeBSD, and other tuples fail as unsupported until a reviewed
SBOM is added.

### Keep orchestration generic and consent explicit

A dependency-free stage-zero parser validates tuple, schema, package, graph, version,
and command-template invariants. It lives in the narrow `literate_ai.bootstrap`
namespace so importing it does not require the runtime closure it is about to install.
The host adapter detects the tuple, queries packages without a
shell, probes the actual runtime, and expands only the validated `{package}` and
`{packages}` argument tokens. `install_litai.py` selects the SBOM and orchestrates this
adapter before creating the private environment.

Managed archives retain their declared package topology. The primary executable is
normalized only within the first declared managed `PATH` directory, so adjacent helper
processes remain discoverable by the tool itself. Reuse requires the integrity-bound
manifest, normalized executable, and every safe relative runtime-companion path to be
present; a standalone executable is not treated as a complete package when its SBOM
declares companions.

When a dependency is absent, an interactive installation asks once before changing the
host. `LITAI_INSTALL_DEPENDENCIES=yes` is the explicit noninteractive authorization;
`no` declines. Decline or an unattended missing-dependency run prints the required
packages and exits without installing the CLI. A missing package manager cannot safely
bootstrap itself and produces its own prerequisite diagnostic.

An unsupported tuple prints the detected tuple, all supported tuples, the SBOM root,
and instructions to copy the nearest tuple directory to begin a port. No fallback tuple
is guessed. Symlinked or mismatched SBOMs are not admitted.

Uninstallation is deliberately asymmetric. `make uninstall` removes only the exact
launcher and private environment named by the host-install ownership manifest. It never
invokes the native package manager and never removes SBOM dependencies, durable user
configuration/state, projects, caches, or generated output, because those resources
may be shared or have independent retention policy. A legacy installation without the
0.9 ownership manifest must be reinstalled into the same prefix before automated
removal; guessing ownership during deletion is unsafe.

### Retain host-native installation roots

When `PREFIX` is absent, the installer uses ADR 0030's `HostPaths.install_root` rather
than Make reconstructing a home-relative default. An explicit `PREFIX` remains highest
precedence. Platform system metadata such as Linux `/etc/os-release` is represented by
the same centralized host-policy surface.

## Consequences

Package requirements and manager commands are inspectable without reading Python.
Adding a manager with the existing query/install template shape is data-only; a manager
with genuinely different semantics requires a deliberate adapter extension rather than
shell text hidden in an SBOM. Interactive and CI behavior are deterministic, and
installation never weakens permissions or silently downloads a package manager.

The tuple SBOMs are host-prerequisite SBOMs, not replacements for the Python lock or the
resolved build/runtime SBOMs emitted for Components. Those authorities remain separate
because conflating distro packages, PyPI distributions, and generated-product closure
would make each less accurate.

## Validation

- Validate every document against the official CycloneDX 1.7 JSON schema and the
  Literate AI tuple/property/graph contract.
- Exercise detection, architecture aliases, unsupported tuples, symlink rejection,
  package-manager absence, consent/decline, installation commands, version probes,
  managed runtime-closure preservation/invalidation, and post-install re-probe without
  changing the test host.
- Run the dependency-only preflight on the development host, then run `make install`
  from a clean checkout into a disposable explicit prefix.
- Run `make install` with explicit noninteractive consent in the Ubuntu, macOS, and
  Windows CI matrix.
- Exercise install/uninstall/install cycles and prove that a repeated uninstall is
  idempotent, package-manager-owned dependencies and independently retained sibling
  data survive, and custody-changing symbolic links fail closed.
