# ADR 0053: Declare rustup as an alternative Rust host-tool provider

- Status: Proposed
- Date: 2026-11-01
- Decision owners: directing user and literate-ai maintainers
- Roadmap: [1.4 integration issue 214](../roadmap/1.4-integration.md)

## Context

Issue [214](https://github.com/jordanhubbard/literate-ai/issues/214) identifies two
independent host-readiness failures. Resolving a `cargo` or `rustc` symlink before
execution changes rustup's multicall dispatch to `rustup --version`. Preserving
the invocation path repairs that ordinary probe defect and needs no change to the
declared toolchain contract.

Separately, host-install OS realizations require native package provenance. A
working rustup-only Rust installation has no Homebrew/dpkg/pkg/WinGet Rust package,
so successful tool versions still leave `manager_installed` false. Treating every
successful command as a managed installation would weaken all host prerequisites.
Supporting a second explicitly selected provider needs its own authority and
evidence rule. Under ADR 0006 this proposed provider extension requires acceptance
before implementation; the independent invocation-path repair can proceed.

## Proposed decision

1. Add an optional `literate-ai:host-install:alternative-provider` property with
   the single supported value `rustup` to native-package Rust realizations.
   Absence preserves the existing package-manager provenance requirement. Reject
   unknown values and use on capabilities other than command-probed `rustc` and
   `cargo`. Canonical OS realizations may explicitly opt in; downstream authored
   realizations retain their own choice. The native package remains the declared
   installation fallback. This adds no new download or automatic installation.

2. Prefer an already satisfied native-package realization. When it is absent,
   evaluate the declared alternative only after the selected compiler/Cargo probe
   succeeds under its existing version constraint. All commands retain finite
   time/output bounds and the same working-directory/environment/toolchain
   override context as the original probe. A version string alone cannot prove
   provider ownership.

3. Identify a real `rustup` executable and verify its manager probe. Ask that
   manager for the active `rustc` or `cargo` toolchain executable using its `which`
   command, require exactly one absolute executable path, and verify the actual
   selected tool's provenance. A multicall proxy must name the correct tool and
   share the admitted manager's file identity (including Windows hardlinks); a
   direct toolchain executable must match the manager's selected tool path.
   An unrelated rustup on PATH, a successful arbitrary wrapper, a changed manager
   or selected tool, a missing toolchain, ambiguous query output or a mismatched
   selection refuses. Do not select another toolchain or repair user configuration.

4. Report the actual proven provider distinctly, including rustup provenance and
   the selected toolchain executable. Do not label rustup as a Homebrew/dpkg/pkg/
   WinGet installation. Preserve the readiness conjunction: the tool version and
   one explicitly permitted provider must both be proven. Ordinary native-package
   and managed-archive checks retain their current requirements. Host readiness
   remains preflight observation; retained execution still captures and admits its
   own exact executable/toolchain identities.

5. Qualify symlink proxies, Windows hardlink proxies and direct selected toolchain
   paths. Prove refusal for an undeclared alternative, unsupported capability,
   unrelated manager, missing/changed toolchain, malformed/oversized output,
   manager/tool failure and incompatible compiler/Cargo versions. Run the existing
   native-manager controls, actual rustup-only doctor/status readiness and the
   supported platform checks before issue 214 closes. Fixture success does not
   establish native Windows or release qualification.

## Consequences

Normal rustup installations can satisfy Rust host prerequisites through explicit
authority instead of a hidden exception. Authored SBOMs gain a small optional
provider choice; implementations must preserve its absence and reject unsupported
extensions. Provider-specific evidence and both proxy/direct invocation paths add
tests and platform work. This does not relax tool versions, grant execution,
install rustup, transfer source acceptance or mark release 1.4 ready.

The directing user's acceptance is pending. No alternative-provider implementation
or canonical realization change is authorized by this proposed document itself.
